"""Run/evaluation store and background execution shared by the API and the CLI."""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .agents import make_backend
from .audit import AuditLog
from .config import RUNS_DIR
from .evals import run_evaluation
from .perturbations import apply_many, build_manifest
from .registry import Registry, default_registry
from .schemas import (
    EvaluationResult, HumanReviewItem, PerturbationManifest, PipelineEvent, RunConfig, RunResult, RunSummary,
)
from .workflow import Pipeline


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path | None = None, registry: Registry | None = None):
        self.root = root or RUNS_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = registry or default_registry()
        self.audit = AuditLog(self.root / "audit.jsonl")
        self.reference = self.registry
        self.manifest: PerturbationManifest = self._perturbation_manifest()
        self.runs: dict[str, RunResult] = {}
        self.events: dict[str, list[PipelineEvent]] = {}
        self.evals: dict[str, EvaluationResult | dict] = {}
        self.decisions: dict[str, HumanReviewItem] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cohortlens")
        self._load()

    def _perturbation_manifest(self) -> PerturbationManifest:
        if self.registry.manifest and self.registry.manifest.planted_scenarios:
            return build_manifest(self.registry)
        # uploaded data without known ground truth: no targeted perturbations can be declared
        return PerturbationManifest(manifest_id="M-none", created_at=_now(), dataset_hash=self.registry.hash,
                                    protocol_version=self.registry.manifest.protocol_version if self.registry.manifest else "",
                                    perturbations=[])

    def activate_upload(self, files: dict[str, str]) -> dict:
        """Ingest uploaded CSVs; on success they become the source of truth for subsequent runs."""
        from .dataset_io import ingest, manifest_for
        records, errors = ingest(files)
        if not records:
            return {"activated": False, "errors": errors or ["no records found"]}
        reg = Registry(records, manifest_for(records, self.reference))
        integrity = reg.integrity_failures()
        if integrity:
            return {"activated": False, "errors": errors + [f"hash mismatch: {x}" for x in integrity[:10]]}
        self.registry = reg
        self.manifest = self._perturbation_manifest()
        self.audit.append("dataset_activated", {"dataset_hash": reg.hash, "dataset_id": reg.manifest.dataset_id,
                                                "n_records": len(records), "files": sorted(files), "errors": errors[:20]})
        return {"activated": True, "dataset_id": reg.manifest.dataset_id, "dataset_hash": reg.hash,
                "matches_reference": reg.hash == self.reference.hash, "n_records": len(records),
                "n_participants": reg.manifest.n_participants, "records_by_stream": reg.manifest.records_by_stream,
                "errors": errors, "perturbations_available": len(self.manifest.perturbations)}

    def reset_dataset(self) -> dict:
        self.registry = self.reference
        self.manifest = self._perturbation_manifest()
        self.audit.append("dataset_activated", {"dataset_hash": self.registry.hash, "reset": True})
        return {"activated": True, "dataset_id": self.registry.manifest.dataset_id, "dataset_hash": self.registry.hash}

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        for f in sorted((self.root / "runs").glob("*/result.json")) if (self.root / "runs").exists() else []:
            try:
                r = RunResult.model_validate_json(f.read_text(encoding="utf-8"))
                self.runs[r.run_id] = r
                ev = f.parent / "events.jsonl"
                self.events[r.run_id] = [PipelineEvent.model_validate_json(x) for x in ev.read_text(encoding="utf-8").splitlines() if x] if ev.exists() else []
            except ValueError:
                continue
        for f in sorted((self.root / "evaluations").glob("*.json")) if (self.root / "evaluations").exists() else []:
            try:
                e = EvaluationResult.model_validate_json(f.read_text(encoding="utf-8"))
                self.evals[e.evaluation_id] = e
            except ValueError:
                continue
        dec = self.root / "decisions.jsonl"
        if dec.exists():
            for line in dec.read_text(encoding="utf-8").splitlines():
                if line:
                    item = HumanReviewItem.model_validate_json(line)
                    self.decisions[item.item_id] = item

    def _save_run(self, r: RunResult) -> None:
        d = self.root / "runs" / r.run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "result.json").write_text(r.model_dump_json(), encoding="utf-8")
        with open(d / "events.jsonl", "w", encoding="utf-8") as f:
            for e in self.events.get(r.run_id, []):
                f.write(e.model_dump_json() + "\n")

    # ------------------------------------------------------------------ runs
    def new_run_id(self) -> str:
        return f"RUN-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"

    def _emit(self, e: PipelineEvent) -> None:
        with self._lock:
            self.events.setdefault(e.run_id, []).append(e)

    def start_run(self, cfg: RunConfig, perturbation_ids: list[str] | None = None, wait: bool = False) -> str:
        run_id = self.new_run_id()
        perts = [p for p in self.manifest.perturbations if p.perturbation_id in set(perturbation_ids or [])]
        cfg = cfg.model_copy(update={"perturbations": perts})
        self.runs[run_id] = RunResult(run_id=run_id, status="queued", config=cfg)
        self.events[run_id] = []
        fut = self._pool.submit(self._execute, run_id, cfg)
        if wait:
            fut.result()
        return run_id

    def _execute(self, run_id: str, cfg: RunConfig) -> None:
        try:
            truth, ctxs, skipped = apply_many(self.registry, cfg.perturbations, cfg.backend)
            if cfg.perturbations:
                self.audit.append("perturbations_applied", {"ids": [p.perturbation_id for p in cfg.perturbations], "skipped": skipped,
                                                            "manifest_hash": self.manifest.manifest_hash}, run_id)
            self.runs[run_id] = RunResult(run_id=run_id, status="running", config=cfg, started_at=_now())
            pipe = Pipeline(truth, make_backend(cfg.backend, self.audit), cfg, run_id, self.audit, emit=self._emit, contexts=ctxs)
            self.runs[run_id] = pipe.run()
        except Exception as e:  # noqa: BLE001
            self.runs[run_id] = RunResult(run_id=run_id, status="failed", config=cfg, error=f"{type(e).__name__}: {e}", finished_at=_now())
            self._emit(PipelineEvent(run_id=run_id, seq=10**9, ts=_now(), type="run_failed", data={"error": str(e)}))
        self._save_run(self.runs[run_id])

    def summaries(self) -> list[RunSummary]:
        out = []
        for r in sorted(self.runs.values(), key=lambda r: r.started_at or "", reverse=True):
            counts: dict[str, int] = {}
            for p in r.participants:
                counts[p.final_routing.value] = counts.get(p.final_routing.value, 0) + 1
            out.append(RunSummary(run_id=r.run_id, status=r.status, mode=r.config.mode, backend=r.config.backend, label=r.config.label,
                                  started_at=r.started_at, finished_at=r.finished_at, n_participants=len(r.participants),
                                  routing_counts=counts, human_review_items=len(r.review_queue)))
        return out

    def latest_completed(self) -> RunResult | None:
        done = [r for r in self.runs.values() if r.status == "completed"]
        return max(done, key=lambda r: r.finished_at or "") if done else None

    # ------------------------------------------------------------------ human review
    def queue(self, run_id: str | None = None) -> list[HumanReviewItem]:
        runs = [self.runs[run_id]] if run_id and run_id in self.runs else list(self.runs.values())
        return [self.decisions.get(i.item_id, i) for r in runs for i in r.review_queue]

    def decide(self, item_id: str, decision: str, reviewer: str, note: str | None, override_routing: str | None) -> HumanReviewItem:
        item = next((i for i in self.queue() if i.item_id == item_id), None)
        if item is None:
            raise KeyError(item_id)
        status = {"uphold": "confirmed", "confirm": "confirmed", "override": "overridden", "escalate": "confirmed"}[decision]
        item = item.model_copy(update={"status": status, "reviewer_decision": f"{decision}{f' -> {override_routing}' if override_routing else ''}",
                                       "reviewer_note": f"{reviewer}: {note or ''}".strip()})
        self.decisions[item_id] = item
        with open(self.root / "decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(item.model_dump_json() + "\n")
        self.audit.append("human_decision", {"item_id": item_id, "decision": decision, "override_routing": override_routing,
                                             "reviewer": reviewer, "note": note}, item.run_id)
        return item

    # ------------------------------------------------------------------ evaluations
    def start_evaluation(self, backend: str, reps: int, participants: list[str] | None, wait: bool = False) -> str:
        eval_id = f"EVAL-PENDING-{uuid.uuid4().hex[:6]}"
        self.evals[eval_id] = {"evaluation_id": eval_id, "status": "running", "started_at": _now()}

        def job():
            try:
                res = run_evaluation(self.registry, backend, reps, participants, audit=self.audit)
                self.evals.pop(eval_id, None)
                self.evals[res.evaluation_id] = res
                self.evals[eval_id] = {"evaluation_id": eval_id, "status": "completed", "result_id": res.evaluation_id}
                self.save_evaluation(res)
            except Exception as e:  # noqa: BLE001
                self.evals[eval_id] = {"evaluation_id": eval_id, "status": "failed", "error": f"{type(e).__name__}: {e}"}
        fut = self._pool.submit(job)
        if wait:
            fut.result()
        return eval_id

    def save_evaluation(self, res: EvaluationResult) -> Path:
        d = self.root / "evaluations"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{res.evaluation_id}.json"
        path.write_text(res.model_dump_json(indent=1), encoding="utf-8")
        flat = {"evaluation_id": res.evaluation_id, "created_at": res.created_at, "backend": res.backend, "reps": res.reps}
        for mode, m in res.modes.items():
            for k, v in m.items():
                if isinstance(v, dict) and "rate" in v:
                    flat[f"{mode}.{k}"] = v["rate"]
                elif isinstance(v, (int, float)) or v is None:
                    flat[f"{mode}.{k}"] = v
        for ce in res.claim_eval:
            flat[f"claims.{ce.verifier}.false_approval_rate"] = ce.false_approvals.rate
        with open(self.root / "metrics_history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(flat) + "\n")
        return path

    def metrics_history(self) -> dict:
        path = self.root / "metrics_history.jsonl"
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x] if path.exists() else []
        charts = {}
        keys = sorted({k for r in rows for k, v in r.items() if isinstance(v, (int, float)) and k != "reps"})
        for k in keys:
            vals = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            sd = (sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0
            charts[k] = {"points": [{"evaluation_id": r["evaluation_id"], "created_at": r["created_at"], "value": r.get(k)} for r in rows],
                         "mean": mean, "ucl": mean + 3 * sd, "lcl": mean - 3 * sd}
        return {"n": len(rows), "control_charts": charts}
