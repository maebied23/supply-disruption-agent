"""Load and validate a company pack: policy.yaml + seed.yaml.

Every seed row is validated against the canonical schema, ids must be unique per
table, and every reference must resolve. A pack that fails here never reaches the agent.
"""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from sdra.policy import Policy, load_policy
from sdra.schema import (
    Allocation,
    Approver,
    BOMLine,
    Customer,
    Item,
    Location,
    Model,
    PurchaseOrder,
    PurchaseOrderLine,
    SalesOrder,
    SalesOrderLine,
    StockPosition,
    Substitution,
    Supplier,
    SupplierItem,
    SupplierOffer,
    WorkOrder,
    WorkOrderSubstitution,
)


def _unique(name: str, ids: list[str]) -> set[str]:
    seen = set(ids)
    if len(seen) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate {name} ids: {dupes}")
    return seen


def _resolve(what: str, refs: list[str], known: set[str]) -> None:
    missing = sorted(set(refs) - known)
    if missing:
        raise ValueError(f"unresolved {what}: {missing}")


class Seed(Model):
    locations: list[Location] = Field(min_length=1)
    items: list[Item] = Field(min_length=1)
    bom: list[BOMLine] = Field(default_factory=list)
    substitutions: list[Substitution] = Field(default_factory=list)
    stock: list[StockPosition] = Field(default_factory=list)
    allocations: list[Allocation] = Field(default_factory=list)
    customers: list[Customer] = Field(default_factory=list)
    sales_orders: list[SalesOrder] = Field(default_factory=list)
    sales_order_lines: list[SalesOrderLine] = Field(default_factory=list)
    work_orders: list[WorkOrder] = Field(default_factory=list)
    work_order_substitutions: list[WorkOrderSubstitution] = Field(default_factory=list)
    suppliers: list[Supplier] = Field(default_factory=list)
    supplier_items: list[SupplierItem] = Field(default_factory=list)
    purchase_orders: list[PurchaseOrder] = Field(default_factory=list)
    purchase_order_lines: list[PurchaseOrderLine] = Field(default_factory=list)
    offers: list[SupplierOffer] = Field(default_factory=list)
    approvers: list[Approver] = Field(default_factory=list)

    @model_validator(mode="after")
    def _referential_integrity(self) -> Seed:
        locs = _unique("location", [x.id for x in self.locations])
        items = _unique("item", [x.id for x in self.items])
        custs = _unique("customer", [x.id for x in self.customers])
        sos = _unique("sales order", [x.id for x in self.sales_orders])
        sols = _unique("sales order line", [x.id for x in self.sales_order_lines])
        sups = _unique("supplier", [x.id for x in self.suppliers])
        pos = _unique("purchase order", [x.id for x in self.purchase_orders])
        _unique("purchase order line", [x.id for x in self.purchase_order_lines])
        wos = _unique("work order", [x.id for x in self.work_orders])
        _unique("work order substitution", [x.id for x in self.work_order_substitutions])
        _resolve("substituted work orders", [x.work_order_id for x in self.work_order_substitutions], wos)
        _resolve("applied substitutions", [x.substitution_id for x in self.work_order_substitutions],
                 {s.id for s in self.substitutions})
        _unique("substitution", [x.id for x in self.substitutions])
        _unique("allocation", [x.id for x in self.allocations])
        _unique("offer", [x.id for x in self.offers])
        _unique("approver", [x.id for x in self.approvers])
        _unique("stock position", [f"{x.item_id}@{x.location_id}" for x in self.stock])

        _resolve("BOM items", [i for b in self.bom for i in (b.parent_item_id, b.component_item_id)], items)
        _resolve(
            "substitution refs",
            [i for s in self.substitutions for i in (s.item_id, s.substitute_item_id, *s.parent_item_ids)],
            items,
        )
        _resolve("substitution customers", [c for s in self.substitutions for c in s.customer_ids], custs)
        _resolve("stock items", [s.item_id for s in self.stock + self.allocations], items)
        _resolve("stock locations", [s.location_id for s in self.stock + self.allocations], locs)
        _resolve("allocation lines", [a.sales_order_line_id for a in self.allocations], sols)
        _resolve("work order lines", [w.sales_order_line_id for w in self.work_orders if w.sales_order_line_id], sols)
        _resolve("sales order customers", [s.customer_id for s in self.sales_orders], custs)
        _resolve("sales order line orders", [line.sales_order_id for line in self.sales_order_lines], sos)
        _resolve(
            "order/work-order items",
            [x.item_id for x in [*self.sales_order_lines, *self.work_orders, *self.purchase_order_lines]],
            items,
        )
        _resolve("supplier refs", [x.supplier_id for x in [*self.supplier_items, *self.purchase_orders, *self.offers]], sups)
        _resolve("supplier item/offer items", [x.item_id for x in [*self.supplier_items, *self.offers]], items)
        _resolve("purchase order line orders", [line.purchase_order_id for line in self.purchase_order_lines], pos)

        make = {i.id for i in self.items if i.sourcing == "make"}
        parents = {b.parent_item_id for b in self.bom}
        if make - parents:
            raise ValueError(f"make items without a BOM: {sorted(make - parents)}")
        return self


class Pack(Model):
    name: str
    policy: Policy
    seed: Seed

    @model_validator(mode="after")
    def _approver_roles_defined(self) -> Pack:
        unknown = {r for a in self.seed.approvers for r in a.roles} - set(self.policy.roles)
        if unknown:
            raise ValueError(f"approver roles not in policy: {sorted(unknown)}")
        currencies = {
            m.currency
            for m in [
                *(line.unit_price for line in self.seed.purchase_order_lines),
                *(line.unit_price for line in self.seed.sales_order_lines),
                *(o.unit_price for o in self.seed.offers),
            ]
        } | {s.currency for s in self.seed.suppliers}
        missing = currencies - set(self.policy.fx_rates_to_base)
        if missing:
            raise ValueError(f"no fx rate for: {sorted(missing)}")
        return self


def apply_overrides(raw: dict, overrides: list[dict]) -> dict:
    """Patch raw seed rows for one scenario. Each override names a table and either `add`s a
    row, or selects one row by `id` or by a `match` dict and `set`s fields (exactly one row
    must match). The patched seed is validated like any other."""
    raw = copy.deepcopy(raw)
    for o in overrides:
        if "add" in o:
            raw.setdefault(o["table"], []).append(o["add"])
            continue
        rows = raw[o["table"]]
        select = {"id": o["id"]} if "id" in o else o["match"]
        hits = [r for r in rows if all(r.get(k) == v for k, v in select.items())]
        if len(hits) != 1:
            raise ValueError(f"override {select} matched {len(hits)} rows in {o['table']}")
        hits[0].update(o["set"])
    return raw


def merge(base: dict, patch: dict) -> dict:
    """Deep-merge `patch` into a copy of `base` (dicts merge, anything else replaces)."""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_pack(directory: str | Path, overrides: list[dict] | None = None, policy_overrides: dict | None = None) -> Pack:
    """`overrides` patch seed rows; `policy_overrides` deep-merge into policy.yaml (scenario
    and sweep variants). Either way the result is validated like the original."""
    directory = Path(directory)
    with open(directory / "seed.yaml", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    seed = Seed.model_validate(apply_overrides(raw, overrides or []))
    if policy_overrides:
        with open(directory / "policy.yaml", encoding="utf-8") as f:
            policy = Policy.model_validate(merge(yaml.safe_load(f), policy_overrides))
    else:
        policy = load_policy(directory / "policy.yaml")
    return Pack(name=directory.name, policy=policy, seed=seed)
