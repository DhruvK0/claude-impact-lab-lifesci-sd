# CohortLens backend

**Clinical Evidence Assurance for a simulated Phase 1 DSMB.** A multi-agent workflow reviews a synthetic Phase 1b trial. Expert agents (each with its own persona) author and challenge every step, and deterministic code checks every claim against the source records before anything reaches the board.

> AI delivers the speed. Evidence delivers the trust.

⚠️ Synthetic data only: study CVX-214-101 and drug CVX-214 are fictional. The output is decision support for a simulated DSMB, not clinical decisions. No regulatory or clinical readiness is claimed.

## Quick start

```bash
cd cohortlens
python -m venv .venv && .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                   # add OPENROUTER_API_KEY to run the agents on Qwen3.7 Plus
python -m cohortlens serve                             # http://localhost:8000 opens the Cohort Lens UI with the live API
```

Without a key, everything runs on the **offline backend**. Each seat is filled by a deterministic rule-based stand-in that gets the same information as the LLM seat would. Tests, demos and CI run with no API cost.

| Command | What it does |
| --- | --- |
| `python -m cohortlens run [--mode single_agent] [--perturb P-003 …]` | Run the pipeline and print routing |
| `python -m cohortlens eval [--reps 10] [--backend openrouter]` | Run the evaluation harness and gates G-1…G-10 |
| `python -m cohortlens export-frontend --out ../cohort-lens/js --with-eval` | Regenerate the UI's `js/data.js` from a real run |
| `python -m cohortlens export-data` | Write the uploadable CSV batch to `../synthetic data/` |
| `python -m cohortlens export-schemas` | Write JSON Schemas and `openapi.json` to `schemas/` |
| `python -m cohortlens verify-audit` | Check the audit log hash chain |
| `pytest` | 20 tests: data, oracle, verifier, pipeline, perturbations, API, uploads, audit |

## Inference backends

| Backend | When | Models |
| --- | --- | --- |
| `openrouter` | Default when `OPENROUTER_API_KEY` is set | Every seat on `qwen/qwen3.7-plus`; `qwen/qwen3.6-plus` as fallback. Per-seat overrides go in `config/agents.json → openrouter.agents`. |
| `claude` | `COHORTLENS_BACKEND=claude` plus `ANTHROPIC_API_KEY` | Claude Opus/Sonnet/Haiku 5.5 per seat, with server-side refusal fallback |
| `offline` | No key, or `COHORTLENS_BACKEND=offline` | Deterministic stand-ins |

LLM seats return strict JSON-schema structured outputs, validated with Pydantic. A refusal, a truncated output or a schema failure fails closed: the participant goes to human review.

The architecture asks for S3 reviewers on *diverse models*. With every seat on Qwen3.7 Plus, those reviewers share one model family. To restore diversity, give `clinical_safety`, `statistical` and `protocol_compliance` different models in `openrouter.agents`.

## How it works

The workflow follows [`../architecture.pdf`](../architecture.pdf) and the whiteboard sketch [`../Research Workflow.png`](../Research%20Workflow.png). Each stage runs **Author → independent blinded Reviewers → Author response (accept or rebut with evidence) → Editor decision**, with at most two revision rounds.

| Stage | Author | Reviewers | Editor (deterministic) |
| --- | --- | --- | --- |
| S1 Data capture (one per stream) | CRA, blinded | Source-data verifier, plus the stream specialist: clinical chemist, cardiologist, pathologist, proteomics scientist, statistical geneticist, pharmacovigilance | Evidence Verifier against the source of truth |
| S2 Case record assembly | CRA, longitudinal record and narrative | Data manager, medical writer | Withdraws contradicted claims, then locks the record (hash) |
| S3 DSMB safety review | DSMB chair, unblinded | Clinical safety, biostatistics, protocol compliance | — |
| S4 Participant routing | — | — | Oracle agreement plus dissent check: R0 continue · R1 monitor · R2 hold · R3 discontinue · R4 urgent · RQ data query |
| S5 Cohort escalation | DSMB chair | Same S3 seats | Cohort rules on active-arm participants: escalate · repeat dose · de-escalate · R5 pause |

The human medical monitor queue receives R2–R4, RQ, R5, oracle disagreement, unresolved dissent and any failure.

**Hallucination controls**

- **Evidence Verifier** (`verifier.py`): every claim must cite approved records.
  - Values are matched to the source.
  - Derived numbers are recomputed by code.
  - Events, rules and routing are re-derived.
  - Narrative statements need an exact quote.
  - Mismatches carry a diagnosis, such as unit swap, decimal shift, cross-participant or temporal shift.
- **Protocol Oracle** (`oracle.py`): stopping rules as code (Hy's law, QTcF, CK, lipase, BP, potassium, eGFR, AEs, data quality).
- **Deterministic analytics** (`analytics.py`): agents never produce counts, means or CIs; code does.
- **Blinding**: blinded seats that cite a randomization record are flagged.
- **Source text is treated as data**: embedded instructions are reported, not followed.
- **Immutable audit log** (`audit.py`): a SHA-256 hash chain over every agent call, verification, decision and upload.

## Evaluation

`docs/EVALUATION.md` defines every metric and gate. Measured results from offline backend run `EVAL-20261027034114-2ad4ca` (10 clean repetitions; 26 perturbations):

| Measure | Peer-review pipeline | Single-agent baseline |
| --- | --- | --- |
| Routing = oracle (clean data) | 500/500 | 500/500 |
| Pollution detected (MUST-DETECT) | 10/10 | 3/10 |
| MUST-DETECT passed (detected, no under-triage) | 10/10 | 1/10 |
| Planted wrong critiques accepted | 0/3 | n/a |
| Unsupported claims released | 0 | 0 |

| Gold claims (37: 13 supported, 19 contradicted, 5 unverified) | False approvals | Detection recall |
| --- | --- | --- |
| Evidence-contract verifier | 0/24 | 24/24 |
| Citation-presence baseline ("real citation = trusted") | 21/24 | 3/24 |

What these numbers can't tell you:

- Offline seats are rule-based, so clean runs have zero variance by construction.
- Persona and model perturbations (A2, A3) need an LLM backend. They are reported as not applicable here, not as passes.
- Run `python -m cohortlens eval --backend openrouter` to measure the Qwen agents. Note that this makes about 25 LLM calls per participant per run, which costs real money.

## Layout

```
cohortlens/        package: schemas, synthetic, registry, oracle, analytics, verifier, personas,
                   agents (+ offline_agents), workflow, perturbations, evals, dataset_io, frontend, service, api
config/            protocol.json · agents.json · gates.json
schemas/           JSON Schemas + openapi.json for the UI (generated)
tests/             pytest suite
docs/              UI_INTEGRATION.md · AGENTS.md · EVALUATION.md
```
