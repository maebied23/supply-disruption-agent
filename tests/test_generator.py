"""Generated packs and scenarios are current, and their messages agree with their labels."""

import importlib.util
from sdra.agent.leaks import check_message
from sdra.generator import compose_email
from sdra.intake.grounding import check
from sdra.schema import ActionKind, SendMessage
from scenario_support import ROOT, SCENARIOS, load, messages


def _generate_module():
    spec = importlib.util.spec_from_file_location("generate", ROOT / "scripts" / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_files_are_current():
    stale = [str(p.relative_to(ROOT)) for p, text in _generate_module().outputs().items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert not stale, f"run scripts/generate.py: {stale}"


def test_composed_messages_state_their_claims():
    """Every verified claim of a generated scenario can be read from its own message."""
    checked = 0
    generated = {p.name[:3] for p in (ROOT / "scenarios").glob("S*.yaml") if p.read_text().startswith("# GENERATED")}
    for sid, sc in SCENARIOS.items():
        if sid not in generated:
            continue
        bodies = {m.id: m.body for m in messages(sid)}
        for c in sc["expected"]["claims"]:
            if c["verification"] == "verified" and c.get("source") in bodies:
                assert not check([bodies[c["source"]]], c.get("new_qty"), c.get("new_date"),
                                 next(m.received_at.date() for m in messages(sid) if m.id == c["source"])), (sid, c)
                checked += 1
    assert checked > 20


def test_phrasing_is_deterministic_and_varied():
    facts = {"facts": [{"kind": "delay", "po": "PO-1", "item": "X-1", "qty": 5, "new_date": "2027-01-04"}]}
    assert compose_email("S99", 0, facts) == compose_email("S99", 0, facts)
    assert len({compose_email(f"S{n}", 0, facts)[1] for n in range(20)}) > 3


def test_template_quantities_are_not_read_as_amounts_but_free_text_is():
    """A PO-change template saying '200' is a quantity; the same number in free text is checked."""
    pack, store = load("S01")
    secrets = {"internal_cost": ["200.00", "200"]}
    body = "PO-4471: quantity of CMP-24 reduced from 500 to 300. The 200 units you advised as late are cancelled."
    template = SendMessage(kind=ActionKind.SEND_SUPPLIER_MESSAGE, recipient_id="SUP-A", template_id="po_change",
                           subject="Change", body=body, record_quantities=[500, 300, 200])
    free = template.model_copy(update={"template_id": None})
    assert check_message(store, pack.policy, template, secrets) == []
    assert check_message(store, pack.policy, free, secrets) == ["internal_cost: 200.00"]
