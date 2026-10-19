"""Turn the recommended option into an action bundle, classify it, and record its basis.

Every quantity, date and price in an action comes from the deterministic core. Facts for
the autonomy rules are computed here from records, never from the LLM.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sdra.agent import templates
from sdra.core.money import to_base
from sdra.core.options import Analysis, Option
from sdra.governance import Classification, classify, classify_bundle
from sdra.policy import Policy
from sdra.schema import (
    ActionKind, ApplySubstitution, CancelPOLine, ChangeKind, CreatePO, ProposedAction, RescheduleWorkOrder,
    SendMessage, SplitPOLine, SplitSOLine, SupplyChangeEvent, UpdatePOExpectedDate, UpdatePOQuantity,
    UpdateSOPromiseDate,
)
from sdra.store import MemoryStore


@dataclass
class Proposal:
    option: Option | None  # None for a records-only update
    actions: list[ProposedAction]
    classifications: list[Classification]
    bundle: Classification
    basis: dict
    evidence: list[str] = field(default_factory=list)
    commitment_base: Decimal = Decimal(0)  # new or increased supplier commitment, for the caps
    incremental_base: Decimal = Decimal(0)  # extra cost vs the current plan (never below 0), for the caps
    item_id: str = ""  # the item whose facts the basis records

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.basis, sort_keys=True).encode()).hexdigest()


def basis_of(store: MemoryStore, policy: Policy, item_id: str, events: list[SupplyChangeEvent] = ()) -> dict:
    """The facts a recommendation relies on; re-checked before execution. `events` are the
    ledger's supplier claims (verified or held) not yet reflected in records."""
    return {
        "events": {e.id: [e.kind.value, e.new_qty, e.new_date.isoformat() if e.new_date else None, e.verification]
                   for e in events if e.item_id == item_id},
        "stock": {f"{s.item_id}@{s.location_id}": s.on_hand for s in store.rows("stock") if s.item_id == item_id},
        "po_lines": {l.id: [l.open_qty, l.expected_date.isoformat(), l.status]
                     for l in store.rows("purchase_order_lines") if l.item_id == item_id},
        "offers": {o.id: str(to_base(o.unit_price, policy)) for o in store.rows("offers") if o.item_id == item_id},
        "promise_dates": {l.id: l.promised_date.isoformat() for l in store.rows("sales_order_lines")},
        "work_orders": {w.id: [w.qty, w.start_date.isoformat(), w.status] for w in store.rows("work_orders")},
        "substitutions": sorted(f"{x.work_order_id}:{x.substitution_id}:{x.qty}"
                                for x in store.rows("work_order_substitutions")),
    }


def basis_changed(policy: Policy, old: dict, new: dict) -> list[str]:
    """Reasons an approval is void, per policy.approvals.void_if_changed."""
    rules, reasons = policy.approvals.void_if_changed, []
    if old.get("events", {}) != new.get("events", {}):  # always on: new or disputed supplier information
        reasons.append(f"supplier_information: {sorted(old.get('events', {}))} -> {sorted(new.get('events', {}))}")
    for key, before in old["stock"].items():
        after = new["stock"].get(key, 0)
        if before != after and (before == 0 or abs(after - before) * 100 / before > rules.stock_on_hand_pct):
            reasons.append(f"stock_on_hand_pct: {key} {before} -> {after}")
    for key, before in old["offers"].items():
        after = new["offers"].get(key)
        if after is None or abs(Decimal(after) - Decimal(before)) * 100 / Decimal(before) > rules.offer_price_pct:
            reasons.append(f"offer_price_pct: {key} {before} -> {after}")
    for key, (qty, expected, status) in old["po_lines"].items():
        after = new["po_lines"].get(key)
        if after is None or after[0] != qty or after[2] != status or (rules.any_po_expected_date and after[1] != expected):
            reasons.append(f"po_line: {key} {[qty, expected, status]} -> {after}")
    if rules.any_customer_promise_date:
        for key, before in old["promise_dates"].items():
            if new["promise_dates"].get(key) != before:
                reasons.append(f"promise_date: {key} {before} -> {new['promise_dates'].get(key)}")
    for key, before in old.get("work_orders", {}).items():  # always on: the plan is built on the schedule
        if new.get("work_orders", {}).get(key) != before:
            reasons.append(f"work_order: {key} {before} -> {new.get('work_orders', {}).get(key)}")
    if old.get("substitutions", []) != new.get("substitutions", []):
        reasons.append("work_order_substitutions changed")
    return reasons


def _deliver_to(store: MemoryStore, item_id: str) -> str:
    """Name of the location that holds the item, from records (first location otherwise)."""
    held = [p.location_id for p in store.rows("stock") if p.item_id == item_id]
    location = store.get("locations", held[0]) if held else store.rows("locations")[0]
    return location.name


def _facts(store: MemoryStore, policy: Policy, analysis: Analysis, option: Option, params) -> dict:
    f = dict(commitment_base=Decimal(0), incremental_cost_base=Decimal(0), changes_customer_date=False,
             creates_downstream_shortage=False, item_critical=False, supplier_approved_for_item=True,
             within_mandate=True, requires_customer_consent=False, customer_consent_recorded=False,
             template_only=True, hard_allocation=False)
    item_id = getattr(params, "item_id", None)
    if hasattr(params, "po_line_id"):
        item_id = store.get("purchase_order_lines", params.po_line_id).item_id
    if item_id:
        f["item_critical"] = store.get("items", item_id).critical
    match params:
        case CreatePO():
            f["commitment_base"] = to_base(params.unit_price, policy) * params.qty
            f["incremental_cost_base"] = option.incremental if len(option.buys) == 1 else Decimal(0)
            supplier = store.get("suppliers", params.supplier_id)
            f["supplier_approved_for_item"] = bool(
                supplier.approved and store.get("supplier_items", f"{params.supplier_id}:{params.item_id}"))
            m = analysis.mandate
            f["within_mandate"] = bool(m and m.option_id == option.id and m.offer_within_mandate)
        case UpdatePOQuantity():
            line = store.get("purchase_order_lines", params.po_line_id)
            f["commitment_base"] = max(params.new_qty - line.qty_ordered, 0) * to_base(line.unit_price, policy)
        case UpdateSOPromiseDate() | SplitSOLine():
            f["changes_customer_date"] = True
        case ApplySubstitution():
            sub = store.get("substitutions", params.substitution_id)
            f["item_critical"] = store.get("items", sub.item_id).critical
            f["incremental_cost_base"] = max(option.incremental, Decimal(0))
            f["requires_customer_consent"] = sub.requires_customer_consent
            f["customer_consent_recorded"] = False  # V1 keeps no consent records
    return f


def build_proposal(store: MemoryStore, policy: Policy, analysis: Analysis, option: Option,
                   events: list[SupplyChangeEvent], incident_id: str, run: int, now: datetime,
                   ledger_events: list[SupplyChangeEvent] | None = None) -> Proposal:
    params: list = []
    s = option.shortage

    # Supply side
    for line_id, qty in sorted(option.cancels.items()):
        line = store.get("purchase_order_lines", line_id)
        po = store.get("purchase_orders", line.purchase_order_id)
        if qty >= line.open_qty:
            params.append(CancelPOLine(po_line_id=line_id))
            subject, body = templates.po_cancellation(po.id, line.item_id, line.open_qty)
        else:
            new_qty = line.qty_ordered - qty
            params.append(UpdatePOQuantity(po_line_id=line_id, new_qty=new_qty))
            subject, body = templates.po_change(po.id, line.item_id, line.qty_ordered, new_qty)
        params.append(SendMessage(kind=ActionKind.SEND_SUPPLIER_MESSAGE, recipient_id=po.supplier_id,
                                  template_id="po_cancellation" if qty >= line.open_qty else "po_change",
                                  subject=subject, body=body,
                                  record_quantities=[line.open_qty] if qty >= line.open_qty
                                  else [line.qty_ordered, line.qty_ordered - qty, qty]))
    for b in option.buys:
        offer = store.get("offers", b.offer_id)
        po_id = store.new_id("purchase_orders")  # reserved; a rejected proposal leaves a gap
        params.append(CreatePO(po_id=po_id, supplier_id=b.supplier_id, item_id=s.item_id, qty=b.qty,
                               unit_price=offer.unit_price, expected_date=b.arrival, offer_id=offer.id,
                               mandate_id=f"MND-{incident_id}-{run}" if analysis.mandate else None))
        subject, body = templates.purchase_order(po_id, s.item_id, b.qty, offer.unit_price, b.arrival,
                                                 _deliver_to(store, s.item_id))
        params.append(SendMessage(kind=ActionKind.SEND_SUPPLIER_MESSAGE, recipient_id=b.supplier_id,
                                  template_id="purchase_order", subject=subject, body=body,
                                  record_quantities=[b.qty]))
    if option.substitute:
        params.append(ApplySubstitution(substitution_id=option.substitute.substitution_id,
                                        work_order_id=s.work_order_id, qty=option.substitute.qty))
    # Make the records match what the supplier told us, for late quantity the option keeps.
    params += _record_params(store, events, {s.item_id}, option.cancels)

    # Schedule and customer side
    for wo in store.rows("work_orders"):
        outcome = option.outcomes.get(wo.sales_order_line_id) if wo.sales_order_line_id else None
        if not outcome or outcome.completion is None or outcome.completion == wo.due_date:
            continue
        line = store.get("sales_order_lines", wo.sales_order_line_id)
        order = store.get("sales_orders", line.sales_order_id)
        if line.id == option.split_line_id:  # partial delivery: the line and its work order split in two
            first = line.qty - outcome.late_qty
            params.append(SplitSOLine(so_line_id=line.id, first_qty=first, second_date=outcome.completion))
            subject, body = templates.delivery_split(order.id, line.item_id, first, line.promised_date,
                                                     outcome.late_qty, outcome.completion)
            params.append(SendMessage(kind=ActionKind.SEND_CUSTOMER_MESSAGE, recipient_id=order.customer_id,
                                      template_id="delivery_split", subject=subject, body=body,
                                      record_quantities=[first, outcome.late_qty]))
            continue
        assembly = timedelta(days=store.get("items", wo.item_id).assembly_lead_days)
        params.append(RescheduleWorkOrder(work_order_id=wo.id, new_start_date=outcome.completion - assembly,
                                          new_due_date=outcome.completion))
        if outcome.completion > line.promised_date:
            params.append(UpdateSOPromiseDate(so_line_id=line.id, new_date=outcome.completion))
            subject, body = templates.delivery_date_change(order.id, line.item_id, line.qty, outcome.completion)
            params.append(SendMessage(kind=ActionKind.SEND_CUSTOMER_MESSAGE, recipient_id=order.customer_id,
                                      template_id="delivery_date_change", subject=subject, body=body,
                                      record_quantities=[line.qty]))

    actions = [ProposedAction(id=f"{incident_id}-R{run}-A{i + 1}", incident_id=incident_id,
                              idempotency_key=f"{incident_id}:{run}:{i + 1}:{p.kind.value}", params=p)
               for i, p in enumerate(params)]
    facts = [_facts(store, policy, analysis, option, a.params) for a in actions]
    classifications = [classify(policy, a.params.kind, f) for a, f in zip(actions, facts)]

    evidence = []
    for b in option.buys:
        slack = (s.need_by - b.arrival).days
        evidence.append(f"{b.supplier_id} arrives {b.arrival.isoformat()}, needed {s.need_by.isoformat()}: "
                        + ("zero days of slack" if slack == 0 else f"{slack} days of slack"))
        evidence.append(f"{b.supplier_id} on-time rate {store.get('suppliers', b.supplier_id).on_time_rate:.2f}")
    if option.donor_work_order_id:
        donor = store.get("work_orders", option.donor_work_order_id)
        after = option.outcomes.get(donor.sales_order_line_id) if donor.sales_order_line_id else None
        when = ("never, in the current plan" if after and after.completion is None
                else f"{after.completion.isoformat()} ({after.late_days} days late)" if after and after.late_days
                else "within its promise date")
        evidence.append(f"{donor.id} gives up its {s.item_id} and completes {when}")
    if option.substitute:
        x = option.substitute
        evidence.append(f"{x.qty} x {x.item_id} on hand and not needed by other orders by {s.need_by.isoformat()} "
                        f"({x.substitution_id})")
    if option.split_line_id:
        out = option.outcomes[option.split_line_id]
        evidence.append(f"{option.split_line_id} allows partial delivery: {out.late_qty} units follow on "
                        f"{out.completion.isoformat()}")
    if option.penalty and all(o.penalty for o in analysis.options):
        evidence.append(f"penalty {option.penalty:.0f} now unavoidable")
    return Proposal(option, actions, classifications, classify_bundle(policy, classifications),
                    basis_of(store, policy, s.item_id, ledger_events if ledger_events is not None else events), evidence,
                    commitment_base=sum((f["commitment_base"] for f in facts), Decimal(0)),
                    incremental_base=max(option.incremental, Decimal(0)), item_id=s.item_id)


def _record_params(store: MemoryStore, events: list[SupplyChangeEvent], item_ids: set[str],
                   cancels: dict[str, int] | None = None) -> list:
    """Record updates that make PO lines match verified supplier dates, for late quantity
    that is kept (not cancelled)."""
    params: list = []
    for e in events:
        if not (e.po_line_id and e.kind == ChangeKind.DELAY and e.item_id in item_ids and e.new_date):
            continue
        line = store.get("purchase_order_lines", e.po_line_id)
        cut = (cancels or {}).get(line.id, 0)
        remaining = line.open_qty - cut
        late = (e.new_qty if e.new_qty is not None else line.open_qty) - cut
        if late <= 0 or remaining <= 0:
            continue
        if late >= remaining:
            params.append(UpdatePOExpectedDate(po_line_id=line.id, new_date=e.new_date))
        else:
            params.append(SplitPOLine(po_line_id=line.id, first_qty=remaining - late, second_date=e.new_date))
    return params


def build_record_update(store: MemoryStore, policy: Policy, events: list[SupplyChangeEvent], item_ids: set[str],
                        incident_id: str, run: int, ledger_events: list[SupplyChangeEvent]) -> Proposal | None:
    """No shortage follows from the supplier's news, but the PO records should still show it."""
    params = _record_params(store, events, item_ids)
    if not params:
        return None
    actions = [ProposedAction(id=f"{incident_id}-R{run}-A{i + 1}", incident_id=incident_id,
                              idempotency_key=f"{incident_id}:{run}:{i + 1}:{p.kind.value}", params=p)
               for i, p in enumerate(params)]
    facts = []
    for a in actions:
        f = _facts(store, policy, None, None, a.params)
        f["creates_downstream_shortage"] = False  # the plan with these dates has no shortage of these items
        facts.append(f)
    classifications = [classify(policy, a.params.kind, f) for a, f in zip(actions, facts)]
    item_id = store.get("purchase_order_lines", params[0].po_line_id).item_id
    evidence = [f"no shortage follows: {', '.join(sorted({store.get('purchase_order_lines', p.po_line_id).item_id for p in params}))} "
                "demand is still covered on time"]
    return Proposal(None, actions, classifications, classify_bundle(policy, classifications),
                    basis_of(store, policy, item_id, ledger_events), evidence, item_id=item_id)
