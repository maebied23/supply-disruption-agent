"""Shortages, candidate options, expected costs, ranking and mandates.

Every option is evaluated the same way: change the supply (add bought buckets, cancel
displaced late PO quantity), re-peg, and price the resulting plan:

  expected_total = incremental + risk + penalties + goodwill

incremental = sum over bought (or substituted) units of (unit cost - price of what it
replaces): units that replace cancelled late supply are compared with that supply's price,
and any surplus beyond it (a supplier minimum, say) with the item's standard price (its
primary contract price), at which surplus inventory is valued.

Candidate options for the most urgent shortage (each enabled in policy.options.enabled):
  accept_delay      wait for the late supply (always listed: it bounds every mandate)
  buy_from_backup   one backup offer, or the cheapest two-supplier split
  reallocate_stock  serve the short work order before another work order of the same item
                    (the donor), which then waits; never from a protected customer tier
  substitute        cover the gap with free stock of an approved substitute
  partial_delivery  deliver the on-time units now and the rest later, where the order allows

Limitations (V1): one option set per shortage, evaluated independently; one
lever per option (no reallocation + buy combinations); pegging is a heuristic, not a MIP.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import combinations

from sdra.core.costing import LineOutcome, line_outcomes, risk_cost
from sdra.core.money import cents, floor_cents, to_base
from sdra.core.pegging import Bucket, Demand, demands, peg, po_line_buckets, stock_buckets
from sdra.policy import Policy
from sdra.schema import SupplyChangeEvent
from sdra.store import RecordStore


@dataclass(frozen=True)
class Shortage:
    so_line_id: str | None
    work_order_id: str
    item_id: str
    short_qty: int
    need_by: date
    available_by: date | None
    line_at_risk: int
    penalty: Decimal
    demand: Demand = field(repr=False)


@dataclass(frozen=True)
class Buy:
    offer_id: str
    supplier_id: str
    qty: int
    unit_base: Decimal
    arrival: date


@dataclass(frozen=True)
class Substitute:
    substitution_id: str
    item_id: str  # the substitute item
    qty: int  # original units replaced
    unit_base: Decimal  # substitute cost per original unit (ratio applied)


@dataclass
class Option:
    id: str
    kind: str  # "wait" | "buy" | "reallocate" | "substitute" | "partial"
    shortage: Shortage
    buys: list[Buy]
    cancels: dict[str, int]  # po_line_id -> qty cancelled
    baseline_unit: Decimal | None
    incremental: Decimal
    risk: Decimal
    penalty: Decimal
    goodwill: Decimal
    outcomes: dict[str, LineOutcome] = field(default_factory=dict, repr=False)  # per sales line, after the option
    donor_work_order_id: str | None = None  # reallocate
    substitute: Substitute | None = None  # substitute
    split_line_id: str | None = None  # partial: the sales-order line delivered in two parts

    @property
    def suppliers(self) -> tuple[str, ...]:
        return tuple(sorted(b.supplier_id for b in self.buys))

    @property
    def expected_total(self) -> Decimal:
        return self.incremental + self.risk + self.penalty + self.goodwill


@dataclass(frozen=True)
class Excluded:
    option: str
    reason: str


@dataclass(frozen=True)
class Mandate:
    option_id: str
    item_id: str
    min_qty: int
    max_qty: int
    latest_date: date
    max_unit_price: Decimal  # base currency
    next_best_option_id: str
    offer_within_mandate: bool


@dataclass
class Analysis:
    shortages: list[Shortage]
    options: list[Option]  # ranked, best first
    excluded: list[Excluded]
    mandate: Mandate | None

    @property
    def recommendation(self) -> Option | None:
        return self.options[0] if self.options else None

    @property
    def blocked(self) -> bool:
        """Best option is outside its mandate: escalate (policy on_best_option_blocked)."""
        return self.mandate is not None and not self.mandate.offer_within_mandate


class Planner:
    def __init__(self, store: RecordStore, policy: Policy, events: list[SupplyChangeEvent], now: datetime,
                 order=None):
        self.store, self.policy, self.now, self.order = store, policy, now, order
        self.today = now.date()
        self.base_buckets = stock_buckets(store, self.today) + po_line_buckets(store, policy, events)
        self.demands = demands(store)
        self.base = peg(self.base_buckets, self.demands, order=order)

    # --- shortages ------------------------------------------------------------------

    def shortages(self) -> list[Shortage]:
        out = []
        outcomes = line_outcomes(self.store, self.policy, self.base)
        for d in self.demands:
            short = d.qty - self.base.on_time_qty(d)
            if short <= 0:
                continue
            line = self.store.get("sales_order_lines", d.so_line_id) if d.so_line_id else None
            out.append(Shortage(
                so_line_id=d.so_line_id, work_order_id=d.work_order_id, item_id=d.item_id, short_qty=short,
                need_by=d.need_by, available_by=self.base.available_by(d),
                line_at_risk=(short if line is None or line.partial_delivery_allowed else line.qty - line.qty_shipped),
                penalty=outcomes[d.so_line_id].contractual_penalty if d.so_line_id else Decimal(0), demand=d,
            ))
        return sorted(out, key=lambda s: (s.need_by, s.demand.tier, s.work_order_id, s.item_id))

    # --- evaluation -------------------------------------------------------------------

    def _late_po_allocs(self, s: Shortage) -> list[tuple[Bucket, int]]:
        return [(a.bucket, a.qty) for a in self.base.for_demand(s.demand)
                if a.bucket.po_line_id and a.bucket.available > s.need_by]

    def _standard_unit(self, item_id: str) -> Decimal:
        """The item's standard cost: its primary supplier's contract price."""
        primary = [si for si in self.store.rows("supplier_items") if si.item_id == item_id and si.role == "primary"]
        return to_base(primary[0].contract_price, self.policy) if primary and primary[0].contract_price else Decimal(0)

    def _line_unit(self, po_line_id: str) -> Decimal:
        return to_base(self.store.get("purchase_order_lines", po_line_id).unit_price, self.policy)

    def evaluate(self, s: Shortage, buys: list[Buy] = (), *, donor: str | None = None,
                 substitute: Substitute | None = None, split: bool = False) -> Option:
        buys = list(buys)
        demand_list = self.demands
        if substitute:  # part of the short demand is met by the substitute item instead
            sub = self.store.get("substitutions", substitute.substitution_id)
            demand_list = [d for d in self.demands if d != s.demand] + [
                Demand(s.item_id, s.demand.qty - substitute.qty, s.need_by, s.work_order_id, s.so_line_id, s.demand.tier),
                Demand(sub.substitute_item_id, math.ceil(substitute.qty * sub.ratio), s.need_by, s.work_order_id,
                       s.so_line_id, s.demand.tier)]
            demand_list = [d for d in demand_list if d.qty]
        buckets = list(self.base_buckets)
        cancels: dict[str, int] = {}
        covered = sum(b.qty for b in buys) + (substitute.qty if substitute else 0)
        late = self._late_po_allocs(s)
        if covered and self.policy.options.late_po_quantities_cancellable and covered >= sum(q for _, q in late):
            for bucket, _ in late:
                if bucket.available <= self.store.get("purchase_order_lines", bucket.po_line_id).expected_date:
                    continue  # arriving as agreed: not a late quantity, so not cancellable
                # cancel the whole late bucket except what other demands still use
                others = sum(a.qty for a in self.base.allocs if a.bucket == bucket and a.demand != s.demand)
                cancel = bucket.qty - others
                if cancel > 0:
                    buckets.remove(bucket)
                    if others:
                        buckets.append(Bucket(**{**bucket.__dict__, "qty": others}))
                    cancels[bucket.po_line_id] = cancels.get(bucket.po_line_id, 0) + cancel
        new = [Bucket(item_id=s.item_id, qty=b.qty, available=b.arrival, source=f"new:{b.offer_id}",
                      supplier_id=b.supplier_id, unit_base=b.unit_base) for b in buys]
        last = frozenset({donor}) if donor else frozenset()
        split_lines = frozenset({s.so_line_id}) if split and s.so_line_id else frozenset()
        outcomes = line_outcomes(self.store, self.policy, peg(buckets + new, demand_list, last, self.order),
                                 split_lines=split_lines)
        # A line depends on a new supplier if it is on time with the option and late without
        # that supplier's delivery (units are interchangeable, so pegging alone can't say).
        fed: dict[str, set[str]] = {}
        for supplier in sorted({b.supplier_id for b in buys}):
            rest = [n for n in new if n.supplier_id != supplier]
            without = line_outcomes(self.store, self.policy, peg(buckets + rest, demand_list, last, self.order),
                                    split_lines=split_lines)
            for line_id, o in outcomes.items():
                if not o.late_days and without[line_id].late_days:
                    fed.setdefault(line_id, set()).add(supplier)
        outcomes = {k: replace(o, fed_by_suppliers=frozenset(fed.get(k, ()))) for k, o in outcomes.items()}
        # Units that replace cancelled late supply are compared with its price; any surplus
        # beyond that is inventory, compared with the item's standard (primary contract) price.
        baseline = None
        extra = Decimal(0)
        if covered:
            standard = self._standard_unit(s.item_id)
            displaced = min(covered, sum(cancels.values()))
            late_price = (sum(self._line_unit(k) * q for k, q in cancels.items()) / sum(cancels.values())
                          if cancels else standard)
            spend = sum((b.unit_base * b.qty for b in buys), Decimal(0)) + (
                substitute.unit_base * substitute.qty if substitute else Decimal(0))
            extra = spend - late_price * displaced - standard * (covered - displaced)
            baseline = (late_price * displaced + standard * (covered - displaced)) / covered  # per unit covered
        if buys:
            kind, label = "buy", "-".join(sorted(b.supplier_id for b in buys))
        elif donor:
            kind, label = "reallocate", f"REALLOC-{donor}"
        elif substitute:
            kind, label = "substitute", f"SUB-{substitute.item_id}"
        elif split:
            kind, label = "partial", "PARTIAL"
        else:
            kind, label = "wait", "WAIT"
        return Option(
            id=f"OPT-{label}", kind=kind, shortage=s, buys=buys, cancels=cancels, baseline_unit=baseline,
            incremental=cents(extra),
            risk=cents(risk_cost(self.store, self.policy, outcomes)),
            penalty=cents(sum((o.penalty for o in outcomes.values()), Decimal(0))),
            goodwill=cents(sum((o.goodwill for o in outcomes.values()), Decimal(0))),
            outcomes=outcomes, donor_work_order_id=donor, substitute=substitute,
            split_line_id=s.so_line_id if split else None,
        )

    # --- candidates ---------------------------------------------------------------------

    def _offers(self, s: Shortage, excluded_suppliers: set[str], excluded: list[Excluded]):
        out = []
        for o in self.store.rows("offers"):
            if o.item_id != s.item_id:
                continue
            label = f"Buy from {o.supplier_id}"
            supplier = self.store.get("suppliers", o.supplier_id)
            source = self.store.get("supplier_items", f"{o.supplier_id}:{o.item_id}")
            if o.supplier_id in excluded_suppliers:
                excluded.append(Excluded(label, f"{o.supplier_id} excluded by an approver"))
            elif o.valid_until < self.now:
                excluded.append(Excluded(label, f"{o.id} expired {o.valid_until.date().isoformat()}"))
            elif not (supplier.approved and source):
                excluded.append(Excluded(label, f"{o.supplier_id} is not an approved source for {o.item_id}"))
            else:
                min_qty = max(o.min_qty, source.min_order_qty)
                out.append((o, min_qty))
        return out

    def _customer(self, so_line_id: str | None):
        if not so_line_id:
            return None
        line = self.store.get("sales_order_lines", so_line_id)
        return self.store.get("customers", self.store.get("sales_orders", line.sales_order_id).customer_id)

    def _donors(self, s: Shortage, excluded: list[Excluded]) -> list[str]:
        """Work orders holding supply of the short item that arrives in time for the shortage."""
        protect = set(self.policy.options.reallocation.protect_priority_tiers)
        out = []
        for d in sorted({a.demand for a in self.base.allocs if a.bucket.item_id == s.item_id
                         and a.bucket.available <= s.need_by and a.demand.work_order_id != s.work_order_id},
                        key=lambda d: d.work_order_id):
            customer = self._customer(d.so_line_id)
            if customer and customer.priority_tier in protect:
                excluded.append(Excluded(f"Reallocate {s.item_id} from {d.work_order_id}",
                                         f"{d.work_order_id} serves priority tier {customer.priority_tier} "
                                         f"(protect_priority_tiers)"))
            else:
                out.append(d.work_order_id)
        return out

    def _substitutes(self, s: Shortage, excluded: list[Excluded]) -> list[Substitute]:
        customer = self._customer(s.so_line_id)
        wo = self.store.get("work_orders", s.work_order_id)
        out = []
        for sub in self.store.rows("substitutions"):
            if sub.item_id != s.item_id:
                continue
            label = f"Substitute {sub.substitute_item_id} on {wo.id}"
            if sub.customer_ids and (customer is None or customer.id not in sub.customer_ids):
                excluded.append(Excluded(label, f"{sub.id} is approved for {', '.join(sub.customer_ids)} only"))
                continue
            if sub.parent_item_ids and wo.item_id not in sub.parent_item_ids:
                excluded.append(Excluded(label, f"{sub.id} does not apply to {wo.item_id}"))
                continue
            if (sub.valid_from and s.need_by < sub.valid_from) or (sub.valid_to and s.need_by > sub.valid_to):
                excluded.append(Excluded(label, f"{sub.id} is not valid on {s.need_by.isoformat()}"))
                continue
            free = sum(b.qty - self.base.allocated_from(b) for b in self.base_buckets
                       if b.item_id == sub.substitute_item_id and b.available <= s.need_by)
            qty = min(s.short_qty, int(free / sub.ratio))
            if qty <= 0:
                excluded.append(Excluded(label, f"no free {sub.substitute_item_id} by {s.need_by.isoformat()}"))
                continue
            primary = [si for si in self.store.rows("supplier_items")
                       if si.item_id == sub.substitute_item_id and si.role == "primary" and si.contract_price]
            unit = to_base(primary[0].contract_price, self.policy) if primary else Decimal(0)
            out.append(Substitute(sub.id, sub.substitute_item_id, qty, unit * sub.ratio))
        return out

    def options(self, excluded_suppliers: set[str] = frozenset(), excluded_option_ids: set[str] = frozenset(),
                items: set[str] | None = None) -> Analysis:
        """Options for the most urgent shortage (of `items`, when given: an incident plans for
        the items its supplier news is about)."""
        shortages = [s for s in self.shortages() if items is None or s.item_id in items]
        if not shortages:
            return Analysis([], [], [], None)
        s = shortages[0]  # V1: plan for the most urgent shortage
        enabled = set(self.policy.options.enabled)
        excluded: list[Excluded] = []
        options = [self.evaluate(s)]  # accept the delay: always listed, it bounds every mandate

        if "buy_from_backup" in enabled:
            offers = self._offers(s, set(excluded_suppliers), excluded)
            unit = {o.id: to_base(o.unit_price, self.policy) for o, _ in offers}
            arrival = {o.id: self.today + timedelta(days=o.lead_time_days) for o, _ in offers}

            def buy(o, q):
                return Buy(o.id, o.supplier_id, q, unit[o.id], arrival[o.id])

            for o, min_qty in offers:
                q = max(s.short_qty, min_qty)
                if q <= o.max_qty:
                    options.append(self.evaluate(s, [buy(o, q)]))
            # Two-supplier split: the dearer supplier takes its minimum, the cheaper the rest.
            for (a, amin), (b, bmin) in combinations(offers, 2):
                (dear, dmin), (cheap, cmin) = sorted([(a, amin), (b, bmin)], key=lambda x: (-unit[x[0].id], x[0].id))
                if dmin and cmin and dmin <= dear.max_qty and cmin <= s.short_qty - dmin <= cheap.max_qty:
                    options.append(self.evaluate(s, [buy(dear, dmin), buy(cheap, s.short_qty - dmin)]))

        if "reallocate_stock" in enabled:
            wait = options[0]
            for donor in self._donors(s, excluded):
                option = self.evaluate(s, donor=donor)
                if not s.so_line_id or option.outcomes[s.so_line_id].late_days < wait.outcomes[s.so_line_id].late_days:
                    options.append(option)  # only if it actually brings the short order forward

        line = self.store.get("sales_order_lines", s.so_line_id) if s.so_line_id else None
        if "partial_delivery" in enabled and line:
            if not line.partial_delivery_allowed:
                excluded.append(Excluded("Partial delivery", f"{line.id} partial_delivery_allowed is false"))
            else:
                option = self.evaluate(s, split=True)
                out = option.outcomes[line.id]
                wo = self.store.get("work_orders", s.work_order_id)
                if not 0 < out.late_qty < wo.qty:
                    excluded.append(Excluded("Partial delivery", f"no units of {wo.id} can be built on time"))
                elif out.completion is None:
                    excluded.append(Excluded("Partial delivery", f"no date for the remaining {out.late_qty} units"))
                else:
                    options.append(option)

        if "substitute" in enabled:
            options += [self.evaluate(s, substitute=x) for x in self._substitutes(s, excluded)]

        options = [o for o in options if o.id not in excluded_option_ids]
        ranked = sorted(options, key=lambda o: (o.expected_total, len(o.buys), o.suppliers, o.id))
        return Analysis(shortages, ranked, excluded, self.mandate(ranked))

    # --- mandate ----------------------------------------------------------------------------

    def mandate(self, ranked: list[Option]) -> Mandate | None:
        """For the best option, if it is a single-supplier buy:
        headroom = E[total] of the next-best option - non-price cost of this option
        max incremental = headroom - margin x |headroom|   (always stricter than headroom)
        max unit price = baseline unit price + max incremental / qty, rounded down."""
        if len(ranked) < 2 or ranked[0].kind != "buy" or len(ranked[0].buys) != 1:
            return None
        best, next_best = ranked[0], ranked[1]
        (b,) = best.buys
        margin = self.policy.mandate.safety_margin_pct / 100
        headroom = next_best.expected_total - (best.expected_total - best.incremental)
        ceiling = headroom - margin * abs(headroom)
        max_unit = best.baseline_unit + floor_cents(ceiling / b.qty)
        s = best.shortage
        return Mandate(
            option_id=best.id, item_id=s.item_id, min_qty=b.qty, max_qty=b.qty,
            latest_date=max(s.need_by, b.arrival), max_unit_price=max_unit,
            next_best_option_id=next_best.id, offer_within_mandate=b.unit_base <= max_unit,
        )
