"""Immutable audit log: append-only JSONL with a SHA-256 hash chain.

Every agent call, verification, editor decision, perturbation manifest, upload and
human decision is appended. Changing or deleting any line breaks the chain, which
`verify()` detects (gate G-10).

Several processes may share one log (the API server and a CLI evaluation, say).
Each append therefore takes a cross-process lock file and chains from the last
entry actually on disk, never from this process's memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schemas import AuditEntry

GENESIS = "0" * 64


class _FileLock:
    """Exclusive lock via O_EXCL lock-file creation; portable across Windows and POSIX."""

    def __init__(self, path: Path, stale_after_s: float = 30.0):
        self.lock = f"{path}.lock"
        self.stale_after_s = stale_after_s
        self.fd: int | None = None

    def __enter__(self):
        start = time.time()
        while True:
            try:
                self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except (FileExistsError, PermissionError):  # Windows reports a lock file mid-deletion as PermissionError
                if time.time() - start > self.stale_after_s:  # a writer crashed while holding the lock
                    try:
                        os.remove(self.lock)
                    except OSError:
                        pass
                time.sleep(0.002)

    def __exit__(self, *exc):
        os.close(self.fd)
        for _ in range(500):
            try:
                os.remove(self.lock)
                return
            except PermissionError:  # another writer is probing the lock on Windows; retry
                time.sleep(0.002)


def _last_line(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos, buf = f.tell(), b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.rstrip(b"\n").split(b"\n")
            if len(lines) > 1 or pos == 0:
                return lines[-1].decode("utf-8")
    return None


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._entries: list[AuditEntry] = []  # in-memory log when no path is given
        self._cache: tuple[tuple[int, float], list[AuditEntry]] | None = None

    @staticmethod
    def digest(obj: Any) -> str:
        data = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, default=str)
        return hashlib.sha256(data.encode()).hexdigest()

    @staticmethod
    def _entry_hash(seq: int, ts: str, run_id: str | None, event: str, payload_hash: str, prev: str) -> str:
        return hashlib.sha256(f"{seq}|{ts}|{run_id}|{event}|{payload_hash}|{prev}".encode()).hexdigest()

    def _make(self, last: AuditEntry | None, event: str, payload: dict, run_id: str | None) -> AuditEntry:
        prev = last.hash if last else GENESIS
        seq = last.seq + 1 if last else 0
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        ph = self.digest(payload)
        return AuditEntry(seq=seq, ts=ts, run_id=run_id, event=event, payload_hash=ph, payload=payload,
                          prev_hash=prev, hash=self._entry_hash(seq, ts, run_id, event, ph, prev))

    def append(self, event: str, payload: dict[str, Any], run_id: str | None = None) -> AuditEntry:
        with self._lock:
            if self.path is None:
                entry = self._make(self._entries[-1] if self._entries else None, event, payload, run_id)
                self._entries.append(entry)
                return entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _FileLock(self.path):
                tail = _last_line(self.path)
                entry = self._make(AuditEntry.model_validate_json(tail) if tail else None, event, payload, run_id)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(entry.model_dump_json() + "\n")
            return entry

    def _all(self) -> list[AuditEntry]:
        if self.path is None:
            return self._entries
        if not self.path.exists():
            return []
        st = self.path.stat()
        key = (st.st_size, st.st_mtime)
        if self._cache is None or self._cache[0] != key:
            text = self.path.read_text(encoding="utf-8")
            self._cache = (key, [AuditEntry.model_validate_json(x) for x in text.splitlines() if x.strip()])
        return self._cache[1]

    def entries(self, run_id: str | None = None, limit: int | None = None) -> list[AuditEntry]:
        es = [e for e in self._all() if run_id is None or e.run_id == run_id]
        return es[-limit:] if limit else es

    def verify(self) -> tuple[bool, int | None]:
        """Return (valid, first_bad_seq)."""
        prev = GENESIS
        for i, e in enumerate(self._all()):
            if e.seq != i or e.prev_hash != prev or e.payload_hash != self.digest(e.payload) \
                    or e.hash != self._entry_hash(e.seq, e.ts, e.run_id, e.event, e.payload_hash, prev):
                return False, i
            prev = e.hash
        return True, None
