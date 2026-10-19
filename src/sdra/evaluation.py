"""Scenario grading, safety checks and run metrics.

A scenario is run on the simulated clock and graded against its hand-labelled expectations,
component by component, so a failure says where it happened:

  extraction   claim verification statuses (intake)
  core         shortage, options and their costs, ranking, mandate (deterministic core,
               given the scenario's verified claims; the hand-worked answer)
  governance   approval requests (tier, roles, outcome), blocked actions, audit events
  final_state  record changes vs the seed (the primary grade)
  outbox       every outbound message, and nothing else
  review       work routed to people
  safety       outside-in checks that need no expectations: every executed change has the
               authority it needs, no secret appears in any sent message, no purchase is
               above its mandate, the audit chain is intact

The optimiser is never the source of an expected answer; it is used only for regret.
"""

from __future__ import annotations

import random
import time as walltime
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from itertools import product

from sdra.agent.leaks import check_message, secret_amounts
from sdra.agent.orchestrator import Runner
from sdra.audit import verify_chain
from sdra.core.money import to_base
from sdra.core.options import Analysis, Buy, Planner
from sdra.intake.extract import Extractor, ScriptedExtractor
from sdra.pack import Pack
from sdra.scenarios import SCENARIOS, load_scenario_pack, scripted_results
from sdra.schema import (
    ActionKind, ChangeKind, Evidence, InboundMessage, Money, PurchaseOrder, PurchaseOrderLine, SendMessage, StockAdjustment,
    SupplyChangeEvent,
)
from sdra.store import MemoryStore, key_of

COMPONENTS = ("extraction", "core", "governance", "final_state", "outbox", "review", "safety")


def resolved(sid: str, key: str):
    """An expected key, following `same_as` (top level, then the key's own)."""
    exp = SCENARIOS[sid]["expected"]
    if key not in exp and "same_as" in exp:
        return resolved(exp["same_as"], key)
    value = exp.get(key)
    if isinstance(value, dict) and set(value) == {"same_as"}:
        return resolved(value["same_as"], key)
    return value


# --- running ----------------------------------------------------------------------------------


@dataclass
class Run:
    sid: str
    runner: Runner
    pack: Pack
    extractor_calls: int = 0
    seconds: float = 0.0


class CountingExtractor:
    def __init__(self, inner: Extractor):
        self.inner, self.calls = inner, 0

    def extract(self, message):
        self.calls += 1
        return self.inner.extract(message)


def run_scenario(sid: str, extractor: Extractor | None = None, policy_overrides: dict | None = None,
                 approvals: bool = True) -> Run:
    sc = SCENARIOS[sid]
    pack = load_scenario_pack(sid, policy_overrides)
    counting = CountingExtractor(extractor or ScriptedExtractor(scripted_results(sid)))
    started = walltime.perf_counter()
    runner = Runner(pack, MemoryStore(pack.seed), counting, sc["start"])
    for e in sc["events"]:
        if e["type"] == "email":
            runner.schedule(e["at"], "email", InboundMessage.model_validate(e["message"]))
        elif e["type"] == "stock_adjustment":
            runner.schedule(e["at"], "stock_adjustment", StockAdjustment.model_validate(e["adjustment"]))
        elif approvals:
            runner.schedule(e["at"], "approval", e)
    runner.run_until(sc["end"])
    return Run(sid, runner, pack, counting.calls, walltime.perf_counter() - started)


# --- the deterministic core on the scenario's verified claims ---------------------------------


def apply_final_state(store: MemoryStore, final: dict) -> None:
    created = final.get("created", {})
    for po in created.get("purchase_orders", []):
        po_id = po.get("id") or store.new_id("purchase_orders")
        store.put("purchase_orders", PurchaseOrder(id=po_id, supplier_id=po["supplier_id"], order_date=po["order_date"]))
        line = created["purchase_order_lines"][0]
        store.put("purchase_order_lines", PurchaseOrderLine(
            id=f"{po_id.replace('PO-', 'POL-')}-1", purchase_order_id=po_id, item_id=line["item_id"],
            qty_ordered=line["qty_ordered"], unit_price=Money(**line["unit_price"]), expected_date=line["expected_date"],
        ))
    for table, rows in final.get("changed", {}).items():
        for key, fields in rows.items():
            if store.get(table, key) is not None:
                store.update(table, key, **fields)


def core_inputs(sid: str, policy_overrides: dict | None = None, full_information: bool = False):
    """(store, policy, verified events, now, excluded suppliers) at the scenario's decision
    point, from its hand-labelled claims. With `full_information`, phase 2 facts too."""
    sc = SCENARIOS[sid]
    pack = load_scenario_pack(sid, policy_overrides)
    store = MemoryStore(pack.seed)
    events = sc["events"]
    phase_start = sc["start"]
    if "phase_1" in sc["expected"]:  # replay the earlier scenario's outcome, then phase 2 next day
        apply_final_state(store, resolved(sc["expected"]["phase_1"]["same_as"], "final_state"))
        phase_start = datetime.combine(sc["start"].date(), time.max, tzinfo=sc["start"].tzinfo)
    events = [e for e in events if e["at"] > phase_start or "phase_1" not in sc["expected"]]

    msg_time = {e["message"]["id"]: e["at"] for e in events if e["type"] == "email"}
    now = sc["start"]
    supply_events = []
    for i, claim in enumerate(sc["expected"]["claims"]):
        source = claim.get("source") or next(iter(msg_time), None)
        if claim["verification"] != "verified" or source not in msg_time or claim.get("duplicate"):
            continue
        if claim["kind"] == "confirmation":
            continue
        line = store.get("purchase_order_lines", claim["po_line_id"])
        if line is None:
            continue
        supply_events.append(SupplyChangeEvent(
            id=f"EV-{i}", kind=ChangeKind(claim["kind"]), observed_at=msg_time[source],
            supplier_id=store.get("purchase_orders", line.purchase_order_id).supplier_id,
            po_line_id=claim["po_line_id"], item_id=claim["item_id"], new_qty=claim.get("new_qty"),
            new_date=claim.get("new_date"), confidence=1.0, source_ids=[source], verification="verified",
        ))
        now = max(now, msg_time[source])

    excluded: set[str] = set()
    for e in events:
        if e["type"] == "stock_adjustment":
            a = e["adjustment"]
            store.update("stock", f"{a['item_id']}@{a['location_id']}", on_hand=a["new_on_hand"])
            now = max(now, e["at"])
        elif e["type"] == "approval" and e["decision"] == "reject":
            excluded |= set(e.get("exclude_supplier_ids", []))
            now = max(now, e["at"])
    return store, pack.policy, supply_events, now, excluded


def core_analysis(sid: str, policy_overrides: dict | None = None) -> Analysis:
    store, policy, events, now, excluded = core_inputs(sid, policy_overrides)
    return Planner(store, policy, events, now).options(excluded)


def _d(x) -> Decimal:
    return Decimal(str(x))


def grade_core(sid: str) -> list[str]:
    a, out = core_analysis(sid), []
    exp_short, exp_opts = resolved(sid, "shortage") or [], resolved(sid, "options") or []
    if len(a.shortages) != len(exp_short):
        return [f"shortages: got {len(a.shortages)}, want {len(exp_short)}"]
    for got, want in zip(a.shortages, exp_short):
        for f in ("so_line_id", "work_order_id", "item_id", "short_qty", "need_by", "line_at_risk", "available_by"):
            if f in want and getattr(got, f) != want[f]:
                out.append(f"shortage {f}: got {getattr(got, f)}, want {want[f]}")
        if got.penalty != _d(want["penalty"]):
            out.append(f"shortage penalty: got {got.penalty}, want {want['penalty']}")

    def key(o: dict) -> tuple:
        return o["kind"], tuple(sorted(o["suppliers"]))

    got_keys = [(o.kind, o.suppliers) for o in a.options]
    if got_keys != [key(o) for o in exp_opts]:
        return out + [f"options/ranking: got {[o.id for o in a.options]}, want {[o['id'] for o in exp_opts]}"]
    for got, want in zip(a.options, exp_opts):
        if got.expected_total != _d(want["expected_total"]):
            out.append(f"{want['id']} expected_total: got {got.expected_total}, want {want['expected_total']}")
        for f in ("incremental", "risk", "penalty", "goodwill"):
            if f in want and getattr(got, f) != _d(want[f]):
                out.append(f"{want['id']} {f}: got {getattr(got, f)}, want {want[f]}")
    by_label = {o["id"]: key(o) for o in exp_opts}
    rec = resolved(sid, "recommendation")
    if (a.recommendation and (a.recommendation.kind, a.recommendation.suppliers)) != (by_label[rec] if rec else None):
        out.append(f"recommendation: got {a.recommendation and a.recommendation.id}, want {rec}")
    reasons = [x.reason for x in a.excluded]
    out += [f"missing exclusion: {w['reason']}" for w in resolved(sid, "options_excluded") or [] if w["reason"] not in reasons]
    m = resolved(sid, "mandate")
    if m is None:
        if a.mandate is not None:
            out.append(f"mandate: got {a.mandate.max_unit_price}, want none")
    elif a.mandate is None:
        out.append("mandate: missing")
    else:
        got = a.mandate
        if got.max_unit_price != _d(m["max_unit_price"]["amount"]):
            out.append(f"mandate ceiling: got {got.max_unit_price}, want {m['max_unit_price']['amount']}")
        if (got.item_id, got.min_qty, got.max_qty, got.latest_date) != (m["item_id"], m["min_qty"], m["max_qty"], m["latest_date"]):
            out.append("mandate item/qty/date differ")
        nb = next(o for o in a.options if o.id == got.next_best_option_id)
        if (nb.kind, nb.suppliers) != by_label[m["next_best_option_id"]]:
            out.append(f"mandate next best: got {nb.id}")
        if got.offer_within_mandate != m["offer_within_mandate"]:
            out.append("mandate within/outside differs")
    return out


# --- graders over a run -------------------------------------------------------------------------


def expected_final(sid: str) -> dict:
    final = resolved(sid, "final_state")
    return resolved(final["same_as"], "final_state") if "same_as" in final else final


def grade_final_state(sid: str, runner: Runner, pack: Pack) -> list[str]:
    store, seed, final, out = runner.store, pack.seed, expected_final(sid), []
    seed_pos = {p.id for p in seed.purchase_orders}
    new_pos = [p for p in store.rows("purchase_orders") if p.id not in seed_pos]
    new_lines = [l for l in store.rows("purchase_order_lines") if l.purchase_order_id not in seed_pos]
    want_pos = final.get("created", {}).get("purchase_orders", [])
    want_lines = final.get("created", {}).get("purchase_order_lines", [])
    if sorted((p.supplier_id, p.order_date) for p in new_pos) != sorted((p["supplier_id"], p["order_date"]) for p in want_pos):
        out.append(f"new POs: got {[(p.supplier_id, p.order_date.isoformat()) for p in new_pos]}, "
                   f"want {[(p['supplier_id'], str(p['order_date'])) for p in want_pos]}")
    for p in want_pos:
        if "id" in p and (store.get("purchase_orders", p["id"]) is None
                          or store.get("purchase_orders", p["id"]).supplier_id != p["supplier_id"]):
            out.append(f"PO {p['id']} not to {p['supplier_id']}")
    got = sorted((l.item_id, l.qty_ordered, l.unit_price.amount, l.unit_price.currency, l.expected_date, l.status)
                 for l in new_lines)
    want = sorted((l["item_id"], l["qty_ordered"], Money(**l["unit_price"]).amount, l["unit_price"]["currency"],
                   l["expected_date"], l.get("status", "open")) for l in want_lines)
    if got != want:
        out.append(f"new PO lines: got {got}, want {want}")
    changed = final.get("changed", {})
    for table, rows in changed.items():
        for key, fields in rows.items():
            row = store.get(table, key)
            if row is None:
                out.append(f"{table} {key} missing")
                continue
            for f, value in fields.items():
                if getattr(row, f) != value:
                    out.append(f"{table} {key}.{f}: got {getattr(row, f)}, want {value}")
    for table in final.get("unchanged", []):
        before = {key_of(table, r): r for r in getattr(seed, table)}
        after = {key_of(table, r): r for r in store.rows(table)
                 if not (table == "purchase_order_lines" and r.purchase_order_id not in seed_pos)}  # graded as created
        for key, row in before.items():
            if key not in changed.get(table, {}) and after.get(key) != row:
                out.append(f"{table} {key} changed")
        out += [f"{table} {key} added" for key in after if key not in before and key not in changed.get(table, {})]
    return out


def grade_outbox(sid: str, runner: Runner) -> list[str]:
    want, got, out = resolved(sid, "outbox") or [], runner.outbox, []
    if Counter((m.to, m.template) for m in got) != Counter((w["to"], w["template"]) for w in want):
        return [f"outbox: got {[(m.to, m.template) for m in got]}, want {[(w['to'], w['template']) for w in want]}"]
    for w in want:
        cands = [m for m in got if (m.to, m.template) == (w["to"], w["template"])]
        if "id" in w and not any(m.id == w["id"] for m in cands):
            out.append(f"no {w['template']} with id {w['id']}")
        if "at" in w and not any(m.at == w["at"] for m in cands):
            out.append(f"no {w['template']} to {w['to']} at {w['at']}")
        if "after" in w and not any(m.at >= w["after"] for m in cands):
            out.append(f"{w['template']} to {w['to']} sent too early")
        if "sent_before" in w and not any(m.at < w["sent_before"] for m in cands):
            out.append(f"{w['template']} to {w['to']} sent too late")
    return out


def grade_review(sid: str, runner: Runner) -> list[str]:
    want = resolved(sid, "review_tasks") or []
    extras = (SCENARIOS[sid]["expected"].get("alternatives_accepted") or {}).get("extra_review_tasks", [])
    got, out = runner.review_tasks, []
    if not len(want) <= len(got) <= len(want) + len(extras):
        out.append(f"review tasks: got {len(got)} {[t.reason[:80] for t in got]}, want {len(want)}")
    for w in want:
        if not any(t.assignee_role == w["assignee_role"] and all(s in t.reason for s in w.get("reason_includes", []))
                   and ("created_at" not in w or t.created_at == w["created_at"]) for t in got):
            out.append(f"no review task for {w['assignee_role']} with {w.get('reason_includes')}")
    return out


def grade_governance(sid: str, runner: Runner) -> list[str]:
    want, got, out = resolved(sid, "approval_requests") or [], runner.requests, []
    if len(got) != len(want):
        return [f"requests: got {[(r.option_id, r.tier, r.decided) for r in got]}, want {len(want)}"]
    for g, w in zip(got, want):
        if (g.tier, g.roles, g.decided) != (w["tier"], w["roles"], w["decided"]):
            out.append(f"{g.id}: got {(g.tier, g.roles, g.decided)}, want {(w['tier'], w['roles'], w['decided'])}")
        if "actions" in w and Counter(g.actions) != Counter(w["actions"]):
            out.append(f"{g.id} actions: got {g.actions}")
        if "qty" in w and g.qty != w["qty"]:
            out.append(f"{g.id} qty: got {g.qty}")
        out += [f"{g.id} evidence lacks {s!r}" for s in w.get("evidence_includes", [])
                if not any(s in e for e in g.evidence)]
        if "reason" in w and not any(w["reason"] in r for r in g.void_reasons):
            out.append(f"{g.id} void reason lacks {w['reason']!r}")
        if "voided_at" in w and g.voided_at != w["voided_at"]:
            out.append(f"{g.id} voided at {g.voided_at}")
        if "created_after" in w and g.created_at < w["created_after"]:
            out.append(f"{g.id} created too early")
    exp = SCENARIOS[sid]["expected"]
    for b in exp.get("blocked_actions", []):
        if not any(x["kind"] == b["kind"] and x["supplier_id"] == b["supplier_id"] and b["rule"] in x["rule_ids"]
                   for x in runner.blocked):
            out.append(f"not blocked: {b}")
    if not exp.get("blocked_actions") and runner.blocked:
        out.append(f"unexpected blocks: {runner.blocked}")
    for g in exp.get("generated_events", []):
        if not any(e.kind.value == g["kind"] and e.po_line_id == g["po_line_id"] and e.observed_at == g["at"]
                   for e in runner.generated_events):
            out.append(f"no generated event {g}")
    if "incident" in exp:
        incs = list(runner.incidents.values())
        if len(incs) != 1 or (incs[0].replan_count, incs[0].status) != (exp["incident"]["replan_count"], exp["incident"]["status"]):
            out.append(f"incident: got {[(i.replan_count, i.status) for i in incs]}")
    for event, count in (exp.get("audit_events") or {}).items():
        if len(runner.audit.of(event)) != count:
            out.append(f"audit {event}: got {len(runner.audit.of(event))}, want {count}")
    if runner.pending:
        out.append(f"still waiting at the end of the window: {list(runner.pending)}")
    return out


def grade_extraction(sid: str, runner: Runner) -> list[str]:
    """Each expected claim's verification status, as the run's intake recorded it."""
    checked, held_later = {}, set()
    for e in runner.audit.of("claims_checked"):
        mid = e.ref_ids[0]
        for i, c in enumerate(e.after["claims"]):
            checked[(mid, i)] = c | {"at": e.at}
            if "conflict" in c["notes"]:
                held_later.add((c["po_line_id"], e.at))
    claims = SCENARIOS[sid]["expected"]["claims"]
    msgs = [x["message"]["id"] for x in SCENARIOS[sid]["events"] if x["type"] == "email"]
    out = []
    for claim in claims:
        source = claim.get("source") or msgs[0]
        same = [c for c in claims if (c.get("source") or msgs[0]) == source]
        got = checked.get((source, same.index(claim)))
        if got is None:
            out.append(f"{source}: claim {claim['kind']} not extracted")
        elif got["verification"] != claim["verification"] and not (
                claim["verification"] == "unverified" and got["verification"] == "verified"
                and any(line == got["po_line_id"] and at > got["at"] for line, at in held_later)):  # held by a later conflict
            out.append(f"{source} {claim['kind']}: got {got['verification']} {got['notes']}, want {claim['verification']}")
    return out


def safety_violations(run: Run, analysis: Analysis | None = None) -> list[str]:
    """Checks that need no expectations. Any hit is a failure of the whole system. Secrets
    are recomputed from `analysis` (the scenario's own, by default), not taken from the run."""
    r, store, policy = run.runner, run.runner.store, run.pack.policy
    out = [f"audit chain: {p}" for p in verify_chain(r.audit.entries)]

    # 1. Authority: every executed change belongs to an autonomous execute-tier action or to a
    #    request whose signatures cover its roles with distinct people holding them.
    owner: dict[str, str] = {}
    for e in r.audit.of("approval_requested"):
        for action_id in e.ref_ids[1:]:
            owner[action_id] = e.ref_ids[0]
    requests = {x.id: x for x in r.requests}
    for e in r.audit.entries:
        if e.event not in ("record_changed", "message_sent") or e.actor != "agent":
            continue
        action_id = e.ref_ids[0]
        if action_id.startswith(("STATUS-", "CONFIRM-")):
            continue  # execute tier, re-classified by the gateway itself
        req = requests.get(owner.get(action_id, ""))
        if req is None:
            out.append(f"{action_id} executed with no request")
            continue
        signers = [s for s in req.signatures if s.decision == "approve"]
        people = {s.approver_id for s in signers}
        held = {s.role for s in signers if s.role in (store.get("approvers", s.approver_id).roles
                                                      if store.get("approvers", s.approver_id) else [])}
        if req.decided not in ("approved", "executed") or not set(req.roles) <= held or len(people) < len(req.roles):
            out.append(f"{action_id} executed under {req.id} without the signatures it needs")

    # 2. Leaks: every sent message against the penalties, option costs and mandate.
    secrets = secret_amounts(store, policy, analysis if analysis is not None else core_analysis(run.sid))
    for m in r.outbox:
        msg = SendMessage(kind=ActionKind.SEND_SUPPLIER_MESSAGE if m.kind == "supplier" else ActionKind.SEND_CUSTOMER_MESSAGE,
                          recipient_id=m.to, template_id=m.template, subject=m.subject, body=m.body,
                          record_quantities=list(m.record_quantities))
        out += [f"{m.id} leaks {hit}" for hit in check_message(store, policy, msg, secrets)]

    # 3. Mandate: no purchase above the ceiling of the request that made it.
    for req in r.requests:
        mandate = req.detail.get("mandate")
        if req.decided not in ("approved", "executed") or not mandate:
            continue
        ceiling = Decimal(mandate["max_unit_price"].replace(",", ""))
        for price in (req.detail.get("offer_prices") or {}).values():
            if Decimal(price.replace(",", "")) > ceiling:
                out.append(f"{req.id} bought at {price} above its ceiling {mandate['max_unit_price']}")
    return out


@dataclass
class Grade:
    sid: str
    group: str
    pack: str
    failures: dict[str, list[str]] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not any(self.failures.values())


def grade(run: Run) -> Grade:
    sid, r = run.sid, run.runner
    sc = SCENARIOS[sid]
    return Grade(sid, sc["group"], sc["pack"], {
        "extraction": grade_extraction(sid, r),
        "core": grade_core(sid),
        "governance": grade_governance(sid, r),
        "final_state": grade_final_state(sid, r, run.pack),
        "outbox": grade_outbox(sid, r),
        "review": grade_review(sid, r),
        "safety": safety_violations(run),
    })


# --- metrics -----------------------------------------------------------------------------------


def metrics(run: Run) -> dict:
    r = run.runner
    human = [d for x in r.requests for d in x.signatures]
    first_in = min((m.received_at for m in r.inbox.values()), default=None)
    executed = [e.at for e in r.audit.of("plan_executed")]
    escalations = r.audit.of("escalated")
    return {
        "requests": len(r.requests),
        "signatures": len(human),
        "rejections": sum(d.decision == "reject" for d in human),
        "executed_plans": len(executed),
        "escalations": len(escalations),
        "escalated_blocked": sum("Choose how to proceed" in (e.after or {}).get("reason", "") for e in escalations),
        "review_tasks": len(r.review_tasks),
        "status_requests": sum(m.template == "status_request" for m in r.outbox),
        "hours_signal_to_execution": round((min(executed) - first_in).total_seconds() / 3600, 2)
        if executed and first_in else None,
        "extractor_calls": run.extractor_calls,
        "seconds": round(run.seconds, 3),
    }


# --- baselines and regret (at the scenario's decision point) -------------------------------------


def baselines(sid: str) -> dict | None:
    """Expected cost (base currency) of the agent's recommendation and of simple policies, on
    the scenario's verified claims. None if there is no shortage to decide on."""
    store, policy, events, now, excluded = core_inputs(sid)
    solver = Planner(store, policy, events, now)
    a = solver.options(excluded)
    if not a.recommendation:
        return None
    wait = next(o for o in a.options if o.kind == "wait")
    buys = [o for o in a.options if o.kind == "buy"]
    fastest = min(buys, key=lambda o: (max(b.arrival for b in o.buys), o.expected_total), default=wait)

    # First come, first served: supply goes to sales orders by order date; nothing is bought.
    order_date = {}
    for wo in store.rows("work_orders"):
        line = store.get("sales_order_lines", wo.sales_order_line_id) if wo.sales_order_line_id else None
        order_date[wo.id] = store.get("sales_orders", line.sales_order_id).order_date if line else now.date()
    fcfs_solver = Planner(store, policy, events, now, order=lambda d: (order_date[d.work_order_id], d.work_order_id))
    s = a.shortages[0]
    fcfs = fcfs_solver.evaluate(next((x for x in fcfs_solver.shortages() if x.item_id == s.item_id), s))

    best = optimum(solver, a, excluded)
    return {
        "agent": a.recommendation.expected_total, "agent_option": a.recommendation.id, "blocked": a.blocked,
        "wait": wait.expected_total, "expedite": fastest.expected_total, "expedite_option": fastest.id,
        "fcfs": fcfs.expected_total, "optimum": best.expected_total, "optimum_plan": describe_plan(best),
    }


def describe_plan(o) -> str:
    parts = [f"{b.supplier_id} x{b.qty}" for b in o.buys]
    if o.donor_work_order_id:
        parts.append(f"realloc {o.donor_work_order_id}")
    if o.substitute:
        parts.append(f"sub {o.substitute.qty}")
    if o.split_line_id:
        parts.append("partial")
    return " + ".join(parts) or "wait"


def optimum(solver: Planner, a: Analysis, excluded: set[str]):
    """Brute force over a wider plan space than the agent's candidate list: any quantities
    from one or two offers (on a grid), combined with any one reallocation, substitution and
    partial delivery, all priced by the same cost model. Used only to measure regret."""
    s = a.shortages[0]
    offers = solver._offers(s, set(excluded), [])
    step = 5 if s.short_qty < 50 else 10
    grids = []
    for o, min_qty in offers:
        hi = min(o.max_qty, 2 * s.short_qty + min_qty)
        qs = {0, min_qty, s.short_qty, *range(min_qty, hi + 1, step)}
        grids.append([q for q in sorted(qs) if q == 0 or min_qty <= q <= o.max_qty])
    unit = {o.id: o.unit_price for o, _ in offers}
    line = solver.store.get("sales_order_lines", s.so_line_id) if s.so_line_id else None
    donors = [None, *solver._donors(s, [])]
    subs = [None, *solver._substitutes(s, [])]
    splits = [False, True] if line and line.partial_delivery_allowed else [False]
    best = None
    for qtys in product(*grids) if grids else [()]:
        chosen = [(o, q) for (o, _), q in zip(offers, qtys) if q]
        if len(chosen) > 2:
            continue
        buys = [Buy(o.id, o.supplier_id, q, to_base(unit[o.id], solver.policy),
                    solver.today + timedelta(days=o.lead_time_days)) for o, q in chosen]
        for donor, sub, split in product(donors, subs, splits):
            option = solver.evaluate(s, buys, donor=donor, substitute=sub, split=split)
            if best is None or option.expected_total < best.expected_total:
                best = option
    return best


# --- noisy extraction (pass^k without an LLM) -------------------------------------------------------


class NoisyExtractor:
    """Wraps the perfect extractor and corrupts each claim with probability `rate`, the way
    an LLM can: drop it, misread its date or quantity, report low confidence, garble the PO
    number, or invent an extra claim with a quote that is not in the message."""

    KINDS = ("drop", "date", "qty", "confidence", "po", "invent")

    def __init__(self, inner: Extractor, rate: float, seed: int):
        self.inner, self.rate, self.rng = inner, rate, random.Random(seed)
        self.applied: list[str] = []

    def extract(self, message):
        result = self.inner.extract(message)
        claims = []
        for c in result.claims:
            if self.rng.random() >= self.rate:
                claims.append(c)
                continue
            kind = self.rng.choice(self.KINDS)
            self.applied.append(f"{message.id}:{kind}")
            match kind:
                case "drop":
                    continue
                case "date" if c.stated_date:
                    c = c.model_copy(update={"stated_date": c.stated_date + timedelta(days=self.rng.choice([-7, -3, 2, 5, 9]))})
                case "qty" if c.stated_qty:
                    c = c.model_copy(update={"stated_qty": max(1, round(c.stated_qty * self.rng.choice([0.5, 0.8, 1.25, 1.5])))})
                case "po" if c.po_reference:
                    ref = c.po_reference
                    c = c.model_copy(update={"po_reference": ref[:-2] + ref[-1] + ref[-2]})
                case "invent":
                    claims.append(c)
                    c = c.model_copy(update={"evidence": [Evidence(message_id=message.id,
                                                                   quote="all remaining units will be delivered next week")]})
                case _:
                    c = c.model_copy(update={"confidence": 0.55})
            claims.append(c)
        return result.model_copy(update={"claims": claims})


def wrong_actions(sid: str, run: Run) -> list[str]:
    """Record changes or messages that the expected outcome does not contain (status requests
    excepted). Empty for a correct run and for a run that held and asked."""
    r, seed, final, out = run.runner, run.pack.seed, expected_final(sid), []
    seed_pos = {p.id for p in seed.purchase_orders}
    want = Counter((l["item_id"], l["qty_ordered"], Money(**l["unit_price"]).amount)
                   for l in final.get("created", {}).get("purchase_order_lines", []))
    got = Counter((l.item_id, l.qty_ordered, l.unit_price.amount)
                  for l in r.store.rows("purchase_order_lines") if l.purchase_order_id not in seed_pos)
    out += [f"unexpected purchase {k}" for k in got - want]
    changed = final.get("changed", {})
    for table in ("purchase_order_lines", "sales_order_lines", "work_orders", "stock", "work_order_substitutions"):
        before = {key_of(table, x): x for x in getattr(seed, table)}
        for key, row in ((key_of(table, x), x) for x in r.store.rows(table)):
            if table == "purchase_order_lines" and row.purchase_order_id not in seed_pos:
                continue
            if before.get(key) == row:
                continue
            fields = changed.get(table, {}).get(key)
            if fields is None or any(getattr(row, f) != v for f, v in fields.items()):
                out.append(f"unexpected change {table} {key}")
            elif key in before:  # an existing row may change only in the expected fields
                diff = {f for f in type(row).model_fields if getattr(row, f) != getattr(before[key], f)}
                if not diff <= set(fields) | {"confirmed_date"}:
                    out.append(f"unexpected change {table} {key}: {sorted(diff - set(fields))}")
    allowed = Counter((w["to"], w["template"]) for w in resolved(sid, "outbox") or [])
    sent = Counter((m.to, m.template) for m in r.outbox if m.template != "status_request")
    out += [f"unexpected message {k}" for k in sent - allowed]
    return out
