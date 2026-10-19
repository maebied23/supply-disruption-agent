from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from sdra.policy import Policy, load_policy
from sdra.schema import (
    ActionParams,
    ChangeKind,
    ExtractedClaim,
    Money,
    PurchaseOrderLine,
    Substitution,
    SupplyChangeEvent,
)

PACK = Path(__file__).parents[1] / "packs" / "riyadh_ac"


def raw_policy() -> dict:
    return yaml.safe_load((PACK / "policy.yaml").read_text(encoding="utf-8"))


def test_pack_policy_loads():
    policy = load_policy(PACK / "policy.yaml")
    assert policy.company.base_currency == "SAR"
    assert policy.fx_rates_to_base["USD"] == Decimal("3.75")


def test_unknown_condition_is_rejected():
    data = raw_policy()
    data["autonomy"]["rules"][0]["when"]["item_critcal"] = False  # typo
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_dual_tier_needs_two_roles():
    data = raw_policy()
    rule = next(r for r in data["autonomy"]["rules"] if r["tier"] == "dual")
    rule["approvers"] = rule["approvers"][:1]
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_undefined_role_is_rejected():
    data = raw_policy()
    data["autonomy"]["default"]["approvers"] = ["cfo"]
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_revalidation_cannot_be_disabled():
    data = raw_policy()
    data["approvals"]["revalidate_on_resume"] = False
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_action_params_discriminate_and_reject_unknown_kinds():
    adapter = TypeAdapter(ActionParams)
    action = adapter.validate_python(
        {"kind": "update_po_expected_date", "po_line_id": "POL-1", "new_date": "2026-11-20"}
    )
    assert action.new_date == date(2026, 11, 20)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "change_payment_terms", "po_line_id": "POL-1"})


def test_extracted_claim_needs_evidence():
    with pytest.raises(ValidationError):
        ExtractedClaim(kind="partial_quantity", stated_qty=300, language="en", confidence=0.9, evidence=[])


def test_verified_delay_needs_new_date():
    with pytest.raises(ValidationError):
        SupplyChangeEvent(
            id="EV-1",
            kind=ChangeKind.DELAY,
            observed_at=datetime(2026, 11, 1, 9),
            supplier_id="SUP-1",
            po_line_id="POL-1",
            item_id="ITM-1",
            confidence=0.95,
            source_ids=["MSG-1"],
            verification="verified",
        )


def test_po_line_open_qty():
    line = PurchaseOrderLine(
        id="POL-1",
        purchase_order_id="PO-1",
        item_id="ITM-1",
        qty_ordered=500,
        qty_received=200,
        unit_price=Money(amount=Decimal("320.00"), currency="USD"),
        expected_date=date(2026, 11, 10),
    )
    assert line.open_qty == 300


def test_substitution_cannot_be_self():
    with pytest.raises(ValidationError):
        Substitution(id="S-1", item_id="ITM-1", substitute_item_id="ITM-1", ratio=Decimal("1"))
