"""Canonical data model.

The agent understands only these types. Nothing here names a product, industry,
company or threshold: those live in the company pack (seed data + policy.yaml).

Groups:
  1. Shared value types (Money, PenaltyTerms)
  2. Records read and written through the adapter
  3. Inbound signals and extracted claims (untrusted until verified)
  4. Decisions: incidents, mandates, proposed actions, approvals
  5. Audit log
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Id = Annotated[str, Field(min_length=1, max_length=64)]
Qty = Annotated[int, Field(ge=0)]  # V1 assumes discrete units
Ratio = Annotated[Decimal, Field(gt=0)]
Pct = Annotated[Decimal, Field(ge=0, le=100)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- 1. Shared value types ---------------------------------------------------


class Money(Model):
    amount: Decimal = Field(max_digits=14, decimal_places=2)
    currency: CurrencyCode


# Penalty semantics (applied per sales-order line, lateness measured past promised_date
# plus grace_days):
#   fixed                  charged once if any quantity on the line is late
#   per_day                charged per late day, up to cap
#   pct_of_value_per_week  pct of the late quantity's value per started week, up to cap


class FixedPenalty(Model):
    kind: Literal["fixed"] = "fixed"
    amount: Money
    grace_days: int = Field(default=0, ge=0)


class PerDayPenalty(Model):
    kind: Literal["per_day"] = "per_day"
    amount_per_day: Money
    grace_days: int = Field(default=0, ge=0)
    cap: Money | None = None


class PctOfValuePenalty(Model):
    kind: Literal["pct_of_value_per_week"] = "pct_of_value_per_week"
    pct_per_week: Pct
    grace_days: int = Field(default=0, ge=0)
    cap_pct_of_value: Pct | None = None


PenaltyTerms = Annotated[
    FixedPenalty | PerDayPenalty | PctOfValuePenalty, Field(discriminator="kind")
]


# --- 2. Records ----------------------------------------------------------------


class Location(Model):
    id: Id
    name: str


class Item(Model):
    id: Id
    name: str
    uom: str = "unit"
    sourcing: Literal["make", "buy"]
    critical: bool = False
    assembly_lead_days: int = Field(default=0, ge=0)  # make items only
    attributes: dict[str, str] = Field(default_factory=dict)  # pack-specific, never read by agent logic

    @model_validator(mode="after")
    def _lead_days_only_for_make(self) -> Item:
        if self.sourcing == "buy" and self.assembly_lead_days:
            raise ValueError("assembly_lead_days applies to make items only")
        return self


class BOMLine(Model):
    parent_item_id: Id
    component_item_id: Id
    qty_per: Ratio

    @model_validator(mode="after")
    def _no_self_reference(self) -> BOMLine:
        if self.parent_item_id == self.component_item_id:
            raise ValueError("an item cannot be its own component")
        return self


class Substitution(Model):
    """An approved substitute, kept separate from the BOM because approval is often
    customer-specific (a customer may have to accept a spec change)."""

    id: Id
    item_id: Id
    substitute_item_id: Id
    ratio: Ratio  # substitute units per original unit
    parent_item_ids: list[Id] = Field(default_factory=list)  # empty = every BOM using item_id
    customer_ids: list[Id] = Field(default_factory=list)  # empty = every customer
    requires_customer_consent: bool = False
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _check(self) -> Substitution:
        if self.item_id == self.substitute_item_id:
            raise ValueError("an item cannot substitute for itself")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from is after valid_to")
        return self


class WorkOrderSubstitution(Model):
    """A substitution applied to one work order: `qty` units of the original component are
    replaced by qty x ratio units of the substitute (written by the apply_substitution action)."""

    id: Id
    work_order_id: Id
    substitution_id: Id
    qty: Qty


class StockPosition(Model):
    item_id: Id
    location_id: Id
    on_hand: Qty
    quarantined: Qty = 0  # on hand but not usable
    safety_stock: Qty = 0

    @property
    def usable(self) -> int:
        return max(self.on_hand - self.quarantined, 0)


class Allocation(Model):
    """Stock reserved for a sales order line. Hard allocations need approval to move."""

    id: Id
    item_id: Id
    location_id: Id
    sales_order_line_id: Id
    qty: Qty
    hard: bool = False


class Customer(Model):
    id: Id
    name: str
    priority_tier: int = Field(ge=1)  # 1 = most important
    language: str = "en"
    contact_addresses: list[str] = Field(default_factory=list)
    default_penalty: PenaltyTerms | None = None
    # Share of contractual penalties actually collected (finance data). Expected cost uses
    # penalty x this; reports still show the contractual amount.
    penalty_enforcement: Decimal = Field(default=Decimal(1), ge=0, le=1)


class SalesOrder(Model):
    id: Id
    customer_id: Id
    order_date: date


class SalesOrderLine(Model):
    id: Id
    sales_order_id: Id
    item_id: Id
    qty: Qty
    qty_shipped: Qty = 0
    unit_price: Money
    requested_date: date
    promised_date: date
    partial_delivery_allowed: bool = False
    penalty: PenaltyTerms | None = None  # overrides the customer default
    status: Literal["open", "shipped", "cancelled"] = "open"

    @model_validator(mode="after")
    def _shipped_within_qty(self) -> SalesOrderLine:
        if self.qty_shipped > self.qty:
            raise ValueError("qty_shipped exceeds qty")
        return self


class WorkOrder(Model):
    id: Id
    item_id: Id
    qty: Qty
    sales_order_line_id: Id | None = None  # make-to-order link; None = build to stock
    start_date: date
    due_date: date
    status: Literal["planned", "released", "done", "cancelled"] = "planned"


class Supplier(Model):
    id: Id
    name: str
    country: str
    language: str = "en"
    currency: CurrencyCode
    contact_addresses: list[str]  # sender verification checks inbound mail against these
    approved: bool = True
    on_time_rate: Decimal = Field(default=Decimal("0.9"), ge=0, le=1)  # historical, drives risk


class SupplierItem(Model):
    """Approved source for an item. No row = the supplier may not supply it."""

    supplier_id: Id
    item_id: Id
    role: Literal["primary", "backup"]
    lead_time_days: int = Field(ge=0)
    min_order_qty: Qty = 0
    contract_price: Money | None = None


class PurchaseOrder(Model):
    id: Id
    supplier_id: Id
    order_date: date


class PurchaseOrderLine(Model):
    id: Id
    purchase_order_id: Id
    item_id: Id
    qty_ordered: Qty
    qty_received: Qty = 0
    unit_price: Money
    confirmed_date: date | None = None
    expected_date: date
    status: Literal["open", "closed", "cancelled"] = "open"

    @model_validator(mode="after")
    def _received_within_ordered(self) -> PurchaseOrderLine:
        if self.qty_received > self.qty_ordered:
            raise ValueError("qty_received exceeds qty_ordered")
        return self

    @property
    def open_qty(self) -> int:
        return self.qty_ordered - self.qty_received if self.status == "open" else 0


class SupplierOffer(Model):
    id: Id
    supplier_id: Id
    item_id: Id
    unit_price: Money
    min_qty: Qty = 0
    max_qty: Qty
    lead_time_days: int = Field(ge=0)  # delivery at our plant = PO date + lead time
    valid_until: datetime
    source: Literal["catalogue", "quote", "negotiation"]
    message_id: Id | None = None  # provenance when the offer came from a message


class Approver(Model):
    id: Id
    name: str
    roles: list[str] = Field(min_length=1)  # role names are defined in policy.yaml
    address: str


# --- 3. Inbound signals and claims -------------------------------------------


class InboundMessage(Model):
    id: Id
    channel: Literal["email"]
    sender: str
    received_at: datetime  # simulated clock
    subject: str = ""
    body: str
    in_reply_to: Id | None = None  # id of our outbox message, if the sender replied to one


class ShipmentUpdate(Model):
    """Structured signal (advance ship notice or tracking event)."""

    id: Id
    received_at: datetime
    supplier_id: Id
    po_line_id: Id
    qty: Qty
    ship_date: date | None = None
    eta: date
    status: Literal["shipped", "in_transit", "delayed", "delivered"]


class StockAdjustment(Model):
    """Correction from the system of record (cycle count, late-posted receipt)."""

    id: Id
    received_at: datetime
    item_id: Id
    location_id: Id
    new_on_hand: Qty
    reason: str


class ChangeKind(StrEnum):
    CONFIRMATION = "confirmation"
    DELAY = "delay"
    PARTIAL_QUANTITY = "partial_quantity"
    CANCELLATION = "cancellation"
    SUBSTITUTION_OFFER = "substitution_offer"
    PRICE_CHANGE = "price_change"
    NO_RESPONSE = "no_response"  # generated by the clock, never extracted


class Evidence(Model):
    message_id: Id
    quote: str = Field(min_length=1)  # verbatim span from the message


class ExtractedClaim(Model):
    """The only thing the quarantined extraction LLM may output.

    References are copied as written in the message (e.g. "PO-4471", "CMP-200");
    deterministic code resolves them to record ids. The LLM never emits internal ids.
    """

    kind: Literal[
        "confirmation",
        "delay",
        "partial_quantity",
        "cancellation",
        "substitution_offer",
        "price_change",
        "unclear",
    ]
    po_reference: str | None = None
    item_reference: str | None = None
    stated_qty: Qty | None = None
    stated_date: date | None = None
    stated_unit_price: Money | None = None
    substitute_reference: str | None = None
    language: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(min_length=1)


class ExtractionResult(Model):
    """One message can carry several claims (e.g. 300 units on time, 200 delayed),
    or none (an unrelated message)."""

    message_id: Id
    claims: list[ExtractedClaim]
    # Advisory flag from the extractor; it can only route the message to a human, never act.
    contains_instructions_to_agent: bool = False


class SupplyChangeEvent(Model):
    """A claim resolved against records. Untrusted until verification == 'verified'.

    partial_quantity: new_qty is what arrives on the line's current expected date.
    delay: new_qty units (all open units if None) move to new_date.
    """

    id: Id
    kind: ChangeKind
    observed_at: datetime
    supplier_id: Id | None  # sender match; None if the sender is unknown
    po_line_id: Id | None
    item_id: Id | None
    new_qty: Qty | None = None
    new_date: date | None = None
    new_unit_price: Money | None = None
    substitute_item_id: Id | None = None
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list)
    source_ids: list[Id] = Field(min_length=1)  # message or shipment update ids
    verification: Literal["unverified", "verified", "failed"] = "unverified"
    verification_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fields_for_kind(self) -> SupplyChangeEvent:
        required = {
            ChangeKind.DELAY: ("new_date",),
            ChangeKind.PARTIAL_QUANTITY: ("new_qty",),
            ChangeKind.SUBSTITUTION_OFFER: ("substitute_item_id",),
            ChangeKind.PRICE_CHANGE: ("new_unit_price",),
        }.get(self.kind, ())
        missing = [f for f in required if getattr(self, f) is None]
        if missing and self.verification == "verified":
            raise ValueError(f"verified {self.kind} event is missing {missing}")
        return self


# --- 4. Decisions --------------------------------------------------------------


class Incident(Model):
    id: Id
    opened_at: datetime
    trigger_event_ids: list[Id] = Field(min_length=1)
    status: Literal["open", "awaiting_approval", "executing", "monitoring", "closed", "escalated"]
    replan_count: int = Field(default=0, ge=0)


class Mandate(Model):
    """Negotiating limits, computed deterministically. Never shown to the negotiator (V2)."""

    id: Id
    incident_id: Id
    item_id: Id
    min_qty: Qty
    max_qty: Qty
    latest_date: date
    max_unit_price: Money  # base currency
    next_best_option_id: str  # what the premium ceiling was derived from
    penalty_avoided: Money
    valid_until: datetime

    @model_validator(mode="after")
    def _qty_range(self) -> Mandate:
        if self.min_qty > self.max_qty:
            raise ValueError("min_qty exceeds max_qty")
        return self


class ActionKind(StrEnum):
    """Everything the agent can do. An action with no kind here is impossible by construction
    (e.g. changing payment terms or customer contract prices)."""

    RECORD_PO_CONFIRMATION = "record_po_confirmation"
    UPDATE_PO_EXPECTED_DATE = "update_po_expected_date"
    UPDATE_PO_QUANTITY = "update_po_quantity"
    SPLIT_PO_LINE = "split_po_line"
    CANCEL_PO_LINE = "cancel_po_line"
    CREATE_PO = "create_po"
    REALLOCATE_STOCK = "reallocate_stock"
    UPDATE_SO_PROMISE_DATE = "update_so_promise_date"
    SPLIT_SO_LINE = "split_so_line"
    APPLY_SUBSTITUTION = "apply_substitution"
    RESCHEDULE_WORK_ORDER = "reschedule_work_order"
    SEND_SUPPLIER_MESSAGE = "send_supplier_message"
    SEND_CUSTOMER_MESSAGE = "send_customer_message"


class RecordPOConfirmation(Model):
    """Record a verified supplier confirmation that matches the PO line as it stands."""

    kind: Literal[ActionKind.RECORD_PO_CONFIRMATION] = ActionKind.RECORD_PO_CONFIRMATION
    po_line_id: Id
    confirmed_date: date


class UpdatePOExpectedDate(Model):
    kind: Literal[ActionKind.UPDATE_PO_EXPECTED_DATE] = ActionKind.UPDATE_PO_EXPECTED_DATE
    po_line_id: Id
    new_date: date


class UpdatePOQuantity(Model):
    kind: Literal[ActionKind.UPDATE_PO_QUANTITY] = ActionKind.UPDATE_PO_QUANTITY
    po_line_id: Id
    new_qty: Qty


class SplitPOLine(Model):
    """Record a split delivery: first_qty keeps the current expected date, the rest moves."""

    kind: Literal[ActionKind.SPLIT_PO_LINE] = ActionKind.SPLIT_PO_LINE
    po_line_id: Id
    first_qty: Qty
    second_date: date


class CancelPOLine(Model):
    kind: Literal[ActionKind.CANCEL_PO_LINE] = ActionKind.CANCEL_PO_LINE
    po_line_id: Id


class CreatePO(Model):
    kind: Literal[ActionKind.CREATE_PO] = ActionKind.CREATE_PO
    po_id: Id  # reserved when proposed, so messages and approvals can reference it
    supplier_id: Id
    item_id: Id
    qty: Qty
    unit_price: Money
    expected_date: date
    offer_id: Id | None = None
    mandate_id: Id | None = None


class ReallocateStock(Model):
    kind: Literal[ActionKind.REALLOCATE_STOCK] = ActionKind.REALLOCATE_STOCK
    item_id: Id
    location_id: Id
    from_so_line_id: Id
    to_so_line_id: Id
    qty: Qty


class UpdateSOPromiseDate(Model):
    kind: Literal[ActionKind.UPDATE_SO_PROMISE_DATE] = ActionKind.UPDATE_SO_PROMISE_DATE
    so_line_id: Id
    new_date: date


class SplitSOLine(Model):
    """Partial delivery: ship first_qty on the current promise date, the rest on second_date."""

    kind: Literal[ActionKind.SPLIT_SO_LINE] = ActionKind.SPLIT_SO_LINE
    so_line_id: Id
    first_qty: Qty
    second_date: date


class ApplySubstitution(Model):
    kind: Literal[ActionKind.APPLY_SUBSTITUTION] = ActionKind.APPLY_SUBSTITUTION
    substitution_id: Id
    work_order_id: Id
    qty: Qty


class RescheduleWorkOrder(Model):
    kind: Literal[ActionKind.RESCHEDULE_WORK_ORDER] = ActionKind.RESCHEDULE_WORK_ORDER
    work_order_id: Id
    new_start_date: date
    new_due_date: date


class SendMessage(Model):
    kind: Literal[ActionKind.SEND_SUPPLIER_MESSAGE, ActionKind.SEND_CUSTOMER_MESSAGE]
    recipient_id: Id  # supplier or customer id
    in_reply_to: Id | None = None
    template_id: str | None = None  # None = free-text draft
    subject: str
    body: str  # may be LLM-drafted; passes the outbound leak check before sending
    # Quantities a template filled in from records. In template messages only, the leak check
    # does not read these numbers as amounts (a quantity of 200 is not a leaked cost of 200).
    record_quantities: list[Qty] = Field(default_factory=list)


ActionParams = Annotated[
    RecordPOConfirmation
    | UpdatePOExpectedDate
    | UpdatePOQuantity
    | SplitPOLine
    | CancelPOLine
    | CreatePO
    | ReallocateStock
    | UpdateSOPromiseDate
    | SplitSOLine
    | ApplySubstitution
    | RescheduleWorkOrder
    | SendMessage,
    Field(discriminator="kind"),
]

Tier = Literal["execute", "approve", "dual", "blocked"]


class ProposedAction(Model):
    """Numbers in `params` come from deterministic code. The LLM may write `rationale` only."""

    id: Id
    incident_id: Id
    idempotency_key: str = Field(min_length=8)
    params: ActionParams
    rationale: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class ApprovalRequest(Model):
    id: Id
    incident_id: Id
    action_ids: list[Id] = Field(min_length=1)
    tier: Literal["approve", "dual"]
    required_roles: list[str] = Field(min_length=1)  # one approval per role, distinct users
    basis_fingerprint: str  # hash of the facts the recommendation relied on; re-checked on resume
    created_at: datetime
    expires_at: datetime


class ApprovalDecision(Model):
    request_id: Id
    approver_id: Id
    role: str
    decision: Literal["approve", "reject"]
    exclude_supplier_ids: list[Id] = Field(default_factory=list)  # structured reason, chosen in the UI
    comment: str = ""  # recorded, never parsed into plan changes
    decided_at: datetime

    @model_validator(mode="after")
    def _exclusions_only_on_reject(self) -> ApprovalDecision:
        if self.exclude_supplier_ids and self.decision != "reject":
            raise ValueError("exclude_supplier_ids applies to rejections only")
        return self


class ReviewTask(Model):
    """Work routed to a person that is not an approval (e.g. an unverifiable claim)."""

    id: Id
    incident_id: Id | None = None
    assignee_role: str
    reason: str
    ref_ids: list[Id] = Field(default_factory=list)
    created_at: datetime
    status: Literal["open", "done"] = "open"


# --- 5. Audit ------------------------------------------------------------------


class AuditEntry(Model):
    """Append-only and hash-chained, so a removed or edited entry is detectable."""

    seq: int = Field(ge=0)
    at: datetime  # simulated clock
    actor: str  # "agent", "system", or an approver id
    event: str  # e.g. "claim_extracted", "action_proposed", "approval_decided", "action_executed"
    incident_id: Id | None = None
    ref_ids: list[Id] = Field(default_factory=list)
    before: dict | None = None
    after: dict | None = None
    prev_hash: str
    hash: str
