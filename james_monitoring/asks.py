"""Permission requests ("asks") — the only way an agent gets a 🔴 action approved."""
from __future__ import annotations

import threading
import contextlib
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .fileio import atomic_write, path_lock
from .mdoc import MDoc
from .util import now

LEVELS = ["yellow", "red"]         # green actions never need an ask
DEFAULTS = ["wait", "approve", "reject"]


class AskError(Exception):
    pass


def _clean(text) -> str:
    from .tasks import clean_text
    return clean_text(text)


@dataclass
class Ask:
    path: Path
    doc: MDoc

    @property
    def id(self) -> str: return str(self.doc.meta["id"])
    @property
    def status(self) -> str: return str(self.doc.meta.get("status", "pending"))
    @property
    def requester(self) -> str: return str(self.doc.meta.get("from", ""))
    @property
    def level(self) -> str: return str(self.doc.meta.get("level", "red"))
    @property
    def summary(self) -> str: return str(self.doc.meta.get("summary", ""))
    @property
    def kind(self) -> str: return str(self.doc.meta.get("kind", "general"))
    @property
    def default(self) -> str: return str(self.doc.meta.get("default", "wait"))

    @property
    def execution(self) -> dict:
        value = self.doc.meta.get("execution", {})
        if not isinstance(value, dict):
            raise AskError(f"{self.id} has an invalid execution record; repair it before making a decision.")
        return value

    def deadline(self) -> datetime | None:
        v = self.doc.meta.get("deadline")
        return datetime.fromisoformat(str(v)) if v else None

    def card(self, manager: str = "Manager") -> str:
        m = self.doc.meta
        dl = self.deadline()
        dl_s = dl.strftime("%d %b %H:%M") if dl else "none"
        icon = "🔴" if self.level == "red" else "🟡"
        default = {"wait": "it waits", "approve": "auto-approve", "reject": "auto-reject"}[self.default]
        lines = [f"{icon} [{self.id}] {m.get('from', '').title()} asks · reply by {dl_s}",
                 self.summary]
        details = self.doc.sections.get("Details", "").strip()
        if details:
            lines.append(details[:1500])
        if m.get("task"):
            lines.append(f"Task: {m['task']}")
        if self.execution:
            lines.append(f"Execution: {self.execution.get('status', 'unknown')} — do not replay this request.")
        if m.get("recommendation"):          # written by whoever asks — say so, don't dress it up as the manager's
            lines.append(f"{str(m.get('from', '')).title()} suggests: {m['recommendation']}")
        lines.append(f"If no reply: {default}")
        return "\n".join(lines)


class AskStore:
    def __init__(self, root: Path, tz: str, default_hours: int = 24):
        self.dir = root / "asks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self.default_hours = default_hours
        self._lock = threading.RLock()

    @contextlib.contextmanager
    def decision_guard(self, ask_id: str):
        """Nonblocking OS lock, held across an async decision without a thread-owned RLock.

        This protects independent runtimes/processes sharing a local workspace. A persisted attempt,
        not the lock alone, prevents replay after a process dies. Network filesystems are not supported.
        """
        from .fileio import fcntl
        ask_id = ask_id.upper()
        if not re.fullmatch(r"ASK-\d+", ask_id):
            raise AskError("Invalid approval id.")
        if fcntl is None:
            raise AskError("Safe approval execution requires POSIX file locks on this host.")
        directory = self.dir.parent / ".jm" / "decision-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / (ask_id + ".lock")).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AskError(f"{ask_id} is being decided elsewhere; wait for that attempt to finish.") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def start_execution(self, expected: Ask, by: str) -> Ask:
        with path_lock(expected.path):
            current = self.get(expected.id)
            if current.execution:
                raise AskError(f"{current.id} already has an execution attempt; reconcile it instead of retrying.")
            if current.doc.render() != expected.doc.render() or current.status != "pending":
                raise AskError("The approval changed during review; read it again before deciding.")
            current.doc.meta["execution"] = {
                "status": "started", "by": by, "started_at": now(self.tz).isoformat(timespec="seconds"),
                "request_sha256": hashlib.sha256(current.doc.render().encode()).hexdigest(),
            }
            atomic_write(current.path, current.doc.render())
            return current

    def finish_execution(self, ask_id: str, status: str, error_type: str = "") -> Ask:
        if status not in ("succeeded", "uncertain"):
            raise AskError("Invalid execution outcome.")
        a = self.get(ask_id)
        with path_lock(a.path):
            a = self.get(ask_id)
            if a.execution.get("status") != "started":
                raise AskError(f"{a.id} has no active attempt to finish.")
            a.execution.update(status=status, finished_at=now(self.tz).isoformat(timespec="seconds"))
            if error_type:
                a.execution["error_type"] = error_type  # never persist provider exception text or credentials
            atomic_write(a.path, a.doc.render())
            return a

    def reconcile(self, ask_id: str, outcome: str, by: str, evidence: str) -> Ask:
        """Caller holds decision_guard; resolving never executes or retries the action."""
        if outcome not in ("completed", "not_completed") or not evidence.strip():
            raise AskError("Choose completed/not_completed and record the evidence checked at the destination.")
        a = self.get(ask_id)
        with path_lock(a.path):
            a = self.get(ask_id)
            if a.execution.get("status") not in ("started", "uncertain"):
                raise AskError("Only an interrupted or uncertain attempt can be reconciled.")
            a.execution.update(status="reconciled", outcome=outcome, reconciled_by=by,
                               reconciled_at=now(self.tz).isoformat(timespec="seconds"))
            a.doc.meta.update(status="approved" if outcome == "completed" else "rejected", decided_by=by,
                              decided_at=now(self.tz).isoformat(timespec="seconds"))
            a.doc.add_line("Outcome", f"- Reconciled {outcome} by {by}: {_clean(evidence)}")
            atomic_write(a.path, a.doc.render())
            return a

    def all(self) -> list[Ask]:
        out = []
        for p in sorted(self.dir.glob("ASK-*.md")):
            try:
                out.append(Ask(p, MDoc.parse(p.read_text())))
            except Exception:
                continue
        return out

    def pending(self) -> list[Ask]:
        return [a for a in self.all() if a.status == "pending"]

    def unresolved_executions(self) -> list[Ask]:
        return [a for a in self.all() if a.execution.get("status") in ("started", "uncertain")]

    def execution_tasks(self, exclude: str = "") -> set[str]:
        tasks = set()
        for ask in self.unresolved_executions():
            if ask.id == exclude:
                continue
            payload = ask.doc.meta.get("payload") or {}
            action = payload.get("action") or {}
            tasks.update(str(t) for t in (ask.doc.meta.get("task"), payload.get("event_task"),
                                         action.get("task"), action.get("id") if action.get("type") == "update_task"
                                         else "") if t)
        return tasks

    def get(self, ask_id: str) -> Ask:
        ask_id = ask_id.upper()
        for a in self.all():
            if a.id == ask_id:
                return a
        raise AskError(f"No ask {ask_id}")

    def find_pending(self, requester: str, summary: str, task: str = "") -> Ask | None:
        """The same request, still waiting (so a work session doesn't file it again every time)."""
        key = " ".join(summary.lower().split())
        for a in self.pending():
            if a.requester == requester and " ".join(a.summary.lower().split()) == key \
                    and str(a.doc.meta.get("task") or "") == (task or ""):
                return a
        return None

    def create(self, *, requester: str, summary: str, details: str = "", level: str = "red",
               task: str = "", default: str = "wait", hours: float | None = None, kind: str = "general",
               payload: dict | None = None, recommendation: str = "") -> Ask:
        from .fileio import path_lock
        with self._lock, path_lock(self.dir / ".ids"):
            level = level if level in LEVELS else "red"
            default = default if default in DEFAULTS else "wait"
            if level == "red":
                default = "wait"       # 🔴 never auto-decides: money, public, prod, deletes, legal
            try:
                hours = float(hours) if hours not in (None, "") else float(self.default_hours)
            except (TypeError, ValueError):
                hours = float(self.default_hours)
            hours = min(max(hours, 1.0), 24 * 14)          # at least an hour to answer, at most two weeks
            import re
            nums = [int(m.group(1)) for p in self.dir.glob("ASK-*.md") if (m := re.match(r"ASK-(\d+)", p.name))]
            aid = f"ASK-{(max(nums) + 1) if nums else 1:03d}"
            created = now(self.tz)
            deadline = created + timedelta(hours=hours)
            doc = MDoc(meta={
                "id": aid, "from": requester, "summary": summary.strip(), "level": level, "kind": kind,
                "status": "pending", "task": task or "", "default": default,
                "recommendation": recommendation or "",
                "created": created.isoformat(timespec="minutes"),
                "deadline": deadline.isoformat(timespec="minutes"),
                "payload": payload or {},
            }, sections={"Details": _clean(details), "Outcome": ""})
            a = Ask(self.dir / f"{aid}.md", doc)
            atomic_write(a.path, doc.render())
            return a

    def decide(self, ask_id: str, decision: str, by: str, note: str = "") -> Ask:
        with self._lock, path_lock(self.dir / (ask_id.upper() + ".md")):
            a = self.get(ask_id)
            if a.status != "pending":
                raise AskError(f"{a.id} is already {a.status}")
            if decision not in ("approved", "rejected", "expired"):
                raise AskError("decision must be approved | rejected | expired")
            a.doc.meta["status"] = decision
            a.doc.meta["decided_by"] = by
            a.doc.meta["decided_at"] = now(self.tz).isoformat(timespec="minutes")
            a.doc.add_line("Outcome", f"- {decision} by {by}" + (f": {' '.join(str(note).split())}" if note else ""))
            atomic_write(a.path, a.doc.render())
            return a

    def due_for_default(self) -> list[Ask]:
        t = now(self.tz)
        return [a for a in self.pending() if a.deadline() and a.deadline() <= t]
