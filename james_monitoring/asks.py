"""Permission requests ("asks") — the only way an agent gets a 🔴 action approved."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .fileio import atomic_write
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
        with self._lock:
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
        with self._lock:
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
