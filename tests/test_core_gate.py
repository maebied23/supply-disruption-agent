"""Day-2 gate: the deterministic core reproduces every scenario's hand-worked answer
when given perfect structured events (the verified claims), with no LLM involved.

Workflow behaviour (holding contradictions, chasing silence, approvals, voiding) is not
tested here; it belongs to the orchestrator and gateway (days 3-4).
"""

from decimal import Decimal
from pathlib import Path

import pytest

from sdra.core.options import Planner
from sdra.evaluation import grade_core
from sdra.pack import load_pack
from sdra.scenarios import SCENARIOS
from sdra.schema import ChangeKind, SupplyChangeEvent
from sdra.store import MemoryStore

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_core_matches_hand_worked_answer(sid):
    assert not grade_core(sid)


def test_penalty_enforcement_can_change_the_choice():
    """Halving enforcement makes the cheaper, less reliable SUP-C best in S01:
    C = 11,250 + 0.30 x 45,000 = 24,750 < B = 30,000 + 0.05 x 45,000 = 32,250."""
    sc = SCENARIOS["S01"]
    overrides = [{"table": "customers", "id": "CUS-HOTEL", "set": {"penalty_enforcement": "0.5"}}]
    pack = load_pack(ROOT / "packs" / sc["pack"], overrides)
    store = MemoryStore(pack.seed)
    at = sc["events"][0]["at"]
    events = [
        SupplyChangeEvent(id=f"EV-{i}", kind=ChangeKind(c["kind"]), observed_at=at, supplier_id="SUP-A",
                          po_line_id=c["po_line_id"], item_id=c["item_id"], new_qty=c.get("new_qty"),
                          new_date=c.get("new_date"), confidence=1.0, source_ids=["MSG-S01-1"], verification="verified")
        for i, c in enumerate(sc["expected"]["claims"])
    ]
    a = Planner(store, pack.policy, events, at).options()
    totals = {o.id: o.expected_total for o in a.options}
    assert totals["OPT-WAIT"] == Decimal("93000.00")      # 45,000 expected penalty + 48,000 goodwill
    assert totals["OPT-SUP-B"] == Decimal("32250.00")
    assert totals["OPT-SUP-C"] == Decimal("24750.00")
    assert a.recommendation.id == "OPT-SUP-C"
    assert a.shortages[0].penalty == Decimal("90000.00")  # reports stay contractual


def test_surplus_units_are_priced_at_standard_cost():
    """S10 phase 2 replaces SUP-B's late 200 (SAR 1,275) with SUP-C (1,181.25). The 200 that
    replace them save 93.75 each; another 200 bought on top are surplus, valued at the
    standard price 1,125, so they cost 56.25 each instead of 'saving' 93.75 (found by the
    regret search, which bought 400 to collect the false saving)."""
    from sdra.core.options import Buy
    from sdra.evaluation import core_inputs

    store, policy, events, now, excluded = core_inputs("S10")
    solver = Planner(store, policy, events, now)
    s = solver.options(excluded).shortages[0]
    c = Buy("OFF-C-1", "SUP-C", 400, Decimal("1181.25"), now.date().replace(day=29))
    option = solver.evaluate(s, [c])
    assert option.incremental == Decimal("-18750.00") + 200 * Decimal("56.25")
    assert option.expected_total == Decimal("88500.00")


def test_partial_delivery_needs_a_date_for_the_rest():
    """Found by the noisy-extraction runs: if only '70 of 150 on time' is known, the other 80
    have no date, so a two-part delivery cannot be promised (it crashed proposal building)."""
    from sdra.evaluation import core_inputs

    store, policy, events, now, excluded = core_inputs("S13")
    partial_only = [e for e in events if e.kind == ChangeKind.PARTIAL_QUANTITY]
    a = Planner(store, policy, partial_only, now).options(excluded)
    assert "no date for the remaining 80 units" in [x.reason for x in a.excluded]
    assert all(o.kind != "partial" for o in a.options)
