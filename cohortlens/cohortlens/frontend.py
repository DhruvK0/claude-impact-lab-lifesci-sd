"""Adapter from pipeline output to the Cohort Lens front end (window.COHORT_LENS_DATA).

The front end (github.com/Draider2001/impact-lab, cohort-lens/) expects
  agents[], docs[], patients[], rules[], defaultFocus[]
This module fills that shape from a real RunResult; nothing here is invented.
Reviewer "confidence" is derived from each seat's structured critiques, using
the fixed mapping in CONFIDENCE (documented in meta.confidenceNote).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .config import cohort_dose, protocol
from .evals import mode_metrics
from .registry import Registry, to_protocol_units
from .schemas import ROUTING_SEVERITY, Claim, ParticipantResult, RunResult, SourceRecord

AGENTS = [
    ("sdv_reviewer", "Source data verifier", "Every value vs source", "Every transcribed value matched its source document."),
    ("spec_labs", "Clinical chemist", "Lipids, liver, kidney, units", "Lab values in protocol units and plausible ranges."),
    ("spec_vitals_ecg", "Cardiologist", "BP, heart rate, QTcF", "Vitals and QTcF consistent with this participant's baseline."),
    ("spec_pathology", "Pathologist", "Urinalysis, interpretive comments", "Pathology reports transcribed without concern."),
    ("spec_proteomics", "Proteomics scientist", "ApoC-III, Lp(a), hs-CRP (exploratory)", "Exploratory biomarkers consistent; not decision-driving."),
    ("spec_genomics", "Statistical geneticist", "Lipid genes, PGx (research use)", "Genotype calls well-formed; exploratory only."),
    ("spec_ae_meds", "Pharmacovigilance", "AE grading, causality, con-meds", "Adverse events and medications captured consistently."),
    ("data_manager", "Data manager", "Completeness, visit windows", "All scheduled visits present, in order and in window."),
    ("narrative_reviewer", "Medical writer", "Narrative vs tables, identity", "Narrative matches the case record."),
    ("clinical_safety", "Clinical safety physician", "Hunts for under-triage", "No stopping rule missed."),
    ("statistical", "Biostatistician", "Numbers, denominators, comparators", "Numbers and denominators check out."),
    ("protocol_compliance", "Protocol auditor", "Rule mapping, protocol version", "Routing maps correctly to the protocol in force."),
    ("cite", "Citation verifier", "Deterministic: every claim vs source", ""),
    ("oracle", "Protocol oracle", "Deterministic stopping rules", ""),
]
CONFIDENCE = {"none": 0.95, "resolved": 0.72, "minor": 0.62, "major": 0.3, "critical": 0.1}
VERDICT = {"R0": "PASS", "R1": "REVIEW", "RQ": "REVIEW", "R2": "FAIL", "R3": "FAIL", "R4": "FAIL"}
SEV = {"R0": 0, "RQ": 1, "R1": 2, "R2": 3, "R3": 4, "R4": 4}
DOC_FOR_STREAM = {"labs": "LAB", "vitals_ecg": "VIT", "pathology": "PATH", "proteomics": "PRO", "genomics": "GEN",
                  "ae_meds": "AE", "treatment_arm": "ARM"}


def _source_line(rec: SourceRecord | None, field: str | None) -> str:
    if rec is None:
        return "Record not found in the approved registry."
    a = protocol()["analytes"].get(field or "")
    if field and field in rec.values and a:
        v = rec.values[field]
        unit = rec.units.get(field, a["unit"])
        return f"{rec.participant_id} · {rec.visit} (Day {rec.visit_day}) · {a['label']} {'missing' if v is None else f'{v:g}'} {unit}"
    if rec.text:
        return rec.text if len(rec.text) <= 260 else rec.text[:257] + "…"
    shown = ", ".join(f"{k} {v:g}" if isinstance(v, (int, float)) and not isinstance(v, bool) else f"{k} {v}" for k, v in list(rec.values.items())[:6])
    return f"{rec.participant_id} · {rec.visit} (Day {rec.visit_day}) · {shown}"


def _cite(reg: Registry, c: Claim, ok: bool, claimed: str | None = None, stage: str = "") -> dict:
    ev = c.evidence[0] if c.evidence else None
    rec = reg.get(ev.record_id) if ev else None
    field = ev.field if ev else None
    if rec and not field and c.field:
        field = next((f for f in re.split(r"[/+]", c.field) if f in rec.values), None)
    loc = (ev.record_id + (f" · {field}" if field else "")) if ev else "no citation"
    out = {"doc": DOC_FOR_STREAM.get(rec.stream, "SRC") if rec else "SRC", "loc": (f"{stage} · " if stage else "") + loc,
           "quote": _source_line(rec, field), "ok": ok, "claim": c.text}
    if claimed:
        out["claimed"] = claimed
    return out


def _signals(reg: Registry, p: ParticipantResult) -> str:
    cr = p.case_record
    if cr is None:
        return "Case record unavailable"

    def val(visit, fld):
        for v in cr.visits:
            if v.visit == visit and fld in v.observations:
                o = v.observations[fld]
                return to_protocol_units(fld, o.value, o.unit)
        return None
    parts = []
    s0, s6 = val("V2", "sbp"), val("V6", "sbp")
    if s0 is not None and s6 is not None:
        parts.append(f"SBP {s0:.0f}→{s6:.0f}")
    t0, t6 = val("V2", "triglycerides"), val("V6", "triglycerides")
    if t0 and t6 is not None:
        parts.append(f"TG {(t6 - t0) / t0 * 100:+.0f}%".replace("-", "−"))
    alts = [x for x in (val(v.visit, "alt") for v in cr.visits) if x is not None]
    if alts:
        parts.append(f"ALT max {max(alts) / 40:.1f}× ULN")
    q0 = val("V2", "qtcf")
    qs = [x for x in (val(v.visit, "qtcf") for v in cr.visits if v.visit not in ("V1", "V2")) if x is not None]
    if q0 and qs:
        parts.append(f"ΔQTcF max {max(qs) - q0:+.0f} ms")
    g = [ae for ae in cr.adverse_events if ae.grade >= 2]
    if g:
        parts.append(f"AE G{max(a.grade for a in g)}")
    return " · ".join(parts)


def _ov(reg: Registry, p: ParticipantResult) -> tuple[dict, list[dict]]:
    ov: dict[str, list] = {}
    adversarial = []
    accepted = {i.critique_id: i for s in p.stages for r in s.rounds if r.response for i in r.response.items}
    by_seat: dict[str, list] = {}
    for s in p.stages:
        for r in s.rounds:
            for c in r.critiques:
                if c.planted:
                    item = accepted.get(c.critique_id)
                    adversarial.append({"stage": s.stage, "assertion": c.assertion, "response": item.decision if item else "none",
                                        "rationale": item.rationale if item else ""})
                    continue
                by_seat.setdefault(c.reviewer_id, []).append((s.stage, c, accepted.get(c.critique_id)))
    for key, _, _, ok in AGENTS:
        if key in ("cite", "oracle"):
            continue
        items = by_seat.get(key)
        if not items:
            ov[key] = [CONFIDENCE["none"], ok]
            continue
        verdict_level = [c for st, c, _ in items if st in ("S3", "S4")]
        if verdict_level:
            worst = max(verdict_level, key=lambda c: {"minor": 0, "major": 1, "critical": 2}[c.severity])
            ov[key] = [CONFIDENCE[worst.severity], worst.assertion]
            continue
        unresolved = [c for st, c, it in items if not (it and it.decision == "ACCEPT")]
        if unresolved:
            c = unresolved[0]
            ov[key] = [CONFIDENCE[c.severity], c.assertion]
        else:
            ov[key] = [CONFIDENCE["resolved"], f"Caught before DSMB review and corrected ({len(items)} item{'s' if len(items) > 1 else ''}): {items[0][1].assertion}"]
    # deterministic seats
    contradicted = [(s.stage, v) for s in p.stages for r in s.rounds for v in r.verification.verdicts if v.status == "CONTRADICTED"]
    released_bad = [v for v in p.released_verdicts if v.status != "SUPPORTED"]
    n_rel = len(p.released_verdicts)
    if released_bad:
        ov["cite"] = [0.05, f"{len(released_bad)} released claim(s) not supported: {released_bad[0].reason}"]
    elif contradicted:
        st, v = contradicted[0]
        ov["cite"] = [0.6, f"{len(contradicted)} claim(s) contradicted by source during {st} and removed or corrected before release: {'; '.join(v.diagnosis) or v.reason}"]
    else:
        ov["cite"] = [0.97, f"{n_rel}/{n_rel} released claims verified against the source records."]
    rules = sorted({h.rule_id for h in p.rule_hits if h.routing == p.oracle_routing}) if p.oracle_routing else []
    if p.oracle_routing is None:
        ov["oracle"] = [0.5, "Oracle check not run (single-agent baseline)."]
    elif p.agent_initial_routing == p.oracle_routing:
        ov["oracle"] = [0.97, f"Oracle agrees: {p.oracle_routing.value}" + (f" ({', '.join(rules)})" if rules else " (no rule met)")]
    else:
        ov["oracle"] = [0.08, f"Oracle routes {p.oracle_routing.value} ({', '.join(rules) or 'no rule met'}); primary proposed {p.agent_initial_routing.value}."]
    return ov, adversarial


def _cites(reg: Registry, p: ParticipantResult) -> list[dict]:
    cites: list[dict] = []
    s3 = next((s for s in p.stages if s.stage == "S3"), None)
    if s3 and s3.rounds:
        r0 = s3.rounds[0]
        status = {v.claim_id: v for v in r0.verification.verdicts}
        for c in r0.draft.claims:
            if c.kind in ("rule", "value", "event") and c.evidence:
                v = status.get(c.claim_id)
                ok = bool(v and v.status == "SUPPORTED")
                cites.append(_cite(reg, c, ok, None if ok else c.text, "S3"))
    seen = set()
    for s in p.stages:
        if s.stage == "S3":
            continue
        for r in s.rounds:
            bad = {v.claim_id: v for v in r.verification.verdicts if v.status == "CONTRADICTED"}
            for c in r.draft.claims:
                if c.claim_id in bad and (c.evidence[0].record_id if c.evidence else None, c.field) not in seen:
                    seen.add((c.evidence[0].record_id if c.evidence else None, c.field))
                    cites.append(_cite(reg, c, False, f"{c.text} — {'; '.join(bad[c.claim_id].diagnosis) or bad[c.claim_id].reason}", s.stage))
    if not cites and p.case_record:
        rid = next((o.record_id for v in p.case_record.visits if v.visit == "V6" for o in v.observations.values()), None)
        if rid:
            cites.append({"doc": "LAB", "loc": rid, "quote": _source_line(reg.get(rid), "triglycerides"), "ok": True,
                          "claim": "Primary efficacy visit record"})
    unique, seen_loc = [], set()
    for c in sorted(cites, key=lambda c: c["ok"]):
        if c["loc"] not in seen_loc:
            seen_loc.add(c["loc"])
            unique.append(c)
    return unique[:8]


def build(run: RunResult, reg: Registry, research_associate: str = "Research associate", evaluation: dict | None = None) -> dict:
    proto = protocol()
    rules_cfg = {r["rule_id"]: r for r in proto["stopping_rules"]}
    labels = {k: v["label"] for k, v in proto["routing_codes"].items()}
    patients = []
    for p in sorted(run.participants, key=lambda x: x.participant_id):
        arm = reg.arm(p.participant_id)
        initial = (p.agent_initial_routing or p.final_routing).value
        final = p.final_routing.value
        ov, adversarial = _ov(reg, p)
        s3 = next((s for s in p.stages if s.stage == "S3"), None)
        rationale = s3.rounds[0].draft.summary if s3 and s3.rounds else "; ".join(p.errors) or ""
        perturb = [{"id": x.perturbation_id, "lane": x.lane, "kind": x.kind, "description": x.description}
                   for x in run.config.perturbations if x.participant_id == p.participant_id or (x.kind == "arm_label_flip" and x.cohort == p.cohort)]
        override_to = p.oracle_routing.value if p.oracle_routing and p.oracle_routing.value != initial else None
        patients.append({
            "id": p.participant_id,
            "cohort": "Placebo" if arm == "placebo" else f"{cohort_dose(p.cohort)} mg",
            "cohortNumber": p.cohort,
            "verdict": VERDICT[initial],
            "base": CONFIDENCE["none"],
            "signals": _signals(reg, p),
            "rationale": rationale,
            "cites": _cites(reg, p),
            "ov": ov,
            "sev": SEV[final],
            "reason": "; ".join(p.human_review_reasons) or None,
            "overrideLabel": f"Override → {override_to}" if override_to else "Override routing",
            "humanReview": p.human_review,
            "routing": {"initial": initial, "final": final, "oracle": p.oracle_routing.value if p.oracle_routing else None,
                        "initialLabel": labels[initial], "finalLabel": labels[final]},
            "rules": [{"id": h.rule_id, "routing": h.routing.value, "visit": h.visit, "observed": str(h.observed) if h.observed is not None else None,
                       "description": rules_cfg[h.rule_id]["description"]} for h in p.rule_hits],
            "dataQueries": [{"record": q.record_id, "field": q.field, "issue": q.issue, "raisedBy": q.raised_by} for q in p.data_queries],
            "stages": [{"stage": s.stage, "name": s.stage_name, "subject": s.subject, "outcome": s.final_outcome, "rounds": len(s.rounds)}
                       for s in p.stages],
            "adversarial": perturb,
            "plantedCritiques": adversarial,
            "lockHash": p.case_record.lock_hash if p.case_record else None,
        })
    counts = {s: sum(1 for r in reg.all_records() if r.stream == s) for s in DOC_FOR_STREAM}
    docs = [
        {"code": "PROT", "name": f"Protocol {proto['protocol_id']} v{proto['protocol_version']}", "meta": f"Synthetic · {len(proto['cohorts'])} dose cohorts · {len(proto['visits'])} visits",
         "sections": ["Eligibility", "Dose escalation", "Visit schedule"]},
        {"code": "CHTR", "name": "DSMB stopping rules (protocol §6)", "meta": f"{len(proto['stopping_rules'])} rules · routing R0–R4, RQ",
         "sections": ["Hepatic (HEP)", "QTc (QTC)", "Blood pressure (BP)", "CK / lipase", "Renal & potassium", "Adverse events", "Data quality (DQ)"]},
        {"code": "LAB", "name": "Central lab reports", "meta": f"{counts['labs']} records · 13 analytes", "sections": ["Lipid panel", "ALT / AST / bilirubin", "CK / lipase", "eGFR / creatinine / potassium"]},
        {"code": "VIT", "name": "Vitals & ECG forms", "meta": f"{counts['vitals_ecg']} records", "sections": ["Seated SBP / DBP", "Heart rate", "QTcF"]},
        {"code": "PATH", "name": "Pathology reports", "meta": f"{counts['pathology']} reports", "sections": ["Urinalysis microscopy", "Interpretive comments"]},
        {"code": "PRO", "name": "Proteomics panel (exploratory)", "meta": f"{counts['proteomics']} panels", "sections": ["ApoC-III / ANGPTL3", "Lp(a) / PCSK9", "hs-CRP / NT-proBNP"]},
        {"code": "GEN", "name": "Pharmacogenomic panel (research use)", "meta": f"{counts['genomics']} panels", "sections": ["Lipid genes", "SLCO1B1 / CYP3A5"]},
        {"code": "AE", "name": "Adverse events & con-meds", "meta": f"{counts['ae_meds']} records", "sections": ["Grade ≥ 2 events", "Related events", "Serious AEs", "Concomitant meds"]},
        {"code": "ARM", "name": "Randomization (restricted)", "meta": f"{counts['treatment_arm']} records · DSMB only", "sections": ["Treatment arm"]},
    ]
    rules = [{"doc": "CHTR", "loc": r["rule_id"], "quote": f"{r['description']} → {r['routing']} ({labels[r['routing']]})", "ok": True}
             for r in proto["stopping_rules"] if r["routing"] in ("R4", "R3", "R2")]
    ranked = sorted(patients, key=lambda x: (-x["sev"], x["id"]))
    agents = [{"key": k, "name": n, "focus": f, "ok": ok} for k, n, f, ok in AGENTS]
    metrics = mode_metrics(reg, [run]) if run.participants else {}
    summary = {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in metrics.items()}
    doses = ", ".join(f"{c['dose_mg']}" for c in proto["cohorts"])
    return {
        "agents": agents, "docs": docs, "patients": patients, "rules": rules,
        "defaultFocus": ["CHTR:0", "CHTR:1", "CHTR:2", "CHTR:3", "CHTR:5", "LAB:1", "VIT:2", "AE:0", "AE:2"],
        "cohorts": [{"cohort": c.cohort, "dose": f"{c.dose_mg} mg", "decision": c.final_decision.value, "oracle": c.oracle_decision.value,
                     "agent": c.agent_decision.value if c.agent_decision else None, "humanReview": c.human_review,
                     "reasons": c.human_review_reasons, "efficacy": c.efficacy} for c in run.cohorts],
        "meta": {
            "study": proto["protocol_id"], "protocolVersion": proto["protocol_version"],
            "design": "Phase 1b · multiple ascending dose", "indication": "Blood pressure & triglyceride lowering",
            "participantsLabel": f"{len(patients)} participants · placebo, {doses} mg",
            "dataCut": datetime.now(timezone.utc).strftime("%d %b %Y"), "researchAssociate": research_associate,
            "runId": run.run_id, "mode": run.config.mode, "backend": run.config.backend, "finishedAt": run.finished_at,
            "versions": run.versions.model_dump() if run.versions else None,
            "perturbations": [{"id": x.perturbation_id, "lane": x.lane, "kind": x.kind, "participant": x.participant_id,
                               "description": x.description, "expected": x.expected_class} for x in run.config.perturbations],
            "defaultSelected": ranked[0]["id"] if ranked else None,
            "confidenceNote": ("Reviewer confidence is derived from each seat's structured critiques: no critique 0.95, "
                               "caught-and-corrected before DSMB review 0.72, minor 0.62, major 0.30, critical 0.10. "
                               "Citation verifier and protocol oracle are deterministic code."),
            "runMetrics": summary,
            "evaluation": evaluation,
            "synthetic": True,
        },
    }
