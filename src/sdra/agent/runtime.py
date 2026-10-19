"""The runner's own bookkeeping, as typed records so it can be saved and restored.

Company records live in the record store, workflow position in the LangGraph checkpointer;
this is everything else (incidents, approval requests, open chases, the event queue...).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sdra.agent.executor import OutMessage
from sdra.schema import ApprovalDecision, InboundMessage, ReviewTask, StockAdjustment, SupplyChangeEvent


class Rec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalEvent(Rec):
    """A decision arriving from the approver UI (or a scenario script)."""

    approver_id: str
    decision: Literal["approve", "reject"]
    request_id: str | None = None  # needed only when several requests are pending
    exclude_supplier_ids: list[str] = Field(default_factory=list)
    comment: str = ""


class RequestRecord(Rec):
    id: str
    incident_id: str
    run: int
    option_id: str
    kind: str  # wait | buy | reallocate | substitute | partial | records
    item_id: str = ""  # the item whose facts the basis records
    suppliers: list[str]
    tier: str
    roles: list[str]
    rule_ids: list[str]
    actions: list[str]
    qty: int | None
    evidence: list[str]
    fingerprint: str
    basis: dict
    created_at: datetime
    expires_at: datetime
    decided: Literal["approved", "executed", "rejected", "voided", "expired", "refused"] | None = None
    decided_at: datetime | None = None
    voided_at: datetime | None = None
    void_reasons: list[str] = Field(default_factory=list)
    signatures: list[ApprovalDecision] = Field(default_factory=list)
    detail: dict = Field(default_factory=dict)


class PendingRun(Rec):
    thread_id: str
    incident_id: str
    request_id: str
    outstanding_roles: list[str]


class IncidentRecord(Rec):
    id: str
    status: str = "open"
    replan_count: int = 0  # re-openings by new supplier information
    plans: int = 0  # every planning pass, capped by policy caps.max_replans_per_incident
    run: int = 0
    lines: set[str] = Field(default_factory=set)
    items: set[str] = Field(default_factory=set)


class Chase(Rec):
    first: datetime
    count: int = 0


class Queued(Rec):
    at: datetime
    seq: int
    kind: Literal["email", "stock_adjustment", "approval", "silence", "reminder", "expiry"]
    email: InboundMessage | None = None
    stock_adjustment: StockAdjustment | None = None
    approval: ApprovalEvent | None = None
    ref: str | None = None  # po line (silence) or request id (reminder, expiry)
    stage: int | None = None


class RuntimeState(Rec):
    now: datetime
    queue: list[Queued]
    ledger_events: dict[str, SupplyChangeEvent]
    status_requests: dict[str, str]
    outbox: list[OutMessage]
    review_tasks: list[ReviewTask]
    requests: list[RequestRecord]
    blocked: list[dict]
    generated_events: list[SupplyChangeEvent]
    incidents: dict[str, IncidentRecord]
    pending: dict[str, PendingRun]
    chases: dict[str, Chase]
    last_inbound: dict[str, datetime]
    inbox: dict[str, InboundMessage]
    notifications: list[dict]
    done: dict[str, str | None]
    usage: dict[str, dict[str, Decimal | int]]
