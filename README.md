# Impact Lab — CohortLens

**See the patterns. Trust the evidence.**

CohortLens is a concept and proposed MVP for evidence-verified clinical research intelligence. Researchers upload an approved batch of clinical documents, ask a question about the patient population in plain English, and get cohort results where every material claim links back to the source record, encounter and passage.

> Synthetic data only. Thresholds and figures are illustrative. CohortLens is decision support, not a clinical decision-maker.

## The problem

Clinical research teams review hundreds or thousands of patient documents: notes, lab reports, treatment histories and follow-ups. Manual abstraction is slow, and records are inconsistent or incomplete. Generative AI can read these files, but it can also mix up patients, misread a value, or state something the source never said. In research, those mistakes change the conclusion.

## How it works

1. **Ingest** — secure document upload, classification and patient-level reconciliation
2. **Structure** — extract clinical events, diagnoses, treatments, lab results and outcomes
3. **Analyze** — build cohorts and calculate results with reproducible SQL/Python queries
4. **Verify** — cross-check claims against source passages and flag contradictions

Output: cohort findings, denominators, exclusions, document-level citations and review flags.

## Three safeguards

| Safeguard | What it does |
|---|---|
| Evidence traceability | Each clinical assertion keeps a link to the patient, encounter, source file and supporting passage |
| Deterministic analytics | Counts, denominators and percentages come from code, not LLM estimates |
| Adversarial review | Independent reviewers challenge inconsistencies, missing context, ambiguous timelines and unsupported interpretations |

## Architecture: Adversarial Peer Review Framework

`architecture.pdf` applies the approach to a synthetic Phase 1 cardiovascular trial (50 participants × 8 visits ≈ 400 records). Each workflow stage is peer reviewed: Author → blinded Reviewers → Author response → Editor decision.

- **Clinical workflow:** S1 Data Capture → S2 Case Record Assembly → S3 DSMB Safety Review → S4 Participant Routing (R0–R4, RQ) → S5 Cohort Escalation Review → Human Medical Monitor
- **Adversarial lane:** polluted inputs, personas and models; cross-participant leakage; temporal omission; planted "Reviewer 2" critiques, all logged to a perturbation manifest before each run
- **Monitoring lane:** deterministic protocol oracle (QTcF, Hy's law, CK, lipase, BP), clean baseline (N = 10 reps per record), drift monitor, metrics store with gates G-1…G-10, and an immutable audit log

Rule: irrelevant perturbations must not move facts or routing; relevant ones must be detected or change routing correctly.

## Repository contents

| Path | Description |
|---|---|
| `cohort-lens/` | Front-end prototype of the DSMB adversarial review workflow (static HTML/CSS/JS, see its own README) |
| `cohortlens/` | Python backend: multi-agent workflow, protocol oracle, verifier, perturbations, evals and a FastAPI service |
| `cohortlens/schemas/` | JSON Schemas for every record type, plus `openapi.json` |
| `cohortlens/tests/` | pytest suite |
| `architecture.pdf` | Adversarial Peer Review Framework diagram |
| `Research Workflow.png` | Original whiteboard sketch: CRA → case record → DSMB agent → participant routing |
| `CohortLens_Product_Pitch_With_Speaker_Notes.pptx` | 8-slide product pitch with speaker notes |

## Getting started

Frontend: open `cohort-lens/index.html` in a browser, or serve the folder with `python -m http.server 8000`.

Backend:

```bash
cd cohortlens
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
cp .env.example .env          # add an OpenRouter or Anthropic key, or leave empty to run offline

python -m cohortlens generate # build the synthetic dataset into data/
python -m cohortlens run      # run the workflow (results go to runs/)
python -m cohortlens eval     # run the hallucination evals
python -m cohortlens serve    # start the API
pytest
```

`data/`, `runs/` and `.env` are generated locally and are not committed.

## Author

Team Alex
