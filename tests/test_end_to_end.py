"""Scenario suite, end to end: clock -> intake -> verification -> incident graph -> approvals
-> execution, with a scripted (perfect) extractor in place of the LLM.

Graded by sdra.evaluation, component by component: final record state (the primary grade),
the full outbox, review tasks, approval requests, blocked actions, clock events, claim
verification, and the safety checks that need no expectations.
"""

import pytest

from sdra.agent.orchestrator import Runner
from sdra.evaluation import COMPONENTS, grade, grade_final_state, grade_outbox, run_scenario
from sdra.intake.extract import ScriptedExtractor
from sdra.schema import InboundMessage
from scenario_support import SCENARIOS, scripted_results


def check_final_state(sid, runner, pack):
    assert not grade_final_state(sid, runner, pack)


def check_outbox(sid, runner):
    assert not grade_outbox(sid, runner)


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_scenario_end_to_end(sid):
    g = grade(run_scenario(sid))
    failures = {c: g.failures[c] for c in COMPONENTS if c != "core" and g.failures[c]}
    assert not failures, failures


def test_rejection_exclusion_reaches_the_replan():
    """S03 with SUP-C at USD 360: after rejecting OPT-SUP-B, the B+C split (67,650) would beat
    SUP-C alone (72,000) unless the approver's exclusion of SUP-B is honoured."""
    from sdra.pack import load_pack
    from sdra.store import MemoryStore
    from scenario_support import ROOT

    sc = SCENARIOS["S03"]
    pack = load_pack(ROOT / "packs" / sc["pack"],
                     [{"table": "offers", "id": "OFF-C-1", "set": {"unit_price": {"amount": "360.00", "currency": "USD"}}}])
    runner = Runner(pack, MemoryStore(pack.seed), ScriptedExtractor(scripted_results("S03")), sc["start"])
    for e in sc["events"]:
        if e["type"] == "email":
            runner.schedule(e["at"], "email", InboundMessage.model_validate(e["message"]))
        else:
            runner.schedule(e["at"], "approval", e)
    runner.run_until(sc["end"])
    assert [(r.option_id, r.decided) for r in runner.requests] == [("OPT-SUP-B", "rejected"), ("OPT-SUP-C", "approved")]
    assert "SUP-B" not in {m.to for m in runner.outbox}
