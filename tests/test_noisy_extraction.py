"""Robustness to extraction errors (no LLM): with claims dropped, misread, garbled or invented
at random, a run may hold and ask, but it must never do something wrong or unsafe.

The full table (every scenario, k = 5) is in docs/evaluation.md; this keeps a fast subset as
a regression test.
"""

import pytest

from sdra.evaluation import NoisyExtractor, run_scenario, safety_violations, wrong_actions
from sdra.intake.extract import ScriptedExtractor
from sdra.scenarios import scripted_results

CASES = ["S01", "S04", "S09", "S10", "S12", "S13", "S19", "S23", "S32", "S33"]


@pytest.mark.parametrize("sid", CASES)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_corrupted_extraction_never_leads_to_a_wrong_action(sid, seed):
    noisy = NoisyExtractor(ScriptedExtractor(scripted_results(sid)), rate=0.3, seed=seed * 7919)
    run = run_scenario(sid, extractor=noisy)
    assert not safety_violations(run), noisy.applied
    assert not wrong_actions(sid, run), (noisy.applied, wrong_actions(sid, run))
