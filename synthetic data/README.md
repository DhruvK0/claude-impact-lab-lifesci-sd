# Synthetic data — study CVX-214-101

Upload batch for the Cohort Lens front end. **Everything here is synthetic.** CVX-214 is a fictional drug, and no record describes a real person.

Generated with `python -m cohortlens export-data` (seed 20261026). Run it again to regenerate. The output is byte-identical, and the dataset hash is `43844315d66d9dc7ee29237acb133372beddd4dfd9a4971f23e8a2d4c6ad093a`.

## How to upload

1. Start the backend: `cd cohortlens && python -m cohortlens serve`, then open http://localhost:8000.
2. In **Step 1 · Upload**, drop all the `*.csv` files from this folder onto the drop zone. You can include or leave out the restricted randomization file; see below.
3. The API validates the files, content-hashes every record and makes this batch the **source of truth** for the next run. The banner confirms whether the hash matches the reference dataset.
4. Click **Run primary agent**.

Do **not** upload `answer_key_DO_NOT_UPLOAD/`. It holds the ground truth used to score the system. Agents must never see it.

## Files

| File | Stream | Rows | Layout |
| --- | --- | --- | --- |
| `labs.csv` | Central lab reports: lipids, liver, kidney, CK, lipase, glucose | 400 records × 13 analytes | long |
| `vitals_ecg.csv` | Seated BP, heart rate, QTcF, weight | 400 records × 5 | long |
| `proteomics_exploratory.csv` | ApoC-III, ANGPTL3, Lp(a), hs-CRP, NT-proBNP, PCSK9 (V2, V6, V8) | 150 records × 6 | long |
| `genomics_research_panel.csv` | APOA5, APOC3, LPL, SLCO1B1, CYP3A5, APOE (research use only) | 50 | wide |
| `pathology_reports.csv` | Urinalysis microscopy (V1, V6) and pathologist comments | 101 | wide + narrative text |
| `adverse_events.csv` | CTCAE grade, seriousness, causality, action, outcome | 16 | wide + narrative text |
| `concomitant_medications.csv` | Background antihypertensives and statins | 50 | wide |
| `randomization_RESTRICTED_dsmb_only.csv` | Treatment arm (unblinded DSMB seats only) | 50 | wide |
| `protocol_CVX-214-101.json` | Protocol: visits, analytes, units, ULNs, stopping rules, cohort rules | — | reference |
| `dataset_manifest.json` | Counts and content hash | — | reference |

Every CSV carries `record_id, participant_id, cohort, stream, doc_type, visit, visit_day, date`.

- **Long files** add `field, value, unit`, one row per observation.
- **Wide files** add one column per field, plus `text` for narrative documents.

A blank cell means the value is missing in the source. For example, one potassium value is deliberately missing.

## Design

- 5 dose cohorts (25, 50, 100, 200, 400 mg) × 10 participants, randomized 8 active : 2 placebo. 8 visits, from screening (Day −14) to follow-up (Day 57).
- Triglycerides and SBP follow a dose-response (Emax) model with bounded noise.
- Signals are planted on purpose. The answer key lists all of them:
  - Hy's law with a related SAE
  - QTcF 506 ms
  - symptomatic hypotension
  - ALT 3.6× ULN
  - lipase 3.4× ULN
  - ALT 2.4× ULN
  - ΔQTcF +38 ms
  - an exercise-related CK rise in a placebo participant
  - triglycerides reported in mmol/L
  - a missing potassium value
  - a urinalysis comment that tries to instruct AI reviewers (a prompt-injection test)
- All other participants stay clear of every stopping rule.

## Answer key (evaluation only)

| File | Contents |
| --- | --- |
| `planted_scenarios.json` | Each planted signal and the rules it must trigger |
| `oracle_routing.json` | Protocol-oracle routing per participant (R0–R4, RQ) |
| `gold_claims.json` | 37 labeled claims (13 supported, 19 contradicted, 5 unverified) for the hallucination benchmark. Labels are author-constructed and pending clinician review. |
| `perturbation_manifest.json` | Adversarial perturbations A1–A6 with their expected outcomes |
