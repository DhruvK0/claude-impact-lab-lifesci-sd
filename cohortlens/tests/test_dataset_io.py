from fastapi.testclient import TestClient

from cohortlens import api
from cohortlens.dataset_io import export_csv, ingest
from cohortlens.registry import Registry
from cohortlens.service import Store


def test_csv_round_trip_is_byte_identical(reg, tmp_path):
    export_csv(reg, tmp_path)
    records, errors = ingest({p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.csv")})
    assert errors == []
    assert Registry(records).hash == reg.hash


def test_upload_activates_dataset_and_rejects_foreign_files(reg, tmp_path):
    export_csv(reg, tmp_path / "csv")
    api._store = Store(root=tmp_path / "runs", registry=reg)
    client = TestClient(api.app)
    files = [{"name": p.name, "text": p.read_text(encoding="utf-8")} for p in (tmp_path / "csv").glob("*.csv")]
    r = client.post("/api/datasets/upload", json={"files": files + [{"name": "notes.csv", "text": "a,b\n1,2\n"}]})
    body = r.json()
    assert r.status_code == 200 and body["matches_reference"] and body["n_records"] == 1217
    assert any("notes.csv" in e for e in body["errors"])
    assert client.post("/api/datasets/upload", json={"files": [{"name": "x.csv", "text": "a\n1\n"}]}).status_code == 422
    api._store = None
