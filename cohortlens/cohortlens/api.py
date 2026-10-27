"""HTTP API for the Cohort Lens UI.

Run:  python -m cohortlens serve            (http://localhost:8000, docs at /docs)
UI:   open cohort-lens/index.html?api=http://localhost:8000

Blinding: `view=open` (default) hides treatment-arm records and arm fields;
`view=closed` is the unblinded DSMB view. A real deployment must put closed
views behind authorization; this prototype only separates them.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import frontend
from .config import PROJECT_ROOT, SCHEMA_VERSION, backend_name, protocol
from .oracle import evaluate_registry
from .personas import all_personas
from .schemas import ClaimContent, RunConfig, VerificationReport
from .service import Store
from .verifier import Verifier

app = FastAPI(title="CohortLens API", version=SCHEMA_VERSION,
              description="Clinical Evidence Assurance for a simulated Phase 1 DSMB. Synthetic data only.")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("COHORTLENS_CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


View = Literal["open", "closed"]


def _record(r, view: View) -> dict:
    d = r.model_dump()
    if view == "open" and r.restricted:
        return {}
    return d


# ---------------------------------------------------------------------------- meta

@app.get("/api/health")
def health():
    return {"status": "ok", "schema_version": SCHEMA_VERSION}


@app.get("/api/meta")
def meta():
    s = store()
    p = protocol()
    return {"schema_version": SCHEMA_VERSION, "protocol_id": p["protocol_id"], "protocol_version": p["protocol_version"],
            "dataset": s.registry.manifest.model_dump(exclude={"planted_scenarios"}), "default_backend": backend_name(),
            "backends": ["offline", "openrouter", "claude"], "routing_codes": p["routing_codes"], "cohort_rules": p["cohort_rules"],
            "disclaimer": p["disclaimer"]}


@app.get("/api/protocol")
def get_protocol():
    return protocol()


@app.get("/api/personas")
def personas():
    return [p.model_dump() for p in all_personas()]


@app.get("/api/dataset")
def dataset(include_ground_truth: bool = False):
    m = store().registry.manifest
    return m.model_dump() if include_ground_truth else m.model_dump(exclude={"planted_scenarios"})


@app.get("/api/participants")
def participants(view: View = "open"):
    reg = store().registry
    out = []
    for pid in reg.participant_ids():
        row = {"participant_id": pid, "cohort": reg.cohort_of(pid), "n_records": len(reg.participant_records(pid))}
        if view == "closed":
            row["arm"] = reg.arm(pid)
        out.append(row)
    return out


@app.get("/api/participants/{pid}/records")
def participant_records(pid: str, view: View = "open", stream: str | None = None):
    reg = store().registry
    if pid not in reg.participant_ids():
        raise HTTPException(404, f"unknown participant {pid}")
    recs = reg.participant_records(pid, include_restricted=view == "closed")
    return [r.model_dump() for r in recs if stream is None or r.stream == stream]


@app.get("/api/records/{record_id}")
def record(record_id: str, view: View = "open"):
    r = store().registry.get(record_id)
    if r is None or (r.restricted and view == "open"):
        raise HTTPException(404, f"unknown record {record_id}")
    return r.model_dump()


@app.get("/api/oracle")
def oracle(view: View = "closed"):
    if view != "closed":
        raise HTTPException(403, "Oracle routing is an unblinded view")
    return {p: r.model_dump() for p, r in evaluate_registry(store().registry).items()}


@app.get("/api/perturbations")
def perturbations():
    return store().manifest.model_dump()


class UploadFile(BaseModel):
    name: str
    text: str = Field(max_length=20_000_000)


class UploadRequest(BaseModel):
    files: list[UploadFile]


@app.post("/api/datasets/upload")
def upload_dataset(req: UploadRequest):
    """Ingest CohortLens source CSVs (see `synthetic data/README.md`). They become the source of truth for new runs."""
    result = store().activate_upload({f.name: f.text for f in req.files})
    if not result["activated"]:
        raise HTTPException(422, result)
    return result


@app.get("/api/datasets/active")
def active_dataset():
    s = store()
    m = s.registry.manifest
    return {"dataset_id": m.dataset_id, "dataset_hash": s.registry.hash, "n_records": m.n_records,
            "records_by_stream": m.records_by_stream, "matches_reference": s.registry.hash == s.reference.hash}


@app.post("/api/datasets/reset")
def reset_dataset():
    return store().reset_dataset()


# ---------------------------------------------------------------------------- runs

class RunRequest(BaseModel):
    mode: Literal["adversarial", "single_agent"] = "adversarial"
    backend: Literal["offline", "openrouter", "claude"] | None = None
    participants: list[str] | None = None
    cohorts: list[int] | None = None
    perturbation_ids: list[str] = Field(default_factory=list)
    max_revision_rounds: int = Field(default=2, ge=0, le=2)
    specialist_panel: bool = True
    label: str = ""


@app.post("/api/runs", status_code=202)
def create_run(req: RunRequest):
    cfg = RunConfig(mode=req.mode, backend=req.backend or backend_name(), participants=req.participants, cohorts=req.cohorts,
                    max_revision_rounds=req.max_revision_rounds, specialist_panel=req.specialist_panel, label=req.label)
    run_id = store().start_run(cfg, req.perturbation_ids)
    return {"run_id": run_id, "events": f"/api/runs/{run_id}/events", "result": f"/api/runs/{run_id}"}


@app.get("/api/runs")
def list_runs():
    return [s.model_dump() for s in store().summaries()]


def _run(run_id: str):
    s = store()
    if run_id == "latest":
        r = s.latest_completed()
        if r is None:
            raise HTTPException(404, "no completed runs")
        return r
    if run_id not in s.runs:
        raise HTTPException(404, f"unknown run {run_id}")
    return s.runs[run_id]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, include_stages: bool = True):
    r = _run(run_id)
    if include_stages:
        return r.model_dump()
    d = r.model_dump(exclude={"participants": {"__all__": {"stages"}}})
    return d


@app.get("/api/runs/{run_id}/participants/{pid}")
def run_participant(run_id: str, pid: str):
    r = _run(run_id)
    p = next((p for p in r.participants if p.participant_id == pid), None)
    if p is None:
        raise HTTPException(404, f"{pid} not in run {run_id}")
    return p.model_dump()


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = -1):
    """Server-sent events: one `data:` line per PipelineEvent until the run finishes."""
    s = store()
    if run_id not in s.runs:
        raise HTTPException(404, f"unknown run {run_id}")

    def gen():
        sent = after
        while True:
            evs = [e for e in s.events.get(run_id, []) if e.seq > sent]
            for e in evs:
                sent = e.seq
                yield f"id: {e.seq}\nevent: {e.type}\ndata: {e.model_dump_json()}\n\n"
            if s.runs[run_id].status in ("completed", "failed") and not [e for e in s.events.get(run_id, []) if e.seq > sent]:
                yield f"event: end\ndata: {json.dumps({'status': s.runs[run_id].status})}\n\n"
                return
            time.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/runs/{run_id}/frontend")
def run_frontend(run_id: str, research_associate: str = "Research associate"):
    """The run in the exact shape cohort-lens/js/app.js reads (window.COHORT_LENS_DATA)."""
    r = _run(run_id)
    if r.status != "completed":
        raise HTTPException(409, f"run {r.run_id} is {r.status}")
    return frontend.build(r, store().registry, research_associate)


# ---------------------------------------------------------------------------- human medical monitor

class Decision(BaseModel):
    decision: Literal["uphold", "confirm", "override", "escalate"]
    reviewer: str = "research-associate"
    note: str | None = None
    override_routing: str | None = None


@app.get("/api/review-queue")
def review_queue(run_id: str | None = None):
    return [i.model_dump() for i in store().queue(run_id)]


@app.post("/api/review-queue/{item_id}")
def decide(item_id: str, d: Decision):
    try:
        return store().decide(item_id, d.decision, d.reviewer, d.note, d.override_routing).model_dump()
    except KeyError:
        raise HTTPException(404, f"unknown review item {item_id}") from None


# ---------------------------------------------------------------------------- verification

class VerifyRequest(BaseModel):
    claims: list[ClaimContent]
    blinded: bool = True


@app.post("/api/verify", response_model=VerificationReport)
def verify(req: VerifyRequest):
    from .schemas import Claim
    claims = [Claim(**c.model_dump(), claim_id=f"API-{i + 1:03d}") for i, c in enumerate(req.claims)]
    return Verifier(store().registry).verify(claims, req.blinded)


# ---------------------------------------------------------------------------- evaluation & monitoring

class EvalRequest(BaseModel):
    backend: Literal["offline", "openrouter", "claude"] | None = None
    reps: int = Field(default=10, ge=1, le=50)
    participants: list[str] | None = None


@app.post("/api/evaluations", status_code=202)
def create_evaluation(req: EvalRequest):
    eid = store().start_evaluation(req.backend or backend_name(), req.reps, req.participants)
    return {"job_id": eid, "status": f"/api/evaluations/{eid}"}


@app.get("/api/evaluations")
def list_evaluations():
    out = []
    for k, v in store().evals.items():
        out.append(v if isinstance(v, dict) else {"evaluation_id": v.evaluation_id, "created_at": v.created_at, "backend": v.backend,
                                                   "gates_passed": sum(g.passed is True for g in v.gates), "gates": len(v.gates)})
    return out


@app.get("/api/evaluations/{eval_id}")
def get_evaluation(eval_id: str):
    s = store()
    if eval_id == "latest":
        done = [v for v in s.evals.values() if not isinstance(v, dict)]
        if not done:
            raise HTTPException(404, "no evaluations")
        return max(done, key=lambda e: e.created_at).model_dump()
    v = s.evals.get(eval_id)
    if v is None:
        raise HTTPException(404, f"unknown evaluation {eval_id}")
    return v if isinstance(v, dict) else v.model_dump()


@app.get("/api/metrics/history")
def metrics_history():
    return store().metrics_history()


@app.get("/api/gates")
def gates():
    done = [v for v in store().evals.values() if not isinstance(v, dict)]
    if not done:
        raise HTTPException(404, "no evaluations yet")
    return [g.model_dump() for g in max(done, key=lambda e: e.created_at).gates]


@app.get("/api/audit")
def audit(run_id: str | None = None, limit: int = Query(200, le=5000)):
    return [e.model_dump() for e in store().audit.entries(run_id, limit)]


@app.get("/api/audit/verify")
def audit_verify():
    ok, bad = store().audit.verify()
    return {"valid": ok, "first_bad_seq": bad, "entries": len(store().audit.entries())}


# ---------------------------------------------------------------------------- UI (optional, same origin)

_UI_DIR = Path(os.environ.get("COHORTLENS_UI_DIR") or next(
    (d for d in (PROJECT_ROOT.parent / "cohort-lens", PROJECT_ROOT.parent / "impact-lab" / "cohort-lens") if (d / "index.html").exists()),
    PROJECT_ROOT.parent / "cohort-lens"))
if (_UI_DIR / "index.html").exists():
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root(request: Request):
        return RedirectResponse(f"/ui/?api={str(request.base_url).rstrip('/')}")
