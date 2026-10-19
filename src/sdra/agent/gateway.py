"""Action gateway: the only path from a decision to a record write or an outbound message.

Before anything runs it checks, from records and policy (never from the LLM):
  authority   the bundle is not blocked; signatures are approvals from distinct people who
              hold the required roles, for this request, before it expired; execute-tier
              actions re-classify as execute
  caps        cumulative commitment and extra cost per incident, commitment per day,
              actions per incident (policy `caps`)
  leaks       every outbound message passes the never-disclose check
Then it runs each action once per idempotency key and writes an audit entry per effect.
All checks pass or nothing runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sdra.agent.executor import Executor, OutMessage
from sdra.agent.leaks import check_message
from sdra.audit import AuditLog
from sdra.governance import classify
from sdra.policy import Policy
from sdra.schema import ApprovalDecision, ProposedAction, SendMessage
from sdra.store import Change, MemoryStore


class Refused(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class Authority:
    tier: str
    roles: list[str]
    request_id: str | None = None
    signatures: list[ApprovalDecision] = field(default_factory=list)
    expires_at: datetime | None = None


@dataclass
class Usage:
    """Running totals the caps are checked against (persisted with the runtime state)."""

    commitment: dict[str, Decimal] = field(default_factory=dict)  # incident -> base amount executed
    incremental: dict[str, Decimal] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    daily_commitment: dict[str, Decimal] = field(default_factory=dict)  # ISO date -> base amount


class Gateway:
    def __init__(self, store: MemoryStore, policy: Policy, audit: AuditLog, outbox: list[OutMessage],
                 done: dict[str, str | None] | None = None, usage: Usage | None = None):
        self.store, self.policy, self.audit = store, policy, audit
        self.executor = Executor(store, outbox, done)
        self.usage = usage or Usage()

    # --- checks -----------------------------------------------------------------------------

    def cap_problems(self, incident_id: str, commitment: Decimal, incremental: Decimal, n_actions: int,
                     now: datetime) -> list[str]:
        caps, u, out = self.policy.caps, self.usage, []
        total = u.commitment.get(incident_id, Decimal(0)) + commitment
        if total > caps.per_incident_commitment_base:
            out.append(f"cap per_incident_commitment_base: {total:.2f} > {caps.per_incident_commitment_base}")
        day = u.daily_commitment.get(now.date().isoformat(), Decimal(0)) + commitment
        if day > caps.per_day_commitment_base:
            out.append(f"cap per_day_commitment_base: {day:.2f} > {caps.per_day_commitment_base}")
        extra = u.incremental.get(incident_id, Decimal(0)) + incremental
        if extra > caps.per_incident_incremental_cost_base:
            out.append(f"cap per_incident_incremental_cost_base: {extra:.2f} > {caps.per_incident_incremental_cost_base}")
        count = u.actions.get(incident_id, 0) + n_actions
        if count > caps.max_actions_per_incident:
            out.append(f"cap max_actions_per_incident: {count} > {caps.max_actions_per_incident}")
        return out

    def leak_problems(self, actions: list[ProposedAction], secrets: dict[str, list[str]]) -> list[str]:
        out = []
        for a in actions:
            if isinstance(a.params, SendMessage):
                out += [f"{a.id} to {a.params.recipient_id}: {r}"
                        for r in check_message(self.store, self.policy, a.params, secrets)]
        return out

    def preflight(self, incident_id: str, actions: list[ProposedAction], commitment: Decimal, incremental: Decimal,
                  secrets: dict[str, list[str]], now: datetime) -> list[str]:
        """Checks that don't depend on signatures; run at plan time so nobody signs a bundle
        the gateway would refuse, and again at execution."""
        return (self.cap_problems(incident_id, commitment, incremental, len(actions), now)
                + self.leak_problems(actions, secrets))

    def authority_problems(self, auth: Authority, now: datetime) -> list[str]:
        if auth.tier == "blocked":
            return ["bundle is blocked by policy"]
        if auth.tier == "execute":
            return []
        out = []
        if auth.expires_at and now > auth.expires_at:
            out.append(f"approval {auth.request_id} expired at {auth.expires_at.isoformat()}")
        approvals = [s for s in auth.signatures if s.decision == "approve" and s.request_id == auth.request_id]
        people = {s.approver_id for s in approvals}
        if len(people) != len(approvals):
            out.append("one person signed twice")
        covered = set()
        for s in approvals:
            approver = self.store.get("approvers", s.approver_id)
            if approver is None or s.role not in approver.roles:
                out.append(f"{s.approver_id} does not hold role {s.role}")
            else:
                covered.add(s.role)
        missing = [r for r in auth.roles if r not in covered]
        if missing:
            out.append(f"missing signatures: {', '.join(missing)}")
        if len(people) < len(auth.roles):
            out.append(f"{len(auth.roles)} distinct signers required, {len(people)} signed")
        return out

    # --- execution -----------------------------------------------------------------------

    def execute(self, incident_id: str, actions: list[ProposedAction], auth: Authority, now: datetime, *,
                commitment: Decimal = Decimal(0), incremental: Decimal = Decimal(0),
                secrets: dict[str, list[str]] | None = None, autonomous_facts: list[dict] | None = None) -> list:
        problems = self.authority_problems(auth, now)
        if autonomous_facts is not None:  # execute tier: the gateway re-derives the tier itself
            for a, facts in zip(actions, autonomous_facts, strict=True):
                tier = classify(self.policy, a.params.kind, facts).tier
                if tier != "execute":
                    problems.append(f"{a.id} needs {tier}, not execute")
        problems += self.preflight(incident_id, actions, commitment, incremental, secrets or {}, now)
        if problems:
            self.audit.append(now, "gateway", "execution_refused", incident_id, [a.id for a in actions],
                              after={"reasons": problems})
            raise Refused(problems)

        results, applied = [], 0
        for a in actions:
            fresh, result = self.executor.run(a, now)
            results.append(result)
            if not fresh:
                continue
            applied += 1
            if isinstance(result, OutMessage):
                self.audit.append(now, "agent", "message_sent", incident_id, [a.id, result.id],
                                  after={"to": result.to, "template": result.template, "subject": result.subject,
                                         "body": result.body})
            else:
                for change in result:
                    self._audit_change(now, incident_id, a, change)
        u = self.usage
        u.commitment[incident_id] = u.commitment.get(incident_id, Decimal(0)) + commitment
        u.incremental[incident_id] = u.incremental.get(incident_id, Decimal(0)) + incremental
        u.actions[incident_id] = u.actions.get(incident_id, 0) + applied
        day = now.date().isoformat()
        u.daily_commitment[day] = u.daily_commitment.get(day, Decimal(0)) + commitment
        return results

    def _audit_change(self, now: datetime, incident_id: str, action: ProposedAction, change: Change) -> None:
        self.audit.append(now, "agent", "record_changed", incident_id, [action.id, change.key],
                          before=change.before, after=change.after | {"_table": change.table})
