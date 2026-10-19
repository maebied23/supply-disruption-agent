"""Orchestrator: intake routing outside the graph, one LangGraph thread per incident run.

Intake (per inbound message): extract (quarantined) -> verify (deterministic) -> route:
  review tasks for anything a person should see; a templated status request (execute tier)
  for genuine but unusable or conflicting claims; a confirmation that matches records is
  recorded; other verified changes open or re-open an incident.

Incident graph (per run):  plan -> request_signature (one interrupt per signature, looped)
  -> revalidate -> execute.  A rejection removes the option (and any excluded supplier) and
  re-plans once; a changed basis voids the approval and re-plans; a blocked bundle, a bundle
  the gateway would refuse, or too many re-plans escalate. Unsigned requests expire.

Every write and message goes through the action gateway; every step is in the audit log.
After each event the runner saves its state, so a run can stop and resume (`Runner.resume`).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sdra.agent import templates
from sdra.agent.executor import OutMessage
from sdra.agent.explain import record_update_detail, request_detail
from sdra.agent.gateway import Authority, Gateway, Refused, Usage
from sdra.agent.leaks import secret_amounts
from sdra.agent.proposal import basis_changed, basis_of, build_proposal, build_record_update
from sdra.agent.runtime import (
    ApprovalEvent, Chase, IncidentRecord, PendingRun, Queued, RequestRecord, RuntimeState,
)
from sdra.audit import AuditLog
from sdra.clock import SimClock
from sdra.core.options import Planner
from sdra.intake.extract import ExtractionError, Extractor
from sdra.intake.verify import Ledger, verify
from sdra.pack import Pack
from sdra.schema import (
    ActionKind, ApprovalDecision, ChangeKind, InboundMessage, ProposedAction, RecordPOConfirmation, ReviewTask,
    SendMessage, StockAdjustment, SupplyChangeEvent,
)
from sdra.store import MemoryStore


class IncidentState(TypedDict, total=False):
    incident_id: str
    run: int
    excluded_suppliers: list[str]
    excluded_options: list[str]
    reproposals: int
    request: dict | None
    signatures: list[dict]
    status: str


class Runner:
    def __init__(self, pack: Pack, store: MemoryStore, extractor: Extractor, start: datetime, *,
                 checkpointer=None, state: RuntimeState | None = None):
        self.pack, self.policy, self.store, self.extractor = pack, pack.policy, store, extractor
        self.routing = pack.policy.routing
        self.audit = AuditLog(store.audit_entries(), sink=store.append_audit)
        st = state
        self.clock = SimClock(st.now if st else start, [(q.at, q.seq, q) for q in st.queue] if st else None)
        self.ledger = Ledger()
        if st:
            self.ledger.events, self.ledger.status_requests = dict(st.ledger_events), dict(st.status_requests)
        self.outbox: list[OutMessage] = list(st.outbox) if st else []
        self.review_tasks: list[ReviewTask] = list(st.review_tasks) if st else []
        self.requests: list[RequestRecord] = list(st.requests) if st else []
        self.blocked: list[dict] = list(st.blocked) if st else []
        self.generated_events: list[SupplyChangeEvent] = list(st.generated_events) if st else []
        self.incidents: dict[str, IncidentRecord] = dict(st.incidents) if st else {}
        self.pending: dict[str, PendingRun] = dict(st.pending) if st else {}
        self.chases: dict[str, Chase] = dict(st.chases) if st else {}
        self.last_inbound: dict[str, datetime] = dict(st.last_inbound) if st else {}
        self.inbox: dict[str, InboundMessage] = dict(st.inbox) if st else {}
        self.notifications: list[dict] = list(st.notifications) if st else []
        usage = Usage(**{k: dict(v) for k, v in st.usage.items()}) if st else None
        self.gateway = Gateway(store, self.policy, self.audit, self.outbox, st.done if st else None, usage)
        self.graph = self._build_graph().compile(checkpointer=checkpointer or InMemorySaver())
        if st is None:
            self.audit.append(start, "system", "run_started", after={"company": self.policy.company.name})
            for line in self.store.rows("purchase_order_lines"):
                self._watch_line(line.id, start)
            self.save()

    @classmethod
    def resume(cls, pack: Pack, store: MemoryStore, extractor: Extractor, *, checkpointer) -> Runner:
        """Rebuild a runner from the state its store saved (records, audit and runtime)."""
        raw = store.load_runtime()
        if raw is None:
            raise ValueError("nothing saved in this store")
        state = RuntimeState.model_validate_json(raw)
        return cls(pack, store, extractor, state.now, checkpointer=checkpointer, state=state)

    @property
    def now(self) -> datetime:
        return self.clock.now

    def request(self, request_id: str) -> RequestRecord:
        return next(r for r in self.requests if r.id == request_id)

    # --- persistence ----------------------------------------------------------------------

    def state(self) -> RuntimeState:
        u = self.gateway.usage
        return RuntimeState(
            now=self.now, queue=[q.model_copy(update={"at": at, "seq": seq}) for at, seq, q in self.clock.queued()],
            ledger_events=self.ledger.events, status_requests=self.ledger.status_requests, outbox=self.outbox,
            review_tasks=self.review_tasks, requests=self.requests, blocked=self.blocked,
            generated_events=self.generated_events, incidents=self.incidents, pending=self.pending,
            chases=self.chases, last_inbound=self.last_inbound, inbox=self.inbox, notifications=self.notifications,
            done=self.gateway.executor.done,
            usage={"commitment": u.commitment, "incremental": u.incremental, "actions": u.actions,
                   "daily_commitment": u.daily_commitment},
        )

    def save(self) -> None:
        self.store.save_runtime(self.state().model_dump_json())
        self.store.commit()

    # --- inputs -----------------------------------------------------------------------------

    def schedule(self, at: datetime, kind: str, payload=None, **fields) -> None:
        match kind:
            case "email":
                q = Queued(at=at, seq=0, kind=kind, email=payload)
            case "stock_adjustment":
                q = Queued(at=at, seq=0, kind=kind, stock_adjustment=payload)
            case "approval":
                if isinstance(payload, dict):
                    payload = ApprovalEvent.model_validate({k: v for k, v in payload.items() if k not in ("at", "type")})
                q = Queued(at=at, seq=0, kind=kind, approval=payload)
            case _:
                q = Queued(at=at, seq=0, kind=kind, **fields)
        self.clock.schedule(at, q)

    def run_until(self, end: datetime, stop: Callable[[], bool] | None = None) -> None:
        """Process events up to `end`; with `stop`, return early once it holds after an event."""
        for q in self.clock.run_until(end):
            self._dispatch(q)
            self.save()
            if stop and stop():
                return
        self.save()

    def decide(self, event: ApprovalEvent) -> None:
        """A decision made now (approver UI)."""
        self._on_approval(event)
        self.save()

    def _dispatch(self, q: Queued) -> None:
        match q.kind:
            case "email":
                self._on_email(q.email)
            case "stock_adjustment":
                self._on_stock_adjustment(q.stock_adjustment)
            case "approval":
                self._on_approval(q.approval)
            case "silence":
                self._on_silence(q.ref, q.stage)
            case "reminder":
                self._on_reminder(q.ref)
            case "expiry":
                self._on_expiry(q.ref)

    def _on_email(self, msg: InboundMessage) -> None:
        self.inbox[msg.id] = msg
        self.audit.append(self.now, "system", "message_received", ref_ids=[msg.id],
                          after={"sender": msg.sender, "subject": msg.subject})
        try:
            result = self.extractor.extract(msg)
        except ExtractionError as e:
            self._task(self.routing.review, f"could not read {msg.id}: {e}", [msg.id])
            return
        intake = verify(self.store, self.policy, self.ledger, msg, result, self.now)
        self.audit.append(self.now, "agent", "claims_checked", ref_ids=[msg.id], after={
            "supplier_id": intake.supplier_id, "instructions_flag": result.contains_instructions_to_agent,
            "claims": [{"id": e.id, "kind": e.kind.value, "po_line_id": e.po_line_id, "qty": e.new_qty,
                        "date": e.new_date.isoformat() if e.new_date else None, "verification": e.verification,
                        "notes": e.verification_notes} for e in intake.events],
            "superseded": intake.superseded_ids, "duplicates": intake.duplicate_ids})
        if intake.supplier_id:
            self.last_inbound[intake.supplier_id] = self.now
        lines = {l.id for l in self.store.rows("purchase_order_lines")}
        if intake.review_reasons:  # one task per message, with the exposure of each line it names
            reasons = list(dict.fromkeys(intake.review_reasons))
            mentioned = sorted(l for l in lines if any(l in r for r in reasons))
            self._task(self.routing.review, " | ".join([f"{msg.id}: " + "; ".join(reasons),
                                                        *(self._exposure(l) for l in mentioned)]), [msg.id, *mentioned])
        for line_id in sorted(intake.conflict_lines | intake.unverified_lines):
            if intake.supplier_id and line_id not in self.chases:
                self._status_request(line_id)
        changes = []
        for e in intake.verified:
            self.chases.pop(e.po_line_id, None)
            if e.id in intake.duplicate_ids:
                continue
            if e.kind == ChangeKind.CONFIRMATION:
                self._record_confirmation(e)
            else:
                changes.append(e)
        if changes:
            self._supply_change(changes)

    def _on_stock_adjustment(self, adj: StockAdjustment) -> None:
        change = self.store.update("stock", f"{adj.item_id}@{adj.location_id}", on_hand=adj.new_on_hand)
        self.audit.append(self.now, "system", "record_changed", ref_ids=[adj.id, change.key], before=change.before,
                          after=change.after | {"_table": "stock", "_reason": adj.reason})

    def _on_approval(self, ev: ApprovalEvent) -> None:
        pending = (self.pending.get(ev.request_id) if ev.request_id
                   else next(iter(self.pending.values())) if len(self.pending) == 1 else None)
        approver = self.store.get("approvers", ev.approver_id)
        if pending is None or approver is None:
            why = "unknown approver" if approver is None else "no single request awaiting a decision"
            self.audit.append(self.now, ev.approver_id, "decision_ignored", ref_ids=[ev.request_id] if ev.request_id else [],
                              after={"reason": why, "decision": ev.decision})
            return
        req = self.request(pending.request_id)
        role = next((r for r in pending.outstanding_roles if r in approver.roles), None)
        signed = {s.approver_id for s in req.signatures}
        if role is None or approver.id in signed:
            why = "already signed" if approver.id in signed else f"holds none of {pending.outstanding_roles}"
            self.audit.append(self.now, approver.id, "decision_ignored", req.incident_id, [req.id],
                              after={"reason": why, "decision": ev.decision})
            return
        decision = ApprovalDecision(
            request_id=req.id, approver_id=approver.id, role=role, decision=ev.decision,
            exclude_supplier_ids=ev.exclude_supplier_ids, comment=ev.comment, decided_at=self.now)
        req.signatures.append(decision)
        self.audit.append(self.now, approver.id, "approval_decided", req.incident_id, [req.id],
                          after=decision.model_dump(mode="json"))
        config = {"configurable": {"thread_id": pending.thread_id}}
        self._after_invoke(self.graph.invoke(Command(resume=decision.model_dump(mode="json")), config), config)

    def _on_silence(self, line_id: str, stage: int) -> None:
        line = self.store.get("purchase_order_lines", line_id)
        supplier = self.store.get("purchase_orders", line.purchase_order_id).supplier_id
        chase = self.chases.get(line_id)
        replied = chase and self.last_inbound.get(supplier, datetime.min.replace(tzinfo=self.now.tzinfo)) > chase.first
        if line.confirmed_date or self.ledger.for_line(line_id) or replied or line.status != "open":
            return
        follow_up = timedelta(hours=self.policy.extraction.follow_up_hours)
        if stage == 0:
            event = SupplyChangeEvent(
                id=f"EV-SILENCE-{line_id}", kind=ChangeKind.NO_RESPONSE, observed_at=self.now, supplier_id=supplier,
                po_line_id=line_id, item_id=line.item_id, confidence=1.0, source_ids=["clock"],
                verification="verified")
            self.generated_events.append(event)
            self.audit.append(self.now, "system", "no_response", ref_ids=[line_id], after={"supplier_id": supplier})
            self._status_request(line_id)
        elif stage == 1:
            self._status_request(line_id)
        else:
            self._task(self.routing.review, f"{line_id} still unconfirmed after 2 status requests | {self._exposure(line_id)}",
                       [line_id])
            return
        self.schedule(self.now + follow_up, "silence", ref=line_id, stage=stage + 1)

    def _on_reminder(self, request_id: str) -> None:
        if request_id in self.pending:
            p = self.pending[request_id]
            self.notifications.append({"at": self.now.isoformat(), "request_id": request_id, "roles": p.outstanding_roles})
            self.audit.append(self.now, "system", "approval_reminder", p.incident_id, [request_id],
                              after={"outstanding_roles": p.outstanding_roles})

    def _on_expiry(self, request_id: str) -> None:
        p = self.pending.pop(request_id, None)
        if p is None:
            return
        req = self.request(request_id)
        req.decided, req.decided_at = "expired", self.now
        self.audit.append(self.now, "system", "approval_expired", p.incident_id, [request_id],
                          after={"outstanding_roles": p.outstanding_roles})
        signed = ", ".join(f"{s.approver_id} ({s.role})" for s in req.signatures) or "nobody"
        self._task(p.outstanding_roles[0], f"{p.incident_id}: approval {request_id} for {req.option_id} expired after "
                   f"{self.policy.approvals.expiry_hours} h (signed: {signed}). Nothing was executed; "
                   "re-plan when ready.", [request_id], p.incident_id)
        self.incidents[p.incident_id].status = "escalated"

    # --- helpers ------------------------------------------------------------------------------

    def _watch_line(self, line_id: str, not_before: datetime) -> None:
        """Schedule the no-confirmation check for an open, unconfirmed PO line."""
        line = self.store.get("purchase_order_lines", line_id)
        if line.status != "open" or not line.open_qty or line.confirmed_date is not None:
            return
        days = timedelta(days=self.policy.extraction.silence.confirmation_due_days_before_expected)
        due = datetime.combine(line.expected_date - days, time(8), tzinfo=self.now.tzinfo)
        self.schedule(max(due, not_before), "silence", ref=line_id, stage=0)

    def _task(self, role: str, reason: str, refs: list[str], incident_id: str | None = None) -> None:
        task = ReviewTask(id=self.store.new_id("review_tasks"), incident_id=incident_id, assignee_role=role,
                          reason=reason, ref_ids=refs, created_at=self.now)
        self.review_tasks.append(task)
        self.audit.append(self.now, "agent", "review_task_created", incident_id, [task.id, *refs],
                          after={"assignee_role": role, "reason": reason})

    def _autonomous(self, scope: str, action: ProposedAction, facts: dict):
        """Run one execute-tier action through the gateway; a refusal goes to a person."""
        try:
            (result,) = self.gateway.execute(scope, [action], Authority(tier="execute", roles=[]), self.now,
                                             secrets=secret_amounts(self.store, self.policy),
                                             autonomous_facts=[facts])
            return result
        except Refused as r:
            self._task(self.routing.review, f"{action.id} refused by the action gateway: {'; '.join(r.reasons)}", [action.id])
            return None

    def _status_request(self, line_id: str) -> OutMessage | None:
        line = self.store.get("purchase_order_lines", line_id)
        po = self.store.get("purchase_orders", line.purchase_order_id)
        subject, body = templates.status_request(po.id, line.item_id, line.open_qty, line.expected_date)
        chase = self.chases.setdefault(line_id, Chase(first=self.now))
        chase.count += 1
        n = chase.count
        action = ProposedAction(
            id=f"STATUS-{line_id}-{n}", incident_id="intake", idempotency_key=f"status:{line_id}:{n}",
            params=SendMessage(kind=ActionKind.SEND_SUPPLIER_MESSAGE, recipient_id=po.supplier_id,
                               template_id="status_request", subject=subject, body=body,
                               record_quantities=[line.open_qty]))
        msg = self._autonomous(f"CHASE-{line_id}", action, {"template_only": True})
        if msg is not None:
            self.ledger.status_requests[msg.id] = line_id
        return msg

    def _record_confirmation(self, e: SupplyChangeEvent) -> None:
        line = self.store.get("purchase_order_lines", e.po_line_id)
        action = ProposedAction(
            id=f"CONFIRM-{e.id}", incident_id="intake", idempotency_key=f"confirm:{e.id}",
            params=RecordPOConfirmation(po_line_id=line.id, confirmed_date=e.new_date or line.expected_date))
        # Stays in the ledger: a contradicting message inside the window must still be caught (S04).
        self._autonomous(f"CONFIRM-{line.id}", action, {})

    def _exposure(self, line_id: str) -> str:
        line = self.store.get("purchase_order_lines", line_id)
        po = self.store.get("purchase_orders", line.purchase_order_id)
        current = Planner(self.store, self.policy, self.ledger.verified(), self.now)
        uses = sorted({(a.demand.work_order_id, a.demand.so_line_id, a.demand.need_by)
                       for a in current.base.allocs if a.bucket.po_line_id == line_id})
        feeds = "; ".join(f"feeds {wo} for {so} (needed {need.isoformat()}, "
                          f"{(need - line.expected_date).days} days slack)" for wo, so, need in uses)
        return f"{po.id} / {line_id} ({line.item_id}, {line.open_qty} open, expected {line.expected_date.isoformat()}) {feeds}"

    def _void_pending(self, incident_id: str, reasons: list[str]) -> None:
        for rid, p in list(self.pending.items()):
            if p.incident_id == incident_id:
                req = self.request(rid)
                req.decided, req.voided_at, req.void_reasons = "voided", self.now, reasons
                del self.pending[rid]
                self.audit.append(self.now, "system", "approval_voided", incident_id, [rid], after={"reasons": reasons})

    def _supply_change(self, events: list[SupplyChangeEvent]) -> None:
        lines, items = {e.po_line_id for e in events}, {e.item_id for e in events}
        inc = next((i for i, d in self.incidents.items() if d.lines & lines or d.items & items), None)
        if inc is None:
            inc = f"INC-{len(self.incidents) + 1}"
            self.incidents[inc] = IncidentRecord(id=inc)
            self.audit.append(self.now, "agent", "incident_opened", inc, sorted(lines))
        else:
            self._void_pending(inc, [f"new supplier information: {', '.join(e.id for e in events)}"])
            self.incidents[inc].replan_count += 1
            self.audit.append(self.now, "agent", "incident_reopened", inc, sorted(lines))
        record = self.incidents[inc]
        record.lines |= lines
        record.items |= items
        record.run += 1
        config = {"configurable": {"thread_id": f"{inc}:{record.run}"}}
        state = {"incident_id": inc, "run": record.run, "excluded_suppliers": [], "excluded_options": [],
                 "reproposals": 0, "signatures": []}
        try:
            result = self.graph.invoke(state, config)
        except Exception as e:  # one incident's failure must not stop the runner: a person takes over
            self.audit.append(self.now, "agent", "planning_failed", inc, sorted(lines),
                              after={"error": f"{type(e).__name__}: {e}"})
            self._escalate(inc, self.routing.escalation, f"planning failed ({type(e).__name__}: {e}); "
                           "nothing was changed or sent", [inc])
            return
        self._after_invoke(result, config)

    def _after_invoke(self, result: dict, config: dict) -> None:
        thread = config["configurable"]["thread_id"]
        for rid in [r for r, p in self.pending.items() if p.thread_id == thread]:
            del self.pending[rid]
        if result.get("__interrupt__"):
            (intr,) = result["__interrupt__"]
            rid = intr.value["request_id"]
            self.pending[rid] = PendingRun(thread_id=thread, incident_id=result["incident_id"], request_id=rid,
                                           outstanding_roles=intr.value["outstanding_roles"])

    def _escalate(self, inc: str, role: str, reason: str, refs: list[str]) -> dict:
        self._task(role, f"{inc}: {reason}", refs, inc)
        self.incidents[inc].status = "escalated"
        self.audit.append(self.now, "agent", "escalated", inc, refs, after={"role": role, "reason": reason})
        return {"status": "escalated", "request": None}

    def _blocked(self, inc: str, analysis, option, proposal) -> dict:
        """The best option is blocked by a rule: record it and route the choice to a person."""
        rules = []
        for action, c in zip(proposal.actions, proposal.classifications):
            if c.tier == "blocked":
                rules += c.rule_ids
                self.blocked.append({"kind": action.params.kind.value, "rule_ids": c.rule_ids,
                                     "supplier_id": getattr(action.params, "supplier_id", None)})
        m, rest = analysis.mandate, [o.id for o in analysis.options[1:2]]
        detail = (f"best option {option.id} is outside mandate: offer {option.buys[0].unit_base:.2f} > max "
                  f"{m.max_unit_price:.2f} {self.policy.company.base_currency}; next best {', '.join(rest)}"
                  if m and not m.offer_within_mandate else
                  f"best option {option.id} blocked by {', '.join(rules)}; next best {', '.join(rest)}")
        role = next((self.routing.blocked_by_rule[r] for r in rules if r in self.routing.blocked_by_rule),
                    self.routing.blocked_option)
        return self._escalate(inc, role, f"{detail}. Choose how to proceed.", [inc])

    # --- incident graph ----------------------------------------------------------------------

    def _build_graph(self) -> StateGraph:
        def plan(state: IncidentState) -> IncidentState:
            inc, run = state["incident_id"], state["run"]
            record = self.incidents[inc]
            record.plans += 1
            cap = self.policy.caps.max_replans_per_incident
            if record.plans > 1 + cap:
                return self._escalate(inc, self.routing.escalation,
                                      f"re-planned {record.plans - 1} times (cap max_replans_per_incident {cap})", [inc])
            events = self.ledger.verified()
            ledger_events = list(self.ledger.events.values())
            analysis = Planner(self.store, self.policy, events, self.now).options(
                set(state.get("excluded_suppliers", [])), set(state.get("excluded_options", [])), items=record.items)
            if not analysis.recommendation:
                # No shortage: still make the records match what suppliers told us.
                proposal = build_record_update(self.store, self.policy, events, record.items, inc, run, ledger_events)
                if proposal is None:
                    record.status = "monitoring"
                    self.audit.append(self.now, "agent", "no_shortage", inc)
                    return {"status": "no_shortage", "request": None}
                option_id, kind, suppliers, total = "OPT-RECORDS", "records", [], "0.00"
                secrets = secret_amounts(self.store, self.policy)
                detail = record_update_detail(self.store, self.policy, proposal, ledger_events, self.inbox)
            else:
                option = analysis.recommendation
                proposal = build_proposal(self.store, self.policy, analysis, option, events, inc, run, self.now,
                                          ledger_events=ledger_events)
                if proposal.bundle.tier == "blocked":
                    return self._blocked(inc, analysis, option, proposal)
                option_id, kind, suppliers, total = option.id, option.kind, list(option.suppliers), str(option.expected_total)
                secrets = secret_amounts(self.store, self.policy, analysis)
                detail = request_detail(self.store, self.policy, analysis, proposal, ledger_events, self.inbox)
            problems = self.gateway.preflight(inc, proposal.actions, proposal.commitment_base,
                                              proposal.incremental_base, secrets, self.now)
            if problems:
                return self._escalate(inc, self.routing.escalation,
                                      f"recommended {option_id} would be refused by the action gateway: "
                                      + "; ".join(problems), [inc])
            buys = [a.params for a in proposal.actions if a.params.kind == ActionKind.CREATE_PO]
            expires = self.now + timedelta(hours=self.policy.approvals.expiry_hours)
            rec = RequestRecord(
                id=self.store.new_id("approval_requests"), incident_id=inc, run=run, option_id=option_id,
                kind=kind, item_id=proposal.item_id, suppliers=suppliers, tier=proposal.bundle.tier,
                roles=list(proposal.bundle.roles), rule_ids=proposal.bundle.rule_ids,
                actions=[a.params.kind.value for a in proposal.actions], qty=buys[0].qty if buys else None,
                evidence=proposal.evidence, fingerprint=proposal.fingerprint, basis=proposal.basis,
                created_at=self.now, expires_at=expires, detail=detail)
            self.requests.append(rec)
            self.audit.append(self.now, "agent", "approval_requested", inc, [rec.id, *(a.id for a in proposal.actions)],
                              after={"option_id": option_id, "tier": rec.tier, "roles": rec.roles,
                                     "rule_ids": rec.rule_ids, "fingerprint": rec.fingerprint,
                                     "expected_total": total,
                                     "actions": [a.model_dump(mode="json") for a in proposal.actions]})
            if rec.roles:
                self.schedule(self.now + timedelta(hours=self.policy.approvals.reminder_after_hours), "reminder",
                              ref=rec.id)
                self.schedule(expires, "expiry", ref=rec.id)
            record.status = "awaiting_approval"
            return {"status": "awaiting_approval", "signatures": [], "request": {
                "id": rec.id, "tier": rec.tier, "roles": rec.roles, "option_id": option_id,
                "item_id": proposal.item_id, "basis": proposal.basis, "expires_at": expires.isoformat(),
                "actions": [a.model_dump(mode="json") for a in proposal.actions], "secrets": secrets,
                "commitment": str(proposal.commitment_base), "incremental": str(proposal.incremental_base),
            }}

        def after_plan(state: IncidentState) -> str:
            req = state.get("request")
            if not req:
                return END
            return "request_signature" if req["roles"] else "revalidate"

        def request_signature(state: IncidentState) -> IncidentState:
            req, signed = state["request"], state.get("signatures", [])
            outstanding = [r for r in req["roles"] if r not in {s["role"] for s in signed}]
            raw = interrupt({"request_id": req["id"], "outstanding_roles": outstanding},
                            response_schema=ApprovalDecision)
            d = ApprovalDecision.model_validate(raw)
            approver = self.store.get("approvers", d.approver_id)
            if d.request_id != req["id"] or d.role not in outstanding or approver is None or d.role not in approver.roles:
                raise ValueError(f"invalid signature {d.approver_id}/{d.role} for {req['id']}")
            if d.approver_id in {s["approver_id"] for s in signed}:
                raise ValueError(f"{d.approver_id} already signed {req['id']}")
            return {"signatures": [*signed, d.model_dump(mode="json")]}

        def after_signature(state: IncidentState) -> str:
            req, signed = state["request"], state["signatures"]
            if signed[-1]["decision"] == "reject":
                return "rejected"
            return "revalidate" if {s["role"] for s in signed} >= set(req["roles"]) else "request_signature"

        def rejected(state: IncidentState) -> Command:
            req, last, inc = state["request"], state["signatures"][-1], state["incident_id"]
            log = self.request(req["id"])
            log.decided, log.decided_at = "rejected", self.now
            self.audit.append(self.now, last["approver_id"], "approval_rejected", inc, [req["id"]],
                              after={"exclude_supplier_ids": last["exclude_supplier_ids"], "comment": last["comment"]})
            if state.get("reproposals", 0) < self.policy.approvals.reproposals_after_rejection:
                return Command(goto="plan", update={
                    "excluded_suppliers": [*state.get("excluded_suppliers", []), *last["exclude_supplier_ids"]],
                    "excluded_options": [*state.get("excluded_options", []), req["option_id"]],
                    "reproposals": state.get("reproposals", 0) + 1, "request": None, "signatures": []})
            return Command(goto=END, update=self._escalate(inc, self.routing.escalation, "recommendation rejected twice",
                                                           [req["id"]]))

        def revalidate(state: IncidentState) -> Command:
            req = state["request"]
            current = basis_of(self.store, self.policy, req["item_id"], list(self.ledger.events.values()))
            reasons = basis_changed(self.policy, req["basis"], current)
            if reasons:
                log = self.request(req["id"])
                log.decided, log.voided_at, log.void_reasons = "voided", self.now, reasons
                self.audit.append(self.now, "agent", "approval_voided", state["incident_id"], [req["id"]],
                                  after={"reasons": reasons})
                return Command(goto="plan", update={"request": None, "signatures": []})
            return Command(goto="execute")

        def execute(state: IncidentState) -> IncidentState:
            req, inc = state["request"], state["incident_id"]
            actions = [ProposedAction.model_validate(raw) for raw in req["actions"]]
            log = self.request(req["id"])
            auth = Authority(tier=req["tier"], roles=req["roles"], request_id=req["id"],
                             signatures=[ApprovalDecision.model_validate(s) for s in state.get("signatures", [])],
                             expires_at=datetime.fromisoformat(req["expires_at"]))
            try:
                self.gateway.execute(inc, actions, auth, self.now, commitment=Decimal(req["commitment"]),
                                     incremental=Decimal(req["incremental"]), secrets=req["secrets"])
            except Refused as r:
                log.decided, log.decided_at = "refused", self.now
                return self._escalate(inc, self.routing.escalation,
                                      f"action gateway refused {req['id']}: {'; '.join(r.reasons)}", [req["id"]])
            for action in actions:
                if action.params.kind == ActionKind.CREATE_PO:
                    line_id = action.params.po_id.replace("PO-", "POL-", 1) + "-1"
                    self.incidents[inc].lines.add(line_id)
                    self._watch_line(line_id, self.now + timedelta(hours=self.policy.extraction.follow_up_hours))
            self.ledger.consume(self.incidents[inc].lines)
            log.decided, log.decided_at = ("approved" if req["roles"] else "executed"), self.now
            self.incidents[inc].status = "monitoring"
            self.audit.append(self.now, "agent", "plan_executed", inc, [req["id"]])
            return {"status": "executed"}

        g = StateGraph(IncidentState)
        for name, fn in [("plan", plan), ("request_signature", request_signature), ("rejected", rejected),
                         ("revalidate", revalidate), ("execute", execute)]:
            g.add_node(name, fn)
        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", after_plan, ["request_signature", "revalidate", END])
        g.add_conditional_edges("request_signature", after_signature, ["request_signature", "revalidate", "rejected"])
        g.add_edge("execute", END)
        return g
