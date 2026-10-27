"""The peer-reviewed clinical workflow (architecture S1-S5).

Every stage follows the same loop: Author -> independent blinded Reviewers ->
Author response (accept / rebut with evidence) -> Editor decision, with at most
`max_revision_rounds` revisions. Editors are deterministic code: they consult the
Evidence Verifier and the Protocol Oracle, never a vote between agents.

Reviewers never see the verifier's output, so reviewer catches and misses can be
measured independently of the deterministic checks.
"""
from __future__ import annotations

import itertools
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import analytics
from .agents import (
    AgentError, AgentTask, Backend, CaptureOutput, CapturedValue, CohortOutput, DSMBOutput, NarrativeOutput,
    ResponseOutput, ReviewOutput,
)
from .audit import AuditLog
from .config import SCHEMA_VERSION, cohort_dose, protocol
from .oracle import COHORT_ORDER, PData, evaluate, evaluate_cohort, stale_protocol
from .personas import PERSONAS, S3_REVIEWERS, SPECIALIST_FOR_STREAM
from .registry import Registry
from .schemas import (
    HUMAN_REVIEW_ROUTINGS, ROUTING_SEVERITY, AEEntry, AuthorDraft, AuthorResponse, CaseRecord, Claim, ClaimContent,
    ClaimVerdict, CohortDecisionCode, CohortResult, Critique, CritiqueContent, DataQuery, EditorDecision, EvidenceRef,
    HumanReviewItem, Observation, ParticipantResult, PipelineEvent, ReviewRound, RoutingCode, RunConfig, RunResult,
    RunVersions, SourceRecord, StageResult, VerificationReport, VisitEntry, most_severe,
)
from .synthetic import canonical_hash
from .verifier import VERIFIER_VERSION, Verifier

S1_STREAMS = ["labs", "vitals_ecg", "pathology", "proteomics", "genomics", "ae_meds"]
STAGE_NAMES = {"S1": "Data capture", "S2": "Case record assembly", "S3": "DSMB safety review",
               "S4": "Participant routing", "S5": "Cohort escalation review"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump_record(r: SourceRecord) -> dict:
    return r.model_dump(exclude={"content_hash"})


@dataclass
class PContext:
    """Everything one participant's run sees. Perturbations edit this, never the agents."""
    view: list[SourceRecord]
    framing: dict[str, str] = field(default_factory=dict)          # agent_id or "*" -> preamble (A2)
    system_suffix: dict[str, str] = field(default_factory=dict)    # A3 system-prompt tamper
    model_override: dict[str, str] = field(default_factory=dict)   # A3 model swap
    effort_override: dict[str, str] = field(default_factory=dict)  # A3 effort change
    stale_protocol: bool = False                                   # A3 stale protocol for the DSMB author
    truncate_after: str | None = None                              # A3 context truncation: DSMB author sees visits <= this
    planted: list[tuple[str, CritiqueContent]] = field(default_factory=list)  # A6 (stage, critique)
    verifier_down: bool = False                                    # A3 tool failure

    def for_agent(self, d: dict[str, str], agent_id: str) -> str | None:
        return d.get(agent_id) or d.get("*")


class Pipeline:
    def __init__(self, truth: Registry, backend: Backend, config: RunConfig, run_id: str, audit: AuditLog,
                 emit: Callable[[PipelineEvent], None] | None = None, contexts: dict[str, PContext] | None = None):
        self.truth = truth
        self.backend = backend
        self.cfg = config
        self.run_id = run_id
        self.audit = audit
        self._emit = emit
        self.contexts = contexts or {}
        self.proto = protocol()
        self._seq = itertools.count()
        self._ids = itertools.count(1)

    # ------------------------------------------------------------------ plumbing
    def emit(self, type_: str, stage: str | None = None, subject: str | None = None, **data) -> None:
        if self._emit:
            self._emit(PipelineEvent(run_id=self.run_id, seq=next(self._seq), ts=now(), type=type_, stage=stage,
                                     subject=subject, data=data))

    def nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):05d}"

    def parallel(self, fn, items: list) -> list:
        """Map fn over independent items; concurrent for LLM backends (calls are I/O bound), in order for offline."""
        if self.backend.name == "offline" or len(items) < 2:
            return [fn(x) for x in items]
        with ThreadPoolExecutor(max_workers=min(6, len(items))) as ex:
            return list(ex.map(fn, items))

    def context(self, pid: str) -> PContext:
        if pid not in self.contexts:
            self.contexts[pid] = PContext(view=self.truth.participant_records(pid, include_restricted=True))
        return self.contexts[pid]

    def verifier(self, ctx: PContext) -> Verifier:
        return Verifier(self.truth, available=not ctx.verifier_down)

    def call(self, ctx: PContext, role: str, agent_id: str, stage: str, instruction: str, payload: dict, out_model):
        task = AgentTask(role=role, agent_id=agent_id, stage=stage, instruction=instruction, payload=payload,
                         output_model=out_model, framing=ctx.for_agent(ctx.framing, agent_id),
                         system_suffix=ctx.for_agent(ctx.system_suffix, agent_id),
                         model_override=ctx.for_agent(ctx.model_override, agent_id),
                         effort_override=ctx.for_agent(ctx.effort_override, agent_id), meta={"run_id": self.run_id})
        return self.backend.run(task)

    def to_claims(self, contents: list[ClaimContent], stage: str, author: str, pid: str | None = None) -> list[Claim]:
        out = []
        for c in contents:
            d = c.model_dump()
            if pid and not d.get("participant_id") and d["kind"] not in ("count",) and stage != "S5":
                d["participant_id"] = pid
            out.append(Claim(**d, claim_id=self.nid(f"{stage}-CL"), author_id=author, stage=stage))
        return out

    def review(self, ctx, agent_id: str, role: str, stage: str, instruction: str, payload: dict) -> list[Critique]:
        out: ReviewOutput = self.call(ctx, role, agent_id, stage, instruction, payload, ReviewOutput)
        p = PERSONAS[agent_id]
        return [Critique(**c.model_dump(), critique_id=self.nid(f"{stage}-CR"), reviewer_id=agent_id, reviewer_name=p.name)
                for c in out.critiques]

    def planted(self, ctx: PContext, stage: str, stream: str | None = None) -> list[Critique]:
        out = []
        for st, c in ctx.planted:
            if st != stage:
                continue
            if stream and c.target_record_id and self._stream_of(c.target_record_id) != stream:
                continue
            out.append(Critique(**c.model_dump(), critique_id=self.nid(f"{stage}-CR"), reviewer_id="reviewer_2",
                                reviewer_name="Reviewer 2", planted=True))
        return out

    def _stream_of(self, record_id: str) -> str | None:
        r = self.truth.get(record_id)
        return r.stream if r else None

    def verify_critiques(self, critiques: list[Critique], ver: Verifier, pid: str, blinded: bool) -> None:
        """Check the factual part of each critique (a proposed corrected value) against the source of truth."""
        for c in critiques:
            if c.proposed_value is None or not c.target_record_id or not c.target_field:
                continue
            rec = self.truth.get(c.target_record_id)
            claim = Claim(claim_id=c.critique_id, kind="value", participant_id=pid, visit=rec.visit if rec else c.target_visit,
                          field=c.target_field, value=c.proposed_value, unit=c.proposed_unit,
                          text=c.assertion, evidence=[EvidenceRef(record_id=c.target_record_id, field=c.target_field)])
            c.verdict = ver.verify([claim], blinded).verdicts[0]

    @staticmethod
    def public_critique(c: Critique) -> dict:
        d = c.model_dump(exclude={"planted"})
        d["reviewer_id"] = "reviewer" if c.planted else c.reviewer_id  # authors never learn which critique was planted
        return d

    # ------------------------------------------------------------------ run
    def run(self) -> RunResult:
        t0 = time.time()
        res = RunResult(run_id=self.run_id, status="running", config=self.cfg, started_at=now(),
                        versions=RunVersions(schema_version=SCHEMA_VERSION, protocol_version=self.proto["protocol_version"],
                                             dataset_hash=self.truth.hash, verifier_version=VERIFIER_VERSION,
                                             backend=self.backend.name,
                                             models={a: self.backend.model_for(a) for a in PERSONAS}))
        pids = self.cfg.participants or self.truth.participant_ids()
        if self.cfg.cohorts:
            pids = [p for p in pids if self.truth.cohort_of(p) in self.cfg.cohorts]
        self.audit.append("run_started", {"config": self.cfg.model_dump(exclude={"perturbations"}),
                                          "perturbations": [p.perturbation_id for p in self.cfg.perturbations],
                                          "dataset_hash": self.truth.hash}, self.run_id)
        self.emit("run_started", n_participants=len(pids), mode=self.cfg.mode, backend=self.backend.name)
        workers = 1 if self.backend.name == "offline" else 4
        with ThreadPoolExecutor(max_workers=workers) as ex:
            res.participants = list(ex.map(self.run_participant, pids))
        for c in sorted({self.truth.cohort_of(p) for p in pids}):
            members = self.truth.cohort_participants(c)
            if set(members) <= set(pids):
                res.cohorts.append(self.run_cohort(c, [p for p in res.participants if p.cohort == c]))
        res.review_queue = self.review_queue(res)
        res.status, res.finished_at, res.duration_s = "completed", now(), round(time.time() - t0, 2)
        res.llm_calls = self.backend.calls
        self.audit.append("run_completed", {"routing": {p.participant_id: p.final_routing.value for p in res.participants},
                                            "cohorts": {c.cohort: c.final_decision.value for c in res.cohorts}}, self.run_id)
        self.emit("run_completed", duration_s=res.duration_s, review_items=len(res.review_queue))
        return res

    def run_participant(self, pid: str) -> ParticipantResult:
        ctx = self.context(pid)
        try:
            if self.cfg.mode == "single_agent":
                return self.run_single_agent(pid, ctx)
            return self.run_adversarial(pid, ctx)
        except AgentError as e:
            return self._failed(pid, ctx, str(e))
        except Exception as e:  # noqa: BLE001 - a participant failure must never take down the batch
            return self._failed(pid, ctx, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")

    def _failed(self, pid: str, ctx: PContext, err: str) -> ParticipantResult:
        self.emit("log", subject=pid, level="error", message=err)
        self.audit.append("participant_failed", {"participant_id": pid, "error": err[:500]}, self.run_id)
        return ParticipantResult(participant_id=pid, cohort=self.truth.cohort_of(pid), final_routing=RoutingCode.RQ,
                                 human_review=True, human_review_reasons=["Agent failure - failing closed to human review"],
                                 errors=[err])

    # ------------------------------------------------------------------ adversarial mode
    def run_adversarial(self, pid: str, ctx: PContext) -> ParticipantResult:
        t0 = time.time()
        s1_results, s1_claims, queries, s1_escalated = self.stage_s1(pid, ctx)
        cr, s2, s2_claims = self.stage_s2(pid, ctx, s1_claims, queries)
        s3, s4, routing_info = self.stage_s3_s4(pid, ctx, cr)
        reasons = list(routing_info["reasons"])
        if s1_escalated:
            reasons.append("S1 data discrepancies unresolved after revision rounds")
        if s2.final_outcome == "ESCALATE":
            reasons.append("S2 narrative claims withdrawn after verification")
        final = routing_info["final"]
        if final.value in HUMAN_REVIEW_ROUTINGS:
            reasons.append(f"{final.value} requires medical monitor confirmation")
        released = [c for s in [*s1_results, s2, s3] for c in s.final_claims]
        verdicts = [v for s in [*s1_results, s2, s3] for v in s.released_verdicts]
        result = ParticipantResult(
            participant_id=pid, cohort=self.truth.cohort_of(pid), stages=[*s1_results, s2, s3, s4], case_record=cr,
            agent_initial_routing=routing_info["initial"], agent_final_routing=routing_info["agent"],
            oracle_routing=routing_info["oracle"], final_routing=final, rule_hits=routing_info["rule_hits"],
            human_review=bool(reasons), human_review_reasons=reasons, data_queries=cr.data_queries,
            released_claims=released, released_verdicts=verdicts,
        )
        self.emit("participant_routed", "S4", pid, routing=final.value, oracle=routing_info["oracle"].value,
                  human_review=result.human_review, seconds=round(time.time() - t0, 2))
        return result

    # ------------------------------------------------------------------ S1
    def stage_s1(self, pid: str, ctx: PContext):
        results, claims_all, queries, escalated = [], [], [], False
        jobs = []
        for stream in S1_STREAMS:
            view = [r for r in ctx.view if r.stream == stream]
            truth = [r for r in self.truth.participant_records(pid) if r.stream == stream]
            if view or truth:
                jobs.append((stream, view, truth))
        for sr, claims, qs in self.parallel(lambda j: self._s1_stream(pid, j[0], j[1], j[2], ctx), jobs):
            results.append(sr)
            claims_all += claims
            queries += qs
            escalated |= sr.final_outcome == "ESCALATE"
        return results, claims_all, queries, escalated

    def _s1_stream(self, pid, stream, view, truth, ctx):
        stage, subject = "S1", f"{pid}:{stream}"
        ver = self.verifier(ctx)
        self.emit("stage_started", stage, subject, stream=stream, records=len(view))
        units = {f: a["unit"] for f, a in self.proto["analytes"].items() if a["stream"] == stream}
        cap: CaptureOutput = self.call(ctx, "s1_capture", "cra", stage,
                                       f"Transcribe every value in these {stream} source records for {pid} exactly as written, with units. Report data issues.",
                                       {"participant_id": pid, "stream": stream, "records": [dump_record(r) for r in view],
                                        "protocol_units": units}, CaptureOutput)
        meta = {r.record_id: r for r in view} | {r.record_id: r for r in truth if r.record_id not in {v.record_id for v in view}}
        claims = self._captured_to_claims(pid, cap.values, meta)
        queries = [DataQuery(query_id=self.nid("DQ"), participant_id=pid, record_id=d.record_id, field=d.field,
                             visit=meta[d.record_id].visit if d.record_id in meta else None, issue=d.issue, raised_by="cra")
                   for d in cap.data_issues]
        rounds: list[ReviewRound] = []
        for rnd in range(self.cfg.max_revision_rounds + 1):
            report = ver.verify(claims, blinded=True)
            draft = AuthorDraft(author_id="cra", summary=f"{len(claims)} values captured from {len(view)} {stream} records.",
                                claims=claims, data_queries=list(queries))
            self.emit("author_draft", stage, subject, round=rnd, claims=len(claims))
            self.emit("verification", stage, subject, round=rnd, supported=report.n_supported,
                      contradicted=report.n_contradicted, unverified=report.n_unverified)
            captured = [c.model_dump() for c in claims]
            seats = [("sdv_reviewer", "s1_sdv_review", "Verify each captured value against the original source documents.",
                      {"participant_id": pid, "stream": stream, "captured": captured,
                       "source_documents": [dump_record(r) for r in truth]})]
            if self.cfg.specialist_panel and stream in SPECIALIST_FOR_STREAM:
                seats.append((SPECIALIST_FOR_STREAM[stream], "s1_specialist_review",
                              f"Review the captured {stream} values for units, plausibility and internal consistency.",
                              {"participant_id": pid, "stream": stream, "captured": captured,
                               "protocol_analytes": {f: a for f, a in self.proto["analytes"].items() if a["stream"] == stream},
                               "records": [{"record_id": r.record_id, "text": r.text} for r in view if r.text]}))
            critiques = [c for batch in self.parallel(lambda x: self.review(ctx, x[0], x[1], stage, x[2], x[3]), seats) for c in batch]
            critiques += self.planted(ctx, stage, stream)
            self.verify_critiques(critiques, ver, pid, blinded=True)
            self.emit("critiques", stage, subject, round=rnd, n=len(critiques),
                      by_reviewer=_count_by(critiques, "reviewer_id"))
            response = None
            if critiques:
                rout: ResponseOutput = self.call(ctx, "s1_respond", "cra", stage,
                                                 "Respond to each critique: ACCEPT (and give the corrected value from the cited source) or REBUT with evidence.",
                                                 {"captured": captured, "verification": [v.model_dump() for v in report.verdicts],
                                                  "critiques": [self.public_critique(c) for c in critiques]}, ResponseOutput)
                response = AuthorResponse(author_id="cra", items=rout.items)
                claims = self._apply_corrections(pid, claims, rout.corrected_values, meta)
                accepted = {i.critique_id for i in rout.items if i.decision == "ACCEPT"}
                for c in critiques:
                    if c.critique_id in accepted and not (c.verdict and c.verdict.status == "SUPPORTED"):
                        queries.append(DataQuery(query_id=self.nid("DQ"), participant_id=pid, record_id=c.target_record_id,
                                                 field=c.target_field, visit=c.target_visit, issue=c.assertion, raised_by=c.reviewer_id))
                self.emit("author_response", stage, subject, round=rnd,
                          accepted=sum(i.decision == "ACCEPT" for i in rout.items), rebutted=sum(i.decision == "REBUT" for i in rout.items))
            post = ver.verify(claims, blinded=True)
            editor = self._editor("S1", rnd, post, critiques, response)
            rounds.append(ReviewRound(round=rnd, draft=draft, verification=report, critiques=critiques, response=response, editor=editor))
            self.emit("editor_decision", stage, subject, round=rnd, outcome=editor.outcome, reasons=editor.reasons)
            if editor.outcome != "REVISE":
                break
        final_ver = ver.verify(claims, blinded=True)
        bad = {v.claim_id for v in final_ver.verdicts if v.status == "CONTRADICTED"}
        for c in claims:
            if c.claim_id in bad:
                v = next(x for x in final_ver.verdicts if x.claim_id == c.claim_id)
                queries.append(DataQuery(query_id=self.nid("DQ"), participant_id=pid, record_id=c.evidence[0].record_id if c.evidence else None,
                                         field=c.field, visit=c.visit, issue=f"Captured value conflicts with source: {v.reason}", raised_by="adjudicator"))
        kept = [c for c in claims if c.claim_id not in bad]
        sr = StageResult(stage="S1", stage_name=STAGE_NAMES["S1"], subject=subject, rounds=rounds,
                         final_outcome=rounds[-1].editor.outcome, final_claims=kept,
                         released_verdicts=[v for v in final_ver.verdicts if v.claim_id not in bad])
        self.audit.append("stage_completed", {"stage": "S1", "subject": subject, "outcome": sr.final_outcome,
                                              "claims": len(kept), "excluded": len(bad), "rounds": len(rounds)}, self.run_id)
        self.emit("stage_completed", stage, subject, outcome=sr.final_outcome, released=len(kept), excluded=len(bad))
        return sr, kept, queries

    def _captured_to_claims(self, pid, values: list[CapturedValue], meta: dict[str, SourceRecord]) -> list[Claim]:
        out = []
        for v in values:
            rec = meta.get(v.record_id)
            shown = v.code if v.code is not None else ("missing" if v.value is None else f"{v.value:g}")
            out.append(Claim(claim_id=self.nid("S1-CL"), kind="value", participant_id=pid, visit=rec.visit if rec else None,
                             field=v.field, value=v.value, code=v.code, unit=v.unit, author_id="cra", stage="S1",
                             text=f"{v.record_id}: {v.field} = {shown} {v.unit or ''}".strip(),
                             evidence=[EvidenceRef(record_id=v.record_id, field=v.field)]))
        return out

    def _apply_corrections(self, pid, claims: list[Claim], corrections: list[CapturedValue], meta) -> list[Claim]:
        if not corrections:
            return claims
        idx = {(c.evidence[0].record_id, c.field): i for i, c in enumerate(claims) if c.evidence}
        claims = list(claims)
        for new in self._captured_to_claims(pid, corrections, meta):
            key = (new.evidence[0].record_id, new.field)
            if key in idx:
                claims[idx[key]] = new
            else:
                idx[key] = len(claims)
                claims.append(new)
        return claims

    def _editor(self, stage: str, rnd: int, post: VerificationReport, critiques: list[Critique],
                response: AuthorResponse | None) -> EditorDecision:
        accepted = {i.critique_id for i in (response.items if response else []) if i.decision == "ACCEPT"}
        unresolved = [c.critique_id for c in critiques if c.severity != "minor" and c.critique_id not in accepted
                      and not (c.verdict and c.verdict.status == "CONTRADICTED")]
        reasons = []
        if not post.available:
            return EditorDecision(stage=stage, round=rnd, outcome="ESCALATE", reasons=["Verifier unavailable: failing closed"])
        if post.n_contradicted:
            reasons.append(f"{post.n_contradicted} claim(s) contradicted by source")
        if unresolved:
            reasons.append(f"{len(unresolved)} major critique(s) unresolved")
        outcome = "ACCEPT" if not reasons else ("REVISE" if rnd < self.cfg.max_revision_rounds else "ESCALATE")
        return EditorDecision(stage=stage, round=rnd, outcome=outcome, reasons=reasons or ["All claims verified; no unresolved critiques"],
                              unresolved_critique_ids=unresolved)

    # ------------------------------------------------------------------ S2
    def assemble(self, pid: str, claims: list[Claim], queries: list[DataQuery], ctx: PContext) -> CaseRecord:
        meta = {r.record_id: r for r in self.truth.participant_records(pid)} | {r.record_id: r for r in ctx.view}
        visits: dict[str, VisitEntry] = {}
        aes: dict[str, dict] = {}
        cr = CaseRecord(participant_id=pid, cohort=self.truth.cohort_of(pid), dose_level_mg=cohort_dose(self.truth.cohort_of(pid)))
        for c in claims:
            rid = c.evidence[0].record_id if c.evidence else None
            rec = meta.get(rid or "")
            if rec is None:
                continue
            if rec.stream in ("labs", "vitals_ecg", "proteomics") and rec.visit:
                ve = visits.setdefault(rec.visit, VisitEntry(visit=rec.visit, visit_day=rec.visit_day, date=rec.date))
                ve.observations[c.field] = Observation(value=c.value, code=c.code, unit=c.unit, record_id=rid)
            elif rec.doc_type == "adverse_event":
                aes.setdefault(rid, {"visit": rec.visit})[c.field] = c.code if c.code is not None else c.value
            elif rec.doc_type == "con_med_list" and c.code:
                cr.con_meds.append(c.code)
            elif rec.stream == "genomics" and c.code:
                cr.genomics[c.field] = c.code
            elif rec.stream == "pathology" and c.field in ("impression", "injury_pattern"):
                cr.pathology.append(Observation(code=c.code, record_id=rid))
        for rid, a in aes.items():
            try:
                cr.adverse_events.append(AEEntry(record_id=rid, term=str(a.get("term", "")), grade=int(float(a.get("grade") or 0)),
                                                 serious=str(a.get("serious")).lower() == "true", relatedness=str(a.get("relatedness", "")),
                                                 onset_day=int(float(a["onset_day"])) if a.get("onset_day") is not None else None,
                                                 action_taken=a.get("action_taken"), visit=a.get("visit")))
            except (TypeError, ValueError):
                queries.append(DataQuery(query_id=self.nid("DQ"), participant_id=pid, record_id=rid, issue="Adverse event fields could not be assembled", raised_by="adjudicator"))
        cr.visits = sorted(visits.values(), key=lambda v: (v.visit_day if v.visit_day is not None else 0))
        seen, dq = set(), []
        for q in queries:
            k = (q.record_id, q.field, q.issue)
            if k not in seen:
                seen.add(k)
                dq.append(q)
        cr.data_queries = dq
        return cr

    def stage_s2(self, pid: str, ctx: PContext, s1_claims: list[Claim], queries: list[DataQuery]):
        stage = "S2"
        ver = self.verifier(ctx)
        self.emit("stage_started", stage, pid)
        cr = self.assemble(pid, s1_claims, queries, ctx)
        required = [f for f, a in self.proto["analytes"].items() if a["stream"] in self.proto["required_streams_per_visit"]]
        narr: NarrativeOutput = self.call(ctx, "s2_narrative", "cra", stage,
                                          "Write a short factual narrative of this blinded longitudinal case record. Every number must be a structured claim with citations.",
                                          {"case_record": cr.model_dump(), "baseline_visit": self.proto["baseline_visit"],
                                           "efficacy_visit": self.proto["primary_efficacy_visit"]}, NarrativeOutput)
        claims = self.to_claims(narr.claims, stage, "cra", pid)
        rounds, withdrawn = [], set()
        for rnd in range(self.cfg.max_revision_rounds + 1):
            live = [c for c in claims if c.claim_id not in withdrawn]
            report = ver.verify(live, blinded=True)
            draft = AuthorDraft(author_id="cra", summary=narr.narrative, claims=live, data_queries=cr.data_queries)
            self.emit("verification", stage, pid, round=rnd, supported=report.n_supported, contradicted=report.n_contradicted,
                      unverified=report.n_unverified)
            seats = [("data_manager", "s2_data_manager_review", "Check completeness, chronology and visit windows of this case record.",
                      {"case_record": cr.model_dump(), "visit_schedule": self.proto["visits"], "required_fields": required}),
                     ("narrative_reviewer", "s2_narrative_review", "Check every narrative claim against the case record tables and participant identity.",
                      {"case_record": cr.model_dump(), "narrative": narr.narrative, "claims": [c.model_dump() for c in live],
                       "baseline_visit": self.proto["baseline_visit"]})]
            critiques = [c for batch in self.parallel(lambda x: self.review(ctx, x[0], x[1], stage, x[2], x[3]), seats) for c in batch]
            critiques += self.planted(ctx, stage)
            self.verify_critiques(critiques, ver, pid, blinded=True)
            self.emit("critiques", stage, pid, round=rnd, n=len(critiques), by_reviewer=_count_by(critiques, "reviewer_id"))
            response = None
            if critiques:
                rout: ResponseOutput = self.call(ctx, "s2_respond", "cra", stage,
                                                 "Respond to each critique: ACCEPT (withdraw or query) or REBUT with evidence.",
                                                 {"claims": [c.model_dump() for c in live], "verification": [v.model_dump() for v in report.verdicts],
                                                  "critiques": [self.public_critique(c) for c in critiques]}, ResponseOutput)
                response = AuthorResponse(author_id="cra", items=rout.items)
                accepted = {i.critique_id for i in rout.items if i.decision == "ACCEPT"}
                for c in critiques:
                    if c.critique_id not in accepted:
                        continue
                    if c.target_claim_id:
                        withdrawn.add(c.target_claim_id)
                    else:
                        cr.data_queries.append(DataQuery(query_id=self.nid("DQ"), participant_id=pid, record_id=c.target_record_id,
                                                         field=c.target_field, visit=c.target_visit, issue=c.assertion, raised_by=c.reviewer_id))
            live = [c for c in claims if c.claim_id not in withdrawn]
            post = ver.verify(live, blinded=True)
            editor = self._editor(stage, rnd, post, critiques, response)
            rounds.append(ReviewRound(round=rnd, draft=draft, verification=report, critiques=critiques, response=response, editor=editor))
            self.emit("editor_decision", stage, pid, round=rnd, outcome=editor.outcome, reasons=editor.reasons)
            withdrawn |= {v.claim_id for v in post.verdicts if v.status == "CONTRADICTED"}
            if editor.outcome != "REVISE":
                break
        live = [c for c in claims if c.claim_id not in withdrawn]
        final_ver = ver.verify(live, blinded=True)
        cr.narrative = narr.narrative + (f" [{len(withdrawn)} statement(s) withdrawn after verification.]" if withdrawn else "")
        cr.locked = True
        cr.lock_hash = canonical_hash(cr.model_dump(exclude={"lock_hash", "locked"}))
        rounds[-1].editor.lock_hash = cr.lock_hash
        self.audit.append("record_locked", {"participant_id": pid, "lock_hash": cr.lock_hash, "queries": len(cr.data_queries)}, self.run_id)
        sr = StageResult(stage="S2", stage_name=STAGE_NAMES["S2"], subject=pid, rounds=rounds, final_outcome=rounds[-1].editor.outcome,
                         final_claims=live, released_verdicts=final_ver.verdicts)
        self.emit("stage_completed", stage, pid, outcome=sr.final_outcome, lock_hash=cr.lock_hash)
        return cr, sr, live

    # ------------------------------------------------------------------ S3 + S4
    def stage_s3_s4(self, pid: str, ctx: PContext, cr: CaseRecord):
        arm_rec = next((r for r in ctx.view if r.stream == "treatment_arm"), None)
        unblinded = cr.model_copy(update={"blinded": False, "arm": arm_rec.values.get("arm") if arm_rec else None})
        author_view = unblinded
        if ctx.truncate_after:
            from .config import visit_order
            cut = visit_order(ctx.truncate_after)
            author_view = unblinded.model_copy(update={
                "visits": [v for v in unblinded.visits if visit_order(v.visit) <= cut],
                "adverse_events": [a for a in unblinded.adverse_events if visit_order(a.visit) <= cut]})
        author_proto = stale_protocol() if ctx.stale_protocol else self.proto
        ver = self.verifier(ctx)
        oracle = evaluate(PData.from_case_record(unblinded))
        rounds, feedback = [], []
        initial = agent_routing = None
        released: list[Claim] = []
        verdicts: list[ClaimVerdict] = []
        editor = None
        self.emit("stage_started", "S3", pid)
        for rnd in range(self.cfg.max_revision_rounds + 1):
            out: DSMBOutput = self.call(ctx, "s3_author", "dsmb_chair", "S3",
                                        "Apply the protocol's stopping rules to this unblinded case record. Cite each rule met and propose a routing.",
                                        {"case_record": author_view.model_dump(), "protocol": author_proto, "adjudicator_feedback": feedback},
                                        DSMBOutput)
            initial = initial or out.proposed_routing
            findings = self.to_claims(out.findings, "S3", "dsmb_chair", pid)
            report = ver.verify(findings, blinded=False)
            draft = AuthorDraft(author_id="dsmb_chair", summary=out.rationale, claims=findings, proposed_routing=out.proposed_routing)
            self.emit("author_draft", "S3", pid, round=rnd, routing=out.proposed_routing.value, findings=len(findings))
            payload = {"case_record": unblinded.model_dump(), "protocol": self.proto,
                       "draft": {"proposed_routing": out.proposed_routing.value, "rationale": out.rationale,
                                 "findings": [c.model_dump() for c in findings]}}
            critiques: list[Critique] = [c for batch in self.parallel(
                lambda rid: self.review(ctx, rid, "s3_review", "S3", "Challenge the DSMB author's findings and routing from your seat.", payload),
                S3_REVIEWERS) for c in batch]
            critiques += self.planted(ctx, "S3")
            self.verify_critiques(critiques, ver, pid, blinded=False)
            self.emit("critiques", "S3", pid, round=rnd, n=len(critiques), by_reviewer=_count_by(critiques, "reviewer_id"))
            response, agent_routing = None, out.proposed_routing
            if critiques:
                rout: ResponseOutput = self.call(ctx, "s3_respond", "dsmb_chair", "S3",
                                                 "Respond to each critique: ACCEPT or REBUT with evidence, then give your revised routing (or null if unchanged).",
                                                 {"draft": payload["draft"], "verification": [v.model_dump() for v in report.verdicts],
                                                  "critiques": [self.public_critique(c) for c in critiques], "protocol": author_proto}, ResponseOutput)
                response = AuthorResponse(author_id="dsmb_chair", items=rout.items, revised_routing=rout.revised_routing)
                agent_routing = rout.revised_routing or out.proposed_routing
                self.emit("author_response", "S3", pid, round=rnd, revised_routing=agent_routing.value,
                          accepted=sum(i.decision == "ACCEPT" for i in rout.items), rebutted=sum(i.decision == "REBUT" for i in rout.items))
            # S4 adjudication: oracle agreement + dissent check
            accepted = {i.critique_id for i in (response.items if response else []) if i.decision == "ACCEPT"}
            dissent = [c for c in critiques if c.proposed_routing and c.severity != "minor" and c.critique_id not in accepted
                       and ROUTING_SEVERITY[c.proposed_routing.value] > ROUTING_SEVERITY[agent_routing.value]
                       and not (c.verdict and c.verdict.status == "CONTRADICTED")]
            reasons = []
            if not report.available:
                reasons.append("Verifier unavailable: failing closed")
            if agent_routing != oracle.routing:
                reasons.append(f"Oracle disagreement: agent {agent_routing.value}, oracle {oracle.routing.value} "
                               f"({', '.join(sorted({h.rule_id for h in oracle.rule_hits if h.routing == oracle.routing})) or 'no rules'})")
            if report.n_contradicted:
                reasons.append(f"{report.n_contradicted} finding(s) contradicted by source")
            if dissent:
                reasons.append("Unresolved dissent: " + "; ".join(f"{c.reviewer_name} proposes {c.proposed_routing.value}" for c in dissent))
            outcome = "ACCEPT" if not reasons else ("ESCALATE" if not report.available or rnd >= self.cfg.max_revision_rounds else "REVISE")
            editor = EditorDecision(stage="S4", round=rnd, outcome=outcome, reasons=reasons or ["Agent routing agrees with the protocol oracle; no dissent"],
                                    oracle_routing=oracle.routing, agent_routing=agent_routing,
                                    unresolved_critique_ids=[c.critique_id for c in dissent])
            rounds.append(ReviewRound(round=rnd, draft=draft, verification=report, critiques=critiques, response=response, editor=editor))
            self.emit("editor_decision", "S4", pid, round=rnd, outcome=outcome, reasons=editor.reasons,
                      agent=agent_routing.value, oracle=oracle.routing.value)
            bad = {v.claim_id for v in report.verdicts if v.status == "CONTRADICTED"}
            released = [c for c in findings if c.claim_id not in bad]
            verdicts = [v for v in report.verdicts if v.claim_id not in bad]
            if outcome != "REVISE":
                break
            feedback = editor.reasons + [f"Oracle rule hits: " + "; ".join(f"{h.rule_id}@{h.visit}" for h in oracle.rule_hits)]
        if editor.outcome == "ACCEPT":
            final, human = agent_routing, []
        else:
            final = RoutingCode(most_severe([agent_routing.value, oracle.routing.value] +
                                            [c.proposed_routing.value for r in rounds[-1:] for c in r.critiques
                                             if c.critique_id in editor.unresolved_critique_ids]))
            human = editor.reasons
        s3 = StageResult(stage="S3", stage_name=STAGE_NAMES["S3"], subject=pid, rounds=rounds, final_outcome=editor.outcome,
                         final_claims=released, released_verdicts=verdicts)
        s4 = StageResult(stage="S4", stage_name=STAGE_NAMES["S4"], subject=pid, final_outcome=editor.outcome,
                         final_claims=[Claim(claim_id=self.nid("S4-CL"), kind="routing", participant_id=pid, code=final.value,
                                             text=f"Final routing {final.value}", author_id="adjudicator", stage="S4",
                                             evidence=[EvidenceRef(record_id=r) for h in oracle.rule_hits for r in h.record_ids][:8])])
        self.audit.append("routing_decided", {"participant_id": pid, "initial": initial.value, "agent": agent_routing.value,
                                              "oracle": oracle.routing.value, "final": final.value, "outcome": editor.outcome,
                                              "rounds": len(rounds)}, self.run_id)
        return s3, s4, {"initial": initial, "agent": agent_routing, "oracle": oracle.routing, "final": final,
                        "reasons": human, "rule_hits": oracle.rule_hits}

    # ------------------------------------------------------------------ single-agent baseline
    def run_single_agent(self, pid: str, ctx: PContext) -> ParticipantResult:
        proto = stale_protocol() if ctx.stale_protocol else self.proto
        out: DSMBOutput = self.call(ctx, "single_agent", "single_agent", "S3",
                                    "Review this participant's source records against the protocol and propose a routing with cited findings.",
                                    {"records": [dump_record(r) for r in ctx.view], "protocol": proto}, DSMBOutput)
        claims = self.to_claims(out.findings, "S3", "single_agent", pid)
        report = self.verifier(PContext(view=[])).verify(claims, blinded=False)  # post-hoc, for scoring only
        editor = EditorDecision(stage="S4", round=0, outcome="ACCEPT", reasons=["Single-agent baseline: no peer review, verifier gate or oracle check"],
                                agent_routing=out.proposed_routing)
        stage = StageResult(stage="S3", stage_name="Single-agent review", subject=pid,
                            rounds=[ReviewRound(round=0, draft=AuthorDraft(author_id="single_agent", summary=out.rationale, claims=claims,
                                                                            proposed_routing=out.proposed_routing),
                                                verification=report, editor=editor)],
                            final_outcome="ACCEPT", final_claims=claims, released_verdicts=report.verdicts)
        final = out.proposed_routing
        reasons = [f"{final.value} requires medical monitor confirmation"] if final.value in HUMAN_REVIEW_ROUTINGS else []
        self.emit("participant_routed", "S4", pid, routing=final.value, human_review=bool(reasons))
        return ParticipantResult(participant_id=pid, cohort=self.truth.cohort_of(pid), stages=[stage],
                                 agent_initial_routing=final, agent_final_routing=final, final_routing=final,
                                 human_review=bool(reasons), human_review_reasons=reasons,
                                 released_claims=claims, released_verdicts=report.verdicts)

    # ------------------------------------------------------------------ S5
    def run_cohort(self, cohort: int, parts: list[ParticipantResult]) -> CohortResult:
        dose = cohort_dose(cohort)
        ctxs = {p.participant_id: self.context(p.participant_id) for p in parts}
        arms = {}
        for pid, ctx in ctxs.items():
            rec = next((r for r in ctx.view if r.stream == "treatment_arm"), None)
            arms[pid] = rec.values.get("arm") if rec else None
        routings = {p.participant_id: p.final_routing.value for p in parts}
        oracle = evaluate_cohort(cohort, dose, routings, arms)
        records = {p.participant_id: p.case_record for p in parts if p.case_record}
        efficacy = []
        if records:
            src = analytics.CaseRecordSource(records, arms)
            vis = self.proto["primary_efficacy_visit"]
            for fld, metric, base_metric, unit, label in (("triglycerides", "arm_mean_pct_change", "pct_change_from_baseline", "%", "triglyceride change"),
                                                          ("sbp", "arm_mean_change", "change_from_baseline", "mmHg", "seated SBP change")):
                s = analytics.arm_summary(src, cohort, "active", fld, vis, base_metric)
                rids = [o.record_id for pid in s["values"] for v in records[pid].visits if v.visit in (self.proto["baseline_visit"], vis)
                        for k, o in v.observations.items() if k == fld]
                efficacy.append({"field": fld, "metric": metric, "visit": vis, "unit": unit, "label": label, "n": s["n"],
                                 "mean": s["mean"], "ci95": [s["ci95_low"], s["ci95_high"]], "record_ids": rids})
        part_payload = [{"participant_id": p.participant_id, "arm": arms.get(p.participant_id), "routing": p.final_routing.value,
                         "rules": sorted({h.rule_id for h in p.rule_hits})} for p in parts]
        ctx = PContext(view=[])
        merged = [c for pc in ctxs.values() for c in pc.planted if c[0] == "S5"]
        ctx.planted = merged
        ctx.framing = next((pc.framing for pc in ctxs.values() if pc.framing), {})
        ver = self.verifier(ctx)
        self.emit("stage_started", "S5", f"cohort-{cohort}")
        rounds, feedback, editor = [], [], None
        agent_decision = None
        for rnd in range(self.cfg.max_revision_rounds + 1):
            out: CohortOutput = self.call(ctx, "s5_author", "dsmb_chair", "S5",
                                          "Apply the cohort escalation rules (active-arm participants only) and summarise efficacy descriptively.",
                                          {"cohort": cohort, "dose_mg": dose, "participants": part_payload, "efficacy": efficacy,
                                           "cohort_rules": self.proto["cohort_rules"], "adjudicator_feedback": feedback}, CohortOutput)
            claims = self.to_claims(out.findings, "S5", "dsmb_chair")
            report = ver.verify(claims, blinded=False)
            draft = AuthorDraft(author_id="dsmb_chair", summary=out.rationale, claims=claims, proposed_cohort_decision=out.decision)
            critiques: list[Critique] = []
            response = None
            agent_decision = out.decision
            if self.cfg.mode == "adversarial":
                payload = {"cohort": cohort, "participants": part_payload, "cohort_rules": self.proto["cohort_rules"],
                           "draft": {"decision": out.decision.value, "rationale": out.rationale, "findings": [c.model_dump() for c in claims]}}
                critiques += [c for batch in self.parallel(
                    lambda rid: self.review(ctx, rid, "s5_review", "S5", "Challenge the cohort decision from your seat.", payload),
                    S3_REVIEWERS) for c in batch]
                critiques += self.planted(ctx, "S5")
                if critiques:
                    rout: ResponseOutput = self.call(ctx, "s5_respond", "dsmb_chair", "S5",
                                                     "Respond to each critique and give the revised cohort decision (or null if unchanged).",
                                                     {**payload, "critiques": [self.public_critique(c) for c in critiques]}, ResponseOutput)
                    response = AuthorResponse(author_id="dsmb_chair", items=rout.items, revised_cohort_decision=rout.revised_cohort_decision)
                    agent_decision = rout.revised_cohort_decision or out.decision
            reasons = []
            if self.cfg.mode == "adversarial":
                if agent_decision != oracle.decision:
                    reasons.append(f"Cohort oracle disagreement: agent {agent_decision.value}, oracle {oracle.decision.value}")
                if report.n_contradicted:
                    reasons.append(f"{report.n_contradicted} cohort finding(s) contradicted by source")
                if not report.available:
                    reasons.append("Verifier unavailable: failing closed")
            outcome = "ACCEPT" if not reasons else ("REVISE" if rnd < self.cfg.max_revision_rounds and report.available else "ESCALATE")
            editor = EditorDecision(stage="S5", round=rnd, outcome=outcome, reasons=reasons or ["Cohort decision agrees with the cohort rules"],
                                    oracle_cohort_decision=oracle.decision, agent_cohort_decision=agent_decision)
            rounds.append(ReviewRound(round=rnd, draft=draft, verification=report, critiques=critiques, response=response, editor=editor))
            if outcome != "REVISE":
                break
            feedback = reasons
        if editor.outcome == "ESCALATE":
            final = max([agent_decision, oracle.decision], key=lambda d: COHORT_ORDER[d.value])
        else:
            final = agent_decision
        human = list(editor.reasons) if editor.outcome == "ESCALATE" else []
        if final.value in ("R5_PAUSE", "DE_ESCALATE"):
            human.append(f"{final.value} requires DSMB sign-off")
        bad = {v.claim_id for v in rounds[-1].verification.verdicts if v.status == "CONTRADICTED"}
        stage = StageResult(stage="S5", stage_name=STAGE_NAMES["S5"], subject=f"cohort-{cohort}", rounds=rounds, final_outcome=editor.outcome,
                            final_claims=[c for c in rounds[-1].draft.claims if c.claim_id not in bad],
                            released_verdicts=[v for v in rounds[-1].verification.verdicts if v.claim_id not in bad])
        self.audit.append("cohort_decided", {"cohort": cohort, "agent": agent_decision.value, "oracle": oracle.decision.value,
                                             "final": final.value}, self.run_id)
        self.emit("cohort_decided", "S5", f"cohort-{cohort}", decision=final.value, oracle=oracle.decision.value)
        return CohortResult(cohort=cohort, dose_mg=dose, stage=stage, agent_decision=agent_decision, oracle_decision=oracle.decision,
                            final_decision=final, human_review=bool(human), human_review_reasons=human,
                            efficacy=[{k: v for k, v in e.items() if k != "record_ids"} for e in efficacy])

    # ------------------------------------------------------------------ human medical monitor queue
    def review_queue(self, res: RunResult) -> list[HumanReviewItem]:
        items = []
        for p in res.participants:
            if p.human_review:
                items.append(HumanReviewItem(item_id=f"{self.run_id}:{p.participant_id}", run_id=self.run_id, subject=p.participant_id,
                                             participant_id=p.participant_id, cohort=p.cohort, routing=p.final_routing.value,
                                             reasons=p.human_review_reasons))
        for c in res.cohorts:
            if c.human_review:
                items.append(HumanReviewItem(item_id=f"{self.run_id}:cohort-{c.cohort}", run_id=self.run_id, subject=f"cohort-{c.cohort}",
                                             cohort=c.cohort, routing=c.final_decision.value, reasons=c.human_review_reasons))
        return items


def _count_by(items, attr) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        k = getattr(i, attr)
        out[k] = out.get(k, 0) + 1
    return out
