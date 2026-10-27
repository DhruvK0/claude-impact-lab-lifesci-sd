# Wiring a UI to CohortLens

The Cohort Lens front end (`../cohort-lens/`) is already wired. This page is the contract for that UI, or for any replacement.

## Three ways to feed the UI

| Mode | How | Use for |
| --- | --- | --- |
| Static export | `python -m cohortlens export-frontend --out ../cohort-lens/js --with-eval` writes `js/data.js` (`window.COHORT_LENS_DATA`) and `data.json` | GitHub Pages and the Claude artifact |
| Same-origin live | `python -m cohortlens serve`, then open http://localhost:8000; the UI is mounted at `/ui` and `/` redirects to `/ui/?api=<origin>` | Local demos with real runs |
| Cross-origin live | Serve the UI anywhere and open `index.html?api=http://localhost:8000`. Set `COHORTLENS_CORS_ORIGINS` to restrict origins | A separate front-end dev server |

`js/loader.js` picks the mode. With `?api=`, it loads `GET /api/runs/latest/frontend`. If that fails, it falls back to the bundled `data.js` and shows a notice.

## `COHORT_LENS_DATA` shape (`cohortlens/frontend.py`)

```jsonc
{
  "agents":  [{ "key": "sdv_reviewer", "name": "Source data verifier", "focus": "...", "ok": "..." }],   // 12 LLM seats + "cite" (verifier) + "oracle"
  "docs":    [{ "code": "LAB", "name": "Central lab reports", "meta": "400 records · 13 analytes", "sections": ["..."] }],
  "rules":   [{ "doc": "CHTR", "loc": "HEP-HYS", "quote": "... → R4 (Urgent safety review)", "ok": true }],
  "defaultFocus": ["CHTR:0", "LAB:1"],
  "patients": [{
    "id": "P-1044", "cohort": "400 mg", "verdict": "REVIEW",           // PASS=R0, REVIEW=R1/RQ, FAIL=R2–R4 (DSMB agent's first proposal)
    "signals": "SBP 149→147 · TG −42% · ALT max 3.6× ULN", "rationale": "...",
    "cites": [{ "doc": "LAB", "loc": "S3 · LAB-P-1044-V5 · alt", "quote": "<exact source line>", "ok": true, "claim": "...", "claimed": "...if ok=false" }],
    "ov": { "<agent key>": [0.30, "<that seat's critique>"] },          // every seat present; mapping in meta.confidenceNote
    "sev": 3, "humanReview": true, "reason": "Oracle disagreement ...", "overrideLabel": "Override → R2",
    "routing": { "initial": "R1", "final": "R2", "oracle": "R2", "initialLabel": "...", "finalLabel": "..." },
    "rules": [{ "id": "HEP-3X", "routing": "R2", "visit": "V5", "observed": "3.6x ULN", "description": "..." }],
    "dataQueries": [], "stages": [{ "stage": "S1", "name": "Data capture", "outcome": "ACCEPT", "rounds": 1 }],
    "adversarial": [{ "id": "P-018", "lane": "A3", "kind": "stale_protocol", "description": "..." }],
    "plantedCritiques": [{ "stage": "S3", "assertion": "...", "response": "REBUT", "rationale": "..." }],
    "lockHash": "sha256 of the locked case record"
  }],
  "cohorts": [{ "cohort": 5, "dose": "400 mg", "decision": "R5_PAUSE", "oracle": "R5_PAUSE", "efficacy": [{ "label": "...", "mean": -38.9, "n": 8, "ci95": [-41.6, -36.1] }] }],
  "meta": { "study", "runId", "backend", "versions", "perturbations", "defaultSelected", "confidenceNote", "runMetrics", "evaluation" }
}
```

## API (OpenAPI at `/docs`; full schema in `schemas/openapi.json`)

| Method & path | Purpose |
| --- | --- |
| `GET /api/meta`, `/api/protocol`, `/api/personas` | Versions, routing legend, protocol, persona cards |
| `GET /api/participants?view=open\|closed` | Roster; `closed` adds treatment arm (unblinded DSMB view) |
| `GET /api/participants/{pid}/records`, `GET /api/records/{id}` | Source documents for the evidence inspector |
| `POST /api/datasets/upload` `{files:[{name,text}]}` | Ingest CohortLens CSVs as the source of truth (422 with reasons if rejected) |
| `GET /api/datasets/active`, `POST /api/datasets/reset` | Active dataset, or reset to the reference synthetic set |
| `POST /api/runs` `{mode, backend, participants, perturbation_ids}` | Start a run (202, returns `run_id`) |
| `GET /api/runs/{id}/events` | Server-sent events: `stage_started`, `verification`, `critiques`, `author_response`, `editor_decision`, `participant_routed`, `cohort_decided`, `run_completed`, `end` |
| `GET /api/runs/{id}`, `/participants/{pid}`, `/frontend` | Full result (every stage, round, critique and verdict), one participant, or the UI shape |
| `GET /api/review-queue`, `POST /api/review-queue/{item_id}` | Human medical monitor queue; decisions `uphold, confirm, override, escalate` are written to the audit log |
| `POST /api/verify` `{claims, blinded}` | Verify arbitrary claims against the source of truth |
| `POST /api/evaluations`, `GET /api/evaluations/{id\|latest}`, `GET /api/gates` | Evaluation harness and gates |
| `GET /api/metrics/history` | Control-chart series (mean ± 3σ) per metric across evaluations |
| `GET /api/audit`, `GET /api/audit/verify` | Audit log entries and hash-chain check |

## Rules for any UI

- Never show treatment arm in open (blinded) views. `view=closed` is the DSMB session; put it behind authorization before real use.
- Never present UNVERIFIED as false, or SUPPORTED without its citation. Show colour *and* a text label.
- Display counts with denominators (`numerator/denominator`), not percentages alone.
- Show offline-backend evaluations as such. Gates whose `passed` is `null` are "not measurable", not "passed".
