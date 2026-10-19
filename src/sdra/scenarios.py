"""Scenario files: loading, and the scripted (perfect) extraction used without an LLM.

Used by the test suite and by the approver UI's demo mode.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from sdra.intake import grounding
from sdra.pack import Pack, load_pack, merge
from sdra.schema import Evidence, ExtractedClaim, ExtractionResult, InboundMessage
from sdra.store import MemoryStore

ROOT = Path(__file__).parents[2]
SCENARIOS = {p.stem[:3]: yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((ROOT / "scenarios").glob("S*.yaml"))}


def load_scenario_pack(sid: str, policy_overrides: dict | None = None) -> Pack:
    sc = SCENARIOS[sid]
    return load_pack(ROOT / "packs" / sc["pack"], sc.get("seed_overrides"),
                     merge(sc.get("policy_overrides") or {}, policy_overrides or {}))


def load(sid: str) -> tuple[Pack, MemoryStore]:
    pack = load_scenario_pack(sid)
    return pack, MemoryStore(pack.seed)


def messages(sid: str) -> list[InboundMessage]:
    return [InboundMessage.model_validate(e["message"]) for e in SCENARIOS[sid]["events"] if e["type"] == "email"]


def _quote(message: InboundMessage, ref: str | None, qty: int | None = None, when=None) -> str:
    """The shortest run of lines holding the PO reference, the quantity and the date, as a
    careful extractor would quote it (lines joined by single spaces)."""
    lines = [ln.strip() for ln in message.body.splitlines() if ln.strip()]
    around = message.received_at.date()
    want_ref = ref if ref and ref in message.body else None
    best = None
    for i in range(len(lines)):
        for j in range(i, len(lines)):
            text = " ".join(lines[i:j + 1])
            if (want_ref is None or want_ref in text) and grounding.present([text], qty, when, around):
                if best is None or len(text) < len(best):
                    best = text
                break
    return best or next((ln for ln in lines if ref and ref in ln), lines[0])


def scripted_results(sid: str) -> dict[str, ExtractionResult]:
    """Perfect extraction: the scenario's expected claims, written the way a supplier would
    reference them (PO number and item code as text, quotes copied from the message)."""
    msgs = {m.id: m for m in messages(sid)}
    by_msg: dict[str, list[ExtractedClaim]] = defaultdict(list)
    for claim in SCENARIOS[sid]["expected"]["claims"]:
        mid = claim.get("source") or next(iter(msgs))
        msg = msgs[mid]
        po_ref = "PO-" + claim["po_line_id"].removeprefix("POL-").rsplit("-", 1)[0]
        by_msg[mid].append(ExtractedClaim(
            kind=claim["kind"], po_reference=po_ref, item_reference=claim["item_id"],
            stated_qty=claim.get("new_qty"), stated_date=claim.get("new_date"),
            language="ar" if any("؀" <= ch <= "ۿ" for ch in msg.body) else "en",
            confidence=claim.get("confidence", 0.95),
            evidence=[Evidence(message_id=mid, quote=claim.get("quote") or _quote(
                msg, po_ref, claim.get("new_qty"), claim.get("new_date")))],
        ))
    flags = SCENARIOS[sid]["expected"].get("extraction_flags") or {}
    return {mid: ExtractionResult(message_id=mid, claims=by_msg.get(mid, []), **flags.get(mid, {}))
            for mid in {*by_msg, *flags}}
