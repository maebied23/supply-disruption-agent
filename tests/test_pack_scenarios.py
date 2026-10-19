"""Pack integrity, governance resolution, and consistency of the hand-worked scenario answers.

The arithmetic tests recompute each worked answer from the seed and policy, so a change to
either that silently invalidates a scenario fails here. They are not the optimiser.
"""

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest
import yaml

from sdra.governance import MissingFact, classify, classify_bundle
from sdra.pack import load_pack
from sdra.schema import ActionKind, ApprovalDecision, InboundMessage, StockAdjustment

ROOT = Path(__file__).parents[1]
PACK = load_pack(ROOT / "packs" / "riyadh_ac")
SEED, POLICY = PACK.seed, PACK.policy
SCENARIOS = {p.stem[:3]: yaml.safe_load(p.read_text(encoding="utf-8")) for p in (ROOT / "scenarios").glob("S*.yaml")}

by_id = lambda rows: {r.id: r for r in rows}  # noqa: E731
ITEMS, SUPPLIERS, OFFERS = by_id(SEED.items), by_id(SEED.suppliers), by_id(SEED.offers)
SOL, WO, POL = by_id(SEED.sales_order_lines), by_id(SEED.work_orders), by_id(SEED.purchase_order_lines)
CUSTOMERS, ORDERS = by_id(SEED.customers), by_id(SEED.sales_orders)


def sar(money) -> Decimal:
    return money.amount * POLICY.fx_rates_to_base[money.currency]


def facts(**overrides):
    base = dict(
        commitment_base=Decimal(0),
        incremental_cost_base=Decimal(0),
        changes_customer_date=False,
        creates_downstream_shortage=False,
        item_critical=True,
        supplier_approved_for_item=True,
        within_mandate=True,
        requires_customer_consent=False,
        customer_consent_recorded=False,
        template_only=True,
        hard_allocation=False,
    )
    return base | overrides


# --- pack ------------------------------------------------------------------------


def test_pack_loads_and_roles_are_seniority_ordered():
    assert list(POLICY.roles)[0] == "operations_director"
    assert {r for a in SEED.approvers for r in a.roles} == set(POLICY.roles)


def test_running_example_premise():
    """Before the event, the hotel line is exactly covered by stock + PO-4471."""
    stock = next(s.on_hand for s in SEED.stock if s.item_id == "CMP-24")
    assert stock + POL["POL-4471-1"].open_qty == SOL["SOL-H-1001-1"].qty == WO["WO-1"].qty
    assert POL["POL-4471-1"].expected_date <= WO["WO-1"].start_date
    assert WO["WO-1"].due_date <= SOL["SOL-H-1001-1"].promised_date
    assert WO["WO-1"].start_date + timedelta(days=ITEMS["AC-24"].assembly_lead_days) == WO["WO-1"].due_date


def test_hotel_cannot_use_substitute():
    sub = next(s for s in SEED.substitutions if s.id == "SUB-1")
    hotel = ORDERS[SOL["SOL-H-1001-1"].sales_order_id].customer_id
    assert hotel not in sub.customer_ids


# --- governance --------------------------------------------------------------------


def test_backup_po_is_dual_with_two_senior_roles():
    c = classify(POLICY, ActionKind.CREATE_PO, facts(commitment_base=Decimal(255000), incremental_cost_base=Decimal(30000)))
    assert (c.tier, c.roles) == ("dual", ["operations_director", "procurement_manager"])


def test_po_decrease_needs_procurement_only():
    c = classify(POLICY, ActionKind.UPDATE_PO_QUANTITY, facts())
    assert (c.tier, c.roles) == ("approve", ["procurement_manager"])


def test_template_supplier_message_executes():
    assert classify(POLICY, ActionKind.SEND_SUPPLIER_MESSAGE, facts()).tier == "execute"


def test_bundle_takes_one_signer_per_kind_of_risk():
    big_po = classify(POLICY, ActionKind.CREATE_PO, facts(commitment_base=Decimal(255000)))
    date_move = classify(POLICY, ActionKind.UPDATE_SO_PROMISE_DATE, facts(changes_customer_date=True))
    assert date_move.roles == ["sales_manager", "planner"]
    bundle = classify_bundle(POLICY, [big_po, date_move])
    assert (bundle.tier, bundle.roles) == ("dual", ["operations_director", "sales_manager"])


def test_same_risk_rules_keep_their_two_roles():
    both_spend_rules = classify(
        POLICY, ActionKind.CREATE_PO, facts(commitment_base=Decimal(255000), incremental_cost_base=Decimal(30000))
    )
    assert both_spend_rules.roles == ["operations_director", "procurement_manager"]


def test_blocked_beats_everything():
    c = classify(POLICY, ActionKind.CREATE_PO, facts(commitment_base=Decimal(255000), within_mandate=False))
    assert (c.tier, c.roles) == ("blocked", [])


def test_missing_fact_fails_closed():
    f = facts()
    del f["within_mandate"]
    with pytest.raises(MissingFact):
        classify(POLICY, ActionKind.CREATE_PO, f)


def test_unmatched_action_uses_default():
    c = classify(POLICY, ActionKind.UPDATE_PO_EXPECTED_DATE, facts(item_critical=False, creates_downstream_shortage=True))
    assert (c.tier, c.roles, c.rule_ids) == ("approve", ["planner"], ["default"])


# --- scenario files -----------------------------------------------------------------


CREATED_ID = re.compile(r"^[A-Z]+-N\d{4}(-\d+)?$")  # ids the agent assigns during a run


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_scenario_inputs_are_valid(sid):
    sc = SCENARIOS[sid]
    pack = load_pack(ROOT / "packs" / sc["pack"], sc.get("seed_overrides"))
    approvers = {a.id for a in pack.seed.approvers}
    po_lines = {line.id for line in pack.seed.purchase_order_lines}
    times = [e["at"] for e in sc["events"]]
    assert times == sorted(times)
    assert all(sc["start"] <= t <= sc["end"] for t in times)
    for e in sc["events"]:
        match e["type"]:
            case "email":
                InboundMessage.model_validate(e["message"])
                assert e["message"]["received_at"] == e["at"]
            case "stock_adjustment":
                StockAdjustment.model_validate(e["adjustment"])
            case "approval":  # unknown approvers only where the scenario expects them to be ignored
                assert e["approver_id"] in approvers or (sc["expected"].get("audit_events") or {}).get(
                    "decision_ignored"), e
                ApprovalDecision.model_validate(
                    {k: v for k, v in e.items() if k not in ("at", "type")}
                    | {"request_id": "REQ", "role": "x", "decided_at": e["at"]}
                )
            case other:
                pytest.fail(f"unknown event type {other}")
    for claim in sc["expected"]["claims"]:
        assert claim["po_line_id"] in po_lines or CREATED_ID.match(claim["po_line_id"])
        assert claim["item_id"] in {i.id for i in pack.seed.items}
    for ref in sc["expected"].values():
        if isinstance(ref, dict) and "same_as" in ref:
            assert ref["same_as"] in SCENARIOS
    if "same_as" in sc["expected"]:
        assert sc["expected"]["same_as"] in SCENARIOS


# --- worked-answer arithmetic ------------------------------------------------------------
# Recomputes each scenario's numbers from its (overridden) seed and the policy.


ORDER_DATE = date(2026, 11, 3)


class Calc:
    def __init__(self, sid: str):
        self.sc = SCENARIOS[sid]
        self.pack = load_pack(ROOT / "packs" / self.sc["pack"], self.sc.get("seed_overrides"))
        seed, self.policy = self.pack.seed, self.pack.policy
        self.offers, self.suppliers = by_id(seed.offers), by_id(seed.suppliers)
        hotel = by_id(seed.customers)["CUS-HOTEL"]
        self.penalty = self.sar(hotel.default_penalty.amount)
        self.contract = self.sar(
            next(si.contract_price for si in seed.supplier_items if (si.supplier_id, si.item_id) == ("SUP-A", "CMP-24"))
        )
        self.goodwill_tier1 = self.policy.options.scoring.goodwill_cost_per_late_day_by_tier[1]
        self.promised = by_id(seed.sales_order_lines)["SOL-H-1001-1"].promised_date
        self.assembly = timedelta(days=by_id(seed.items)["AC-24"].assembly_lead_days)

    def sar(self, money) -> Decimal:
        return money.amount * self.policy.fx_rates_to_base[money.currency]

    def unit(self, offer_id) -> Decimal:
        return self.sar(self.offers[offer_id].unit_price)

    def premium(self, offer_id, qty) -> Decimal:
        return (self.unit(offer_id) - self.contract) * qty

    def risk(self, *supplier_ids) -> Decimal:
        p_on_time = Decimal(1)
        for s in supplier_ids:
            p_on_time *= self.suppliers[s].on_time_rate
        return self.penalty * (1 - p_on_time)

    def late_cost(self, components_on: date) -> Decimal:
        late_days = max((components_on + self.assembly - self.promised).days, 0)
        return (self.penalty if late_days else 0) + self.goodwill_tier1 * late_days

    def arrival(self, offer_id, ordered=ORDER_DATE) -> date:
        return ordered + timedelta(days=self.offers[offer_id].lead_time_days)

    def ceiling(self, next_best_total, own_risk, qty) -> Decimal:
        margin = 1 - self.policy.mandate.safety_margin_pct / 100
        per_unit = (margin * (next_best_total - own_risk) / qty).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        return self.contract + per_unit

    def options(self) -> dict[str, Decimal]:
        return {o["id"]: Decimal(str(o["expected_total"])) for o in self.sc["expected"]["options"]}

    def mandate_price(self) -> Decimal:
        return Decimal(self.sc["expected"]["mandate"]["max_unit_price"]["amount"])


REMAINDER = date(2026, 12, 20)


def assert_ranked(opts):
    assert list(opts) == sorted(opts, key=opts.get), "options must be listed best first"


def test_s01():
    c = Calc("S01")
    opts = c.options()
    assert opts["OPT-B"] == c.premium("OFF-B-1", 200) + c.risk("SUP-B")
    assert opts["OPT-C"] == c.premium("OFF-C-1", 200) + c.risk("SUP-C")
    assert opts["OPT-BC"] == c.premium("OFF-B-1", 50) + c.premium("OFF-C-1", 150) + c.risk("SUP-B", "SUP-C")
    assert opts["OPT-WAIT"] == c.late_cost(REMAINDER) == 138000
    assert_ranked(opts)
    assert c.mandate_price() == c.ceiling(opts["OPT-C"], c.risk("SUP-B"), 200)
    assert c.unit("OFF-B-1") <= c.mandate_price()
    assert c.arrival("OFF-B-1") <= WO["WO-1"].start_date
    assert c.arrival("OFF-C-1") == WO["WO-1"].start_date  # zero slack, flagged in S03


def test_s03():
    c = Calc("S03")
    opts = c.options()
    assert set(opts) == {"OPT-C", "OPT-WAIT"}  # every SUP-B option removed
    assert_ranked(opts)
    assert c.mandate_price() == c.ceiling(opts["OPT-WAIT"], c.risk("SUP-C"), 200)
    assert c.unit("OFF-C-1") <= c.mandate_price()


def test_s06_boundary():
    c = Calc("S06")
    opts = c.options()
    commitment = c.unit("OFF-B-1") * 40
    assert commitment == 50000
    assert opts["OPT-B"] == c.premium("OFF-B-1", 40) + c.risk("SUP-B")
    assert opts["OPT-WAIT"] == c.late_cost(REMAINDER)
    assert c.offers["OFF-C-1"].valid_until < datetime.fromisoformat(c.sc["start"].isoformat())
    assert c.mandate_price() == c.ceiling(opts["OPT-WAIT"], c.risk("SUP-B"), 40)
    cls = classify(c.policy, ActionKind.CREATE_PO, facts(commitment_base=commitment, incremental_cost_base=Decimal(5000)))
    assert cls.tier == "dual"
    below = classify(c.policy, ActionKind.CREATE_PO, facts(commitment_base=Decimal("49999.99")))
    assert below.tier == "approve"


def test_s07_best_option_out_of_mandate():
    c = Calc("S07")
    opts = c.options()
    assert opts["OPT-B"] == c.premium("OFF-B-1", 200) + c.risk("SUP-B")
    assert opts["OPT-BC"] == c.premium("OFF-B-1", 50) + c.premium("OFF-C-1", 150) + c.risk("SUP-B", "SUP-C")
    assert_ranked(opts)
    assert c.mandate_price() == c.ceiling(opts["OPT-C"], c.risk("SUP-B"), 200)
    assert c.unit("OFF-B-1") > c.mandate_price()  # blocked
    blocked = classify(c.policy, ActionKind.CREATE_PO, facts(commitment_base=c.unit("OFF-B-1") * 200, within_mandate=False))
    assert blocked.tier == "blocked"


def test_s09_replan_after_void():
    c = Calc("S09")
    adj = next(e["adjustment"] for e in c.sc["events"] if e["type"] == "stock_adjustment")
    old = next(s.on_hand for s in c.pack.seed.stock if s.item_id == "CMP-24")
    change_pct = abs(adj["new_on_hand"] - old) * 100 / old
    assert change_pct >= c.policy.approvals.void_if_changed.stock_on_hand_pct
    short = WO["WO-1"].qty - adj["new_on_hand"] - 300
    assert short == c.sc["expected"]["shortage"][0]["short_qty"] == 150
    opts = c.options()
    assert opts["OPT-B"] == c.premium("OFF-B-1", 150) + c.risk("SUP-B")
    assert opts["OPT-C"] == c.premium("OFF-C-1", 150) + c.risk("SUP-C")
    assert opts["OPT-BC"] == c.premium("OFF-B-1", 50) + c.premium("OFF-C-1", 100) + c.risk("SUP-B", "SUP-C")
    assert_ranked(opts)
    assert c.mandate_price() == c.ceiling(opts["OPT-C"], c.risk("SUP-B"), 150)
    assert c.unit("OFF-B-1") <= c.mandate_price()


def test_s10_backup_slip():
    c = Calc("S10")
    opts = c.options()
    slipped = date(2026, 11, 28)
    assert opts["OPT-WAIT2"] == c.late_cost(slipped) == 94000
    c_arrival = c.arrival("OFF-C-1", ordered=date(2026, 11, 8))
    assert c.offers["OFF-C-1"].valid_until.date() >= date(2026, 11, 8)
    assert c.offers["OFF-B-1"].valid_until.date() < date(2026, 11, 8)  # no fresh SUP-B quote
    baseline = c.unit("OFF-B-1")  # PO-N0001 was placed at SUP-B's quote; it is the supply displaced
    incremental = (c.unit("OFF-C-1") - baseline) * 200
    assert incremental == -18750
    assert opts["OPT-C2"] == c.late_cost(c_arrival) + incremental == 77250
    assert_ranked(opts)
    headroom = opts["OPT-WAIT2"] - c.late_cost(c_arrival)
    ceiling = headroom - c.policy.mandate.safety_margin_pct / 100 * abs(headroom)
    assert c.mandate_price() == baseline + (ceiling / 200).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    assert c.unit("OFF-C-1") <= c.mandate_price()
    wo1 = c.sc["expected"]["final_state"]["changed"]["work_orders"]["WO-1"]
    assert wo1["start_date"] == c_arrival and wo1["due_date"] == c_arrival + c.assembly
    bundle = classify_bundle(
        c.policy,
        [
            classify(c.policy, ActionKind.CANCEL_PO_LINE, facts()),
            classify(c.policy, ActionKind.CREATE_PO, facts(commitment_base=c.unit("OFF-C-1") * 200, incremental_cost_base=incremental)),
            classify(c.policy, ActionKind.RESCHEDULE_WORK_ORDER, facts()),
            classify(c.policy, ActionKind.UPDATE_SO_PROMISE_DATE, facts(changes_customer_date=True)),
            classify(c.policy, ActionKind.SEND_CUSTOMER_MESSAGE, facts()),
        ],
    )
    assert (bundle.tier, bundle.roles) == ("dual", ["operations_director", "sales_manager"])
    assert c.sc["expected"]["approval_requests"][1]["roles"] == list(bundle.roles)
