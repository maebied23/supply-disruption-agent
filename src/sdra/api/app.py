"""Approver API (FastAPI). Serves the approver UI and drives a scenario on the simulated clock.

Demo mode: a scenario's emails and stock corrections play on the clock, but its scripted
approvals are dropped; decisions come from the UI. The clock advances until a request is
waiting for someone or the scenario window ends. Extraction is scripted unless
SDRA_LIVE_EXTRACTION=1 (then Claude reads the emails; needs API credentials).

No sign-in in V1: the UI picks who is signing. A deployment puts this behind the company's
SSO and takes the approver id from the session, never from the request body.

    uvicorn sdra.api.app:app --port 8000
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field

from sdra.agent.orchestrator import Runner
from sdra.agent.proposal import basis_changed, basis_of
from sdra.agent.runtime import ApprovalEvent, RequestRecord
from sdra.audit import verify_chain
from sdra.intake.extract import ClaudeExtractor, ScriptedExtractor
from sdra.scenarios import SCENARIOS, load_scenario_pack, scripted_results
from sdra.schema import InboundMessage, StockAdjustment
from sdra.sqlite_store import SqliteStore


class StartSession(BaseModel):
    scenario_id: str


class Decision(BaseModel):
    approver_id: str
    decision: Literal["approve", "reject"]
    exclude_supplier_ids: list[str] = Field(default_factory=list)
    comment: str = ""


class Session:
    def __init__(self, sid: str, data_dir: Path):
        sc = SCENARIOS[sid]
        self.sid, self.end = sid, sc["end"]
        self.pack = load_scenario_pack(sid)
        n = len(list(data_dir.glob(f"{sid}-*.db"))) // 2 + 1
        self.store = SqliteStore(data_dir / f"{sid}-{n}-records.db", self.pack.seed)
        saver = SqliteSaver(sqlite3.connect(data_dir / f"{sid}-{n}-checkpoints.db", check_same_thread=False))
        extractor = ClaudeExtractor() if os.environ.get("SDRA_LIVE_EXTRACTION") == "1" else ScriptedExtractor(
            scripted_results(sid))
        self.runner = Runner(self.pack, self.store, extractor, sc["start"], checkpointer=saver)
        for e in sc["events"]:
            if e["type"] == "email":
                self.runner.schedule(e["at"], "email", InboundMessage.model_validate(e["message"]))
            elif e["type"] == "stock_adjustment":
                self.runner.schedule(e["at"], "stock_adjustment", StockAdjustment.model_validate(e["adjustment"]))
        self.advance()

    def advance(self) -> None:
        """Run until a new request is waiting, or the window ends."""
        before = set(self.runner.pending)
        self.runner.run_until(self.end, stop=lambda: bool(set(self.runner.pending) - before))

    @property
    def finished(self) -> bool:
        nxt = self.runner.clock.next_at()
        return nxt is None or nxt > self.end


def _request_summary(r: RequestRecord, runner: Runner) -> dict:
    p = runner.pending.get(r.id)
    return {"id": r.id, "incident_id": r.incident_id, "option_id": r.option_id, "summary": r.detail.get("summary"),
            "tier": r.tier, "roles": r.roles, "outstanding_roles": p.outstanding_roles if p else [],
            "decided": r.decided, "created_at": r.created_at, "expires_at": r.expires_at,
            "signatures": [s.model_dump(mode="json") for s in r.signatures], "void_reasons": r.void_reasons}


def create_app(data_dir: Path | None = None) -> FastAPI:
    data = Path(data_dir or os.environ.get("SDRA_DATA_DIR") or tempfile.mkdtemp(prefix="sdra-"))
    data.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="Supply Disruption Response Agent - approvals")
    lock = threading.Lock()
    holder: dict[str, Session] = {}

    def session() -> Session:
        if "s" not in holder:
            raise HTTPException(409, "no scenario loaded; POST /api/session first")
        return holder["s"]

    def state(s: Session) -> dict:
        r = s.runner
        decided = [x for x in r.requests if x.signatures]
        human = [d for x in r.requests for d in x.signatures]
        rejected = sum(d.decision == "reject" for d in human)
        return {
            "scenario": {"id": s.sid, "title": SCENARIOS[s.sid]["title"], "end": s.end},
            "now": r.now, "next_event_at": r.clock.next_at(), "finished": s.finished,
            "base_currency": s.pack.policy.company.base_currency, "timezone": s.pack.policy.company.timezone,
            "pending": [_request_summary(x, r) for x in r.requests if x.id in r.pending],
            "requests": [_request_summary(x, r) for x in r.requests],
            "incidents": [i.model_dump(mode="json") for i in r.incidents.values()],
            "outbox": [{"id": m.id, "at": m.at, "to": m.to, "kind": m.kind, "template": m.template,
                        "subject": m.subject, "body": m.body} for m in r.outbox],
            "review_tasks": [t.model_dump(mode="json") for t in r.review_tasks],
            "notifications": r.notifications,
            "metrics": {"requests": len(r.requests), "with_decisions": len(decided), "decisions": len(human),
                        "rejections": rejected, "override_rate": round(rejected / len(human), 3) if human else None,
                        "by_outcome": {k: sum(x.decided == k for x in r.requests)
                                       for k in ("approved", "executed", "rejected", "voided", "expired", "refused")}},
            "audit": {"entries": len(r.audit.entries), "problems": verify_chain(r.audit.entries)},
        }

    @app.get("/api/scenarios")
    def scenarios() -> list[dict]:
        return [{"id": sid, "title": sc["title"], "group": sc.get("group")} for sid, sc in SCENARIOS.items()]

    @app.post("/api/session")
    def start(body: StartSession) -> dict:
        if body.scenario_id not in SCENARIOS:
            raise HTTPException(404, f"unknown scenario {body.scenario_id}")
        with lock:
            holder["s"] = Session(body.scenario_id, data)
            return state(holder["s"])

    @app.get("/api/state")
    def get_state() -> dict:
        with lock:
            return state(session())

    @app.post("/api/advance")
    def advance() -> dict:
        with lock:
            s = session()
            s.advance()
            return state(s)

    @app.get("/api/approvers")
    def approvers() -> list[dict]:
        with lock:
            s = session()
            return [{"id": a.id, "name": a.name, "roles": a.roles} for a in s.store.rows("approvers")]

    @app.get("/api/requests/{request_id}")
    def request(request_id: str) -> dict:
        with lock:
            s = session()
            r = s.runner
            rec = next((x for x in r.requests if x.id == request_id), None)
            if rec is None:
                raise HTTPException(404, f"unknown request {request_id}")
            item = rec.item_id or rec.detail["shortage"]["item_id"]
            changed = basis_changed(s.pack.policy, rec.basis,
                                    basis_of(s.store, s.pack.policy, item, list(r.ledger.events.values())))
            return _request_summary(rec, r) | {"detail": rec.detail, "changed_since_analysis": changed,
                                                "rule_ids": rec.rule_ids, "evidence": rec.evidence}

    @app.post("/api/requests/{request_id}/decisions")
    def decide(request_id: str, body: Decision) -> dict:
        with lock:
            s = session()
            r = s.runner
            if request_id not in r.pending:
                raise HTTPException(409, f"{request_id} is not waiting for a decision")
            approver = s.store.get("approvers", body.approver_id)
            outstanding = r.pending[request_id].outstanding_roles
            if approver is None or not set(approver.roles) & set(outstanding):
                raise HTTPException(403, f"{body.approver_id} holds none of the outstanding roles {outstanding}")
            if any(x.approver_id == approver.id for x in r.request(request_id).signatures):
                raise HTTPException(409, f"{approver.id} has already signed {request_id}")
            r.decide(ApprovalEvent(request_id=request_id, **body.model_dump()))
            if not r.pending:
                s.advance()
            return state(s)

    @app.get("/api/audit")
    def audit() -> dict:
        with lock:
            entries = session().runner.audit.entries
            return {"problems": verify_chain(entries), "entries": [e.model_dump(mode="json") for e in entries]}

    return app


app = create_app()
