"""Spike: does LangGraph's interrupt/resume support our approval design?

Not product code. Verifies, against the pinned LangGraph version, the behaviour the approval
workflow and action gateway rely on (scenarios S01/S03/S09):

  1. A paused approval survives a "restart" (new saver + newly compiled graph, same SQLite file).
  2. Code before interrupt() re-runs on resume, so it must be side-effect free.
  3. Dual control = one interrupt per signature, looped by a conditional edge, never in-node.
  4. Rejection routes away; nothing executes, including the half that was approved.
  5. Re-validation on resume voids a stale approval (S09).
  6. A crash after a side effect is safe to retry because execution uses an idempotency key.
  7. response_schema validates resume payloads against our ApprovalDecision model.

Run: pytest spikes/
"""

from __future__ import annotations

import hashlib
import json
import operator
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from sdra.schema import ApprovalDecision

DECIDED_AT = datetime.fromisoformat("2026-11-03T10:30:00+03:00")


class State(TypedDict, total=False):
    incident_id: str
    required_roles: list[str]
    fingerprint: str
    signatures: Annotated[list[dict], operator.add]
    status: str


class World:
    """Stand-in for the record adapter and outbox, outside LangGraph's state."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("create table if not exists stock (item text primary key, on_hand int)")
        self.db.execute("create table if not exists outbox (idempotency_key text primary key, body text)")
        self.db.execute("insert or ignore into stock values ('CMP-24', 100)")
        self.db.commit()
        self.pre_interrupt_runs = 0
        self.crash_next_execute = False

    def basis(self) -> str:
        rows = self.db.execute("select item, on_hand from stock order by item").fetchall()
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()

    def outbox(self) -> list[str]:
        return [r[0] for r in self.db.execute("select idempotency_key from outbox")]


def build(world: World) -> StateGraph:
    def plan(state: State) -> State:
        return {"fingerprint": world.basis(), "required_roles": ["operations_director", "procurement_manager"]}

    def request_signature(state: State) -> State:
        signed = {s["role"] for s in state.get("signatures", [])}
        role = next(r for r in state["required_roles"] if r not in signed)
        world.pre_interrupt_runs += 1  # observable only: proves this code re-runs on resume
        decision = interrupt({"incident_id": state["incident_id"], "role": role}, response_schema=ApprovalDecision)
        decision = ApprovalDecision.model_validate(decision)
        if decision.role != role:
            raise ValueError(f"expected a {role} signature, got {decision.role}")
        return {"signatures": [decision.model_dump(mode="json")]}

    def after_signature(state: State) -> str:
        sigs = state["signatures"]
        if sigs[-1]["decision"] == "reject":
            return "rejected"
        if {s["role"] for s in sigs} >= set(state["required_roles"]):
            return "revalidate"
        return "request_signature"

    def revalidate(state: State) -> Command:
        if world.basis() != state["fingerprint"]:
            return Command(update={"status": "voided"}, goto=END)
        return Command(goto="execute")

    def execute(state: State) -> State:
        key = f"{state['incident_id']}:create_po:1"
        world.db.execute("insert or ignore into outbox values (?, ?)", (key, "PO to SUP-B"))
        world.db.commit()
        if world.crash_next_execute:
            world.crash_next_execute = False
            raise RuntimeError("simulated crash after the side effect")
        return {"status": "executed"}

    def rejected(state: State) -> State:
        return {"status": "rejected"}

    g = StateGraph(State)
    for name, fn in [("plan", plan), ("request_signature", request_signature), ("revalidate", revalidate),
                     ("execute", execute), ("rejected", rejected)]:
        g.add_node(name, fn)
    g.add_edge(START, "plan")
    g.add_edge("plan", "request_signature")
    g.add_conditional_edges("request_signature", after_signature, ["request_signature", "revalidate", "rejected"])
    g.add_edge("execute", END)
    g.add_edge("rejected", END)
    return g


def sign(approver_id: str, role: str, decision: str = "approve") -> Command:
    return Command(resume={"request_id": "REQ-1", "approver_id": approver_id, "role": role,
                           "decision": decision, "decided_at": DECIDED_AT.isoformat()})


CONFIG = {"configurable": {"thread_id": "INC-1"}}
START_INPUT = {"incident_id": "INC-1", "signatures": []}


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "checkpoints.sqlite", tmp_path / "world.sqlite"


def pending_role(result) -> str:
    (intr,) = result["__interrupt__"]
    return intr.value["role"]


def test_dual_approval_survives_restart_and_reruns_pre_interrupt_code(paths):
    ckpt, world_db = paths
    world = World(world_db)
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        result = build(world).compile(checkpointer=saver).invoke(START_INPUT, CONFIG)
        assert pending_role(result) == "operations_director"
    assert world.pre_interrupt_runs == 1

    # "Restart": new saver, newly compiled graph, state only from disk.
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = build(world).compile(checkpointer=saver)
        result = graph.invoke(sign("APR-4", "operations_director"), CONFIG)
        assert pending_role(result) == "procurement_manager"
        # The first node re-ran up to interrupt() on resume, then the loop asked for role 2.
        assert world.pre_interrupt_runs == 3
        result = graph.invoke(sign("APR-2", "procurement_manager"), CONFIG)
        assert result["status"] == "executed"
        assert [s["approver_id"] for s in result["signatures"]] == ["APR-4", "APR-2"]
    assert world.outbox() == ["INC-1:create_po:1"]


def test_rejection_executes_nothing(paths):
    ckpt, world_db = paths
    world = World(world_db)
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = build(world).compile(checkpointer=saver)
        graph.invoke(START_INPUT, CONFIG)
        graph.invoke(sign("APR-4", "operations_director"), CONFIG)
        result = graph.invoke(sign("APR-2", "procurement_manager", "reject"), CONFIG)
    assert result["status"] == "rejected"
    assert world.outbox() == []


def test_stale_basis_voids_approval(paths):
    ckpt, world_db = paths
    world = World(world_db)
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = build(world).compile(checkpointer=saver)
        graph.invoke(START_INPUT, CONFIG)
        graph.invoke(sign("APR-2", "operations_director"), CONFIG)
        world.db.execute("update stock set on_hand = 150 where item = 'CMP-24'")  # S09
        world.db.commit()
        result = graph.invoke(sign("APR-4", "procurement_manager"), CONFIG)
    assert result["status"] == "voided"
    assert world.outbox() == []


def test_crash_after_side_effect_is_safe_to_retry(paths):
    ckpt, world_db = paths
    world = World(world_db)
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = build(world).compile(checkpointer=saver)
        graph.invoke(START_INPUT, CONFIG)
        graph.invoke(sign("APR-4", "operations_director"), CONFIG)
        world.crash_next_execute = True
        with pytest.raises(RuntimeError, match="simulated crash"):
            graph.invoke(sign("APR-2", "procurement_manager"), CONFIG)
        assert graph.get_state(CONFIG).next == ("execute",)
        result = graph.invoke(None, CONFIG)  # retry from the last checkpoint
    assert result["status"] == "executed"
    assert world.outbox() == ["INC-1:create_po:1"]  # one row, not two


def test_response_schema_rejects_malformed_resume(paths):
    ckpt, world_db = paths
    world = World(world_db)
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        graph = build(world).compile(checkpointer=saver)
        graph.invoke(START_INPUT, CONFIG)
        with pytest.raises(Exception) as exc:
            graph.invoke(Command(resume={"approver_id": "APR-4", "decision": "yes please"}), CONFIG)
        print("malformed resume raised:", type(exc.value).__name__)
        # The thread is still paused on the same signature and can be resumed correctly.
        assert graph.get_state(CONFIG).next == ("request_signature",)
        result = graph.invoke(sign("APR-4", "operations_director"), CONFIG)
        assert pending_role(result) == "procurement_manager"
