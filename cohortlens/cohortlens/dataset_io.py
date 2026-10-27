"""Export the registry as uploadable CSV files, and ingest uploaded CSVs back into source records.

Two CSV layouts, both carrying explicit `stream` and `doc_type` columns:
  long  (labs, vitals_ecg, proteomics): one row per observation
        record_id, participant_id, cohort, stream, doc_type, visit, visit_day, date, field, value, unit
  wide  (everything else): one row per record; columns other than the metadata columns become values
        record_id, participant_id, cohort, stream, doc_type, visit, visit_day, date, <fields...>, text
Ingesting an export reproduces the original records byte for byte (same content hashes).
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import protocol
from .registry import Registry
from .schemas import STREAMS, DatasetManifest, SourceRecord
from .synthetic import canonical_hash, record_hash

META = ["record_id", "participant_id", "cohort", "stream", "doc_type", "visit", "visit_day", "date"]
LONG_STREAMS = ("labs", "vitals_ecg", "proteomics")
FILES = {
    "labs": "labs.csv", "vitals_ecg": "vitals_ecg.csv", "proteomics": "proteomics_exploratory.csv",
    "genomics": "genomics_research_panel.csv", "pathology": "pathology_reports.csv",
    "adverse_event": "adverse_events.csv", "con_med_list": "concomitant_medications.csv",
    "treatment_arm": "randomization_RESTRICTED_dsmb_only.csv",
}
_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")


def _cell(v) -> str:
    return "" if v is None else str(v)


def _parse(s: str):
    if s == "":
        return None
    if s in ("True", "False"):
        return s == "True"
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    return s


def _file_key(r: SourceRecord) -> str:
    if r.stream == "ae_meds":
        return r.doc_type
    return r.stream


def export_csv(reg: Registry, out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[SourceRecord]] = {}
    for r in sorted(reg.all_records(), key=lambda r: r.record_id):
        groups.setdefault(_file_key(r), []).append(r)
    counts = {}
    for key, recs in groups.items():
        path = out / FILES[key]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if key in LONG_STREAMS:
                w.writerow(META + ["field", "value", "unit"])
                rows = 0
                for r in recs:
                    for fld, v in r.values.items():
                        w.writerow([_cell(getattr(r, m)) for m in META] + [fld, _cell(v), r.units.get(fld, "")])
                        rows += 1
            else:
                fields = list(dict.fromkeys(k for r in recs for k in r.values))
                w.writerow(META + fields + ["text"])
                for r in recs:
                    w.writerow([_cell(getattr(r, m)) for m in META] + [_cell(r.values.get(k)) for k in fields] + [_cell(r.text)])
                rows = len(recs)
        counts[path.name] = rows
    return counts


def ingest(files: dict[str, str]) -> tuple[list[SourceRecord], list[str]]:
    """Parse uploaded CSV texts into source records. Returns (records, errors). Unrecognised files are reported, not guessed."""
    records: dict[str, SourceRecord] = {}
    errors: list[str] = []
    for name, text in files.items():
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        cols = reader.fieldnames or []
        missing = [m for m in META if m not in cols]
        if missing:
            errors.append(f"{name}: not a CohortLens source file (missing columns {', '.join(missing)})")
            continue
        long = {"field", "value", "unit"} <= set(cols)
        for i, row in enumerate(reader, start=2):
            if row["stream"] not in STREAMS:
                errors.append(f"{name}:{i}: unknown stream '{row['stream']}'")
                continue
            rid = row["record_id"]
            rec = records.get(rid)
            if rec is None:
                try:
                    rec = SourceRecord(record_id=rid, participant_id=row["participant_id"], cohort=int(row["cohort"]),
                                       stream=row["stream"], doc_type=row["doc_type"], visit=row["visit"] or None,
                                       visit_day=int(row["visit_day"]) if row["visit_day"] else None, date=row["date"] or None,
                                       restricted=row["stream"] == "treatment_arm")
                except ValueError as e:
                    errors.append(f"{name}:{i}: {e}")
                    continue
                records[rid] = rec
            if long:
                rec.values[row["field"]] = _parse(row["value"])
                if row["unit"]:
                    rec.units[row["field"]] = row["unit"]
            else:
                for k, v in row.items():
                    if k in META or k is None:
                        continue
                    if k == "text":
                        rec.text = v or None
                    elif v != "":  # wide files hold several doc types; blank cells are columns the record does not have
                        rec.values[k] = _parse(v)
    out = []
    for r in records.values():
        r.content_hash = record_hash(r)
        out.append(r)
    return out, errors


def manifest_for(records: list[SourceRecord], reference: Registry | None) -> DatasetManifest:
    by_stream: dict[str, int] = {}
    for r in records:
        by_stream[r.stream] = by_stream.get(r.stream, 0) + 1
    h = canonical_hash(sorted(r.content_hash for r in records))
    if reference is not None and h == reference.hash:
        return reference.manifest.model_copy(update={"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    proto = protocol()
    return DatasetManifest(
        dataset_id=f"upload-{h[:10]}", seed=0,
        protocol_id=proto["protocol_id"], protocol_version=proto["protocol_version"],
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        n_participants=len({r.participant_id for r in records}), n_records=len(records), records_by_stream=by_stream,
        content_hash=h, planted_scenarios=[],
    )


def write_answer_key(reg: Registry, out: Path) -> None:
    """Ground truth for evaluation only. Never upload it or show it to agents."""
    from .evals import gold_claims
    from .oracle import evaluate_registry
    from .perturbations import build_manifest
    out.mkdir(parents=True, exist_ok=True)
    (out / "planted_scenarios.json").write_text(json.dumps([s.model_dump() for s in reg.manifest.planted_scenarios], indent=1), encoding="utf-8")
    oracle = evaluate_registry(reg)
    (out / "oracle_routing.json").write_text(json.dumps(
        {p: {"routing": r.routing.value, "arm": r.arm, "rules": sorted({h.rule_id for h in r.rule_hits})} for p, r in oracle.items()},
        indent=1), encoding="utf-8")
    (out / "gold_claims.json").write_text(json.dumps(
        [{"label": label, "category": cat, "claim": c.model_dump()} for c, label, cat in gold_claims(reg)], indent=1), encoding="utf-8")
    (out / "perturbation_manifest.json").write_text(build_manifest(reg).model_dump_json(indent=1), encoding="utf-8")
