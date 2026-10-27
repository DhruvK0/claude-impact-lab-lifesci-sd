"""Shared data contracts.

Every object the API returns, every agent output and every evaluation artifact is
defined here. `python -m cohortlens export-schemas` writes these as JSON Schema to
`schemas/` so the UI can generate types without importing Python.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

Stream = Literal["labs", "vitals_ecg", "pathology", "proteomics", "genomics", "ae_meds", "treatment_arm"]
STREAMS: tuple[str, ...] = ("labs", "vitals_ecg", "pathology", "proteomics", "genomics", "ae_meds", "treatment_arm")
Scalar = float | int | str | bool | None


# --------------------------------------------------------------------------- routing

class RoutingCode(str, Enum):
    R0 = "R0"  # continue
    R1 = "R1"  # monitor
    RQ = "RQ"  # data query
    R2 = "R2"  # hold
    R3 = "R3"  # discontinue
    R4 = "R4"  # urgent


ROUTING_SEVERITY: dict[str, int] = {"R0": 0, "R1": 1, "RQ": 2, "R2": 3, "R3": 4, "R4": 5}
HUMAN_REVIEW_ROUTINGS = {"R2", "R3", "R4", "RQ"}


def most_severe(codes: list[str]) -> str:
    return max(codes, key=lambda c: ROUTING_SEVERITY[c]) if codes else "R0"


class CohortDecisionCode(str, Enum):
    ESCALATE = "ESCALATE"
    REPEAT_DOSE = "REPEAT_DOSE"
    DE_ESCALATE = "DE_ESCALATE"
    R5_PAUSE = "R5_PAUSE"


# --------------------------------------------------------------------------- source of truth

class SourceRecord(BaseModel):
    """One approved source document. The registry of these is the source of truth."""
    record_id: str
    participant_id: str
    cohort: int
    stream: Stream
    doc_type: str
    visit: str | None = None
    visit_day: int | None = None
    date: str | None = None
    values: dict[str, Scalar] = Field(default_factory=dict)
    units: dict[str, str] = Field(default_factory=dict)
    text: str | None = None
    restricted: bool = False  # treatment-arm records: visible only to unblinded seats
    content_hash: str = ""


class PlantedScenario(BaseModel):
    """Ground-truth signal deliberately placed in the synthetic data, for demos and evals."""
    scenario_id: str
    participant_id: str
    cohort: int
    arm: str
    description: str
    expected_rules: list[str]


class DatasetManifest(BaseModel):
    dataset_id: str
    seed: int
    protocol_id: str
    protocol_version: str
    generated_at: str
    n_participants: int
    n_records: int
    records_by_stream: dict[str, int]
    content_hash: str
    planted_scenarios: list[PlantedScenario]
    synthetic: bool = True


# --------------------------------------------------------------------------- claims & evidence

class EvidenceRef(BaseModel):
    record_id: str = Field(description="ID of an approved source record")
    field: str | None = Field(default=None, description="Field inside the record's values, if any")
    quote: str | None = Field(default=None, description="Exact excerpt copied from the record text, if citing narrative text")


ClaimKind = Literal["value", "derived", "count", "event", "rule", "routing", "statement"]


class ClaimContent(BaseModel):
    """What an agent asserts. Every material finding is expressed as one of these."""
    kind: ClaimKind = Field(description="value=transcribed source value; derived=computed metric; count=participant count; event=adverse event; rule=stopping rule fired; routing=routing code; statement=qualitative statement backed by a quote")
    text: str = Field(description="Human-readable sentence")
    participant_id: str | None = None
    cohort: int | None = None
    arm: Literal["active", "placebo"] | None = None
    visit: str | None = None
    field: str | None = Field(default=None, description="Analyte/vital/proteomic field or AE term")
    value: float | None = None
    unit: str | None = None
    code: str | None = Field(default=None, description="Categorical value: genotype, urinalysis result, routing code")
    metric: str | None = Field(default=None, description="For derived/count claims: x_uln, change_from_baseline, pct_change_from_baseline, arm_mean_change, arm_mean_pct_change, arm_n, count_ae_participants")
    rule_id: str | None = None
    grade: int | None = Field(default=None, description="CTCAE grade (event claims) or minimum grade (count claims)")
    serious: bool | None = None
    relatedness: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class Claim(ClaimContent):
    claim_id: str
    author_id: str = ""
    stage: str = ""


VerdictStatus = Literal["SUPPORTED", "CONTRADICTED", "UNVERIFIED"]


class ClaimVerdict(BaseModel):
    claim_id: str
    status: VerdictStatus
    human_review: bool = False
    checks_passed: list[str] = Field(default_factory=list)
    checks_failed: list[str] = Field(default_factory=list)
    checks_unresolved: list[str] = Field(default_factory=list)
    expected_value: float | None = None
    expected_code: str | None = None
    diagnosis: list[str] = Field(default_factory=list, description="Likely cause of a mismatch, e.g. 'unit swap (/88.57)'")
    reason: str = ""


class VerificationReport(BaseModel):
    verifier_version: str
    registry_hash: str
    verdicts: list[ClaimVerdict]
    n_supported: int = 0
    n_contradicted: int = 0
    n_unverified: int = 0
    available: bool = True  # False when the verifier tool failed: everything fails closed


# --------------------------------------------------------------------------- peer review

Severity = Literal["minor", "major", "critical"]


class CritiqueContent(BaseModel):
    target_claim_id: str | None = None
    target_record_id: str | None = None
    target_field: str | None = None
    target_visit: str | None = None
    severity: Severity
    category: str = Field(description="e.g. value_mismatch, unit_error, missing_data, temporal, identity, rule_missed, overclaim, blinding")
    assertion: str
    proposed_value: float | None = None
    proposed_unit: str | None = None
    proposed_routing: RoutingCode | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class Critique(CritiqueContent):
    critique_id: str
    reviewer_id: str
    reviewer_name: str
    planted: bool = False  # A6 "Reviewer 2" injection; hidden from agents, used for scoring
    verdict: ClaimVerdict | None = None  # verifier result on the critique's own factual assertion


class ResponseItem(BaseModel):
    critique_id: str
    decision: Literal["ACCEPT", "REBUT"]
    rationale: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class DataQuery(BaseModel):
    query_id: str
    participant_id: str
    record_id: str | None = None
    field: str | None = None
    visit: str | None = None
    issue: str
    raised_by: str
    status: Literal["open", "resolved"] = "open"


class AuthorDraft(BaseModel):
    author_id: str
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    proposed_routing: RoutingCode | None = None
    proposed_cohort_decision: CohortDecisionCode | None = None
    data_queries: list[DataQuery] = Field(default_factory=list)


class AuthorResponse(BaseModel):
    author_id: str
    items: list[ResponseItem] = Field(default_factory=list)
    revised_routing: RoutingCode | None = None
    revised_cohort_decision: CohortDecisionCode | None = None


EditorOutcome = Literal["ACCEPT", "REVISE", "ESCALATE"]


class EditorDecision(BaseModel):
    stage: str
    round: int
    outcome: EditorOutcome
    reasons: list[str] = Field(default_factory=list)
    oracle_routing: RoutingCode | None = None
    agent_routing: RoutingCode | None = None
    oracle_cohort_decision: CohortDecisionCode | None = None
    agent_cohort_decision: CohortDecisionCode | None = None
    unresolved_critique_ids: list[str] = Field(default_factory=list)
    lock_hash: str | None = None


class ReviewRound(BaseModel):
    round: int
    draft: AuthorDraft
    verification: VerificationReport
    critiques: list[Critique] = Field(default_factory=list)
    response: AuthorResponse | None = None
    editor: EditorDecision


class StageResult(BaseModel):
    stage: Literal["S1", "S2", "S3", "S4", "S5"]
    stage_name: str
    subject: str  # participant id, "P:stream", or "cohort-N"
    rounds: list[ReviewRound] = Field(default_factory=list)
    final_outcome: EditorOutcome
    final_claims: list[Claim] = Field(default_factory=list)
    released_verdicts: list[ClaimVerdict] = Field(default_factory=list)


# --------------------------------------------------------------------------- case record

class Observation(BaseModel):
    value: float | None = None
    code: str | None = None
    unit: str | None = None
    record_id: str


class VisitEntry(BaseModel):
    visit: str
    visit_day: int | None = None
    date: str | None = None
    observations: dict[str, Observation] = Field(default_factory=dict)


class AEEntry(BaseModel):
    record_id: str
    term: str
    grade: int
    serious: bool
    relatedness: str
    onset_day: int | None = None
    action_taken: str | None = None
    visit: str | None = None


class CaseRecord(BaseModel):
    participant_id: str
    cohort: int
    dose_level_mg: int
    blinded: bool = True
    arm: Literal["active", "placebo"] | None = None
    visits: list[VisitEntry] = Field(default_factory=list)
    adverse_events: list[AEEntry] = Field(default_factory=list)
    con_meds: list[str] = Field(default_factory=list)
    genomics: dict[str, str] = Field(default_factory=dict)
    pathology: list[Observation] = Field(default_factory=list)
    data_queries: list[DataQuery] = Field(default_factory=list)
    narrative: str = ""
    locked: bool = False
    lock_hash: str | None = None


# --------------------------------------------------------------------------- oracle

class RuleHit(BaseModel):
    rule_id: str
    routing: RoutingCode
    description: str
    visit: str | None = None
    field: str | None = None
    observed: float | str | None = None
    threshold: str | None = None
    record_ids: list[str] = Field(default_factory=list)


class OracleResult(BaseModel):
    participant_id: str
    cohort: int
    arm: Literal["active", "placebo"] | None = None
    routing: RoutingCode
    rule_hits: list[RuleHit] = Field(default_factory=list)
    protocol_version: str


class CohortOracleResult(BaseModel):
    cohort: int
    dose_mg: int
    decision: CohortDecisionCode
    reasons: list[str]
    active_routings: dict[str, str]
    placebo_routings: dict[str, str]


# --------------------------------------------------------------------------- perturbations

Lane = Literal["A1", "A2", "A3", "A4", "A5", "A6"]
ExpectedClass = Literal["MUST_DETECT", "MUST_CHANGE", "INVARIANT", "MUST_RESIST"]
PerturbationTarget = Literal["input_view", "source_truth", "agent_context", "reviewers", "model", "verifier"]


class Perturbation(BaseModel):
    perturbation_id: str
    lane: Lane
    kind: str
    target: PerturbationTarget
    participant_id: str | None = None
    cohort: int | None = None
    record_id: str | None = None
    field: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    expected_class: ExpectedClass
    expected_routing: RoutingCode | None = None
    expected_cohort_decision: CohortDecisionCode | None = None
    clean_routing: RoutingCode | None = None
    clean_cohort_decision: CohortDecisionCode | None = None
    requires_llm: bool = False  # persona/model perturbations are no-ops for the offline backend


class PerturbationManifest(BaseModel):
    manifest_id: str
    created_at: str
    dataset_hash: str
    protocol_version: str
    perturbations: list[Perturbation]
    manifest_hash: str = ""


class PerturbationOutcome(BaseModel):
    perturbation_id: str
    lane: Lane
    kind: str
    expected_class: ExpectedClass
    participant_id: str | None = None
    cohort: int | None = None
    applicable: bool = True
    skipped_reason: str | None = None
    detected: bool = False
    detected_by: list[str] = Field(default_factory=list)
    reviewer_catches: dict[str, bool] = Field(default_factory=dict)
    final_routing: RoutingCode | None = None
    expected_routing: RoutingCode | None = None
    clean_routing: RoutingCode | None = None
    final_cohort_decision: CohortDecisionCode | None = None
    expected_cohort_decision: CohortDecisionCode | None = None
    facts_invariant: bool | None = None
    capitulated: bool | None = None
    passed: bool = False
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- runs

RunMode = Literal["adversarial", "single_agent"]


class RunConfig(BaseModel):
    mode: RunMode = "adversarial"
    backend: str = "offline"
    participants: list[str] | None = None  # None = all
    cohorts: list[int] | None = None
    max_revision_rounds: int = 2
    specialist_panel: bool = True
    perturbations: list[Perturbation] = Field(default_factory=list)
    rep: int = 0
    label: str = ""


class HumanReviewItem(BaseModel):
    item_id: str
    run_id: str
    subject: str
    participant_id: str | None = None
    cohort: int | None = None
    routing: str | None = None
    reasons: list[str]
    status: Literal["pending", "confirmed", "overridden"] = "pending"
    reviewer_decision: str | None = None
    reviewer_note: str | None = None


class ParticipantResult(BaseModel):
    participant_id: str
    cohort: int
    stages: list[StageResult] = Field(default_factory=list)
    case_record: CaseRecord | None = None
    agent_initial_routing: RoutingCode | None = None
    agent_final_routing: RoutingCode | None = None
    oracle_routing: RoutingCode | None = None  # operational oracle on the locked case record
    final_routing: RoutingCode
    rule_hits: list[RuleHit] = Field(default_factory=list)
    human_review: bool = False
    human_review_reasons: list[str] = Field(default_factory=list)
    data_queries: list[DataQuery] = Field(default_factory=list)
    released_claims: list[Claim] = Field(default_factory=list)
    released_verdicts: list[ClaimVerdict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CohortResult(BaseModel):
    cohort: int
    dose_mg: int
    stage: StageResult | None = None
    agent_decision: CohortDecisionCode | None = None
    oracle_decision: CohortDecisionCode
    final_decision: CohortDecisionCode
    human_review: bool = False
    human_review_reasons: list[str] = Field(default_factory=list)
    efficacy: list[dict[str, Any]] = Field(default_factory=list)


class RunVersions(BaseModel):
    schema_version: str
    protocol_version: str
    dataset_hash: str
    verifier_version: str
    backend: str
    models: dict[str, str] = Field(default_factory=dict)


class RunResult(BaseModel):
    run_id: str
    status: Literal["queued", "running", "completed", "failed"]
    config: RunConfig
    versions: RunVersions | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    participants: list[ParticipantResult] = Field(default_factory=list)
    cohorts: list[CohortResult] = Field(default_factory=list)
    review_queue: list[HumanReviewItem] = Field(default_factory=list)
    llm_calls: int = 0
    error: str | None = None


class RunSummary(BaseModel):
    run_id: str
    status: str
    mode: str
    backend: str
    label: str
    started_at: str | None
    finished_at: str | None
    n_participants: int
    routing_counts: dict[str, int]
    human_review_items: int


EventType = Literal[
    "run_started", "stage_started", "author_draft", "verification", "critiques",
    "author_response", "editor_decision", "stage_completed", "participant_routed",
    "cohort_decided", "run_completed", "run_failed", "log",
]


class PipelineEvent(BaseModel):
    run_id: str
    seq: int
    ts: str
    type: EventType
    stage: str | None = None
    subject: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- personas

class Persona(BaseModel):
    agent_id: str
    name: str
    title: str
    seat: str  # e.g. "S1 Author", "S3 Reviewer"
    stages: list[str]
    blinded: bool
    streams: list[str] = Field(default_factory=list)
    temperament: str
    expertise: list[str]
    red_flags: list[str]
    voice: str
    system_prompt: str = ""


# --------------------------------------------------------------------------- metrics & gates

class Count(BaseModel):
    numerator: int
    denominator: int
    rate: float | None = None


class GateResult(BaseModel):
    gate_id: str
    metric: str
    description: str
    op: str
    threshold: float
    value: float | None
    passed: bool | None  # None = not measurable in this run (e.g. persona gates on offline backend)
    note: str = ""


class ClaimEvalResult(BaseModel):
    """Verifier accuracy on the labeled gold claim set (PRD FR-11)."""
    verifier: str
    n: int
    confusion: dict[str, dict[str, int]]
    false_approvals: Count
    detection_recall: Count
    detection_precision: Count
    abstention_accuracy: Count
    by_category: dict[str, dict[str, int]]


class EvaluationResult(BaseModel):
    evaluation_id: str
    created_at: str
    backend: str
    reps: int
    participants: list[str]
    manifest: PerturbationManifest
    modes: dict[str, dict[str, Any]]  # mode -> metric name -> Count | float
    perturbation_outcomes: dict[str, list[PerturbationOutcome]]
    claim_eval: list[ClaimEvalResult]
    gates: list[GateResult]
    reviewer_catch: dict[str, Count] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    duration_s: float = 0.0


class AuditEntry(BaseModel):
    seq: int
    ts: str
    run_id: str | None
    event: str
    payload_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str
