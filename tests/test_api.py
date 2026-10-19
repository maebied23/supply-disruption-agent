"""Approver API: drive S01 and S03 from the UI's point of view."""

import pytest
from fastapi.testclient import TestClient

from sdra.api.app import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_s01_two_signatures_from_the_ui(client):
    s = client.post("/api/session", json={"scenario_id": "S01"}).json()
    (pending,) = s["pending"]
    assert pending["outstanding_roles"] == ["operations_director", "procurement_manager"]
    assert s["now"].startswith("2026-11-03T09:12")  # stopped when the request appeared

    d = client.get(f"/api/requests/{pending['id']}").json()
    assert d["changed_since_analysis"] == []
    (source,) = d["detail"]["sources"]
    assert source["message_id"] == "MSG-S01-1" and source["quotes"] and all(q in " ".join(source["body"].split()) for q in source["quotes"])
    alts = {a["option_id"]: a["delta_vs_recommendation"] for a in d["detail"]["alternatives"]}
    assert alts["OPT-SUP-B"] == "0.00" and alts["OPT-SUP-C"] == "3,750.00"
    assert d["detail"]["mandate"]["max_unit_price"] == "1,276.87"

    wrong = client.post(f"/api/requests/{pending['id']}/decisions", json={"approver_id": "APR-3", "decision": "approve"})
    assert wrong.status_code == 403
    s = client.post(f"/api/requests/{pending['id']}/decisions", json={"approver_id": "APR-2", "decision": "approve"}).json()
    assert s["pending"][0]["outstanding_roles"] == ["operations_director"] and s["outbox"] == []
    again = client.post(f"/api/requests/{pending['id']}/decisions", json={"approver_id": "APR-2", "decision": "approve"})
    assert again.status_code == 403  # procurement_manager is no longer outstanding
    s = client.post(f"/api/requests/{pending['id']}/decisions", json={"approver_id": "APR-4", "decision": "approve"}).json()
    assert s["pending"] == [] and s["finished"]
    assert sorted((m["to"], m["template"]) for m in s["outbox"]) == [("SUP-A", "po_change"), ("SUP-B", "purchase_order")]
    assert s["audit"]["problems"] == [] and s["metrics"]["override_rate"] == 0


def test_s03_reject_with_exclusion_reproposes(client):
    s = client.post("/api/session", json={"scenario_id": "S03"}).json()
    rid = s["pending"][0]["id"]
    s = client.post(f"/api/requests/{rid}/decisions", json={
        "approver_id": "APR-4", "decision": "reject", "exclude_supplier_ids": ["SUP-B"],
        "comment": "SUP-B failed our last audit"}).json()
    (pending,) = s["pending"]
    assert pending["id"] != rid and pending["option_id"] == "OPT-SUP-C"
    assert s["metrics"]["override_rate"] == 1.0
    audit = client.get("/api/audit").json()
    assert any(e["event"] == "approval_rejected" and e["after"]["comment"] == "SUP-B failed our last audit"
               for e in audit["entries"])


def test_records_only_request_from_the_ui(client):
    """S14: no shortage, but the planner signs the record update; the screen has no shortage."""
    s = client.post("/api/session", json={"scenario_id": "S14"}).json()
    (pending,) = s["pending"]
    d = client.get(f"/api/requests/{pending['id']}").json()
    assert d["detail"]["shortage"] is None and d["detail"]["alternatives"] == []
    assert d["changed_since_analysis"] == []
    s = client.post(f"/api/requests/{pending['id']}/decisions",
                    json={"approver_id": "APR-1", "decision": "approve"}).json()
    assert s["requests"][0]["decided"] == "approved" and not s["pending"]
