import json

import pytest
from fastapi.testclient import TestClient

from cohortlens import api
from cohortlens.audit import AuditLog
from cohortlens.service import Store


@pytest.fixture()
def client(tmp_path, reg):
    api._store = Store(root=tmp_path, registry=reg)
    yield TestClient(api.app)
    api._store = None


def test_blinding_and_records(client):
    open_rows = client.get("/api/participants").json()
    assert "arm" not in open_rows[0]
    pid = open_rows[0]["participant_id"]
    assert client.get(f"/api/records/ARM-{pid}").status_code == 404
    assert client.get(f"/api/records/ARM-{pid}?view=closed").json()["restricted"] is True


def test_run_frontend_and_review_queue(client):
    run_id = api.store().start_run(api.RunConfig(backend="offline"), ["P-003"], wait=True)
    data = client.get(f"/api/runs/{run_id}/frontend").json()
    assert set(data) >= {"agents", "docs", "patients", "rules", "defaultFocus", "meta"}
    assert len(data["patients"]) == 50 and all({"id", "verdict", "cites", "ov"} <= set(p) for p in data["patients"])
    events = client.get(f"/api/runs/{run_id}/events").text
    assert "event: run_completed" in events and "event: end" in events
    item = client.get(f"/api/review-queue?run_id={run_id}").json()[0]
    r = client.post(f"/api/review-queue/{item['item_id']}", json={"decision": "uphold", "reviewer": "JO"})
    assert r.json()["status"] == "confirmed"


def test_verify_endpoint_rejects_fabricated_citation(client):
    body = {"claims": [{"kind": "value", "text": "ALT 168", "participant_id": "P-1041", "visit": "V5", "field": "alt",
                        "value": 168, "unit": "U/L", "evidence": [{"record_id": "LAB-P-1099-V5", "field": "alt"}]}]}
    rep = client.post("/api/verify", json=body).json()
    assert rep["verdicts"][0]["status"] == "CONTRADICTED"


def test_audit_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append("event", {"i": i})
    assert log.verify() == (True, None)
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["i"] = 99
    lines[1] = json.dumps(tampered)
    path.write_text("\n".join(lines) + "\n")
    assert AuditLog(path).verify() == (False, 1)


def test_two_writers_on_one_file_keep_a_valid_chain(tmp_path):
    """The API server and a CLI evaluation each hold their own AuditLog on the same file."""
    import threading
    path = tmp_path / "audit.jsonl"
    a, b = AuditLog(path), AuditLog(path)

    def write(log, tag):
        for i in range(150):
            log.append("event", {"writer": tag, "i": i})
    threads = [threading.Thread(target=write, args=(a, "server")), threading.Thread(target=write, args=(b, "cli"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(AuditLog(path).entries()) == 300
    assert AuditLog(path).verify() == (True, None)
