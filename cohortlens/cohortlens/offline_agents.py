"""Deterministic offline stand-ins for every agent seat.

Each function receives the same AgentTask payload a Claude agent would and may use
nothing else, so the information boundaries between seats are the same in both
backends (e.g. specialists never see source documents; only SDV does). They are
rule-based and therefore invariant to persona framing.
"""
from __future__ import annotations

import re
import statistics

from .agents import (
    AgentTask, CaptureOutput, CapturedValue, CohortOutput, DataIssue, DSMBOutput, NarrativeOutput, ResponseOutput,
    ReviewOutput,
)
from .oracle import COHORT_ORDER, PData, evaluate
from .registry import norm_unit, to_protocol_units
from .schemas import (
    ROUTING_SEVERITY, CaseRecord, ClaimContent, CohortDecisionCode, CritiqueContent, EvidenceRef, ResponseItem,
    RoutingCode, most_severe,
)

INJECTION = re.compile(r"(ignore (the|all|previous)|system note|ai reviewer|note to ai|route (every|all)|mark all)", re.I)
GENOTYPE = re.compile(r"^([ACGT]/[ACGT]|\*\d+/\*\d+|e\d/e\d|non-carrier|heterozygous|[A-Z]\d+X heterozygous)$")


# --------------------------------------------------------------------------- S1 data capture

def s1_capture(task: AgentTask) -> CaptureOutput:
    values, issues = [], []
    for r in task.payload["records"]:
        for fld, val in r["values"].items():
            numeric = isinstance(val, (int, float)) and not isinstance(val, bool)
            values.append(CapturedValue(record_id=r["record_id"], field=fld, value=float(val) if numeric else None,
                                        code=None if numeric or val is None else str(val), unit=r["units"].get(fld)))
            if val is None:
                issues.append(DataIssue(record_id=r["record_id"], field=fld, issue="Value missing in source document."))
            unit = r["units"].get(fld)
            proto_unit = task.payload.get("protocol_units", {}).get(fld)
            if unit and proto_unit and norm_unit(unit) != norm_unit(proto_unit):
                issues.append(DataIssue(record_id=r["record_id"], field=fld,
                                        issue=f"Recorded in {unit}; protocol unit is {proto_unit}. Transcribed as written."))
        if r.get("text") and INJECTION.search(r["text"]):
            issues.append(DataIssue(record_id=r["record_id"], issue="Source text contains an embedded instruction aimed at AI reviewers; treated as data and not followed."))
    return CaptureOutput(values=values, data_issues=issues)


def s1_sdv_review(task: AgentTask) -> ReviewOutput:
    captured = {(c["evidence"][0]["record_id"], c["field"]): c for c in task.payload["captured"] if c["evidence"]}
    out = []
    for doc in task.payload["source_documents"]:
        for fld, val in doc["values"].items():
            c = captured.get((doc["record_id"], fld))
            src_unit = doc["units"].get(fld)
            if c is None:
                out.append(CritiqueContent(
                    target_record_id=doc["record_id"], target_field=fld, target_visit=doc.get("visit"), severity="major",
                    category="not_transcribed", assertion=f"{doc['record_id']} {fld} = {val} {src_unit or ''} in source but not captured.".strip(),
                    proposed_value=val if isinstance(val, (int, float)) and not isinstance(val, bool) else None,
                    proposed_unit=src_unit, evidence=[EvidenceRef(record_id=doc["record_id"], field=fld)]))
                continue
            got = c["value"] if c["value"] is not None else c["code"]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                ok = c["value"] is not None and abs(c["value"] - val) < 1e-6 and norm_unit(c["unit"]) == norm_unit(src_unit)
            else:
                ok = (got is None and val is None) or (got is not None and str(got).strip().lower() == str(val).strip().lower())
            if not ok:
                out.append(CritiqueContent(
                    target_claim_id=c["claim_id"], target_record_id=doc["record_id"], target_field=fld,
                    target_visit=doc.get("visit"), severity="major", category="value_mismatch",
                    assertion=f"Captured {got} {c['unit'] or ''} but source {doc['record_id']} reads {val} {src_unit or ''}.".replace("  ", " "),
                    proposed_value=val if isinstance(val, (int, float)) and not isinstance(val, bool) else None,
                    proposed_unit=src_unit, evidence=[EvidenceRef(record_id=doc["record_id"], field=fld)]))
    return ReviewOutput(critiques=out)


def s1_specialist_review(task: AgentTask) -> ReviewOutput:
    analytes = task.payload.get("protocol_analytes", {})
    captured = task.payload["captured"]
    out = []
    by_field: dict[str, list[float]] = {}
    for c in captured:
        if c["value"] is not None and c["field"] in analytes:
            std = to_protocol_units(c["field"], c["value"], c["unit"])
            if std is not None:
                by_field.setdefault(c["field"], []).append(std)
    for c in captured:
        fld, a = c["field"], analytes.get(c["field"])
        rid = c["evidence"][0]["record_id"] if c["evidence"] else None
        ev = [EvidenceRef(record_id=rid, field=fld)] if rid else []
        if a and c["value"] is not None:
            if c["unit"] and norm_unit(c["unit"]) != norm_unit(a["unit"]):
                out.append(CritiqueContent(target_claim_id=c["claim_id"], target_record_id=rid, target_field=fld, target_visit=c["visit"],
                                           severity="major", category="unit_error",
                                           assertion=f"{fld} at {c['visit']} is in {c['unit']}, not the protocol unit {a['unit']}; query the site before use.",
                                           evidence=ev))
                continue
            lo, hi = a["plausible"]
            if not lo <= c["value"] <= hi:
                out.append(CritiqueContent(target_claim_id=c["claim_id"], target_record_id=rid, target_field=fld, target_visit=c["visit"],
                                           severity="major", category="implausible",
                                           assertion=f"{fld} {c['value']:g} {a['unit']} at {c['visit']} is outside the plausible range {lo}-{hi}.", evidence=ev))
                continue
            series = by_field.get(fld, [])
            if len(series) >= 3:
                med = statistics.median(series)
                if med > 0 and (c["value"] > 9 * med or c["value"] < med / 9):
                    out.append(CritiqueContent(target_claim_id=c["claim_id"], target_record_id=rid, target_field=fld, target_visit=c["visit"],
                                               severity="major", category="outlier",
                                               assertion=f"{fld} {c['value']:g} at {c['visit']} is ~10x away from this participant's median {med:.3g}; possible decimal or unit error.",
                                               evidence=ev))
        elif a is None and c["code"] is not None and task.payload.get("stream") == "genomics" and not GENOTYPE.match(c["code"]):
            out.append(CritiqueContent(target_claim_id=c["claim_id"], target_record_id=rid, target_field=fld, severity="minor",
                                       category="format", assertion=f"Genotype call '{c['code']}' for {fld} is malformed.", evidence=ev))
    for r in task.payload.get("records", []):
        if r.get("text") and INJECTION.search(r["text"]):
            out.append(CritiqueContent(target_record_id=r["record_id"], severity="minor", category="embedded_instruction",
                                       assertion="Report text contains instructions addressed to AI reviewers. Treat as data only; flag to the site.",
                                       evidence=[EvidenceRef(record_id=r["record_id"])]))
    return ReviewOutput(critiques=out)


def s1_respond(task: AgentTask) -> ResponseOutput:
    items, corrected = [], []
    captured = {c["claim_id"]: c for c in task.payload["captured"]}
    for cr in task.payload["critiques"]:
        verdict = cr.get("verdict") or {}
        if verdict.get("status") == "CONTRADICTED":
            items.append(ResponseItem(critique_id=cr["critique_id"], decision="REBUT",
                                      rationale=f"Verifier contradicts the critique: {verdict.get('reason', '')}",
                                      evidence=cr.get("evidence", [])))
            continue
        items.append(ResponseItem(critique_id=cr["critique_id"], decision="ACCEPT",
                                  rationale="Correcting to the source value." if verdict.get("status") == "SUPPORTED" else "Raising a data query.",
                                  evidence=cr.get("evidence", [])))
        if verdict.get("status") == "SUPPORTED" and cr.get("target_record_id") and cr.get("target_field"):
            prior = captured.get(cr.get("target_claim_id") or "", {})
            corrected.append(CapturedValue(record_id=cr["target_record_id"], field=cr["target_field"], value=cr.get("proposed_value"),
                                           code=prior.get("code") if cr.get("proposed_value") is None else None,
                                           unit=cr.get("proposed_unit")))
    return ResponseOutput(items=items, corrected_values=corrected)


# --------------------------------------------------------------------------- S2 case record

def _cr(task: AgentTask) -> CaseRecord:
    return CaseRecord.model_validate(task.payload["case_record"])


def _obs(cr: CaseRecord, visit: str, fld: str):
    for v in cr.visits:
        if v.visit == visit and fld in v.observations:
            return v.observations[fld]
    return None


def s2_narrative(task: AgentTask) -> NarrativeOutput:
    cr = _cr(task)
    base, eff = task.payload.get("baseline_visit", "V2"), task.payload.get("efficacy_visit", "V6")
    claims: list[ClaimContent] = []
    sentences = [f"{cr.participant_id} (cohort {cr.cohort}, dose level {cr.dose_level_mg} mg; treatment blinded) completed "
                 f"{len(cr.visits)} of 8 scheduled visits."]
    for fld, label, metric, unit in (("triglycerides", "Triglycerides", "pct_change_from_baseline", "%"),
                                     ("sbp", "Seated SBP", "change_from_baseline", "mmHg")):
        b, e = _obs(cr, base, fld), _obs(cr, eff, fld)
        bv = to_protocol_units(fld, b.value, b.unit) if b else None
        ev = to_protocol_units(fld, e.value, e.unit) if e else None
        if bv is None or ev is None:
            sentences.append(f"{label} change could not be computed (missing value).")
            continue
        change = (ev - bv) / bv * 100 if metric == "pct_change_from_baseline" else ev - bv
        claims.append(ClaimContent(kind="derived", participant_id=cr.participant_id, visit=eff, field=fld, metric=metric,
                                   value=round(change, 1), unit=unit,
                                   text=f"{label} changed by {change:+.1f} {unit} from {base} to {eff}.",
                                   evidence=[EvidenceRef(record_id=b.record_id, field=fld), EvidenceRef(record_id=e.record_id, field=fld)]))
        sentences.append(claims[-1].text)
    for ae in cr.adverse_events:
        claims.append(ClaimContent(kind="event", participant_id=cr.participant_id, field=ae.term, grade=ae.grade, serious=ae.serious,
                                   relatedness=ae.relatedness, visit=ae.visit,
                                   text=f"{ae.term} (grade {ae.grade}{', serious' if ae.serious else ''}, {ae.relatedness}) from Day {ae.onset_day}.",
                                   evidence=[EvidenceRef(record_id=ae.record_id)]))
        sentences.append(claims[-1].text)
    if not cr.adverse_events:
        sentences.append("No adverse events recorded.")
    if cr.data_queries:
        sentences.append(f"{len(cr.data_queries)} data quer{'y' if len(cr.data_queries) == 1 else 'ies'} open.")
    return NarrativeOutput(narrative=" ".join(sentences), claims=claims)


def s2_data_manager_review(task: AgentTask) -> ReviewOutput:
    cr = _cr(task)
    sched = task.payload["visit_schedule"]
    required = task.payload["required_fields"]
    present = {v.visit: v for v in cr.visits}
    out, last = [], None
    for sv in sched:
        v = present.get(sv["visit"])
        if v is None:
            out.append(CritiqueContent(target_visit=sv["visit"], severity="major", category="missing_data",
                                       assertion=f"Scheduled visit {sv['visit']} ({sv['label']}) has no captured data."))
            continue
        missing = [f for f in required if f not in v.observations or (v.observations[f].value is None and v.observations[f].code is None)]
        if missing:
            rid = next((v.observations[f].record_id for f in missing if f in v.observations), None)
            out.append(CritiqueContent(target_visit=v.visit, target_record_id=rid, target_field=missing[0], severity="major",
                                       category="missing_data", assertion=f"{v.visit}: required value(s) missing: {', '.join(missing)}."))
        if v.date and last and v.date < last:
            out.append(CritiqueContent(target_visit=v.visit, severity="major", category="temporal",
                                       assertion=f"{v.visit} dated {v.date}, before the previous visit ({last})."))
        last = v.date or last
    return ReviewOutput(critiques=out)


def s2_narrative_review(task: AgentTask) -> ReviewOutput:
    cr = _cr(task)
    out = []
    base = task.payload.get("baseline_visit", "V2")
    for c in task.payload["claims"]:
        for ev in c["evidence"]:
            if cr.participant_id not in ev["record_id"]:
                out.append(CritiqueContent(target_claim_id=c["claim_id"], target_record_id=ev["record_id"], severity="critical",
                                           category="identity", assertion=f"Claim cites {ev['record_id']}, which is not {cr.participant_id}'s record."))
        if c["kind"] == "derived" and c["value"] is not None:
            b, e = _obs(cr, base, c["field"]), _obs(cr, c["visit"], c["field"])
            bv = to_protocol_units(c["field"], b.value, b.unit) if b else None
            evv = to_protocol_units(c["field"], e.value, e.unit) if e else None
            if bv is None or evv is None:
                continue
            exp = (evv - bv) / bv * 100 if c["metric"] == "pct_change_from_baseline" else evv - bv
            if abs(exp - c["value"]) > 0.5:
                out.append(CritiqueContent(target_claim_id=c["claim_id"], severity="major", category="value_mismatch",
                                           assertion=f"Narrative says {c['value']:+.1f}; the table gives {exp:+.1f}.", proposed_value=round(exp, 1)))
    return ReviewOutput(critiques=out)


def s2_respond(task: AgentTask) -> ResponseOutput:
    items = []
    for cr in task.payload["critiques"]:
        verdict = cr.get("verdict") or {}
        decision = "REBUT" if verdict.get("status") == "CONTRADICTED" else "ACCEPT"
        items.append(ResponseItem(critique_id=cr["critique_id"], decision=decision,
                                  rationale="Withdrawn claim / data query raised." if decision == "ACCEPT" else f"Verifier contradicts the critique: {verdict.get('reason', '')}"))
    return ResponseOutput(items=items)


# --------------------------------------------------------------------------- S3 DSMB

def _rule_findings(pid: str, result, cohort: int) -> list[ClaimContent]:
    out = []
    for h in result.rule_hits:
        out.append(ClaimContent(kind="rule", participant_id=pid, cohort=cohort, rule_id=h.rule_id, visit=h.visit, field=h.field,
                                text=f"{h.rule_id} met at {h.visit or 'event'}: {h.observed} ({h.threshold}) -> {h.routing.value}.",
                                evidence=[EvidenceRef(record_id=r) for r in h.record_ids]))
    return out


def s3_author(task: AgentTask) -> DSMBOutput:
    cr = _cr(task)
    proto = task.payload["protocol"]
    res = evaluate(PData.from_case_record(cr), proto)
    findings = _rule_findings(cr.participant_id, res, cr.cohort)
    cited = [r for h in res.rule_hits for r in h.record_ids][:6]
    if not cited:  # an all-clear is a claim about every reviewed record
        cited = sorted({o.record_id for v in cr.visits for o in v.observations.values()})
    findings.append(ClaimContent(kind="routing", participant_id=cr.participant_id, code=res.routing.value,
                                 text=f"Proposed routing {res.routing.value} under protocol {proto['protocol_version']}.",
                                 evidence=[EvidenceRef(record_id=r) for r in cited]))
    rules = sorted({h.rule_id for h in res.rule_hits})
    rationale = (f"Rules met: {', '.join(rules)}. Most severe routing {res.routing.value}." if rules
                 else "No stopping rule met; continue.")
    return DSMBOutput(proposed_routing=res.routing, rationale=rationale, findings=findings)


def s3_review(task: AgentTask) -> ReviewOutput:
    cr = _cr(task)
    proto = task.payload["protocol"]
    draft = task.payload["draft"]
    proposed = draft["proposed_routing"]
    cited = {f.get("rule_id") for f in draft["findings"] if f.get("rule_id")}
    res = evaluate(PData.from_case_record(cr), proto)
    out = []
    if task.agent_id == "clinical_safety":
        missed = [h for h in res.rule_hits if ROUTING_SEVERITY[h.routing.value] > ROUTING_SEVERITY[proposed] and h.rule_id not in cited]
        if missed:
            worst = most_severe([h.routing.value for h in missed])
            out.append(CritiqueContent(severity="critical" if worst in ("R3", "R4") else "major", category="rule_missed",
                                       assertion="Under-triage: " + "; ".join(f"{h.rule_id} at {h.visit} ({h.observed})" for h in missed)
                                       + f" supports {worst}, not {proposed}.", proposed_routing=RoutingCode(worst),
                                       target_record_id=missed[0].record_ids[0] if missed[0].record_ids else None,
                                       evidence=[EvidenceRef(record_id=r) for h in missed for r in h.record_ids][:6]))
    elif task.agent_id == "protocol_compliance":
        rules = {r["rule_id"]: r for r in proto["stopping_rules"]}
        unknown = [r for r in cited if r not in rules]
        if unknown:
            out.append(CritiqueContent(severity="major", category="invented_rule",
                                       assertion=f"Rule(s) {', '.join(unknown)} do not exist in protocol {proto['protocol_version']}."))
        mapped = most_severe([rules[r]["routing"] for r in cited if r in rules])
        if cited and mapped != proposed:
            out.append(CritiqueContent(severity="major", category="routing_mapping", proposed_routing=RoutingCode(mapped),
                                       assertion=f"Under protocol {proto['protocol_version']} the cited rules map to {mapped}, not {proposed}."))
    elif task.agent_id == "statistical":
        for f in draft["findings"]:
            for ev in f.get("evidence", []):
                if cr.participant_id not in ev["record_id"]:
                    out.append(CritiqueContent(severity="major", category="identity", target_record_id=ev["record_id"],
                                               assertion=f"Finding cites {ev['record_id']}, outside {cr.participant_id}'s record."))
    return ReviewOutput(critiques=out)


def s3_respond(task: AgentTask) -> ResponseOutput:
    draft = task.payload["draft"]
    current = draft["proposed_routing"]
    items, accepted = [], [current]
    for cr in task.payload["critiques"]:
        verdict = cr.get("verdict") or {}
        prop = cr.get("proposed_routing")
        if verdict.get("status") == "CONTRADICTED":
            items.append(ResponseItem(critique_id=cr["critique_id"], decision="REBUT",
                                      rationale=f"Source contradicts the critique: {verdict.get('reason', '')}"))
        elif prop and ROUTING_SEVERITY[prop] < ROUTING_SEVERITY[current] and verdict.get("status") != "SUPPORTED":
            items.append(ResponseItem(critique_id=cr["critique_id"], decision="REBUT",
                                      rationale="A less severe routing is not supported by any verified evidence; the rules met still apply."))
        else:
            items.append(ResponseItem(critique_id=cr["critique_id"], decision="ACCEPT", rationale="Evidence supports the critique.",
                                      evidence=cr.get("evidence", [])))
            if prop:
                accepted.append(prop)
    revised = most_severe(accepted)
    return ResponseOutput(items=items, revised_routing=RoutingCode(revised) if revised != current else None)


# --------------------------------------------------------------------------- S5 cohort

def _cohort_decision(participants: list[dict]) -> tuple[str, str]:
    active = [p for p in participants if p["arm"] == "active"]
    serious = [p for p in active if p["routing"] in ("R2", "R3", "R4")]
    if any(p["routing"] == "R4" for p in active) or len(serious) >= 2:
        return "R5_PAUSE", f"{len(serious)} active participants at R2 or higher"
    if any(p["routing"] == "R3" for p in active):
        return "DE_ESCALATE", "one active participant at R3"
    if serious:
        return "REPEAT_DOSE", "one active participant at R2"
    return "ESCALATE", "no active participant at R2 or higher"


def s5_author(task: AgentTask) -> CohortOutput:
    parts = task.payload["participants"]
    decision, why = _cohort_decision(parts)
    findings = [ClaimContent(kind="count", cohort=task.payload["cohort"], arm="active", metric="arm_n",
                             value=float(sum(p["arm"] == "active" for p in parts)),
                             text=f"{sum(p['arm'] == 'active' for p in parts)} active participants reviewed.",
                             evidence=[EvidenceRef(record_id=f"ARM-{p['participant_id']}") for p in parts if p["arm"] == "active"])]
    for row in task.payload.get("efficacy", []):
        if row.get("mean") is None:
            continue
        findings.append(ClaimContent(kind="derived", cohort=task.payload["cohort"], arm="active", field=row["field"],
                                     metric=row["metric"], visit=row["visit"], value=round(row["mean"], 1), unit=row["unit"],
                                     text=f"Mean {row['label']} {row['mean']:+.1f} {row['unit']} (n={row['n']}).",
                                     evidence=[EvidenceRef(record_id=r) for r in row["record_ids"]][:10]))
    return CohortOutput(decision=CohortDecisionCode(decision), rationale=why, findings=findings)


def s5_review(task: AgentTask) -> ReviewOutput:
    parts = task.payload["participants"]
    draft = task.payload["draft"]
    expected, why = _cohort_decision(parts)
    out = []
    if task.agent_id in ("clinical_safety", "protocol_compliance") and COHORT_ORDER[expected] != COHORT_ORDER[draft["decision"]]:
        out.append(CritiqueContent(severity="major", category="cohort_rule",
                                   assertion=f"Cohort rules give {expected} ({why}); draft says {draft['decision']}."))
    if task.agent_id == "statistical":
        n_active = sum(p["arm"] == "active" for p in parts)
        for f in draft["findings"]:
            if f.get("metric") == "arm_n" and f.get("value") is not None and int(f["value"]) != n_active:
                out.append(CritiqueContent(severity="major", category="denominator",
                                           assertion=f"Active denominator is {n_active}, not {int(f['value'])}; placebo must not be counted."))
    return ReviewOutput(critiques=out)


def s5_respond(task: AgentTask) -> ResponseOutput:
    items = [ResponseItem(critique_id=c["critique_id"], decision="ACCEPT", rationale="Applying the cohort rules as written.")
             for c in task.payload["critiques"]]
    expected, _ = _cohort_decision(task.payload["participants"])
    return ResponseOutput(items=items, revised_cohort_decision=CohortDecisionCode(expected) if items else None)


# --------------------------------------------------------------------------- baseline

def single_agent(task: AgentTask) -> DSMBOutput:
    from .schemas import SourceRecord
    recs = [SourceRecord.model_validate(r) for r in task.payload["records"]]
    res = evaluate(PData.from_records(recs), task.payload["protocol"])
    pid, cohort = recs[0].participant_id, recs[0].cohort
    findings = _rule_findings(pid, res, cohort)
    by_id = {r.record_id: r for r in recs}
    seen = set()
    for h in res.rule_hits:
        for rid in h.record_ids:
            r = by_id.get(rid)
            for fld in re.split(r"[/+]", h.field or ""):
                if r and fld in r.values and isinstance(r.values[fld], (int, float)) and (rid, fld) not in seen:
                    seen.add((rid, fld))
                    findings.append(ClaimContent(kind="value", participant_id=pid, visit=r.visit, field=fld,
                                                 value=float(r.values[fld]), unit=r.units.get(fld),
                                                 text=f"{fld} {r.values[fld]} {r.units.get(fld, '')} at {r.visit}.",
                                                 evidence=[EvidenceRef(record_id=rid, field=fld)]))
    return DSMBOutput(proposed_routing=res.routing, rationale=f"Single-agent review: {res.routing.value}.", findings=findings)
