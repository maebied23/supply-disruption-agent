"""Day-4 controls: action gateway (authority, caps, leak check), approval expiry, voiding by
new supplier information, re-plan cap, PO confirmation watch, audit chain and restart."""

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from sdra.agent import templates
from sdra.agent.gateway import Authority, Gateway, Refused
from sdra.agent.leaks import check_message, secret_amounts
from sdra.agent.orchestrator import Runner
from sdra.audit import AuditLog, verify_chain
from sdra.core.options import Planner
from sdra.intake.extract import ScriptedExtractor
from sdra.intake.verify import Ledger, verify
from sdra.schema import (
    ActionKind, ApprovalDecision, Evidence, ExtractedClaim, ExtractionResult, InboundMessage, ProposedAction,
    SendMessage,
)
from sdra.sqlite_store import SqliteStore
from sdra.store import MemoryStore
from scenario_support import SCENARIOS, load, scripted_results
from test_end_to_end import check_final_state, check_outbox

S01 = SCENARIOS["S01"]
T = S01["start"]


def at(hours: float) -> datetime:
    return T + timedelta(hours=hours)


def make_runner(sid="S01", pack=None, store=None, extra_results=None, checkpointer=None):
    sc = SCENARIOS[sid]
    if pack is None:
        pack, store = load(sid)
    results = scripted_results(sid) | (extra_results or {})
    return Runner(pack, store or MemoryStore(pack.seed), ScriptedExtractor(results), sc["start"],
                  checkpointer=checkpointer)


def feed(runner, sid="S01", skip_approvals=(), extra=()):
    for i, e in enumerate(SCENARIOS[sid]["events"]):
        if e["type"] == "email":
            runner.schedule(e["at"], "email", InboundMessage.model_validate(e["message"]))
        elif i not in skip_approvals:
            runner.schedule(e["at"], "approval", e)
    for when, kind, payload in extra:
        runner.schedule(when, kind, payload)


def email(mid, sender, body, when, subject="update", in_reply_to=None):
    return InboundMessage(id=mid, channel="email", sender=sender, received_at=when, subject=subject, body=body,
                          in_reply_to=in_reply_to)


def claim_result(msg, kind, po, item, quote, qty=None, new_date=None):
    return {msg.id: ExtractionResult(message_id=msg.id, claims=[ExtractedClaim(
        kind=kind, po_reference=po, item_reference=item, stated_qty=qty, stated_date=new_date, language="en",
        confidence=0.95, evidence=[Evidence(message_id=msg.id, quote=quote)])])}


def with_policy(pack, **sections):
    policy = pack.policy.model_copy(update={k: getattr(pack.policy, k).model_copy(update=v) for k, v in sections.items()})
    return pack.model_copy(update={"policy": policy})


# --- leak check --------------------------------------------------------------------------

@pytest.fixture
def s01_analysis():
    pack, store = load("S01")
    ledger = Ledger()
    msg = InboundMessage.model_validate(S01["events"][0]["message"])
    verify(store, pack.policy, ledger, msg, scripted_results("S01")[msg.id], msg.received_at)
    analysis = Planner(store, pack.policy, ledger.verified(), msg.received_at).options()
    return pack, store, analysis


def supplier_msg(to, body):
    return SendMessage(kind=ActionKind.SEND_SUPPLIER_MESSAGE, recipient_id=to, template_id=None, subject="x", body=body)


@pytest.mark.parametrize("body, category", [
    ("Our ceiling is SAR 1,276.87 per unit.", "mandate"),
    ("We could go to USD 340.50.", "mandate"),
    ("The hotel would charge us 90000 SAR.", "penalty_terms"),
    ("Waiting would cost us 138,000.", "internal_cost"),
    ("Another supplier quoted 315.00 USD.", "other_supplier_offers"),
    ("Can you match SUP-C?", "other_supplier_offers"),
    ("This is for the Hotel chain (fictional) order SO-H-1001.", "customer_identity_to_suppliers"),
])
def test_leak_check_catches_each_category(s01_analysis, body, category):
    pack, store, analysis = s01_analysis
    reasons = check_message(store, pack.policy, supplier_msg("SUP-B", body), secret_amounts(store, pack.policy, analysis))
    assert any(r.startswith(category) for r in reasons), reasons


def test_leak_check_passes_the_real_templates(s01_analysis):
    pack, store, analysis = s01_analysis
    secrets = secret_amounts(store, pack.policy, analysis)
    po = templates.purchase_order("PO-N0001", "CMP-24", 200, store.get("offers", "OFF-B-1").unit_price,
                                  datetime(2026, 11, 10).date(), "Riyadh assembly plant")
    change = templates.po_change("PO-4471", "CMP-24", 500, 300)
    assert check_message(store, pack.policy, supplier_msg("SUP-B", po[1]), secrets) == []
    assert check_message(store, pack.policy, supplier_msg("SUP-A", change[1]), secrets) == []
    notice = SendMessage(kind=ActionKind.SEND_CUSTOMER_MESSAGE, recipient_id="CUS-HOTEL", subject="x",
                         body=templates.delivery_date_change("SO-H-1001", "AC-24", 600, datetime(2026, 12, 4).date())[1])
    assert check_message(store, pack.policy, notice, secrets) == []
    named = notice.model_copy(update={"body": notice.body + " Our supplier SUP-B was delayed."})
    assert check_message(store, pack.policy, named, secrets) == ["supplier_identity_to_customers: SUP-B"]


def test_leaking_template_is_refused_before_anyone_signs(monkeypatch):
    real = templates.purchase_order
    monkeypatch.setattr(templates, "purchase_order",
                        lambda *a: (real(*a)[0], real(*a)[1] + "\nOur budget allows up to 1276.87 SAR."))
    runner = make_runner()
    feed(runner)
    runner.run_until(S01["end"])
    assert runner.requests == [] and runner.outbox == []
    (task,) = runner.review_tasks
    assert task.assignee_role == "operations_director" and "mandate: 1276.87" in task.reason
    assert runner.store.get("purchase_order_lines", "POL-4471-1").qty_ordered == 500


# --- authority ---------------------------------------------------------------------------

@pytest.fixture
def gateway():
    pack, store = load("S01")
    return Gateway(store, pack.policy, AuditLog(), [])


def sig(approver, role, decision="approve", request="REQ-N0001"):
    return ApprovalDecision(request_id=request, approver_id=approver, role=role, decision=decision, decided_at=T)


def notice_action():
    return ProposedAction(id="A1", incident_id="INC-1", idempotency_key="INC-1:1:1:x", params=SendMessage(
        kind=ActionKind.SEND_SUPPLIER_MESSAGE, recipient_id="SUP-A", template_id="status_request", subject="s", body="b"))


@pytest.mark.parametrize("signatures, expires, problem", [
    ([sig("APR-2", "procurement_manager")], None, "missing signatures: operations_director"),
    ([sig("APR-2", "procurement_manager"), sig("APR-2", "operations_director")], None, "one person signed twice"),
    ([sig("APR-2", "procurement_manager"), sig("APR-3", "operations_director")], None, "APR-3 does not hold role"),
    ([sig("APR-2", "procurement_manager"), sig("APR-4", "operations_director", request="REQ-N0009")], None,
     "missing signatures: operations_director"),
    ([sig("APR-2", "procurement_manager"), sig("APR-4", "operations_director")], T - timedelta(hours=1), "expired"),
])
def test_gateway_refuses_without_full_authority(gateway, signatures, expires, problem):
    auth = Authority(tier="dual", roles=["operations_director", "procurement_manager"], request_id="REQ-N0001",
                     signatures=signatures, expires_at=expires)
    with pytest.raises(Refused) as e:
        gateway.execute("INC-1", [notice_action()], auth, T)
    assert any(problem in r for r in e.value.reasons), e.value.reasons
    assert gateway.executor.outbox == [] and gateway.audit.of("execution_refused")


def test_gateway_refuses_blocked_and_misclassified_autonomous_actions(gateway):
    with pytest.raises(Refused):
        gateway.execute("INC-1", [notice_action()], Authority(tier="blocked", roles=[]), T)
    with pytest.raises(Refused) as e:  # free-text supplier message claimed as execute tier
        gateway.execute("INC-1", [notice_action()], Authority(tier="execute", roles=[]), T,
                        autonomous_facts=[{"template_only": False}])
    assert "needs approve" in e.value.reasons[0]


def test_gateway_runs_each_action_once(gateway):
    auth = Authority(tier="execute", roles=[])
    gateway.execute("INC-1", [notice_action()], auth, T, autonomous_facts=[{"template_only": True}])
    gateway.execute("INC-1", [notice_action()], auth, T, autonomous_facts=[{"template_only": True}])
    assert len(gateway.executor.outbox) == 1 and gateway.usage.actions["INC-1"] == 1


# --- caps --------------------------------------------------------------------------------

def test_commitment_cap_escalates_instead_of_asking_for_signatures():
    pack, _ = load("S01")
    pack = with_policy(pack, caps={"per_incident_commitment_base": Decimal(200000)})
    runner = make_runner(pack=pack, store=MemoryStore(pack.seed))
    feed(runner)
    runner.run_until(S01["end"])
    assert runner.requests == [] and runner.outbox == []
    (task,) = runner.review_tasks
    assert "per_incident_commitment_base: 255000.00 > 200000" in task.reason


def test_replan_cap_escalates():
    pack, _ = load("S03")
    pack = with_policy(pack, caps={"max_replans_per_incident": 0})
    runner = make_runner("S03", pack=pack, store=MemoryStore(pack.seed))
    feed(runner, "S03")
    runner.run_until(SCENARIOS["S03"]["end"])
    assert [r.decided for r in runner.requests] == ["rejected"]
    assert runner.incidents["INC-1"].status == "escalated"
    assert any("max_replans_per_incident" in t.reason for t in runner.review_tasks)


# --- expiry and voiding ---------------------------------------------------------------------

def test_unsigned_request_reminds_then_expires():
    runner = make_runner()
    feed(runner, skip_approvals={2})  # APR-4 never signs
    runner.run_until(at(30))
    (req,) = runner.requests
    assert req.decided == "expired" and not runner.pending
    assert [n["roles"] for n in runner.notifications] == [["operations_director"]]
    assert runner.notifications[0]["at"] == (req.created_at + timedelta(hours=4)).isoformat()
    (task,) = runner.review_tasks
    assert task.assignee_role == "operations_director" and "expired after 24 h" in task.reason
    assert runner.outbox == [] and runner.incidents["INC-1"].status == "escalated"


def test_contradicting_message_while_pending_voids_the_approval():
    """Same contact changes its story between the two signatures: the claims are held as a
    conflict, the second signature finds the basis changed, nothing executes, we ask."""
    msg = email("MSG-X", "sales@cmp-a.example", "Correction: the remaining 200 units of CMP-24 on PO-4471 "
                "will follow on 15 December.", at(2.75))
    runner = make_runner(extra_results=claim_result(msg, "delay", "PO-4471", "CMP-24", "Correction: the remaining "
                                                    "200 units of CMP-24 on PO-4471 will follow on 15 December", qty=200,
                                                    new_date=datetime(2026, 12, 15).date()))
    feed(runner, extra=[(msg.received_at, "email", msg)])
    runner.run_until(S01["end"])
    (req,) = runner.requests
    assert req.decided == "voided" and any("supplier_information" in r for r in req.void_reasons)
    assert [(m.to, m.template) for m in runner.outbox] == [("SUP-A", "status_request")]
    assert runner.store.get("purchase_order_lines", "POL-4471-1").qty_ordered == 500


def test_new_information_on_another_line_of_the_item_voids_and_replans():
    msg = email("MSG-Y", "sales@cmp-a.example", "PO-4480: the 150 units of CMP-24 will arrive on 5 December.",
                at(2.75))
    runner = make_runner(extra_results=claim_result(msg, "delay", "PO-4480", "CMP-24", "PO-4480: the 150 units of CMP-24 will arrive on 5 December",
                                                    qty=150, new_date=datetime(2026, 12, 5).date()))
    feed(runner, skip_approvals={2}, extra=[(msg.received_at, "email", msg)])
    runner.run_until(at(3))
    first, second = runner.requests
    assert first.decided == "voided" and "new supplier information" in first.void_reasons[0]
    assert second.decided is None and list(runner.pending) == [second.id]
    assert len(runner.incidents) == 1 and runner.incidents["INC-1"].replan_count == 1


def test_signer_without_an_outstanding_role_is_ignored():
    runner = make_runner()
    feed(runner, skip_approvals={1, 2})
    runner.run_until(at(2))
    runner.schedule(at(2.1), "approval", {"approver_id": "APR-3", "decision": "approve"})  # sales_manager
    runner.run_until(at(2.2))
    (req,) = runner.requests
    assert req.signatures == [] and req.id in runner.pending
    assert runner.audit.of("decision_ignored")[0].after["reason"].startswith("holds none of")


# --- PO confirmation watch -----------------------------------------------------------------

def test_new_po_is_chased_when_unconfirmed():
    runner = make_runner()
    feed(runner)
    runner.run_until(at(48))
    chases = [(m.to, m.at) for m in runner.outbox if m.template == "status_request"]
    assert chases == [("SUP-B", at(3.25 + 24))]  # placed 11:15; chased 24 h later, not at once


def test_confirmation_is_recorded_and_stops_the_chase():
    msg = email("MSG-C", "orders@cmp-b.example", "We confirm PO-N0001: 200 units of CMP-24 for 10 November.", at(6))
    runner = make_runner(extra_results=claim_result(msg, "confirmation", "PO-N0001", "CMP-24", "We confirm PO-N0001: 200 units of CMP-24 for 10 November",
                                                    qty=200, new_date=datetime(2026, 11, 10).date()))
    feed(runner, extra=[(msg.received_at, "email", msg)])
    runner.run_until(at(72))
    assert runner.store.get("purchase_order_lines", "POL-N0001-1").confirmed_date == datetime(2026, 11, 10).date()
    assert not [m for m in runner.outbox if m.template == "status_request"]
    assert len(runner.requests) == 1  # a matching confirmation does not re-plan


# --- audit -------------------------------------------------------------------------------

def test_audit_chain_detects_edits_and_deletions():
    runner = make_runner()
    feed(runner)
    runner.run_until(S01["end"])
    entries = runner.audit.entries
    assert not verify_chain(entries)
    events = [e.event for e in entries]
    for expected in ["message_received", "claims_checked", "approval_requested", "approval_decided",
                     "record_changed", "message_sent", "plan_executed"]:
        assert expected in events
    sent = next(i for i, e in enumerate(entries) if e.event == "message_sent")
    edited = entries[sent].model_copy(update={"after": entries[sent].after | {"body": "something else"}})
    assert verify_chain(entries[:sent] + [edited] + entries[sent + 1:]) == [f"entry {sent}: content does not match its hash"]
    assert verify_chain(entries[:sent] + entries[sent + 1:])


# --- persistence and restart ------------------------------------------------------------------

def test_sqlite_restart_mid_approval_finishes_the_same(tmp_path):
    pack, _ = load("S01")
    db, ckpt = tmp_path / "records.db", tmp_path / "checkpoints.db"
    store = SqliteStore(db, pack.seed)
    runner = make_runner(pack=pack, store=store, checkpointer=SqliteSaver(sqlite3.connect(ckpt, check_same_thread=False)))
    feed(runner)
    runner.run_until(S01["end"], stop=lambda: bool(runner.requests and runner.requests[0].signatures))
    assert runner.now == at(2.5) and len(runner.pending) == 1  # APR-2 signed; APR-4 still to come
    store.close()
    runner = None  # the first process is gone; only the files remain

    store = SqliteStore(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("DELETE FROM audit WHERE seq = 0")
    resumed = Runner.resume(pack, store, ScriptedExtractor(scripted_results("S01")),
                            checkpointer=SqliteSaver(sqlite3.connect(ckpt, check_same_thread=False)))
    resumed.run_until(S01["end"])  # APR-4's approval is still in the saved event queue
    check_final_state("S01", resumed, pack)
    check_outbox("S01", resumed)
    assert resumed.requests[0].decided == "approved" and not resumed.pending
    assert not verify_chain(SqliteStore(db).audit_entries())


def test_schedule_change_voids_an_approval():
    """The plan is built on the work-order schedule (reallocation moves it): if another plan
    reschedules a work order while this one waits for signatures, the approval is void."""
    from sdra.agent.proposal import basis_changed, basis_of

    pack, store = load("S11")
    before = basis_of(store, pack.policy, "CMP-18")
    store.update("work_orders", "WO-4", start_date=datetime(2026, 12, 20).date())
    reasons = basis_changed(pack.policy, before, basis_of(store, pack.policy, "CMP-18"))
    assert any(r.startswith("work_order: WO-4") for r in reasons)
