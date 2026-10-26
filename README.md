# Claude Community Impact Lab: Biotech & Life Sciences (San Diego)

Part II of the Claude Conversation series. Last month we talked about the future of AI in biotech and life sciences. Tonight we build.

**Submission Deadline: 8:00pm**

> **Disclaimer:** Data and resources below are subject to their own access terms, terms of service, usage policies, licensing, API rate limits, and data-use restrictions. Participants must review these before using any data. Use synthetic or public data only: no real proprietary, patient, or unpublished data in any tool.

## Schedule

| Time | |
|---|---|
| 5:45 | Doors, food, find your team |
| 6:15 | Kickoff and walkthrough |
| 6:35 | Build starts. Mentors float |
| 7:45 | Reminder: submit your repo, record your demo |
| 8:00 | Hard deadline. Judges pick finalists |
| 8:15 | Finalist demos: 5 min + 2 min questions |
| 8:45 | Scores and winners |
| 9:00 | Close. The conversations continue |

## How this works

Pick **one** of the three tracks below. Teams are formed in advance (4 people: 2 biotech, 2 builders). Choose **one user, one workflow, one outcome**: a specific scientist doing a specific thing. "A platform" is not a project. Build with Claude Code or the Claude desktop app, and open-source what you make.

## 1. Preventing hallucinations / "confidently wrong" outputs

**The question:** How do we address concerns around AI being "confidently wrong," especially when dealing with scientific data?

**Ideas** (anything with one user, one workflow, one outcome counts):
- **Evidence highlighter:** an AI answer with every claim colored by whether the sources back it.
- **Number, unit and qualifier guard:** catch the right number about the wrong species, dose, population or comparator.
- **Citation checker:** paste a reference list; check that each paper exists and actually says what is claimed.
- **Ask-the-paper with receipts:** Q&A over a paper where every answer sentence links to the exact passage, and it refuses when the paper is silent.
- **Refuter:** a second pass that tries to disprove each claim and says "cannot tell" when it should.
- **Expert eval kit:** domain experts write 20 hard questions with gold answers for their subfield, plus a scoring harness anyone can rerun. (Great for non-coders to lead.)
- **Data-claim checker:** given a CSV and a written conclusion, recompute it in code and say whether it holds.

| Resource | Link | Notes |
|---|---|---|
| SciFact | [github.com/allenai/scifact](https://github.com/allenai/scifact) | 1.4K expert-written scientific claims with supporting or refuting abstracts and evidence sentences. Listed as CC BY-NC 2.0 on its dataset card: check the repo's LICENSE |
| PubMedQA | [huggingface.co/datasets/bigbio/pubmed_qa](https://huggingface.co/datasets/bigbio/pubmed_qa) | Yes/no/maybe questions built from PubMed abstracts, with 1K expert-labeled. Listed as MIT |
| BioASQ | [bioasq.org](https://bioasq.org/) | Biomedical question-answering challenge data. Custom research license: check before use |
| PubMed | [pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov/) | Abstracts for any topic you choose. Respect NCBI rate limits |
| ClinicalTrials.gov | [clinicaltrials.gov](https://clinicaltrials.gov/) | Public trial records: good for checking claims about trial design and results |

*Hackathon tip:* The sets above are not hallucination benchmarks. Turn their questions and claims into a factuality check, and have the domain experts on your team write the tricky cases. Measure your tool: how many errors does it catch, and how many does it miss?

## 2. In-silico testing and discovery

**The question:** What are the opportunities and use cases for in-silico testing and discovery? ("In silico" means done on a computer: predict, simulate or screen before spending wet-lab time.)

**Ideas:**
- **Triage memo with receipts:** ranked hits and flagged compounds from a screen, with every number traceable to the data.
- **Error bars and the next 8:** confidence intervals, and which experiments to run next with a reason each.
- **Assay twin:** simulate a screen to answer "how many replicates do we need?"
- **Public-data mini screen:** pick one target, build a ranking, and validate it honestly on held-out compounds.
- **Experiment design checker:** show whether edge effects or too few replicates would fool you.
- **Literature-to-hypothesis table:** from public abstracts, extract claims into a table and propose testable hypotheses with the evidence behind each.
- **Sequence design triage:** rank candidate guide RNAs or primers with rule-based scores and an explanation.
- **Cohort simulator:** a synthetic cohort showing how inclusion criteria change who qualifies.

| Resource | Link | Notes |
|---|---|---|
| DepMap / PRISM drug screens | [depmap.org/portal/data_page](https://depmap.org/portal/data_page/) | Real dose-response data (PRISM used 8 doses, 4-fold dilutions from 10 µM). Confirm license and file versions on the downloads page |
| ChEMBL | [ebi.ac.uk/chembl](https://www.ebi.ac.uk/chembl/) | Bioactivity data for drug-like molecules |
| PubChem | [pubchem.ncbi.nlm.nih.gov](https://pubchem.ncbi.nlm.nih.gov/) | Compounds and bioassays |
| Therapeutics Data Commons | [tdcommons.ai](https://tdcommons.ai/) | Ready-to-use drug discovery datasets (`pip install PyTDC`). Check each dataset's license |
| RCSB Protein Data Bank | [rcsb.org](https://www.rcsb.org/) | Experimentally determined protein structures |
| AlphaFold DB | [alphafold.ebi.ac.uk](https://alphafold.ebi.ac.uk/) | Predicted protein structures |
| UniProt | [uniprot.org](https://www.uniprot.org/) | Protein sequences and annotations |

*Hackathon tip:* A confident ranking from three replicates is not a reliable ranking. Say how sure your tool is and where it should not be trusted. Or generate a small fictional screen with Claude (with known ground truth) so you can check your own pipeline.

## 3. Protecting proprietary scientific data

**The question:** How do we protect proprietary scientific data?

**Ideas:**
- **Scrubbing gateway:** codenames, compound IDs, sequences and names never leave; the answer stays useful.
- **Policy tiers and audit log:** public / internal / restricted rules, and a log a security lead can read.
- **Red-team your scrubber:** have a model try to re-identify the program from the scrubbed text, and measure it.
- **"What's sensitive here?" scanner:** scan a folder of lab documents and tag what is sensitive and why, before anyone shares it with an AI tool.
- **Synthetic data generator:** realistic fake lab data that keeps the structure but not the secrets, so teams can test AI workflows safely.
- **Safe-to-share summarizer:** aggregates and derived results only, never raw data.
- **Route by risk:** which tasks may use a cloud model, which need a local one, and which must stay human.
- **Lab AI-use policy wizard:** a short questionnaire that outputs a one-page policy and checklist.

| Resource | Link | Notes |
|---|---|---|
| Synthea | [github.com/synthetichealth/synthea](https://github.com/synthetichealth/synthea) | Open-source synthetic patient generator (Apache-2.0); its open data is public domain |
| Gretel synthetic PII dataset | [huggingface.co/datasets/gretelai/synthetic_pii_finance_multilingual](https://huggingface.co/datasets/gretelai/synthetic_pii_finance_multilingual) | Synthetic documents with labeled PII, made for testing PII scanners and de-identification. Financial documents rather than biotech, but a ready-made test set for a scrubber. Listed as Apache-2.0: check the dataset card |
| PubMed Central Open Access subset | [pmc.ncbi.nlm.nih.gov/tools/openftlist](https://pmc.ncbi.nlm.nih.gov/tools/openftlist/) | Full-text scientific articles with real compound names, sequences and methods: realistic documents for a "what's sensitive here?" scanner. Use only this subset for bulk download, and check each article's Creative Commons license |
| bioRxiv API | [api.biorxiv.org](https://api.biorxiv.org/) | Preprints, which read like unpublished work: good for practicing on early-stage content. Licenses vary by preprint |

*Hackathon tips:* There is no open "proprietary" lab data, which is the point. Have Claude generate fictional lab-notebook entries (invented codenames, compounds, sequences, partners and people) and treat them as if they were real. Pseudonymization is not anonymization: potency numbers, cell lines and sequences can identify a program with no names in sight. Do not use clinical de-identification corpora that require a data use agreement (for example n2c2): their terms forbid posting copies.

## Submission Guidelines

- Submit your **repo link** and an optional **2-minute screen recording** of your project working before the 8:00pm deadline. Organizers will share the submission link at kickoff.
- One submission per team. Only your most recent submission is reviewed.

## Required in Your Repo's README

- Team name and members
- Track and problem statement (who is the user, what is the workflow, what is the outcome)
- What the project does (a short paragraph)
- Data sources used (synthetic or public only: say which)
- Architecture and approach, including how Claude was used to build it and how it runs inside the project
- How you measured it, and what it does **not** guarantee
- Demo recording of about 2 minutes (optional, but it makes judging dramatically easier)

## Rules

- **Timing:** Your first commit must come after the build starts at 6:35pm. Earlier development evidence leads to disqualification. No pre-work, so every team starts level.
- **Originality:** Third-party libraries and public datasets are fine. The core project must be built tonight.
- **Teams:** Teams are formed in advance. One submission per team.
- **Data use:** Synthetic or public data only. No real proprietary, patient, or unpublished data. Do not scrape or overload public services. Never commit an API key.
- **Open source:** Your code must be in a public GitHub repo during judging, and stays open source so the community and our partners can pick it up afterward.

## Judging Criteria

Each category is scored 0 to 5. **Real-world impact counts double**, for a maximum of 30 points.

1. **Real-world impact (x2):** Would a scientist in this room use it on Monday? Does it help them trust, decide, or protect?
2. **Rigor:** Does it measure itself (an eval, a score, a leak test)? Does it say what it does not guarantee?
3. **Product:** Is the experience clear on the first try?
4. **Idea:** Is it creative and non-obvious, regardless of the result?
5. **Demo clarity:** Are the problem, user, and outcome obvious? Well-scoped projects that work beat ambitious ones that crash.

Bonus consideration for reusable tools others can build on (CLIs, MCP servers, evals) and for creative combinations of datasets.

## Submissions

| ID | Team Name | GitHub | Problem Statement |
|----|-----------|--------|-------------------|
