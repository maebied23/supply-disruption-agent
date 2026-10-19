"""Lateness, penalties, goodwill and risk for a pegging result. All amounts in base currency."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sdra.core.money import to_base
from sdra.core.pegging import PegResult
from sdra.policy import Policy
from sdra.schema import FixedPenalty, PctOfValuePenalty, PerDayPenalty, SalesOrderLine
from sdra.store import RecordStore


@dataclass(frozen=True)
class LineOutcome:
    so_line_id: str
    completion: date | None  # None = never completes in the plan
    late_days: int
    contractual_penalty: Decimal
    penalty: Decimal  # expected: contractual x customer.penalty_enforcement
    goodwill: Decimal
    fed_by_suppliers: frozenset[str]  # new suppliers the line depends on (set by the option evaluation)
    late_qty: int = 0  # units completing late; less than the work order when the delivery is split


def penalty_terms(store: RecordStore, policy: Policy, line: SalesOrderLine):
    if line.penalty:
        return line.penalty
    customer = store.get("customers", store.get("sales_orders", line.sales_order_id).customer_id)
    return customer.default_penalty or policy.default_penalty


def penalty_amount(terms, policy: Policy, line: SalesOrderLine, late_days: int, late_qty: int) -> Decimal:
    days = late_days - terms.grace_days
    if days <= 0:
        return Decimal(0)
    match terms:
        case FixedPenalty():
            return to_base(terms.amount, policy)
        case PerDayPenalty():
            amount = to_base(terms.amount_per_day, policy) * days
            return min(amount, to_base(terms.cap, policy)) if terms.cap else amount
        case PctOfValuePenalty():
            unit = to_base(line.unit_price, policy)
            amount = terms.pct_per_week / 100 * math.ceil(days / 7) * unit * late_qty
            if terms.cap_pct_of_value is not None:
                amount = min(amount, terms.cap_pct_of_value / 100 * unit * line.qty)
            return amount
    raise TypeError(terms)


def line_outcomes(store: RecordStore, policy: Policy, result: PegResult, split_lines: frozenset[str] = frozenset()):
    """Per sales-order line: completion, lateness and cost. A line in `split_lines` is delivered
    in two parts (partial delivery): the units whose components are all on time are built on
    schedule, the rest when their components arrive. Only the late units count for a
    percentage-of-value penalty, and goodwill is scaled by the late share of the work order."""
    goodwill_rate = policy.options.scoring.goodwill_cost_per_late_day_by_tier
    unfilled = policy.options.scoring.unfilled_late_days
    outcomes: dict[str, LineOutcome] = {}
    for wo in store.rows("work_orders"):
        if not wo.sales_order_line_id or wo.status not in ("planned", "released"):
            continue
        line = store.get("sales_order_lines", wo.sales_order_line_id)
        ds = [d for d in result.demands if d.work_order_id == wo.id]
        ready = [result.available_by(d) for d in ds]
        if any(r is None for r in ready):
            completion, late_days = None, unfilled
        else:
            start = max([wo.start_date, *ready])
            assembly = store.get("items", wo.item_id).assembly_lead_days
            completion = start + timedelta(days=assembly)
            late_days = max((completion - line.promised_date).days, 0)
        late_qty = wo.qty if late_days else 0
        if late_days and line.id in split_lines:
            late_qty = wo.qty - on_time_units(result, wo.qty, ds)
            if not late_qty:
                completion, late_days = wo.due_date, 0
        terms = penalty_terms(store, policy, line)
        customer = store.get("customers", store.get("sales_orders", line.sales_order_id).customer_id)
        tier = customer.priority_tier
        contractual = penalty_amount(terms, policy, line, late_days, late_qty)
        share = Decimal(late_qty) / wo.qty if line.id in split_lines and wo.qty else Decimal(1)
        outcomes[line.id] = LineOutcome(
            so_line_id=line.id,
            completion=completion,
            late_days=late_days,
            contractual_penalty=contractual,
            penalty=contractual * customer.penalty_enforcement,
            goodwill=Decimal(goodwill_rate.get(tier, 0)) * max(late_days - terms.grace_days, 0) * share,
            fed_by_suppliers=frozenset(),
            late_qty=late_qty,
        )
    return outcomes


def on_time_units(result: PegResult, wo_qty: int, ds) -> int:
    """Finished units whose components are all covered by the work order's need date."""
    return min((result.on_time_qty(d) * wo_qty // d.qty for d in ds if d.qty), default=wo_qty)


def risk_cost(store: RecordStore, policy: Policy, outcomes: dict[str, LineOutcome]) -> Decimal:
    """Expected penalty from new supply slipping: for each line that is on time only thanks to new
    supply, P(any supplier it depends on is late) x the line's penalty for one day past grace."""
    total = Decimal(0)
    for o in outcomes.values():
        if o.late_days or not o.fed_by_suppliers:
            continue
        p_on_time = Decimal(1)
        for s in sorted(o.fed_by_suppliers):
            p_on_time *= store.get("suppliers", s).on_time_rate
        line = store.get("sales_order_lines", o.so_line_id)
        terms = penalty_terms(store, policy, line)
        enforcement = store.get("customers", store.get("sales_orders", line.sales_order_id).customer_id).penalty_enforcement
        total += (1 - p_on_time) * penalty_amount(terms, policy, line, terms.grace_days + 1, line.qty) * enforcement
    return total
