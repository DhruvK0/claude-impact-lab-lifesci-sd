"""Immutable audit log: append-only JSONL with a SHA-256 hash chain.

Every agent call, verification, editor decision, perturbation manifest and human
decision is appended. Changing or deleting any line breaks the chain, which
`verify()` detects (gate G-10).
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import AuditEntry

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._entries: list[AuditEntry] = []
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._entries.append(AuditEntry.model_validate_json(line))

    @staticmethod
    def digest(obj: Any) -> str:
        data = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, default=str)
        return hashlib.sha256(data.encode()).hexdigest()

    @staticmethod
    def _entry_hash(seq: int, ts: str, run_id: str | None, event: str, payload_hash: str, prev: str) -> str:
        return hashlib.sha256(f"{seq}|{ts}|{run_id}|{event}|{payload_hash}|{prev}".encode()).hexdigest()

    def append(self, event: str, payload: dict[str, Any], run_id: str | None = None) -> AuditEntry:
        with self._lock:
            prev = self._entries[-1].hash if self._entries else GENESIS
            seq = len(self._entries)
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            ph = self.digest(payload)
            entry = AuditEntry(seq=seq, ts=ts, run_id=run_id, event=event, payload_hash=ph, payload=payload,
                               prev_hash=prev, hash=self._entry_hash(seq, ts, run_id, event, ph, prev))
            self._entries.append(entry)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(entry.model_dump_json() + "\n")
            return entry

    def entries(self, run_id: str | None = None, limit: int | None = None) -> list[AuditEntry]:
        es = [e for e in self._entries if run_id is None or e.run_id == run_id]
        return es[-limit:] if limit else es

    def verify(self) -> tuple[bool, int | None]:
        """Return (valid, first_bad_seq)."""
        prev = GENESIS
        for i, e in enumerate(self._entries):
            if e.seq != i or e.prev_hash != prev or e.payload_hash != self.digest(e.payload) \
                    or e.hash != self._entry_hash(e.seq, e.ts, e.run_id, e.event, e.payload_hash, prev):
                return False, i
            prev = e.hash
        return True, None
