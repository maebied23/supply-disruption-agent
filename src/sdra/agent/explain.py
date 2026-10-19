"""What an approver sees: plain descriptions of actions and the case for the recommendation.

Built from the deterministic analysis and records only. Designed to make checking cheap
(Vasconcelos et al. 2023): the supplier's own words with the quoted span, the alternatives as
differences from the recommendation, and what the numbers assume.
"""

from __future__ import annotations

from decimal import Decimal

from sdra.agent.proposal import Proposal
from sdra.core.money import to_base
from sdra.core.options import Analysis, Option
from sdra.policy import Policy
from sdra.schema import (
    ApplySubstitution, CancelPOLine, CreatePO, InboundMessage, RecordPOConfirmation, RescheduleWorkOrder,
    SendMessage, SplitPOLine, SplitSOLine, SupplyChangeEvent, UpdatePOExpectedDate, UpdatePOQuantity,
    UpdateSOPromiseDate,
)
from sdra.store import MemoryStore


def _amount(x: Decimal) -> str:
    return f"{x:,.2f}"


def describe_action(store: MemoryStore, params) -> str:
    match params:
        case CreatePO():
            return (f"Order {params.qty} x {params.item_id} from {params.supplier_id} at "
                    f"{params.unit_price.amount:.2f} {params.unit_price.currency}, arriving "
                    f"{params.expected_date.isoformat()} ({params.po_id})")
        case UpdatePOQuantity():
            line = store.get("purchase_order_lines", params.po_line_id)
            return f"Cut {params.po_line_id} from {line.qty_ordered} to {params.new_qty}"
        case CancelPOLine():
            return f"Cancel the open quantity on {params.po_line_id}"
        case UpdatePOExpectedDate():
            return f"Record {params.po_line_id} as expected {params.new_date.isoformat()}"
        case SplitPOLine():
            return f"Split {params.po_line_id}: {params.first_qty} keep their date, the rest {params.second_date.isoformat()}"
        case RecordPOConfirmation():
            return f"Record {params.po_line_id} as confirmed for {params.confirmed_date.isoformat()}"
        case RescheduleWorkOrder():
            return (f"Reschedule {params.work_order_id} to {params.new_start_date.isoformat()} - "
                    f"{params.new_due_date.isoformat()}")
        case UpdateSOPromiseDate():
            return f"Move the promise date of {params.so_line_id} to {params.new_date.isoformat()}"
        case SplitSOLine():
            line = store.get("sales_order_lines", params.so_line_id)
            return (f"Split {params.so_line_id}: {params.first_qty} on {line.promised_date.isoformat()}, "
                    f"{line.qty - params.first_qty} on {params.second_date.isoformat()} (work order splits too)")
        case ApplySubstitution():
            sub = store.get("substitutions", params.substitution_id)
            return (f"Build {params.qty} units of {params.work_order_id} with {sub.substitute_item_id} "
                    f"instead of {sub.item_id} ({sub.id})")
        case SendMessage():
            return f"Send '{params.subject}' to {params.recipient_id} ({params.template_id or 'free text'})"
    return params.kind.value


def describe_option(option: Option) -> str:
    if option.kind == "wait":
        return "Accept the delay"
    if option.kind == "reallocate":
        return f"Give {option.shortage.work_order_id} priority over {option.donor_work_order_id} for {option.shortage.item_id}"
    if option.kind == "substitute":
        x = option.substitute
        return f"Use {x.qty} x {x.item_id} instead of {option.shortage.item_id} ({x.substitution_id})"
    if option.kind == "partial":
        out = option.outcomes[option.split_line_id]
        return f"Deliver {option.split_line_id} in two parts ({out.late_qty} units on {out.completion.isoformat()})"
    parts = [f"Buy {b.qty} from {b.supplier_id} (arrives {b.arrival.isoformat()})" for b in option.buys]
    parts += [f"cancel {q} late on {line}" for line, q in sorted(option.cancels.items())]
    return "; ".join(parts)


def _costs(option: Option, best: Option) -> dict:
    return {"option_id": option.id, "description": describe_option(option), "suppliers": list(option.suppliers),
            "incremental": _amount(option.incremental), "risk": _amount(option.risk),
            "penalty": _amount(option.penalty), "goodwill": _amount(option.goodwill),
            "expected_total": _amount(option.expected_total),
            "delta_vs_recommendation": _amount(option.expected_total - best.expected_total)}


def _sources(item_id: str, events: list[SupplyChangeEvent], inbox: dict[str, InboundMessage]) -> list[dict]:
    sources = []
    for mid in sorted({m for e in events if e.item_id == item_id for m in e.source_ids if m in inbox}):
        msg = inbox[mid]
        claims = [e for e in events if mid in e.source_ids]
        sources.append({
            "message_id": mid, "sender": msg.sender, "received_at": msg.received_at.isoformat(),
            "subject": msg.subject, "body": msg.body,
            "quotes": sorted({ev.quote for e in claims for ev in e.evidence if ev.message_id == mid}),
            "claims": [{"kind": e.kind.value, "po_line_id": e.po_line_id, "qty": e.new_qty,
                        "date": e.new_date.isoformat() if e.new_date else None, "verification": e.verification,
                        "confidence": e.confidence} for e in claims],
        })
    return sources


def _actions(store: MemoryStore, proposal: Proposal) -> list[dict]:
    return [{"id": a.id, "kind": a.params.kind.value, "text": describe_action(store, a.params),
             "tier": c.tier, "rules": c.rule_ids,
             "message": {"to": a.params.recipient_id, "subject": a.params.subject, "body": a.params.body}
             if isinstance(a.params, SendMessage) else None}
            for a, c in zip(proposal.actions, proposal.classifications)]


def record_update_detail(store: MemoryStore, policy: Policy, proposal: Proposal, events: list[SupplyChangeEvent],
                         inbox: dict[str, InboundMessage]) -> dict:
    """A records-only update: no shortage, so no alternatives or mandate."""
    return {"summary": "Update purchase-order records to match the supplier", "shortage": None,
            "actions": _actions(store, proposal), "why_these_signers": proposal.bundle.rule_ids,
            "alternatives": [], "excluded": [], "mandate": None, "commitment": _amount(proposal.commitment_base),
            "sources": _sources(proposal.item_id, events, inbox), "evidence": proposal.evidence, "assumptions": [],
            "base_currency": policy.company.base_currency, "offer_prices": {}}


def request_detail(store: MemoryStore, policy: Policy, analysis: Analysis, proposal: Proposal,
                   events: list[SupplyChangeEvent], inbox: dict[str, InboundMessage]) -> dict:
    option, s = proposal.option, proposal.option.shortage
    base = policy.company.base_currency
    line = store.get("sales_order_lines", s.so_line_id) if s.so_line_id else None
    customer = store.get("customers", store.get("sales_orders", line.sales_order_id).customer_id) if line else None
    sources = _sources(s.item_id, events, inbox)

    assumptions = [f"Exchange rates are fixed pack values ({', '.join(f'{c} {r}' for c, r in policy.fx_rates_to_base.items() if c != base)} to {base})"]
    if customer:
        tier = customer.priority_tier
        rate = policy.options.scoring.goodwill_cost_per_late_day_by_tier.get(tier, 0)
        assumptions.append(f"Goodwill cost {_amount(Decimal(rate))} {base} per late day (tier {tier}) is a pack estimate")
        assumptions.append(f"Penalty enforcement for {customer.id} assumed {customer.penalty_enforcement:.2f}")
    if option.buys:
        assumptions.append("On-time rates are each supplier's delivery history; risk assumes they hold")

    m = analysis.mandate
    return {
        "summary": describe_option(option),
        "shortage": {"item_id": s.item_id, "short_qty": s.short_qty, "need_by": s.need_by.isoformat(),
                     "work_order_id": s.work_order_id, "so_line_id": s.so_line_id,
                     "customer": customer.name if customer else None, "line_at_risk": s.line_at_risk,
                     "penalty": _amount(s.penalty)},
        "actions": _actions(store, proposal),
        "why_these_signers": proposal.bundle.rule_ids,
        "alternatives": [_costs(o, option) for o in analysis.options],
        "excluded": [{"option": x.option, "reason": x.reason} for x in analysis.excluded],
        "mandate": {"max_unit_price": _amount(m.max_unit_price), "currency": base, "next_best": m.next_best_option_id,
                    "offer_unit_price": _amount(option.buys[0].unit_base), "within": m.offer_within_mandate}
        if m else None,
        "commitment": _amount(proposal.commitment_base),
        "sources": sources,
        "evidence": proposal.evidence,
        "assumptions": assumptions,
        "base_currency": base,
        "offer_prices": {b.offer_id: _amount(to_base(store.get("offers", b.offer_id).unit_price, policy))
                         for b in option.buys},
    }
