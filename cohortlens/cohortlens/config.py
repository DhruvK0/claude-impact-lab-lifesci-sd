"""Paths, versions and configuration loading."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:  # credentials such as OPENROUTER_API_KEY may live in cohortlens/.env (gitignored); real env vars win
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = Path(os.environ.get("COHORTLENS_DATA_DIR", PROJECT_ROOT / "data"))
RUNS_DIR = Path(os.environ.get("COHORTLENS_RUNS_DIR", PROJECT_ROOT / "runs"))

SCHEMA_VERSION = "1.0.0"
DEFAULT_SEED = 20261026


@lru_cache(maxsize=None)
def load_config(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def protocol() -> dict:
    return load_config("protocol.json")


def agent_models() -> dict:
    return load_config("agents.json")


def gates_config() -> dict:
    return load_config("gates.json")


def backend_name() -> str:
    """COHORTLENS_BACKEND wins; otherwise OpenRouter when its key is present, else the offline backend."""
    explicit = os.environ.get("COHORTLENS_BACKEND")
    if explicit:
        return explicit.lower()
    return "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "offline"


def analyte(field: str) -> dict | None:
    return protocol()["analytes"].get(field)


def visit_day(visit: str) -> int | None:
    for v in protocol()["visits"]:
        if v["visit"] == visit:
            return v["day"]
    return None


def visit_order(visit: str | None) -> int:
    for i, v in enumerate(protocol()["visits"]):
        if v["visit"] == visit:
            return i
    return 99


def cohort_dose(cohort: int) -> int:
    for c in protocol()["cohorts"]:
        if c["cohort"] == cohort:
            return c["dose_mg"]
    raise KeyError(cohort)
