"""Deterministic cohort analytics. Every number a DSMB report shows is computed here, never by a model."""
from __future__ import annotations

import math
import statistics
from typing import Callable, Protocol

from .config import analyte, cohort_dose, protocol
from .registry import Registry, to_protocol_units
from .schemas import CaseRecord

RELATED = {"possibly related", "probably related", "definitely related", "related"}

_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
         11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
         20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000}


def t975(df: int) -> float:
    keys = [k for k in sorted(_T975) if k <= df]
    return _T975[keys[-1]] if keys else _T975[1]


class ValueSource(Protocol):
    def participants(self) -> list[str]: ...
    def cohort(self, pid: str) -> int: ...
    def arm(self, pid: str) -> str | None: ...
    def value(self, pid: str, visit: str, field: str) -> float | None: ...
    def aes(self, pid: str) -> list[dict]: ...


class RegistrySource:
    def __init__(self, reg: Registry):
        self.reg = reg

    def participants(self):
        return self.reg.participant_ids()

    def cohort(self, pid):
        return self.reg.cohort_of(pid)

    def arm(self, pid):
        return self.reg.arm(pid)

    def value(self, pid, visit, field):
        return self.reg.value_in_protocol_units(pid, visit, field)

    def aes(self, pid):
        return [r.values for r in self.reg.adverse_events(pid)]


class CaseRecordSource:
    def __init__(self, records: dict[str, CaseRecord], arms: dict[str, str | None] | None = None):
        self.records = records
        self.arms = arms or {}

    def participants(self):
        return list(self.records)

    def cohort(self, pid):
        return self.records[pid].cohort

    def arm(self, pid):
        return self.arms.get(pid) or self.records[pid].arm

    def value(self, pid, visit, field):
        for v in self.records[pid].visits:
            if v.visit == visit and field in v.observations:
                o = v.observations[field]
                return to_protocol_units(field, o.value, o.unit)
        return None

    def aes(self, pid):
        return [ae.model_dump() for ae in self.records[pid].adverse_events]


# --------------------------------------------------------------------------- participant metrics

def baseline_visit() -> str:
    return protocol()["baseline_visit"]


def change_from_baseline(src: ValueSource, pid: str, visit: str, field: str) -> float | None:
    b, v = src.value(pid, baseline_visit(), field), src.value(pid, visit, field)
    return None if b is None or v is None else v - b


def pct_change_from_baseline(src: ValueSource, pid: str, visit: str, field: str) -> float | None:
    b, v = src.value(pid, baseline_visit(), field), src.value(pid, visit, field)
    return None if b in (None, 0) or v is None else (v - b) / b * 100


def x_uln(src: ValueSource, pid: str, visit: str, field: str) -> float | None:
    a = analyte(field) or {}
    v = src.value(pid, visit, field)
    return None if v is None or "uln" not in a else v / a["uln"]


PARTICIPANT_METRICS: dict[str, Callable[[ValueSource, str, str, str], float | None]] = {
    "change_from_baseline": change_from_baseline,
    "pct_change_from_baseline": pct_change_from_baseline,
    "x_uln": x_uln,
}


# --------------------------------------------------------------------------- arm metrics

def arm_members(src: ValueSource, cohort: int | None, arm: str) -> list[str]:
    """cohort=None with arm='placebo' pools placebo participants across all cohorts."""
    return [p for p in src.participants() if src.arm(p) == arm and (cohort is None or src.cohort(p) == cohort)]


def arm_summary(src: ValueSource, cohort: int | None, arm: str, field: str, visit: str, metric: str = "pct_change_from_baseline") -> dict:
    fn = PARTICIPANT_METRICS[metric]
    per = {p: fn(src, p, visit, field) for p in arm_members(src, cohort, arm)}
    vals = [v for v in per.values() if v is not None]
    n = len(vals)
    mean = statistics.fmean(vals) if vals else None
    sd = statistics.stdev(vals) if n > 1 else None
    half = t975(n - 1) * sd / math.sqrt(n) if sd is not None else None
    return {
        "cohort": cohort, "arm": arm, "field": field, "visit": visit, "metric": metric,
        "n": n, "n_missing": len(per) - n, "mean": mean, "sd": sd,
        "ci95_low": mean - half if half is not None else None, "ci95_high": mean + half if half is not None else None,
        "values": per,
    }


def count_ae_participants(src: ValueSource, cohort: int | None, arm: str | None, min_grade: int = 1,
                          serious: bool | None = None, term: str | None = None, related_only: bool = False) -> int:
    n = 0
    for p in src.participants():
        if cohort is not None and src.cohort(p) != cohort:
            continue
        if arm is not None and src.arm(p) != arm:
            continue
        for ae in src.aes(p):
            if int(ae.get("grade") or 0) < min_grade:
                continue
            if serious is not None and bool(ae.get("serious")) != serious:
                continue
            if term and term.lower() not in str(ae.get("term", "")).lower():
                continue
            if related_only and str(ae.get("relatedness", "")).lower() not in RELATED:
                continue
            n += 1
            break
    return n


def efficacy_table(src: ValueSource, visit: str | None = None) -> list[dict]:
    """TG % change and SBP change from baseline per active cohort, plus pooled placebo. Descriptive only."""
    visit = visit or protocol()["primary_efficacy_visit"]
    cohorts = sorted({src.cohort(p) for p in src.participants()})
    rows = []
    for c in cohorts:
        rows.append(_eff_row(src, c, "active", visit, f"CVX-214 {cohort_dose(c)} mg"))
    rows.append(_eff_row(src, None, "placebo", visit, "Placebo (pooled)"))
    return rows


def _eff_row(src, cohort, arm, visit, label) -> dict:
    tg = arm_summary(src, cohort, arm, "triglycerides", visit, "pct_change_from_baseline")
    sbp = arm_summary(src, cohort, arm, "sbp", visit, "change_from_baseline")
    r = lambda x: None if x is None else round(x, 1)  # noqa: E731
    return {
        "label": label, "cohort": cohort, "arm": arm, "visit": visit,
        "tg_pct_change": {"n": tg["n"], "mean": r(tg["mean"]), "ci95": [r(tg["ci95_low"]), r(tg["ci95_high"])]},
        "sbp_change_mmhg": {"n": sbp["n"], "mean": r(sbp["mean"]), "ci95": [r(sbp["ci95_low"]), r(sbp["ci95_high"])]},
        "note": "Descriptive Phase 1 summary; not powered for efficacy inference.",
    }
