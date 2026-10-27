# Cohort Lens

**Adversarial safety review for a Data Safety Monitoring Board (DSMB).**

Cohort Lens is the front end for checking an AI safety agent's work. It takes the documents from a phase 1 cardiovascular trial and has the DSMB agent issue a PASS / REVIEW / FAIL verdict for each participant, with citations. Twelve expert reviewer agents and two deterministic checks (citation verifier, protocol oracle) then challenge every verdict. Wherever they can't agree, a citation doesn't match its source, or a stopping rule is met, the case goes to a research associate's worklist.

> ⚠️ All data in this repository is **synthetic**. Study *CVX-214-101* and its participants are fictional. `js/data.js` is generated from a real run of the CohortLens pipeline (`../cohortlens`); with the API running, the UI runs the agents live.

## The five steps

| Step | What happens |
| --- | --- |
| **1 · Upload** | The document batch (protocol, DSMB charter, vitals, labs, ECG, adverse events, concomitant meds). The research associate marks which sources in each document need the closest attention. |
| **2 · Primary agent** | A row-by-row verdict for each of 12 participants, mapped to the charter's §6 stopping rules. Click a citation to see the exact source line. |
| **3 · Swarm** | Ten reviewer personas (cardiologist, nephrologist, citation verifier, red-team skeptic, and others) each score their confidence that the verdict holds. Shown as a heatmap plus each reviewer's argument. |
| **4 · Consensus** | The mean reviewer confidence per participant, its spread, and the batch distribution. An adjustable threshold decides what gets routed to a human. |
| **5 · Worklist** | Routed cases, sorted **most severe first** or **lowest consensus first**. The associate upholds, overrides or escalates each one, then signs and exports the DSMB packet. |

Two cases built into the data show what the swarm is for:
- **P-1007**: the primary agent cites a triglyceride drop of −22%, but the cited row actually says −8.2%. The citation verifier flags the made-up number.
- **P-1006**: the primary agent passes a participant whose potassium is 5.7 mmol/L, above the charter limit. The nephrologist and the red team catch it.

## Running it

No build step and no dependencies.

```bash
# Option 1: open the file directly
open index.html

# Option 2: serve locally
npx serve .
# or
python3 -m http.server 8000
```

To publish on **GitHub Pages**, push the repo, then go to *Settings → Pages* and set the source to the `main` branch, root folder.

## Project structure

```
cohort-lens/
├── index.html          Page shell: masthead, the #app mount point, script tags
├── css/
│   ├── broadsheet.css  Design tokens and base components (Broadsheet design system)
│   ├── theme-dark.css  Dark-ground token overrides
│   └── app.css         Layout for this app only
└── js/
    ├── data.js         Generated pipeline export (window.COHORT_LENS_DATA); data.json is the same as JSON
    ├── loader.js       Picks static export or live API, then starts app.js
    └── app.js          State, rendering for each step, and event handling
```

## How the code works

- **State.** Everything lives in a single `state` object in `app.js`: the current step, the selected participant, the marked sources, the threshold, the sort order and the associate's decisions.
- **Rendering.** Every change calls `render()`, which rebuilds `#app` from template strings. Each step has its own function, `step1()` through `step5()`.
- **Events.** One click listener on the document reads the `data-action` attribute of whatever was clicked (`select`, `open-cite`, `toggle-focus`, `decide`, and so on).
- **Scoring.** `analyse()` works out each participant's reviewer votes, consensus (the mean confidence), spread (standard deviation), citation faults and routing. A reviewer counts as dissenting when its confidence is below 0.5.

## Connecting real agents

This is already wired to the CohortLens backend in `../cohortlens`. Full contract: `../cohortlens/docs/UI_INTEGRATION.md`.

- **Static:** `python -m cohortlens export-frontend --out ../cohort-lens/js --with-eval` regenerates `js/data.js` from a real pipeline run, plus an evaluation for the assurance panel in step 5.
- **Live:** `python -m cohortlens serve`, then open http://localhost:8000. The UI is served with the API attached. Alternatively, open `index.html?api=http://localhost:8000`.
  - Step 1 uploads the CSVs in `../synthetic data/` to the approved-source registry.
  - *Run primary agent* starts a real run and streams its progress.
  - Worklist decisions are posted to the human-review queue and the audit log.
- `js/loader.js` chooses the source. `app.js` reads `window.COHORT_LENS_DATA` in the shape documented above, plus `routing`, `humanReview`, `rules`, `dataQueries`, `adversarial` and `cohorts`.
- Reviewer confidence is derived from each seat's structured critique. The exact mapping is in `meta.confidenceNote`.

## Design

The look follows the **Broadsheet** design system: Source Serif 4 throughout, a paper-white background, cyan for interactive elements and magenta for dissent and failures. The step numerals are printed as misregistered CMYK plates (`.cmyk-num`). The font loads from Google Fonts through `broadsheet.css`.
