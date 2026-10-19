"""Apply approved actions to the records and the outbox, at most once per idempotency key."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from datetime import timedelta

from sdra.schema import (
    ApplySubstitution, CancelPOLine, CreatePO, ProposedAction, PurchaseOrder, PurchaseOrderLine,
    RecordPOConfirmation, RescheduleWorkOrder, SendMessage, SplitPOLine, SplitSOLine, UpdatePOExpectedDate,
    UpdatePOQuantity, UpdateSOPromiseDate, WorkOrderSubstitution,
)
from sdra.store import Change, MemoryStore


@dataclass(frozen=True)
class OutMessage:
    id: str
    at: datetime
    to: str  # supplier or customer id
    kind: str  # "supplier" | "customer"
    template: str | None
    subject: str
    body: str
    in_reply_to: str | None = None
    action_id: str | None = None
    record_quantities: tuple[int, ...] = ()  # template quantities from records (see SendMessage)


class Executor:
    """Only the action gateway calls this. `done` maps idempotency key -> outbox id (or None
    for record writes), so a retried action, even after a restart, is not applied twice."""

    def __init__(self, store: MemoryStore, outbox: list[OutMessage], done: dict[str, str | None] | None = None):
        self.store, self.outbox = store, outbox
        self.done: dict[str, str | None] = dict(done or {})

    def run(self, action: ProposedAction, now: datetime) -> tuple[bool, list[Change] | OutMessage | None]:
        """(applied now, result). A repeat returns (False, the earlier message or None)."""
        if action.idempotency_key in self.done:
            sent = self.done[action.idempotency_key]
            return False, next((m for m in self.outbox if m.id == sent), None)
        result = self._apply(action, now)
        self.done[action.idempotency_key] = result.id if isinstance(result, OutMessage) else None
        return True, result

    def _apply(self, action: ProposedAction, now: datetime):
        s, p = self.store, action.params
        match p:
            case CreatePO():
                return [
                    s.put("purchase_orders", PurchaseOrder(id=p.po_id, supplier_id=p.supplier_id, order_date=now.date())),
                    s.put("purchase_order_lines", PurchaseOrderLine(
                        id=p.po_id.replace("PO-", "POL-", 1) + "-1", purchase_order_id=p.po_id, item_id=p.item_id,
                        qty_ordered=p.qty, unit_price=p.unit_price, expected_date=p.expected_date)),
                ]
            case UpdatePOQuantity():
                return [s.update("purchase_order_lines", p.po_line_id, qty_ordered=p.new_qty)]
            case CancelPOLine():
                return [s.update("purchase_order_lines", p.po_line_id, status="cancelled")]
            case RecordPOConfirmation():
                return [s.update("purchase_order_lines", p.po_line_id, confirmed_date=p.confirmed_date)]
            case UpdatePOExpectedDate():
                return [s.update("purchase_order_lines", p.po_line_id, expected_date=p.new_date)]
            case SplitPOLine():
                line = s.get("purchase_order_lines", p.po_line_id)
                rest = line.open_qty - p.first_qty
                return [
                    s.update("purchase_order_lines", line.id, qty_ordered=line.qty_received + p.first_qty),
                    s.put("purchase_order_lines", line.model_copy(update={
                        "id": f"{line.id}-2", "qty_ordered": rest, "qty_received": 0, "expected_date": p.second_date,
                        "confirmed_date": p.second_date})),
                ]
            case RescheduleWorkOrder():
                return [s.update("work_orders", p.work_order_id, start_date=p.new_start_date, due_date=p.new_due_date)]
            case UpdateSOPromiseDate():
                return [s.update("sales_order_lines", p.so_line_id, promised_date=p.new_date)]
            case SplitSOLine():
                # The order line and the work order building it split together; the second part
                # is promised (and built) for second_date.
                line = s.get("sales_order_lines", p.so_line_id)
                wo = next(w for w in s.rows("work_orders") if w.sales_order_line_id == line.id)
                rest = line.qty - p.first_qty
                assembly = timedelta(days=s.get("items", wo.item_id).assembly_lead_days)
                return [
                    s.update("sales_order_lines", line.id, qty=p.first_qty),
                    s.put("sales_order_lines", line.model_copy(update={
                        "id": f"{line.id}-2", "qty": rest, "qty_shipped": 0, "promised_date": p.second_date})),
                    s.update("work_orders", wo.id, qty=wo.qty - rest),
                    s.put("work_orders", wo.model_copy(update={
                        "id": f"{wo.id}-2", "qty": rest, "sales_order_line_id": f"{line.id}-2",
                        "start_date": p.second_date - assembly, "due_date": p.second_date})),
                ]
            case ApplySubstitution():
                return [s.put("work_order_substitutions", WorkOrderSubstitution(
                    id=s.new_id("work_order_substitutions"), work_order_id=p.work_order_id,
                    substitution_id=p.substitution_id, qty=p.qty))]
            case SendMessage():
                msg = OutMessage(id=s.new_id("outbox"), at=now, to=p.recipient_id,
                                 kind="customer" if p.kind.value == "send_customer_message" else "supplier",
                                 template=p.template_id, subject=p.subject, body=p.body,
                                 in_reply_to=p.in_reply_to, action_id=action.id,
                                 record_quantities=tuple(p.record_quantities))
                self.outbox.append(msg)
                return msg
        raise NotImplementedError(f"no executor for {p.kind}")
