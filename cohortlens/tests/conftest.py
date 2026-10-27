import pytest

from cohortlens.audit import AuditLog
from cohortlens.oracle import evaluate_registry
from cohortlens.registry import Registry
from cohortlens.synthetic import generate


@pytest.fixture(scope="session")
def reg() -> Registry:
    records, manifest = generate()
    return Registry(records, manifest)


@pytest.fixture(scope="session")
def truth_routing(reg):
    return {p: r.routing.value for p, r in evaluate_registry(reg).items()}


@pytest.fixture()
def audit() -> AuditLog:
    return AuditLog()


def pid_for(reg, scenario: str) -> str:
    return next(s.participant_id for s in reg.manifest.planted_scenarios if s.scenario_id == scenario)
