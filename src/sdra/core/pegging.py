"""Time-phased supply and demand per component, and pegging between them.

Supply  = usable stock (available now) + open PO lines, adjusted by verified change events.
Demand  = work-order component requirements on the work order's start date.
Pegging = two passes per item:
  1. on time: demands in (need date, customer tier) order each take supply that arrives by
     their need date, latest-arriving first, so earlier supply stays free for others;
  2. late: what is still unmet takes the earliest remaining supply.
A reallocation option re-pegs with the donor work order served last in both passes.
This is a heuristic, not an optimiser (an allocation MIP is deferred until a case shows regret).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from sdra.core.money import to_base
from sdra.policy import Policy
from sdra.schema import ChangeKind, SupplyChangeEvent
from sdra.store import RecordStore


@dataclass(frozen=True)
class Bucket:
    item_id: str
    qty: int
    available: date
    source: str
    po_line_id: str | None = None
    supplier_id: str | None = None
    unit_base: Decimal | None = None


@dataclass(frozen=True)
class Demand:
    item_id: str
    qty: int
    need_by: date
    work_order_id: str
    so_line_id: str | None
    tier: int


@dataclass(frozen=True)
class Alloc:
    demand: Demand
    bucket: Bucket
    qty: int


@dataclass
class PegResult:
    allocs: list[Alloc]
    demands: list[Demand]

    def for_demand(self, d: Demand) -> list[Alloc]:
        return [a for a in self.allocs if a.demand == d]

    def on_time_qty(self, d: Demand) -> int:
        return sum(a.qty for a in self.for_demand(d) if a.bucket.available <= d.need_by)

    def unmet(self, d: Demand) -> int:
        return d.qty - sum(a.qty for a in self.for_demand(d))

    def available_by(self, d: Demand) -> date | None:
        """Date the demand is fully covered, or None if it never is."""
        allocs = self.for_demand(d)
        if self.unmet(d) or not allocs:
            return None if self.unmet(d) else d.need_by
        return max(a.bucket.available for a in allocs)

    def allocated_from(self, bucket: Bucket) -> int:
        return sum(a.qty for a in self.allocs if a.bucket == bucket)


def po_line_buckets(store: RecordStore, policy: Policy, events: Iterable[SupplyChangeEvent]) -> list[Bucket]:
    by_line: dict[str, list[SupplyChangeEvent]] = defaultdict(list)
    for e in events:
        if e.verification == "verified" and e.po_line_id:
            by_line[e.po_line_id].append(e)

    buckets = []
    for line in store.rows("purchase_order_lines"):
        open_qty = line.open_qty
        if not open_qty:
            continue
        po = store.get("purchase_orders", line.purchase_order_id)
        common = dict(item_id=line.item_id, po_line_id=line.id, supplier_id=po.supplier_id,
                      unit_base=to_base(line.unit_price, policy), source=line.id)
        evs = sorted(by_line.get(line.id, []), key=lambda e: e.observed_at)
        if any(e.kind == ChangeKind.CANCELLATION for e in evs):
            continue
        partials = [e for e in evs if e.kind == ChangeKind.PARTIAL_QUANTITY]
        delays = [e for e in evs if e.kind == ChangeKind.DELAY]
        on_time = partials[-1].new_qty if partials else None
        later: list[tuple[date, int]] = []
        for d in delays:
            rest = open_qty - (on_time or 0) - sum(q for _, q in later)
            later.append((d.new_date, d.new_qty if d.new_qty is not None else rest))
        if on_time is None:
            on_time = open_qty - sum(q for _, q in later)
        if on_time + sum(q for _, q in later) > open_qty or on_time < 0:
            raise ValueError(f"events on {line.id} claim more than the open quantity")
        # Any quantity no event accounts for is treated as unavailable (no guessed dates).
        if on_time:
            buckets.append(Bucket(qty=on_time, available=line.expected_date, **common))
        buckets += [Bucket(qty=q, available=when, **common) for when, q in later if q]
    return buckets


def stock_buckets(store: RecordStore, today: date) -> list[Bucket]:
    return [
        Bucket(item_id=s.item_id, qty=s.usable, available=today, source=f"stock:{s.item_id}@{s.location_id}")
        for s in store.rows("stock")
        if s.usable
    ]


def demands(store: RecordStore) -> list[Demand]:
    """Component requirements of open work orders, after any substitutions applied to them."""
    out = []
    bom = defaultdict(list)
    for b in store.rows("bom"):
        bom[b.parent_item_id].append(b)
    applied = defaultdict(list)
    for ws in store.rows("work_order_substitutions"):
        applied[ws.work_order_id].append((store.get("substitutions", ws.substitution_id), ws.qty))
    for wo in store.rows("work_orders"):
        if wo.status not in ("planned", "released"):
            continue
        tier = 99
        if wo.sales_order_line_id:
            line = store.get("sales_order_lines", wo.sales_order_line_id)
            tier = store.get("customers", store.get("sales_orders", line.sales_order_id).customer_id).priority_tier
        for b in bom[wo.item_id]:
            qty = math.ceil(wo.qty * b.qty_per)
            for sub, q in applied[wo.id]:
                if sub.item_id == b.component_item_id:
                    qty -= q
                    out.append(Demand(sub.substitute_item_id, math.ceil(q * sub.ratio), wo.start_date, wo.id,
                                      wo.sales_order_line_id, tier))
            if qty > 0:
                out.append(Demand(b.component_item_id, qty, wo.start_date, wo.id, wo.sales_order_line_id, tier))
    return out


def peg(buckets: list[Bucket], demand_list: list[Demand], last: frozenset[str] = frozenset(),
        order=None) -> PegResult:
    """`last`: work orders served after every other demand (a reallocation away from them).
    `order`: a different priority key for demands (used by the first-come-first-served baseline)."""
    allocs: list[Alloc] = []
    for item in sorted({d.item_id for d in demand_list}):
        bs = sorted((b for b in buckets if b.item_id == item), key=lambda b: (b.available, b.source))
        left = [b.qty for b in bs]
        def priority(d: Demand) -> tuple:
            return (d.work_order_id in last, *(order(d) if order else (d.need_by, d.tier, d.work_order_id)))

        ds = sorted((d for d in demand_list if d.item_id == item), key=priority)
        need = {d: d.qty for d in ds}

        def take(d: Demand, i: int, left=left, need=need, bs=bs) -> None:  # bound to this item's lists
            q = min(left[i], need[d])
            if q:
                left[i] -= q
                need[d] -= q
                allocs.append(Alloc(d, bs[i], q))

        for d in ds:  # pass 1: on time, latest-arriving first
            for i in reversed(range(len(bs))):
                if bs[i].available <= d.need_by:
                    take(d, i)
        for d in ds:  # pass 2: late, earliest first
            for i in range(len(bs)):
                take(d, i)
    return PegResult(allocs, demand_list)
