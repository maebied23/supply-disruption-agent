"""Opt-in live check: run ClaudeExtractor over every scenario email, verify the claims, and
compare with the hand-labelled expectations. Calls the Claude API (about 12 requests).

    python scripts/live_extraction_check.py [--repeats 3]

Needs credentials (ANTHROPIC_API_KEY, or `ant auth login`). With --repeats N it also
reports pass^N: the share of messages whose verified outcome was right in all N runs.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from sdra.intake.extract import ClaudeExtractor, ExtractionError  # noqa: E402
from sdra.intake.verify import Ledger, verify  # noqa: E402
from scenario_support import SCENARIOS, load, messages  # noqa: E402


def expected_by_message(sid: str) -> dict[str, list[tuple]]:
    msgs = [m.id for m in messages(sid)]
    out = defaultdict(list)
    for c in SCENARIOS[sid]["expected"]["claims"]:
        if c["verification"] == "verified":
            out[c.get("source") or msgs[0]].append((c["kind"], c["po_line_id"], c.get("new_qty"), c.get("new_date")))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    extractor = ClaudeExtractor()
    results = defaultdict(list)  # (sid, message id) -> [ok per run]
    for sid in sorted(SCENARIOS):
        if "phase_1" in SCENARIOS[sid]["expected"]:
            continue  # phase-2 messages reference records created mid-run
        want = expected_by_message(sid)
        for _ in range(args.repeats):
            pack, store = load(sid)
            ledger, final, errors = Ledger(), {}, {}
            for msg in messages(sid):
                try:
                    intake = verify(store, pack.policy, ledger, msg, extractor.extract(msg), msg.received_at)
                except ExtractionError as e:
                    errors[msg.id] = str(e)
                    continue
                final.update({e.id: e for e in intake.events})
                final.update(ledger.events)  # later messages can re-mark earlier claims (conflict)
                for line_id in intake.conflict_lines:  # as the orchestrator's status request would
                    ledger.status_requests[f"OUT-N{len(ledger.status_requests) + 1:04d}"] = line_id
            for msg in messages(sid):  # score final outcomes, not first impressions
                got = errors.get(msg.id) or sorted((e.kind.value, e.po_line_id, e.new_qty, e.new_date)
                                                   for e in final.values()
                                                   if msg.id in e.source_ids and e.verification == "verified")
                ok = got == sorted(want.get(msg.id, []))
                results[(sid, msg.id)].append(ok)
                if not ok:
                    print(f"MISMATCH {sid} {msg.id}\n  want {sorted(want.get(msg.id, []))}\n  got  {got}")
    runs = [ok for oks in results.values() for ok in oks]
    print(f"\nmessages: {len(results)}  runs: {len(runs)}  accuracy: {sum(runs) / len(runs):.0%}")
    if args.repeats > 1:
        print(f"pass^{args.repeats}: {sum(all(o) for o in results.values()) / len(results):.0%}")


if __name__ == "__main__":
    main()
