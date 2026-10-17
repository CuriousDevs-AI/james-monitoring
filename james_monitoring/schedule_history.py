"""Local, fail-closed scheduler receipts. No payloads, secrets or automatic retries.

A file lock belongs to the whole job family (all work slots share one). The receipt belongs to one
occurrence. Releasing a lock after a crash does not make that occurrence safe to run again.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from pathlib import Path

from .fileio import atomic_write, fcntl, path_lock


class ScheduleHistory:
    def __init__(self, root: Path):
        self.directory = root / ".jm" / "schedule-runs"

    @staticmethod
    def family(job: str) -> str:
        return "work" if job.startswith("work:") else job

    @contextlib.contextmanager
    def guard(self, job: str):
        if fcntl is None:
            raise RuntimeError("Durable scheduling requires POSIX file locks on a local filesystem.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(self.family(job).encode()).hexdigest()
        with (self.directory / (key + ".lock")).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def all(self) -> list[dict]:
        # ponytail: linear local receipt scan; use an indexed SQLite store if retention volume makes it slow.
        records = []
        for path in self.directory.glob("*.json"):
            record = json.loads(path.read_text())  # corrupt history must stop scheduling, not reset it
            if not isinstance(record, dict) or record.get("id") != path.stem or not record.get("job") \
                    or record.get("status") not in ("scheduled", "running", "completed", "failed", "interrupted", "skipped"):
                raise ValueError("Invalid scheduler receipt; restore a verified copy before scheduling.")
            records.append(record)
        return sorted(records, key=lambda r: (r["scheduled_at"], r["id"]), reverse=True)

    def save(self, record: dict) -> None:
        atomic_write(self.directory / (record["id"] + ".json"), json.dumps(record, indent=2) + "\n", mode=0o600)

    def recover(self, at: str) -> None:
        """A free family lock proves there is no live handler holding this receipt on this host."""
        for record in self.all():
            if record["status"] not in ("scheduled", "running"):
                continue
            with self.guard(record["job"]) as acquired:
                if acquired:
                    # Re-read: another process might have completed between listing and acquiring.
                    record = json.loads((self.directory / (record["id"] + ".json")).read_text())
                    if record["status"] in ("scheduled", "running"):
                        record.update(status="interrupted", finished_at=at)
                        self.save(record)

    @staticmethod
    def unresolved(record: dict) -> bool:
        return record["status"] in ("failed", "interrupted") and not record.get("acknowledgment")

    def claim(self, job: str, slot: str, at: str) -> dict | None:
        """Caller holds the family guard until the handler finishes, including background jobs."""
        key = hashlib.sha256((job + "\0" + slot).encode()).hexdigest()
        if (self.directory / (key + ".json")).exists():
            return None
        if any(self.family(r["job"]) == self.family(job) and
               (self.unresolved(r) or r["status"] in ("scheduled", "running")) for r in self.all()):
            return None
        record = {"id": key, "job": job, "slot": slot, "scheduled_at": at, "status": "scheduled"}
        self.save(record)
        return record

    def acknowledge(self, run_id: str, by: str, evidence: str, at: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{64}", run_id) or not evidence.strip():
            raise ValueError("Choose a valid run and record what you checked before resuming future runs.")
        path = self.directory / (run_id + ".json")
        with path_lock(path):
            record = json.loads(path.read_text())
            if not self.unresolved(record):
                raise ValueError("Only failed or interrupted runs require acknowledgment.")
            record["acknowledgment"] = {"by": by, "at": at, "evidence": evidence.strip()}
            self.save(record)
        return record
