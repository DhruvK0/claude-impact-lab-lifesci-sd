"""Approved evidence registry: the source of truth every claim is checked against."""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

from .config import DATA_DIR, DEFAULT_SEED, analyte, visit_order
from .schemas import DatasetManifest, SourceRecord
from .synthetic import canonical_hash, generate, load, record_hash, save


class Registry:
    def __init__(self, records: list[SourceRecord], manifest: DatasetManifest | None = None):
        self.manifest = manifest
        self._records: dict[str, SourceRecord] = {r.record_id: r for r in records}
        self._by_pid: dict[str, list[SourceRecord]] = {}
        for r in records:
            self._by_pid.setdefault(r.participant_id, []).append(r)
        for recs in self._by_pid.values():
            recs.sort(key=lambda r: (visit_order(r.visit), r.record_id))
        self.hash = canonical_hash(sorted(r.content_hash for r in records))

    # ------------------------------------------------------------------ lookups
    def get(self, record_id: str) -> SourceRecord | None:
        return self._records.get(record_id)

    def all_records(self) -> list[SourceRecord]:
        return list(self._records.values())

    def participant_ids(self) -> list[str]:
        return sorted(self._by_pid)

    def participant_records(self, pid: str, include_restricted: bool = False) -> list[SourceRecord]:
        return [r for r in self._by_pid.get(pid, []) if include_restricted or not r.restricted]

    def cohort_of(self, pid: str) -> int:
        return self._by_pid[pid][0].cohort

    def cohort_participants(self, cohort: int) -> list[str]:
        return [p for p in self.participant_ids() if self.cohort_of(p) == cohort]

    def arm(self, pid: str) -> str | None:
        rec = self._records.get(f"ARM-{pid}")
        return rec.values.get("arm") if rec else None  # type: ignore[return-value]

    def find_value(self, pid: str, visit: str | None, field: str) -> tuple[SourceRecord, object, str | None] | None:
        """Return (record, raw value, unit) for a field at a visit, or None if no record carries it."""
        for r in self._by_pid.get(pid, []):
            if r.visit == visit and field in r.values and r.stream in ("labs", "vitals_ecg", "proteomics", "genomics", "pathology"):
                return r, r.values[field], r.units.get(field)
        return None

    def adverse_events(self, pid: str) -> list[SourceRecord]:
        return [r for r in self._by_pid.get(pid, []) if r.doc_type == "adverse_event"]

    def value_in_protocol_units(self, pid: str, visit: str, field: str) -> float | None:
        hit = self.find_value(pid, visit, field)
        if not hit:
            return None
        _, raw, unit = hit
        return to_protocol_units(field, raw, unit)

    # ------------------------------------------------------------------ integrity
    def integrity_failures(self) -> list[str]:
        return [rid for rid, r in self._records.items() if record_hash(r) != r.content_hash]

    def clone(self, mutate: Callable[[dict[str, SourceRecord]], None] | None = None) -> "Registry":
        """Copy the registry; `mutate` may edit records (used for counterfactual source-truth perturbations)."""
        copies = {rid: r.model_copy(deep=True) for rid, r in self._records.items()}
        if mutate:
            mutate(copies)
            for r in copies.values():
                r.content_hash = record_hash(r)
        return Registry(list(copies.values()), self.manifest)


def norm_unit(u: str | None) -> str:
    return (u or "").replace(" ", "").lower()


def to_protocol_units(field: str, raw, unit: str | None) -> float | None:
    """Convert a raw numeric value to the protocol unit. None if missing, non-numeric, or unit unknown."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    a = analyte(field)
    if a is None or unit is None or norm_unit(unit) == norm_unit(a["unit"]):
        return float(raw)
    if a.get("si_unit") and norm_unit(unit) == norm_unit(a["si_unit"]):
        return float(raw) * a["si_factor"]
    return None


@lru_cache(maxsize=4)
def default_registry(seed: int = DEFAULT_SEED) -> Registry:
    """Load the dataset from data/ if present (and intact), else generate and save it."""
    path = DATA_DIR / "synthetic" / f"cvx214-synthetic-s{seed}"
    if (path / "records.jsonl").exists():
        records, manifest = load(path)
        reg = Registry(records, manifest)
        if not reg.integrity_failures() and reg.hash == canonical_hash(sorted(r.content_hash for r in records)):
            return reg
    records, manifest = generate(seed)
    save(records, manifest, path)
    return Registry(records, manifest)
