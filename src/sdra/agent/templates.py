"""Outbound message templates, filled only from record fields.

Template messages carry no internal costs, penalties, mandates or other suppliers' prices,
so they qualify as `template_only` for the autonomy rules. Customer messages never name a
supplier or the cause of a delay.
"""

from __future__ import annotations

from datetime import date

from sdra.schema import Money


def _money(m: Money) -> str:
    return f"{m.amount:.2f} {m.currency}"


def status_request(po_id: str, item_id: str, open_qty: int, expected: date) -> tuple[str, str]:
    return (
        f"Status request: {po_id}",
        f"Please confirm the quantity and delivery date for {item_id} on {po_id}.\n"
        f"Our records show {open_qty} units open, expected {expected.isoformat()}.\n"
        "Please reply to this message.",
    )


def purchase_order(po_id: str, item_id: str, qty: int, unit_price: Money, delivery: date,
                   deliver_to: str) -> tuple[str, str]:
    return (
        f"Purchase order {po_id}",
        f"Purchase order {po_id}: {qty} x {item_id} at {_money(unit_price)} per unit, "
        f"delivery {delivery.isoformat()} to {deliver_to}.\nPlease confirm by reply.",
    )


def po_change(po_id: str, item_id: str, old_qty: int, new_qty: int) -> tuple[str, str]:
    return (
        f"Change to {po_id}",
        f"{po_id}: quantity of {item_id} reduced from {old_qty} to {new_qty}. The {old_qty - new_qty} units "
        f"you advised as late are cancelled. Please deliver the remaining {new_qty} as per your latest "
        "confirmed schedule.",
    )


def po_cancellation(po_id: str, item_id: str, qty: int) -> tuple[str, str]:
    return (
        f"Cancellation of {po_id}",
        f"{po_id}: the {qty} x {item_id} not delivered by the agreed date are cancelled.",
    )


def delivery_date_change(so_id: str, item_id: str, qty: int, new_date: date) -> tuple[str, str]:
    return (
        f"Delivery date update for order {so_id}",
        f"Your order {so_id} ({qty} x {item_id}) will now be delivered on {new_date.isoformat()}. "
        "We apologise for the change and will confirm dispatch.",
    )


def delivery_split(so_id: str, item_id: str, first_qty: int, first_date: date, rest_qty: int,
                   second_date: date) -> tuple[str, str]:
    return (
        f"Delivery schedule update for order {so_id}",
        f"Your order {so_id} ({item_id}) will be delivered in two parts: {first_qty} units on "
        f"{first_date.isoformat()} as planned and the remaining {rest_qty} units on {second_date.isoformat()}. "
        "We apologise for the change and will confirm dispatch.",
    )
