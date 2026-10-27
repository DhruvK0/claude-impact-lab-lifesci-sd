"""Evidence Verifier: deterministic claim checks against the source-of-truth registry.

A claim is SUPPORTED only when every citation resolves to an approved record of the
right participant and the asserted content matches what the source says (or what
deterministic code recomputes from it). Content that disagrees with the source is
CONTRADICTED. Anything that cannot be checked is UNVERIFIED, which is never
treated as true. Semantic interpretation of narrative text is not claimed to be
deterministic: quote-backed statements pass only the exact-quote check and are
marked with an unresolved `semantic_alignment` check.
"""
from __future__ import annotations

import math
import re

from . import analytics
from .config import analyte, protocol
from .oracle import evaluate_records
from .registry import Registry, norm_unit, to_protocol_units
from .schemas import Claim, ClaimVerdict, VerificationReport

VERIFIER_VERSION = "1.0.0"

CONTENT_CHECKS = {"value_match", "unit_match", "visit_match", "derived_match", "count_match", "event_exists",
                  "event_attributes", "rule_fires", "routing_match", "quote_exact", "citation_exists",
                  "citation_participant_scope", "citation_field"}
MISSING_CHECKS = {"citation_present", "value_source_found", "metric_supported", "quote_present"}


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _close(a: float, b: float, field: str | None = None, derived: bool = False) -> bool:
    if derived:
        return math.isclose(a, b, rel_tol=0.02, abs_tol=0.5 if (field or "") != "x_uln" else 0.05)
    dec = (analyte(field) or {}).get("decimals", 2) if field else 2
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=0.5 * 10 ** (-dec) + 1e-9)


class Verifier:
    def __init__(self, registry: Registry, available: bool = True):
        self.reg = registry
        self.available = available
        self.src = analytics.RegistrySource(registry)
        self._oracle_cache: dict[str, object] = {}

    # ------------------------------------------------------------------ api
    def verify(self, claims: list[Claim], blinded: bool) -> VerificationReport:
        if not self.available:
            verdicts = [ClaimVerdict(claim_id=c.claim_id, status="UNVERIFIED", human_review=True,
                                     checks_failed=["verifier_available"], reason="Verifier unavailable: failing closed.")
                        for c in claims]
        else:
            verdicts = [self.verify_claim(c, blinded) for c in claims]
        return VerificationReport(
            verifier_version=VERIFIER_VERSION, registry_hash=self.reg.hash, verdicts=verdicts, available=self.available,
            n_supported=sum(v.status == "SUPPORTED" for v in verdicts),
            n_contradicted=sum(v.status == "CONTRADICTED" for v in verdicts),
            n_unverified=sum(v.status == "UNVERIFIED" for v in verdicts),
        )

    def verify_claim(self, c: Claim, blinded: bool) -> ClaimVerdict:
        v = ClaimVerdict(claim_id=c.claim_id, status="UNVERIFIED")
        self._check_citations(c, v, blinded)
        kind_check = {
            "value": self._value, "derived": self._derived, "count": self._count, "event": self._event,
            "rule": self._rule, "routing": self._routing, "statement": self._statement,
        }[c.kind]
        if self.available:
            kind_check(c, v)
        return self._finalize(v)

    # ------------------------------------------------------------------ citations
    def _check_citations(self, c: Claim, v: ClaimVerdict, blinded: bool) -> None:
        if not c.evidence:
            v.checks_failed.append("citation_present")
        for ev in c.evidence:
            rec = self.reg.get(ev.record_id)
            if rec is None:
                v.checks_failed.append("citation_exists")
                v.diagnosis.append(f"fabricated citation: {ev.record_id} is not an approved record")
                continue
            v.checks_passed.append("citation_exists")
            if c.participant_id and rec.participant_id != c.participant_id:
                v.checks_failed.append("citation_participant_scope")
                v.diagnosis.append(f"cites {rec.participant_id}'s record for {c.participant_id}")
            if ev.field and ev.field not in rec.values:
                v.checks_failed.append("citation_field")
                v.diagnosis.append(f"{ev.record_id} has no field '{ev.field}'")
            if ev.quote:
                haystack = _ws(rec.text or "") + " " + _ws(" ".join(str(x) for x in rec.values.values()))
                if _ws(ev.quote) in haystack:
                    v.checks_passed.append("quote_exact")
                else:
                    v.checks_failed.append("quote_exact")
                    v.diagnosis.append(f"quote not found in {ev.record_id}")
            if rec.restricted and blinded:
                v.checks_failed.append("blinding")
                v.diagnosis.append("blinded seat cited a restricted treatment-arm record")
        if blinded and c.arm:
            v.checks_failed.append("blinding")
            v.diagnosis.append("blinded seat asserted a treatment arm")

    # ------------------------------------------------------------------ kinds
    def _value(self, c: Claim, v: ClaimVerdict) -> None:
        if not (c.participant_id and c.field):
            v.checks_unresolved.append("value_source_found")
            return
        rec = None
        for ev in c.evidence:
            r = self.reg.get(ev.record_id)
            if r and c.field in r.values and r.participant_id == c.participant_id:
                rec = r
                break
        if rec is None:
            hit = self.reg.find_value(c.participant_id, c.visit, c.field)
            rec = hit[0] if hit else None
        if rec is None:
            v.checks_unresolved.append("value_source_found")
            v.reason = f"No approved record carries {c.field} for {c.participant_id} at {c.visit}."
            return
        if c.visit and rec.visit and c.visit != rec.visit:
            v.checks_failed.append("visit_match")
            v.diagnosis.append(f"record {rec.record_id} is visit {rec.visit}, claim says {c.visit}")
        truth, unit = rec.values.get(c.field), rec.units.get(c.field)
        if c.code is not None or isinstance(truth, (str, bool)):
            v.expected_code = None if truth is None else str(truth)
            got = c.code if c.code is not None else (None if c.value is None else str(c.value))
            if truth is not None and got is not None and _ws(str(got)) == _ws(str(truth)):
                v.checks_passed.append("value_match")
            else:
                v.checks_failed.append("value_match")
            return
        if truth is None:
            if c.value is None:
                v.checks_passed.append("value_match")
                v.reason = "Source value is missing and the claim reports it as missing."
            else:
                v.checks_failed.append("value_match")
                v.diagnosis.append("source value is missing; claimed value was invented")
            return
        v.expected_value = float(truth)
        if c.value is None:
            v.checks_failed.append("value_match")
            v.diagnosis.append("claim reports a value as missing that the source contains")
            return
        if c.unit and unit and norm_unit(c.unit) != norm_unit(unit):
            claim_std = to_protocol_units(c.field, c.value, c.unit)
            truth_std = to_protocol_units(c.field, truth, unit)
            if claim_std is not None and truth_std is not None and math.isclose(claim_std, truth_std, rel_tol=0.01):
                v.checks_passed.append("value_match")
                v.reason = f"Value correctly converted from source unit {unit}."
            else:
                v.checks_failed.append("unit_match")
                v.diagnosis.extend(self._diagnose(c, float(truth), unit))
            return
        if _close(c.value, float(truth), c.field):
            v.checks_passed.append("value_match")
        else:
            v.checks_failed.append("value_match")
            v.diagnosis.extend(self._diagnose(c, float(truth), unit))

    def _diagnose(self, c: Claim, truth: float, unit: str | None) -> list[str]:
        out: list[str] = []
        a = analyte(c.field or "") or {}
        x = c.value
        if x is None:
            return out
        if a.get("si_factor"):
            f = a["si_factor"]
            if math.isclose(x, truth / f, rel_tol=0.02):
                out.append(f"unit swap (source / {f}, unit label {c.unit or unit})")
            if math.isclose(x, truth * f, rel_tol=0.02):
                out.append(f"unit swap (source x {f})")
        for k in (10, 100):
            if math.isclose(x, truth * k, rel_tol=0.01):
                out.append(f"decimal shift (x{k})")
            if math.isclose(x, truth / k, rel_tol=0.01):
                out.append(f"decimal shift (/{k})")
        if c.participant_id:
            for r in self.reg.participant_records(c.participant_id):
                if r.visit != c.visit and c.field in r.values and isinstance(r.values[c.field], (int, float)) \
                        and _close(x, float(r.values[c.field]), c.field):
                    out.append(f"matches {c.participant_id} {r.visit} (temporal shift)")
            for pid in ([] if out else self.reg.participant_ids()):  # only when nothing explains it better
                if pid == c.participant_id:
                    continue
                hit = self.reg.find_value(pid, c.visit, c.field)
                if hit and isinstance(hit[1], (int, float)) and _close(x, float(hit[1]), c.field):
                    out.append(f"matches {pid} {c.visit} (cross-participant)")
                    break
        if not out:
            out.append(f"value not found in source (source {truth:g} {unit or ''})".strip())
        return out

    def _derived(self, c: Claim, v: ClaimVerdict) -> None:
        if c.value is None or not c.metric or not c.field:
            v.checks_unresolved.append("metric_supported")
            return
        visit = c.visit or protocol()["primary_efficacy_visit"]
        if c.metric in analytics.PARTICIPANT_METRICS and c.participant_id:
            exp = analytics.PARTICIPANT_METRICS[c.metric](self.src, c.participant_id, visit, c.field)
        elif c.metric in ("arm_mean_pct_change", "arm_mean_change") and c.arm:
            base = "pct_change_from_baseline" if c.metric == "arm_mean_pct_change" else "change_from_baseline"
            exp = analytics.arm_summary(self.src, c.cohort, c.arm, c.field, visit, base)["mean"]
        else:
            v.checks_unresolved.append("metric_supported")
            v.reason = f"Metric '{c.metric}' is not deterministically reproducible."
            return
        if exp is None:
            v.checks_unresolved.append("value_source_found")
            return
        v.expected_value = round(exp, 3)
        if _close(c.value, exp, "x_uln" if c.metric == "x_uln" else None, derived=True):
            v.checks_passed.append("derived_match")
        else:
            v.checks_failed.append("derived_match")
            if math.isclose(abs(c.value), abs(exp), rel_tol=0.02):
                v.diagnosis.append("wrong direction of change")
            elif c.metric.endswith("pct_change") or c.metric == "pct_change_from_baseline":
                absolute = (analytics.change_from_baseline(self.src, c.participant_id, visit, c.field)
                            if c.participant_id else None)
                if absolute is not None and math.isclose(c.value, absolute, rel_tol=0.02, abs_tol=0.5):
                    v.diagnosis.append("absolute change reported as percent change")
            v.diagnosis.append(f"recomputed {exp:.1f}, claimed {c.value:g}")

    def _count(self, c: Claim, v: ClaimVerdict) -> None:
        if c.value is None or c.metric not in ("count_ae_participants", "arm_n"):
            v.checks_unresolved.append("metric_supported")
            return
        if c.metric == "arm_n":
            exp = len(analytics.arm_members(self.src, c.cohort, c.arm or "active"))
        else:
            exp = analytics.count_ae_participants(self.src, c.cohort, c.arm, min_grade=c.grade or 1,
                                                  serious=c.serious, term=c.field)
        v.expected_value = float(exp)
        if int(round(c.value)) == exp:
            v.checks_passed.append("count_match")
        else:
            v.checks_failed.append("count_match")
            v.diagnosis.append(f"recounted {exp}, claimed {c.value:g}")

    def _event(self, c: Claim, v: ClaimVerdict) -> None:
        if not c.participant_id or not c.field:
            v.checks_unresolved.append("value_source_found")
            return
        aes = self.reg.adverse_events(c.participant_id)
        cited = {e.record_id for e in c.evidence}
        cand = [a for a in aes if a.record_id in cited] or aes
        match = [a for a in cand if _ws(c.field) in _ws(str(a.values.get("term"))) or _ws(str(a.values.get("term"))) in _ws(c.field)]
        if not match:
            v.checks_failed.append("event_exists")
            v.diagnosis.append(f"no '{c.field}' adverse event for {c.participant_id} in source")
            return
        v.checks_passed.append("event_exists")
        ae = match[0].values
        bad = []
        if c.grade is not None and int(ae.get("grade") or 0) != c.grade:
            bad.append(f"grade {ae.get('grade')} in source, claimed {c.grade}")
        if c.serious is not None and bool(ae.get("serious")) != c.serious:
            bad.append(f"serious={ae.get('serious')} in source")
        if c.relatedness and _ws(c.relatedness) != _ws(str(ae.get("relatedness"))):
            bad.append(f"causality '{ae.get('relatedness')}' in source, claimed '{c.relatedness}'")
        if bad:
            v.checks_failed.append("event_attributes")
            v.diagnosis.extend(bad)
        else:
            v.checks_passed.append("event_attributes")

    def _oracle(self, pid: str):
        if pid not in self._oracle_cache:
            self._oracle_cache[pid] = evaluate_records(self.reg.participant_records(pid, include_restricted=True))
        return self._oracle_cache[pid]

    def _rule(self, c: Claim, v: ClaimVerdict) -> None:
        known = {r["rule_id"] for r in protocol()["stopping_rules"]}
        if not c.rule_id or not c.participant_id:
            v.checks_unresolved.append("value_source_found")
            return
        if c.rule_id not in known:
            v.checks_failed.append("rule_fires")
            v.diagnosis.append(f"rule {c.rule_id} does not exist in protocol {protocol()['protocol_version']}")
            return
        hits = [h for h in self._oracle(c.participant_id).rule_hits if h.rule_id == c.rule_id]
        if c.visit:
            hits = [h for h in hits if h.visit == c.visit]
        if hits:
            v.checks_passed.append("rule_fires")
        else:
            v.checks_failed.append("rule_fires")
            v.diagnosis.append(f"{c.rule_id} is not met by the source data" + (f" at {c.visit}" if c.visit else ""))

    def _routing(self, c: Claim, v: ClaimVerdict) -> None:
        if not c.participant_id or not c.code:
            v.checks_unresolved.append("value_source_found")
            return
        exp = self._oracle(c.participant_id).routing.value
        v.expected_code = exp
        if c.code == exp:
            v.checks_passed.append("routing_match")
        else:
            v.checks_failed.append("routing_match")
            v.diagnosis.append(f"protocol rules give {exp} for the source data")

    def _statement(self, c: Claim, v: ClaimVerdict) -> None:
        if not any(e.quote for e in c.evidence):
            v.checks_unresolved.append("quote_present")
            v.reason = "Qualitative statement without an exact quote cannot be verified."
            return
        v.checks_unresolved.append("semantic_alignment")

    # ------------------------------------------------------------------ status
    @staticmethod
    def _finalize(v: ClaimVerdict) -> ClaimVerdict:
        failed = set(v.checks_failed)
        content_failed = failed & (CONTENT_CHECKS - {"citation_field"})
        if content_failed:
            v.status = "CONTRADICTED"
            v.human_review = True
        elif failed - {"blinding"} or (set(v.checks_unresolved) - {"semantic_alignment"}):
            v.status = "UNVERIFIED"
            v.human_review = True
        elif not (set(v.checks_passed) - {"citation_exists"}):
            v.status = "UNVERIFIED"
        else:
            v.status = "SUPPORTED"
        if "blinding" in failed:
            v.human_review = True
        if not v.reason:
            v.reason = {
                "SUPPORTED": "All citations resolve and the content matches the source.",
                "CONTRADICTED": "Source contradicts the claim: " + "; ".join(v.diagnosis or sorted(content_failed)),
                "UNVERIFIED": "Cannot be verified: " + ", ".join(sorted(failed | set(v.checks_unresolved))),
            }[v.status]
        v.checks_passed = sorted(set(v.checks_passed))
        v.checks_failed = sorted(failed)
        v.checks_unresolved = sorted(set(v.checks_unresolved))
        return v
