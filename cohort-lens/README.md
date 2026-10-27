# Cohort Lens

**Adversarial safety review for a Data Safety Monitoring Board (DSMB).**

Cohort Lens is a front-end prototype for checking an AI safety agent's work. It takes the documents from a phase 1 cardiovascular trial, has a primary agent issue a PASS / REVIEW / FAIL verdict for each participant with citations, then sends a swarm of ten adversarial reviewer agents to challenge every verdict. Wherever the swarm can't agree, or a citation doesn't match its source, the case goes to a research associate's worklist.

> ⚠️ All data in this repository is **synthetic**. Study *CVX-214-101* and its participants are fictional, and no agents actually run — every value is hardcoded in `js/data.js`.

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
│   └── app.css         Layout for this app only
└── js/
    ├── data.js         Synthetic trial data: agents, documents, participants, charter rules
    └── app.js          State, rendering for each step, and event handling
```

## How the code works

- **State.** Everything lives in a single `state` object in `app.js`: the current step, the selected participant, the marked sources, the threshold, the sort order and the associate's decisions.
- **Rendering.** Every change calls `render()`, which rebuilds `#app` from template strings. Each step has its own function, `step1()` through `step5()`.
- **Events.** One click listener on the document reads the `data-action` attribute of whatever was clicked (`select`, `open-cite`, `toggle-focus`, `decide`, and so on).
- **Scoring.** `analyse()` works out each participant's reviewer votes, consensus (the mean confidence), spread (standard deviation), citation faults and routing. A reviewer counts as dissenting when its confidence is below 0.5.

## Connecting real agents

Replace the hardcoded data in `js/data.js` with the output of your pipeline, in the same shape:

- `patients[].verdict`, `rationale` and `cites[]` come from the primary agent. Each citation is `{ doc, loc, quote, ok }`, where `ok: false` means the citation verifier found a mismatch.
- `patients[].ov` holds each reviewer's `[confidence, note]`, keyed by agent.
- `patients[].sev` is the severity rank (0–4) used by "most severe first".

`app.js` doesn't need to change.

## Design

The look follows the **Broadsheet** design system: Source Serif 4 throughout, a paper-white background, cyan for interactive elements and magenta for dissent and failures. The step numerals are printed as misregistered CMYK plates (`.cmyk-num`). The font loads from Google Fonts through `broadsheet.css`.
