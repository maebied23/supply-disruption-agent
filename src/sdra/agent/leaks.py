"""Deterministic outbound leak check (policy `communication.never_disclose`).

Runs on every outbound message, template or not, just before it is sent. It looks for:
  amounts   penalties, option costs, the mandate ceiling, other suppliers' prices
            (as written, with or without thousands separators, and rounded to whole units)
  names     customers in supplier messages, suppliers in customer messages, and other
            suppliers in supplier messages
Record ids and ISO dates are removed before numbers are read, so "PO-4471" or "2026-11-10"
never match an amount, and in a template message the quantities it filled in from records
are skipped (free text gets no such allowance). Amounts under 100 are ignored (too many false alarms from
quantities and dates). A false alarm refuses the message and sends it to a person.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sdra.core.costing import penalty_terms
from sdra.core.money import to_base
from sdra.core.options import Analysis
from sdra.policy import Policy
from sdra.schema import ActionKind, FixedPenalty, Money, PerDayPenalty, SendMessage
from sdra.store import RecordStore

MIN_AMOUNT = Decimal(100)
_IDS = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
_DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBERS = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    text = _DATES.sub(" ", _IDS.sub(" ", text))
    out = set()
    for raw in _NUMBERS.findall(text):
        try:
            out.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return out


def _variants(value: Decimal) -> set[Decimal]:
    value = abs(value)
    return {value, value.quantize(Decimal(1))} if value >= MIN_AMOUNT else set()


def _in_currencies(base: Decimal, policy: Policy) -> set[Decimal]:
    """A base-currency amount as it would be written in each known currency."""
    out = set()
    for rate in policy.fx_rates_to_base.values():
        out |= _variants((base / Decimal(rate)).quantize(Decimal("0.01")))
    return out


def secret_amounts(store: RecordStore, policy: Policy, analysis: Analysis | None = None) -> dict[str, list[str]]:
    """Category -> amounts (as strings) that must not appear in any outbound message."""
    rules = set(policy.communication.never_disclose)
    out: dict[str, set[Decimal]] = {}
    if "penalty_terms" in rules:
        p = out.setdefault("penalty_terms", set())
        for line in store.rows("sales_order_lines"):
            terms = penalty_terms(store, policy, line)
            if isinstance(terms, FixedPenalty):
                p |= _variants(terms.amount.amount) | _variants(to_base(terms.amount, policy))
            elif isinstance(terms, PerDayPenalty):
                p |= _variants(terms.amount_per_day.amount) | _variants(to_base(terms.amount_per_day, policy))
        for s in analysis.shortages if analysis else []:
            p |= _variants(s.penalty)
    if analysis and "internal_cost" in rules:
        c = out.setdefault("internal_cost", set())
        for o in analysis.options:
            for v in (o.incremental, o.risk, o.penalty, o.goodwill, o.expected_total):
                c |= _variants(v)
    if analysis and analysis.mandate and "mandate" in rules:
        out["mandate"] = _in_currencies(analysis.mandate.max_unit_price, policy)
    return {k: sorted(str(v) for v in vs) for k, vs in out.items()}


def _prices_by_supplier(store: RecordStore, policy: Policy) -> dict[str, set[Decimal]]:
    out: dict[str, set[Decimal]] = {}

    def add(supplier_id: str, m: Money) -> None:
        out.setdefault(supplier_id, set()).update(_variants(m.amount) | _variants(to_base(m, policy)))

    for o in store.rows("offers"):
        add(o.supplier_id, o.unit_price)
    for si in store.rows("supplier_items"):
        if si.contract_price:
            add(si.supplier_id, si.contract_price)
    pos = {p.id: p.supplier_id for p in store.rows("purchase_orders")}
    for line in store.rows("purchase_order_lines"):
        add(pos[line.purchase_order_id], line.unit_price)
    return out


def _mentions(text: str, needles: dict[str, str]) -> list[str]:
    low = text.casefold()
    return sorted({label for needle, label in needles.items()
                   if re.search(rf"(?<![\w-]){re.escape(needle.casefold())}(?![\w-])", low)})


def check_message(store: RecordStore, policy: Policy, msg: SendMessage, secrets: dict[str, list[str]]) -> list[str]:
    """Reasons the message must not be sent; empty if it is clean."""
    rules = set(policy.communication.never_disclose)
    text = f"{msg.subject}\n{msg.body}"
    numbers = _numbers(text)
    if msg.template_id:  # record quantities a template filled in are quantities, not amounts
        numbers -= {Decimal(q) for q in msg.record_quantities}
    to_supplier = msg.kind == ActionKind.SEND_SUPPLIER_MESSAGE
    prices = _prices_by_supplier(store, policy)
    own = prices.get(msg.recipient_id, set()) if to_supplier else set()  # the recipient's own prices are fine

    found = []
    for category, amounts in secrets.items():
        if category in rules:
            hits = (numbers & {Decimal(a) for a in amounts}) - own
            found += [f"{category}: {h}" for h in sorted(hits)]
    if "other_supplier_offers" in rules:
        for supplier_id, values in prices.items():
            if supplier_id != msg.recipient_id:
                found += [f"other_supplier_offers: {h} ({supplier_id})" for h in sorted((numbers & values) - own)]

    names: dict[str, str] = {}
    if to_supplier and "customer_identity_to_suppliers" in rules:
        for c in store.rows("customers"):
            names |= {c.id: f"customer_identity_to_suppliers: {c.id}", c.name: f"customer_identity_to_suppliers: {c.id}"}
        for so in store.rows("sales_orders"):
            names[so.id] = f"customer_identity_to_suppliers: {so.customer_id}"
    if to_supplier and "other_supplier_offers" in rules:
        for s in store.rows("suppliers"):
            if s.id != msg.recipient_id:
                names |= {s.id: f"other_supplier_offers: {s.id}", s.name: f"other_supplier_offers: {s.id}"}
    if not to_supplier and "supplier_identity_to_customers" in rules:
        for s in store.rows("suppliers"):
            names |= {s.id: f"supplier_identity_to_customers: {s.id}", s.name: f"supplier_identity_to_customers: {s.id}"}
        for c in store.rows("customers"):
            if c.id != msg.recipient_id:
                names |= {c.id: f"customer_identity: {c.id}", c.name: f"customer_identity: {c.id}"}
    return sorted(set(found)) + _mentions(text, names)
