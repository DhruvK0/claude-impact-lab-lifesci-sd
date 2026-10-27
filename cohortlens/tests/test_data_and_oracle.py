from collections import Counter

from cohortlens.oracle import evaluate_cohort, evaluate_records
from cohortlens.synthetic import generate

from .conftest import pid_for


def test_generation_is_deterministic(reg):
    records, manifest = generate()
    assert manifest.content_hash == reg.manifest.content_hash
    assert manifest.n_participants == 50
    assert manifest.records_by_stream["labs"] == 400 and manifest.records_by_stream["vitals_ecg"] == 400


def test_randomization_is_8_active_2_placebo_per_cohort(reg):
    for c in range(1, 6):
        arms = Counter(reg.arm(p) for p in reg.cohort_participants(c))
        assert arms == {"active": 8, "placebo": 2}


def test_every_planted_scenario_fires_its_rules(reg):
    for s in reg.manifest.planted_scenarios:
        hits = {h.rule_id for h in evaluate_records(reg.participant_records(s.participant_id, include_restricted=True)).rule_hits}
        assert set(s.expected_rules) <= hits, s.scenario_id


def test_unplanted_participants_are_clean(reg, truth_routing):
    planted = {s.participant_id for s in reg.manifest.planted_scenarios}
    assert all(truth_routing[p] == "R0" for p in reg.participant_ids() if p not in planted)
    assert Counter(truth_routing.values()) == {"R0": 40, "R2": 4, "RQ": 2, "R1": 2, "R4": 1, "R3": 1}


def test_cohort_rules_count_active_arm_only(reg, truth_routing):
    ck = pid_for(reg, "ck_exercise")  # placebo participant at R2
    c = reg.cohort_of(ck)
    members = reg.cohort_participants(c)
    arms = {m: reg.arm(m) for m in members}
    assert evaluate_cohort(c, 50, {m: truth_routing[m] for m in members}, arms).decision.value == "ESCALATE"
    arms[ck] = "active"
    assert evaluate_cohort(c, 50, {m: truth_routing[m] for m in members}, arms).decision.value == "REPEAT_DOSE"
    c5 = reg.cohort_participants(5)
    assert evaluate_cohort(5, 400, {m: truth_routing[m] for m in c5}, {m: reg.arm(m) for m in c5}).decision.value == "R5_PAUSE"
