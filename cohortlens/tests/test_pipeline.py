from cohortlens.agents import make_backend
from cohortlens.evals import run_evaluation
from cohortlens.perturbations import apply, build_manifest
from cohortlens.schemas import RunConfig
from cohortlens.workflow import Pipeline

from .conftest import pid_for


def run(reg, audit, mode="adversarial", pids=None, contexts=None, truth=None):
    cfg = RunConfig(mode=mode, backend="offline", participants=pids)
    return Pipeline(truth or reg, make_backend("offline", audit), cfg, "RUN-TEST", audit, contexts=contexts).run()


def test_end_to_end_clean_run_matches_oracle(reg, audit, truth_routing):
    res = run(reg, audit)
    assert res.status == "completed" and len(res.participants) == 50
    assert {p.participant_id: p.final_routing.value for p in res.participants} == truth_routing
    assert [c.final_decision.value for c in res.cohorts] == ["ESCALATE"] * 4 + ["R5_PAUSE"]
    assert all(v.status == "SUPPORTED" for p in res.participants for v in p.released_verdicts)
    assert all(p.case_record.locked and p.case_record.lock_hash for p in res.participants)
    assert audit.verify() == (True, None)


def test_prompt_injection_is_ignored(reg, audit, truth_routing):
    pid = pid_for(reg, "prompt_injection")
    res = run(reg, audit, pids=[pid])
    p = res.participants[0]
    assert p.final_routing.value == truth_routing[pid] == "R0"
    assert any("embedded instruction" in q.issue for q in p.data_queries)


def test_each_perturbation_meets_its_expected_class(reg, audit):
    manifest = build_manifest(reg)
    from cohortlens.evals import score
    clean = {p.participant_id: p for p in run(reg, audit).participants}
    for p in manifest.perturbations:
        truth, ctxs, skip = apply(reg, p, "offline")
        if skip:
            assert p.requires_llm or p.kind == "temperature"
            continue
        res = run(truth, audit, pids=list(ctxs), contexts=ctxs, truth=truth)
        assert score(p, res, clean, truth).passed, p.perturbation_id


def test_single_agent_baseline_is_measurably_worse_under_pollution(reg):
    res = run_evaluation(reg, "offline", reps=1)
    adv, base = res.modes["adversarial"], res.modes["single_agent"]
    assert adv["pollution_detection_rate"]["rate"] == 1.0
    assert base["must_detect_passed"]["numerator"] < adv["must_detect_passed"]["numerator"]
    gates = {g.gate_id: g.passed for g in res.gates}
    assert gates["G-6"] is None  # persona invariance needs an LLM backend
    assert all(v for k, v in gates.items() if k != "G-6")
