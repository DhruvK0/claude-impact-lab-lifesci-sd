"""Protocol Oracle: deterministic stopping rules and cohort escalation rules.

The oracle runs on a normalized participant view that can be built either from
source records (the truth registry, used for scoring) or from a locked case
record (what the DSMB actually reviewed, used by the S4 adjudicator).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import protocol, visit_order
from .registry import Registry, norm_unit, to_protocol_units
from .schemas import (
    CaseRecord, CohortDecisionCode, CohortOracleResult, OracleResult, RoutingCode, RuleHit, SourceRecord,
    most_severe,
)

RELATED = {"possibly related", "probably related", "definitely related", "related"}


@dataclass
class PObs:
    raw: object
    unit: str | None
    record_id: str


@dataclass
class PVisit:
    visit: str
    day: int | None = None
    date: str | None = None
    obs: dict[str, PObs] = field(default_factory=dict)
    streams: set[str] = field(default_factory=set)


@dataclass
class PData:
    participant_id: str
    cohort: int
    arm: str | None = None
    visits: dict[str, PVisit] = field(default_factory=dict)
    aes: list[dict] = field(default_factory=list)

    @classmethod
    def from_records(cls, records: list[SourceRecord]) -> "PData":
        first = records[0]
        pd = cls(first.participant_id, first.cohort)
        for r in records:
            if r.stream == "treatment_arm":
                pd.arm = r.values.get("arm")  # type: ignore[assignment]
            elif r.doc_type == "adverse_event":
                pd.aes.append({**r.values, "record_id": r.record_id, "visit": r.visit})
            elif r.stream in ("labs", "vitals_ecg", "proteomics") and r.visit:
                pv = pd.visits.setdefault(r.visit, PVisit(r.visit, r.visit_day, r.date))
                pv.streams.add(r.stream)
                for k, v in r.values.items():
                    pv.obs[k] = PObs(v, r.units.get(k), r.record_id)
        return pd

    @classmethod
    def from_case_record(cls, cr: CaseRecord) -> "PData":
        pd = cls(cr.participant_id, cr.cohort, cr.arm)
        for ve in cr.visits:
            pv = pd.visits.setdefault(ve.visit, PVisit(ve.visit, ve.visit_day, ve.date))
            for k, o in ve.observations.items():
                pv.obs[k] = PObs(o.value if o.value is not None else o.code, o.unit, o.record_id)
                pv.streams.add(_stream_of(k))
        for ae in cr.adverse_events:
            pd.aes.append({"term": ae.term, "grade": ae.grade, "serious": ae.serious, "relatedness": ae.relatedness,
                           "onset_day": ae.onset_day, "record_id": ae.record_id, "visit": ae.visit})
        return pd


def _stream_of(fld: str) -> str:
    a = protocol()["analytes"].get(fld)
    return a["stream"] if a else "other"


def _rule(rule_id: str, proto: dict) -> dict:
    return next(r for r in proto["stopping_rules"] if r["rule_id"] == rule_id)


def evaluate(pd: PData, proto: dict | None = None) -> OracleResult:
    proto = proto or protocol()
    analytes = proto["analytes"]
    hits: list[RuleHit] = []

    def hit(rule_id, visit=None, fld=None, observed=None, threshold=None, record_ids=()):
        r = _rule(rule_id, proto)
        hits.append(RuleHit(rule_id=rule_id, routing=RoutingCode(r["routing"]), description=r["description"], visit=visit,
                            field=fld, observed=observed, threshold=threshold, record_ids=[x for x in record_ids if x]))

    def val(pv: PVisit, fld: str) -> tuple[float | None, str | None]:
        o = pv.obs.get(fld)
        if o is None:
            return None, None
        a = analytes.get(fld, {})
        if o.raw is None:
            return None, o.record_id
        if o.unit is not None and norm_unit(o.unit) != norm_unit(a.get("unit")):
            hit("DQ-UNIT", pv.visit, fld, f"{o.raw} {o.unit}", f"protocol unit {a.get('unit')}", [o.record_id])
        v = to_protocol_units(fld, o.raw, o.unit)
        if v is not None and "plausible" in a and not (a["plausible"][0] <= v <= a["plausible"][1]):
            hit("DQ-IMPLAUSIBLE", pv.visit, fld, v, f"{a['plausible'][0]}-{a['plausible'][1]} {a['unit']}", [o.record_id])
            return None, o.record_id
        return v, o.record_id

    required_fields = [k for k, a in analytes.items() if a["stream"] in proto["required_streams_per_visit"]]
    base_visit = pd.visits.get(proto["baseline_visit"]) or pd.visits.get("V1")
    base: dict[str, float | None] = {}
    if base_visit:
        for f in ("qtcf", "sbp", "egfr"):
            o = base_visit.obs.get(f)
            base[f] = to_protocol_units(f, o.raw, o.unit) if o else None

    # schedule completeness and chronology
    v2 = pd.visits.get("V2")
    last_date: str | None = None
    for sv in proto["visits"]:
        pv = pd.visits.get(sv["visit"])
        if pv is None:
            hit("DQ-MISSING", sv["visit"], None, "visit not recorded", "scheduled visit")
            continue
        missing = [f for f in required_fields if f not in pv.obs or pv.obs[f].raw is None]
        for f in missing:
            rid = pv.obs[f].record_id if f in pv.obs else None
            hit("DQ-MISSING", pv.visit, f, None, "required value", [rid])
        if pv.date:
            if last_date and pv.date < last_date:
                hit("DQ-VISIT", pv.visit, None, pv.date, f"after {last_date}")
            last_date = pv.date
            if v2 and v2.date and sv["day"] > 1:
                expected = date.fromisoformat(v2.date).toordinal() + sv["day"] - 1
                if abs(date.fromisoformat(pv.date).toordinal() - expected) > proto["visit_window_days"]:
                    hit("DQ-VISIT", pv.visit, None, pv.date, f"Day {sv['day']} +/- {proto['visit_window_days']}")

    for vname in sorted(pd.visits, key=visit_order):
        pv = pd.visits[vname]
        alt, alt_r = val(pv, "alt")
        ast, ast_r = val(pv, "ast")
        tb, tb_r = val(pv, "total_bilirubin")
        uln = lambda f: analytes[f]["uln"]  # noqa: E731
        hep = max([x for x in (alt / uln("alt") if alt is not None else None, ast / uln("ast") if ast is not None else None) if x is not None], default=None)
        if hep is not None:
            src = [alt_r, ast_r]
            if hep > 3 and tb is not None and tb > 2 * uln("total_bilirubin"):
                hit("HEP-HYS", vname, "alt/ast+total_bilirubin", f"{hep:.1f}x ULN, bilirubin {tb / uln('total_bilirubin'):.1f}x ULN", ">3x and >2x ULN", src + [tb_r])
            if hep > 5:
                hit("HEP-5X", vname, "alt/ast", f"{hep:.1f}x ULN", ">5x ULN", src)
            elif hep > 3:
                hit("HEP-3X", vname, "alt/ast", f"{hep:.1f}x ULN", ">3x ULN", src)
            elif hep > 2:
                hit("HEP-2X", vname, "alt/ast", f"{hep:.1f}x ULN", ">2x ULN", src)
        ck, ck_r = val(pv, "ck")
        if ck is not None:
            x = ck / uln("ck")
            for rid, lim in (("CK-10X", 10), ("CK-5X", 5), ("CK-2.5X", 2.5)):
                if x > lim:
                    hit(rid, vname, "ck", f"{x:.1f}x ULN", f">{lim}x ULN", [ck_r])
                    break
        lip, lip_r = val(pv, "lipase")
        if lip is not None:
            x = lip / uln("lipase")
            if x > 3:
                hit("LIP-3X", vname, "lipase", f"{x:.1f}x ULN", ">3x ULN", [lip_r])
            elif x > 2:
                hit("LIP-2X", vname, "lipase", f"{x:.1f}x ULN", ">2x ULN", [lip_r])
        q, q_r = val(pv, "qtcf")
        if q is not None:
            if q >= 500:
                hit("QTC-500", vname, "qtcf", q, ">=500 ms", [q_r])
            if base.get("qtcf") is not None and vname != proto["baseline_visit"] and vname != "V1":
                d = q - base["qtcf"]
                if d > 60:
                    hit("QTC-D60", vname, "qtcf", f"+{d:.0f} ms", ">60 ms from baseline", [q_r])
                elif d > 30:
                    hit("QTC-D30", vname, "qtcf", f"+{d:.0f} ms", ">30 ms from baseline", [q_r])
        sbp, sbp_r = val(pv, "sbp")
        dbp, dbp_r = val(pv, "dbp")
        if (sbp is not None and sbp < 90) or (dbp is not None and dbp < 50):
            hit("BP-LOW", vname, "sbp/dbp", f"{sbp}/{dbp}", "SBP<90 or DBP<50", [sbp_r])
        elif (sbp is not None and sbp >= 180) or (dbp is not None and dbp >= 110):
            hit("BP-HIGH", vname, "sbp/dbp", f"{sbp}/{dbp}", "SBP>=180 or DBP>=110", [sbp_r])
        elif sbp is not None and base.get("sbp") is not None and base["sbp"] - sbp > 25:
            hit("BP-DROP", vname, "sbp", f"-{base['sbp'] - sbp:.0f} mmHg", ">25 mmHg drop from baseline", [sbp_r])
        k, k_r = val(pv, "potassium")
        if k is not None:
            if k > 5.5:
                hit("K-HIGH", vname, "potassium", k, ">5.5 mmol/L", [k_r])
            elif k > 5.0:
                hit("K-MILD", vname, "potassium", k, ">5.0 mmol/L", [k_r])
        eg, eg_r = val(pv, "egfr")
        if eg is not None and base.get("egfr"):
            decline = (base["egfr"] - eg) / base["egfr"] * 100
            if decline > 30:
                hit("EGFR-30", vname, "egfr", f"-{decline:.0f}%", ">30% decline", [eg_r])
        tg, tg_r = val(pv, "triglycerides")
        if tg is not None and tg >= 1000:
            hit("TG-1000", vname, "triglycerides", tg, ">=1000 mg/dL", [tg_r])
        for f in ("ldl_c", "hdl_c", "total_chol", "creatinine", "glucose", "heart_rate", "weight"):
            val(pv, f)  # unit / plausibility checks only

    for ae in pd.aes:
        grade = int(ae.get("grade") or 0)
        related = str(ae.get("relatedness", "")).lower() in RELATED
        rid = [ae["record_id"]]
        if ae.get("serious") and related:
            hit("SAE-REL", ae.get("visit"), ae.get("term"), f"serious, {ae.get('relatedness')}", "related SAE", rid)
        if grade >= 3:
            hit("AE-G3", ae.get("visit"), ae.get("term"), f"grade {grade}", "grade >= 3", rid)
        elif grade == 2 and related:
            hit("AE-G2-REL", ae.get("visit"), ae.get("term"), f"grade 2, {ae.get('relatedness')}", "related grade 2", rid)

    routing = most_severe([h.routing.value for h in hits])
    return OracleResult(participant_id=pd.participant_id, cohort=pd.cohort, arm=pd.arm, routing=RoutingCode(routing),
                        rule_hits=hits, protocol_version=proto["protocol_version"])


def evaluate_records(records: list[SourceRecord], proto: dict | None = None) -> OracleResult:
    return evaluate(PData.from_records(records), proto)


def evaluate_registry(reg: Registry, pids: list[str] | None = None) -> dict[str, OracleResult]:
    return {p: evaluate_records(reg.participant_records(p, include_restricted=True)) for p in (pids or reg.participant_ids())}


COHORT_ORDER = {"ESCALATE": 0, "REPEAT_DOSE": 1, "DE_ESCALATE": 2, "R5_PAUSE": 3}


def evaluate_cohort(cohort: int, dose_mg: int, routings: dict[str, str], arms: dict[str, str | None]) -> CohortOracleResult:
    active = {p: r for p, r in routings.items() if arms.get(p) == "active"}
    placebo = {p: r for p, r in routings.items() if arms.get(p) != "active"}
    serious = [p for p, r in active.items() if r in ("R2", "R3", "R4")]
    r4 = [p for p, r in active.items() if r == "R4"]
    r3 = [p for p, r in active.items() if r == "R3"]
    if r4 or len(serious) >= 2:
        decision = CohortDecisionCode.R5_PAUSE
        reasons = [f"{len(r4)} active R4" if r4 else f"{len(serious)} active participants at R2 or higher: {', '.join(sorted(serious))}"]
    elif r3:
        decision, reasons = CohortDecisionCode.DE_ESCALATE, [f"one active R3: {r3[0]}"]
    elif serious:
        decision, reasons = CohortDecisionCode.REPEAT_DOSE, [f"one active R2: {serious[0]}"]
    else:
        decision, reasons = CohortDecisionCode.ESCALATE, ["no active participant at R2 or higher"]
    return CohortOracleResult(cohort=cohort, dose_mg=dose_mg, decision=decision, reasons=reasons,
                              active_routings=active, placebo_routings=placebo)


def stale_protocol() -> dict:
    """Protocol v0.9 (superseded): looser hepatic and BP thresholds. Used by the A3 stale-protocol perturbation."""
    import copy
    p = copy.deepcopy(protocol())
    p["protocol_version"] = "0.9.0"
    for r in p["stopping_rules"]:
        if r["rule_id"] == "HEP-3X":
            r["routing"] = "R1"
            r["description"] = "ALT or AST > 3x ULN (v0.9: monitor only)"
        if r["rule_id"] == "BP-LOW":
            r["routing"] = "R1"
    return p
