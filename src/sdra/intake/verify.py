"""Deterministic verification of extracted claims.

A claim becomes a SupplyChangeEvent with verification:
  failed      the source can't be trusted (unknown sender, evidence not in the message,
              PO not found or belonging to another supplier, item not on the PO)
  unverified  the source is genuine but the claim can't be used yet (unclear, low
              confidence, missing date or quantity, a quantity or date not found in the
              quoted text, implausible, or in conflict)
  verified    usable for planning

The Ledger holds verified (and conflict-held) events not yet reflected in records, and
applies the contradiction rule: verified-sender messages about the same PO line that
disagree within the contradiction window are all held; a reply to our status request
about that line supersedes them; outside the window, the later message replaces the earlier.
Failed claims never enter the ledger, so they can't hold or cancel anything. A message that
restates exactly what the ledger holds for a line is a duplicate and changes nothing, and a
message that leaves part of a line unclear is held whole for that line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sdra.intake import grounding
from sdra.policy import Policy
from sdra.schema import ChangeKind, ExtractedClaim, ExtractionResult, InboundMessage, SupplyChangeEvent
from sdra.store import RecordStore

REQUIRED = {
    ChangeKind.DELAY: ("new_date", "missing new_date"),
    ChangeKind.PARTIAL_QUANTITY: ("new_qty", "missing quantity"),
    ChangeKind.PRICE_CHANGE: ("new_unit_price", "missing price"),
    ChangeKind.SUBSTITUTION_OFFER: ("substitute_item_id", "missing substitute"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _ref(text: str | None) -> str | None:
    return re.sub(r"\s+", "", text).upper() if text else None


class Ledger:
    def __init__(self) -> None:
        self.events: dict[str, SupplyChangeEvent] = {}
        self.status_requests: dict[str, str] = {}  # our outbox message id -> po_line_id asked about

    def for_line(self, line_id: str) -> list[SupplyChangeEvent]:
        return [e for e in self.events.values() if e.po_line_id == line_id]

    def verified(self) -> list[SupplyChangeEvent]:
        return [e for e in self.events.values() if e.verification == "verified"]

    def held_lines(self) -> set[str]:
        return {e.po_line_id for e in self.events.values() if e.verification == "unverified"}

    def consume(self, line_ids: set[str]) -> None:
        """Drop events once the records reflect them (after an approved plan executes)."""
        self.events = {k: e for k, e in self.events.items() if e.po_line_id not in line_ids}


@dataclass
class Intake:
    message_id: str
    supplier_id: str | None
    events: list[SupplyChangeEvent]
    review_reasons: list[str] = field(default_factory=list)
    conflict_lines: set[str] = field(default_factory=set)
    superseded_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)  # restate what the ledger already holds

    @property
    def verified(self) -> list[SupplyChangeEvent]:
        return [e for e in self.events if e.verification == "verified"]

    @property
    def unverified_lines(self) -> set[str]:
        return {e.po_line_id for e in self.events if e.verification == "unverified" and e.po_line_id}


def _with(event: SupplyChangeEvent, status: str, note: str | None = None) -> SupplyChangeEvent:
    notes = [*event.verification_notes, note] if note else event.verification_notes
    return event.model_copy(update={"verification": status, "verification_notes": notes})


def _check_claim(store: RecordStore, policy: Policy, message: InboundMessage, supplier, claim: ExtractedClaim,
                 index: int) -> SupplyChangeEvent:
    failed, unverified = [], []
    text = _norm(f"{message.subject}\n{message.body}")
    if supplier is None:
        failed.append(f"unknown sender {message.sender}")
    if not claim.evidence or any(_norm(e.quote) not in text for e in claim.evidence):
        failed.append("evidence not found in message")

    kind = ChangeKind.DELAY if claim.kind == "unclear" else ChangeKind(claim.kind)
    if claim.kind == "unclear":
        unverified.append("unclear")

    line = item_id = None
    po = store.get("purchase_orders", _ref(claim.po_reference)) if claim.po_reference else None
    if claim.po_reference and po is None:
        failed.append(f"PO {claim.po_reference} not found")
    elif po is None:
        unverified.append("no PO reference")
    elif supplier is not None and po.supplier_id != supplier.id:
        failed.append(f"{po.id} belongs to another supplier")
    else:
        lines = [x for x in store.rows("purchase_order_lines") if x.purchase_order_id == po.id and x.open_qty]
        if claim.item_reference:
            matching = [x for x in lines if x.item_id == _ref(claim.item_reference)]
            if not matching:
                failed.append(f"item {claim.item_reference} not on {po.id}")
            line = matching[0] if matching else None
        elif len(lines) == 1:
            line = lines[0]
        else:
            unverified.append(f"cannot tell which line of {po.id}")
    if line is not None:
        item_id = line.item_id

    substitute = _ref(claim.substitute_reference)
    event = SupplyChangeEvent(
        id=f"EV-{message.id}-{index}", kind=kind, observed_at=message.received_at,
        supplier_id=supplier.id if supplier else None, po_line_id=line.id if line else None, item_id=item_id,
        new_qty=claim.stated_qty, new_date=claim.stated_date, new_unit_price=claim.stated_unit_price,
        substitute_item_id=substitute if substitute and store.get("items", substitute) else None,
        confidence=claim.confidence, evidence=claim.evidence, source_ids=[message.id], verification="unverified",
    )

    if line is not None and not failed:
        if claim.confidence < policy.extraction.min_confidence:
            unverified.append(f"low confidence {claim.confidence:.2f}")
        need, note = REQUIRED.get(kind, (None, None))
        if need and getattr(event, need) is None and claim.kind != "unclear":
            unverified.append(note)
        if policy.extraction.require_values_in_quote:
            unverified += grounding.check([e.quote for e in claim.evidence], claim.stated_qty, claim.stated_date,
                                          message.received_at.date())
        if claim.stated_qty is not None and claim.stated_qty > line.open_qty and not policy.extraction.allow_qty_above_open_po:
            unverified.append(f"quantity {claim.stated_qty} above open {line.open_qty}")
        if claim.stated_date and abs((claim.stated_date - line.expected_date).days) > policy.extraction.max_date_shift_days:
            unverified.append("date shift beyond policy limit")
        if kind == ChangeKind.CONFIRMATION and (
            (claim.stated_qty is not None and claim.stated_qty != line.open_qty)
            or (claim.stated_date is not None and claim.stated_date != line.expected_date)
        ):
            unverified.append("confirmation disagrees with records")

    if failed:
        return event.model_copy(update={"verification": "failed", "verification_notes": failed + unverified})
    if unverified:
        return event.model_copy(update={"verification_notes": unverified})
    return event.model_copy(update={"verification": "verified"})


def _schedule(events: list[SupplyChangeEvent]) -> tuple:
    return tuple(sorted((e.kind.value, e.new_qty or 0, e.new_date.isoformat() if e.new_date else "") for e in events))


def verify(store: RecordStore, policy: Policy, ledger: Ledger, message: InboundMessage, result: ExtractionResult,
           now: datetime) -> Intake:
    sender = message.sender.strip().casefold()
    supplier = next((s for s in store.rows("suppliers") if sender in {a.casefold() for a in s.contact_addresses}), None)
    events = [_check_claim(store, policy, message, supplier, c, i) for i, c in enumerate(result.claims)]
    intake = Intake(message.id, supplier.id if supplier else None, events)

    # Claims in one message must not add up to more than the open quantity of a line.
    for line_id in {e.po_line_id for e in events if e.verification == "verified"}:
        line = store.get("purchase_order_lines", line_id)
        qty = sum(e.new_qty or 0 for e in events if e.po_line_id == line_id and e.verification == "verified"
                  and e.kind in (ChangeKind.PARTIAL_QUANTITY, ChangeKind.DELAY))
        if qty > line.open_qty:
            intake.events = [_with(e, "unverified", "claims exceed open quantity") if e.po_line_id == line_id else e
                             for e in intake.events]

    # A message that leaves part of a line unclear is held whole for that line: planning on
    # half the picture would act on a guess about the rest (the status request asks for it).
    unclear = {e.po_line_id for e in intake.events if e.verification == "unverified" and e.po_line_id}
    intake.events = [_with(e, "unverified", "other claims on this line need checking")
                     if e.verification == "verified" and e.po_line_id in unclear else e for e in intake.events]

    window = timedelta(hours=policy.extraction.contradiction_window_hours)
    for line_id in sorted({e.po_line_id for e in intake.verified}):
        new = [e for e in intake.verified if e.po_line_id == line_id]
        prior = [e for e in ledger.for_line(line_id) if message.id not in e.source_ids]
        if message.in_reply_to and ledger.status_requests.get(message.in_reply_to) == line_id:
            intake.superseded_ids += [e.id for e in prior]  # our question answered: the reply wins
            for e in prior:
                del ledger.events[e.id]
        elif prior and all(e.verification == "verified" for e in prior) and _schedule(prior) == _schedule(new):
            intake.duplicate_ids += [e.id for e in new]  # same news again: keep what we have
        elif any(now - e.observed_at <= window for e in prior) and _schedule(prior) != _schedule(new):
            intake.conflict_lines.add(line_id)
            intake.events = [_with(e, "unverified", "conflict") if e.po_line_id == line_id and e in new else e
                             for e in intake.events]
            for e in prior:
                ledger.events[e.id] = _with(e, "unverified", "conflict") if e.verification == "verified" else e
        else:
            intake.superseded_ids += [e.id for e in prior]  # later message replaces earlier
            for e in prior:
                del ledger.events[e.id]
    for e in intake.events:
        if e.id in intake.duplicate_ids:
            continue
        if e.verification == "verified" or (e.po_line_id in intake.conflict_lines and e.verification == "unverified"):
            ledger.events[e.id] = e

    # Why a person should look.
    pos = sorted({c.po_reference for c in result.claims if c.po_reference})
    if supplier is None:
        intake.review_reasons.append(f"unknown sender {message.sender} referencing {', '.join(pos) or 'no PO'}")
    if result.contains_instructions_to_agent:
        intake.review_reasons.append(f"{message.id} contains instructions addressed to the agent")
    for line_id in sorted(intake.conflict_lines):
        intake.review_reasons.append(f"conflicting messages on {line_id}")
    for e in intake.events:
        if e.verification == "unverified" and "conflict" not in e.verification_notes:
            intake.review_reasons.append(f"could not verify claim on {e.po_line_id or pos}: {', '.join(e.verification_notes)}")
    return intake
