from cohortlens.evals import gold_claims, run_claim_evals
from cohortlens.schemas import Claim, EvidenceRef
from cohortlens.verifier import Verifier

from .conftest import pid_for


def test_gold_claims_labels_match_verifier(reg):
    v = Verifier(reg)
    wrong = [(cat, label, v.verify_claim(c, blinded=False).status) for c, label, cat in gold_claims(reg)
             if v.verify_claim(c, blinded=False).status != label]
    assert wrong == []


def test_zero_false_approvals_and_baseline_is_worse(reg):
    contract, baseline = run_claim_evals(reg)
    assert contract.false_approvals.numerator == 0
    assert baseline.false_approvals.numerator > contract.false_approvals.numerator


def test_diagnoses_unit_swap_and_decimal_shift(reg):
    v = Verifier(reg)
    hys = pid_for(reg, "hys_law")
    c = Claim(claim_id="t1", kind="value", text="", participant_id=hys, visit="V5", field="total_bilirubin", value=0.31,
              unit="mg/dL", evidence=[EvidenceRef(record_id=f"LAB-{hys}-V5", field="total_bilirubin")])
    verdict = v.verify_claim(c, blinded=True)
    assert verdict.status == "CONTRADICTED" and any("decimal shift" in d for d in verdict.diagnosis)


def test_blinded_seat_cannot_cite_treatment_arm(reg):
    v = Verifier(reg)
    p = reg.participant_ids()[0]
    c = Claim(claim_id="t2", kind="value", text="", participant_id=p, field="arm", code=reg.arm(p),
              evidence=[EvidenceRef(record_id=f"ARM-{p}", field="arm")])
    verdict = v.verify_claim(c, blinded=True)
    assert "blinding" in verdict.checks_failed and verdict.human_review


def test_verifier_outage_fails_closed(reg):
    hys = pid_for(reg, "hys_law")
    c = Claim(claim_id="t3", kind="value", text="", participant_id=hys, visit="V5", field="alt", value=168, unit="U/L",
              evidence=[EvidenceRef(record_id=f"LAB-{hys}-V5", field="alt")])
    rep = Verifier(reg, available=False).verify([c], blinded=True)
    assert rep.n_supported == 0 and not rep.available
