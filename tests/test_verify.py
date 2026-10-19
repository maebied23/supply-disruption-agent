"""Verification of extracted claims: per-scenario outcomes and targeted adversarial cases."""

from datetime import date, datetime, time

import pytest

from sdra.intake.verify import Ledger, verify
from sdra.schema import Evidence, ExtractedClaim, ExtractionResult, InboundMessage
from scenario_support import SCENARIOS, apply_final_state, load, messages, resolved, scripted_results

WITH_CLAIMS = sorted(sid for sid, sc in SCENARIOS.items() if sc["expected"]["claims"])


def run_intake(sid):
    """Feed every email through verification in order; return final status per claim."""
    sc = SCENARIOS[sid]
    pack, store = load(sid)
    ledger, results = Ledger(), scripted_results(sid)
    status, intakes = {}, {}
    # Phase 1 ends with the last approval on the first day (its plan has executed by then).
    phase_end = max((e["at"] for e in sc["events"] if e["type"] == "approval" and e["at"].date() == sc["start"].date()),
                    default=datetime.combine(sc["start"].date(), time.max, tzinfo=sc["start"].tzinfo))
    phase_applied = "phase_1" not in sc["expected"]
    for msg in messages(sid):
        if not phase_applied and msg.received_at > phase_end:
            apply_final_state(store, resolved(sc["expected"]["phase_1"]["same_as"], "final_state"))
            ledger.consume({e.po_line_id for e in ledger.verified()})
            phase_applied = True
        intake = verify(store, pack.policy, ledger, msg, results[msg.id], msg.received_at)
        intakes[msg.id] = intake
        for e in intake.events:
            status[e.id] = e
        for e in ledger.events.values():  # the ledger may re-mark earlier events (conflict)
            status[e.id] = e
        for line_id in intake.conflict_lines:  # the orchestrator would send this status request
            ledger.status_requests[f"OUT-N{len(ledger.status_requests) + 1:04d}"] = line_id
    return status, intakes


@pytest.mark.parametrize("sid", WITH_CLAIMS)
def test_claim_verification_matches_scenario(sid):
    status, _ = run_intake(sid)
    claims = SCENARIOS[sid]["expected"]["claims"]
    msgs = [m.id for m in messages(sid)]
    for claim in claims:
        source = claim.get("source") or msgs[0]
        same_source = [c for c in claims if (c.get("source") or msgs[0]) == source]
        event = status[f"EV-{source}-{same_source.index(claim)}"]
        assert event.verification == claim["verification"], (claim, event.verification_notes)
        assert event.po_line_id == claim["po_line_id"]
        if "note" in claim:
            assert any(claim["note"] in n for n in event.verification_notes), event.verification_notes
        if "supplier_id" in claim:
            assert event.supplier_id == claim["supplier_id"]


def test_s04_reply_supersedes_held_claims():
    _, intakes = run_intake("S04")
    assert intakes["MSG-S04-2"].conflict_lines == {"POL-4471-1"}
    assert len(intakes["MSG-S04-3"].superseded_ids) == 3
    assert "conflicting messages on POL-4471-1" in intakes["MSG-S04-2"].review_reasons


def test_s08_spoofed_sender_is_flagged_and_does_not_hold_the_plan():
    _, intakes = run_intake("S08")
    spoof = intakes["MSG-S08-2"]
    assert not spoof.conflict_lines and not spoof.verified
    assert any("unknown sender sales@cmp-a.example.co" in r and "PO-4471" in r for r in spoof.review_reasons)


def test_s02_vague_message_routes_to_review():
    _, intakes = run_intake("S02")
    (reason,) = intakes["MSG-S02-1"].review_reasons
    assert "POL-4471-1" in reason and "missing new_date" in reason


# --- targeted cases --------------------------------------------------------------------


AT = datetime.fromisoformat("2026-11-03T09:12:00+03:00")


def check(body, claim_kwargs, sender="sales@cmp-a.example", quote=None):
    pack, store = load("S01")
    msg = InboundMessage(id="M", channel="email", sender=sender, received_at=AT, body=body)
    claim = ExtractedClaim(language="en", confidence=0.95,
                           evidence=[Evidence(message_id="M", quote=quote or body.splitlines()[0])], **claim_kwargs)
    return verify(store, pack.policy, Ledger(), msg, ExtractionResult(message_id="M", claims=[claim]), AT).events[0]


def test_fabricated_quote_fails():
    e = check("Only 300 units on PO-4471.", dict(kind="partial_quantity", po_reference="PO-4471", stated_qty=300),
              quote="We will deliver 300 units")
    assert (e.verification, e.verification_notes[0]) == ("failed", "evidence not found in message")


def test_po_of_another_supplier_fails():
    e = check("Delay on PO-4471 to 20 Nov.", dict(kind="delay", po_reference="PO-4471", stated_date=date(2026, 11, 20)),
              sender="orders@cmp-b.example")
    assert e.verification == "failed" and "belongs to another supplier" in e.verification_notes[0]


def test_quantity_above_open_and_extreme_date_are_held():
    e = check("PO-4471: 900 units on 10 Nov.", dict(kind="partial_quantity", po_reference="PO-4471", stated_qty=900))
    assert e.verification == "unverified" and "above open" in e.verification_notes[0]
    e = check("PO-4471 moves to 2027.", dict(kind="delay", po_reference="PO-4471", stated_date=date(2027, 6, 1)))
    assert e.verification == "unverified" and any("date shift" in n for n in e.verification_notes)


def test_low_confidence_is_held():
    pack, store = load("S01")
    msg = InboundMessage(id="M", channel="email", sender="sales@cmp-a.example", received_at=AT, body="PO-4471 late, 20 Dec")
    claim = ExtractedClaim(kind="delay", po_reference="PO-4471", stated_date=date(2026, 12, 20),
                           language="en", confidence=0.5, evidence=[Evidence(message_id="M", quote="PO-4471 late, 20 Dec")])
    e = verify(store, pack.policy, Ledger(), msg, ExtractionResult(message_id="M", claims=[claim]), AT).events[0]
    assert e.verification == "unverified" and e.verification_notes == ["low confidence 0.50"]


def test_later_message_outside_window_replaces_earlier():
    pack, store = load("S01")
    ledger = Ledger()

    def send(mid, at, d):
        msg = InboundMessage(id=mid, channel="email", sender="sales@cmp-a.example", received_at=at,
                             body=f"PO-4471 now arrives {d.isoformat()}")
        claim = ExtractedClaim(kind="delay", po_reference="PO-4471", stated_date=d, language="en", confidence=0.95,
                               evidence=[Evidence(message_id=mid, quote=msg.body)])
        return verify(store, pack.policy, ledger, msg, ExtractionResult(message_id=mid, claims=[claim]), at)

    send("M1", AT, date(2026, 11, 20))
    later = send("M2", AT.replace(day=7), date(2026, 11, 22))  # 4 days later > 48 h window
    assert later.verified and later.superseded_ids == ["EV-M1-0"]
    assert [e.new_date for e in ledger.verified()] == [date(2026, 11, 22)]
