"""Git-backed task board. One markdown file per task under tasks/."""
from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .fileio import atomic_write
from .mdoc import MDoc
from .util import parse_date, slugify, today

STATUSES = ["todo", "doing", "review", "blocked", "done", "cut"]
OPEN = {"todo", "doing", "review", "blocked"}
PRIORITIES = ["P0", "P1", "P2"]
TASK_ID = re.compile(r"\bT-\d{3,}\b")
MAX_DOING = 2
SECTIONS = ["Goal", "Done means", "Log", "Feedback", "Output"]


class TaskError(Exception):
    pass


def clean_text(text) -> str:
    """Text from agents/people that goes inside a task file: it must not be able to start a new `## Section`
    (e.g. a fake "## Feedback" or a replaced "## Done means") or break the front matter."""
    t = str(text or "").replace("\r", "")
    t = re.sub(r"(?m)^(\s*)(#{1,2})(\s)", r"\1###\3", t)
    return re.sub(r"(?m)^---\s*$", "—", t).strip()


@dataclass
class Task:
    path: Path
    doc: MDoc

    @property
    def id(self) -> str: return str(self.doc.meta.get("id"))
    @property
    def title(self) -> str: return str(self.doc.meta.get("title", ""))
    @property
    def owner(self) -> str: return str(self.doc.meta.get("owner", ""))
    @property
    def status(self) -> str: return str(self.doc.meta.get("status", "todo"))
    @property
    def priority(self) -> str: return str(self.doc.meta.get("priority", "P1"))
    @property
    def project(self) -> str: return str(self.doc.meta.get("project", "") or "")
    @property
    def blocked_on(self) -> str: return str(self.doc.meta.get("blocked_on", "") or "")

    def due(self, tz: str = "UTC") -> date | None:
        return parse_date(self.doc.meta.get("due"), tz)

    def date_field(self, key: str) -> date | None:
        return parse_date(self.doc.meta.get(key))

    @property
    def is_open(self) -> bool: return self.status in OPEN

    def overdue(self, tz: str) -> bool:
        d = self.due(tz)
        return bool(d and self.is_open and d < today(tz))

    def line(self, tz: str = "UTC") -> str:
        due = self.doc.meta.get("due") or "no date"
        extra = f" — blocked on {self.blocked_on}" if self.status == "blocked" and self.blocked_on else ""
        od = " ⚠️ OVERDUE" if self.overdue(tz) else ""
        return f"{self.id} [{self.priority}] {self.title} — {self.owner} — {self.status} — due {due}{extra}{od}"

    def summary(self) -> str:
        """Compact text for LLM context."""
        m = self.doc.meta
        dm = self.doc.sections.get("Done means", "").strip()
        log = "\n".join(self.doc.sections.get("Log", "").strip().splitlines()[:3])
        fb = "\n".join(self.doc.sections.get("Feedback", "").strip().splitlines()[-3:])      # the newest
        parts = [f"{self.id} | {self.title} | owner={m.get('owner')} | {m.get('priority')} | status={m.get('status')}"
                 f" | due={m.get('due') or '-'} | project={m.get('project') or '-'}"
                 + (f" | blocked_on={self.blocked_on}" if self.blocked_on else "")]
        if dm:
            parts.append(f"  done means: {dm.replace(chr(10), ' ')}")
        if log:
            parts.append(f"  recent log: {log.replace(chr(10), ' / ')}")
        if fb:
            parts.append(f"  feedback: {fb.replace(chr(10), ' / ')}")
        if m.get("rework"):
            parts.append("  ↩ the founder asked for changes — rework this first")
        return "\n".join(parts)


class TaskStore:
    def __init__(self, root: Path, tz: str = "UTC", owner_id: str = "owner"):
        self.dir = root / "tasks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self.owner_id = owner_id
        self._lock = threading.RLock()
        self._cache: dict[Path, tuple] = {}

    # -- read ----------------------------------------------------------------
    def all(self) -> list[Task]:
        """Every task. Parsed files are cached by (mtime, size): a board of 1000 tasks costs a stat per file."""
        tasks = []
        for p in sorted(self.dir.glob("T-*.md")):
            try:
                st = p.stat()
                key = (st.st_mtime_ns, st.st_size)
                hit = self._cache.get(p)
                if not hit or hit[0] != key:
                    hit = (key, MDoc.parse(p.read_text()))
                    self._cache[p] = hit
                d = hit[1]                    # a copy: callers change task docs before saving them
                tasks.append(Task(p, MDoc(meta=copy.deepcopy(d.meta), sections=dict(d.sections), preamble=d.preamble)))
            except Exception:  # a broken file must not break the board
                continue
        return tasks

    def get(self, task_id: str) -> Task:
        task_id = task_id.upper()
        for t in self.all():
            if t.id == task_id:
                return t
        raise TaskError(f"No task {task_id}")

    def for_owner(self, owner: str, open_only: bool = True) -> list[Task]:
        return [t for t in self.all() if t.owner == owner and (t.is_open or not open_only)]

    def _next_id(self) -> str:
        """From the file names, so a broken task file never lets its id be reused."""
        nums = [int(m.group(1)) for p in self.dir.glob("T-*.md") if (m := re.match(r"T-(\d+)", p.name))]
        return f"T-{(max(nums) + 1) if nums else 1:03d}"

    # -- write ---------------------------------------------------------------
    def save(self, task: Task) -> None:
        task.doc.meta["updated"] = today(self.tz).isoformat()
        atomic_write(task.path, task.doc.render())
        self._cache.pop(task.path, None)

    def create(self, *, title: str, owner: str, created_by: str, priority: str = "P1",
               due: str | None = None, project: str = "", goal: str = "", done_means: list[str] | None = None,
               description: str = "", depends_on: list[str] | None = None, reviewer: str | None = None) -> Task:
        from .fileio import path_lock
        with self._lock, path_lock(self.dir / ".ids"):     # ids stay unique across processes (jm chat + jm run)
            priority = (priority or "P1").upper()
            if priority not in PRIORITIES:
                raise TaskError(f"priority must be one of {PRIORITIES}")
            is_owner = created_by == self.owner_id
            if priority == "P0" and not is_owner:
                existing = [t for t in self.for_owner(owner) if t.priority == "P0"]
                if existing:
                    raise TaskError(f"{owner} already has a P0 ({existing[0].id}). One P0 per person — "
                                    f"ask {self.owner_id} to re-prioritise.")
            d = parse_date(due, self.tz) if due else None
            tid = self._next_id()
            t0 = today(self.tz).isoformat()
            doc = MDoc(meta={
                "id": tid, "title": " ".join(clean_text(title).split()) or "untitled", "owner": owner, "project": project or "",
                "priority": priority, "status": "todo", "assigned": t0,
                "due": d.isoformat() if d else "", "depends_on": depends_on or [],
                "reviewer": reviewer or self.owner_id, "goal": goal or "",
                "created_by": created_by, "blocked_on": "", "status_since": t0, "updated": t0,
            })
            doc.sections = {s: "" for s in SECTIONS}
            doc.sections["Goal"] = clean_text(description) or clean_text(title)
            doc.sections["Done means"] = "\n".join(f"- [ ] {clean_text(x)}" for x in (done_means or []) if clean_text(x))
            doc.add_line("Log", f"- {t0} — created by {created_by}, assigned to {owner}")
            path = self.dir / f"{tid}-{slugify(title)}.md"
            t = Task(path, doc)
            self.save(t)
            return t

    def set_status(self, task_id: str, status: str, by: str, note: str = "",
                   blocked_on: str = "") -> tuple[Task, str]:
        """Change status, enforcing team rules. Returns (task, message)."""
        with self._lock:
            status = status.lower().strip()
            if status not in STATUSES:
                raise TaskError(f"status must be one of {STATUSES}")
            t = self.get(task_id)
            is_owner = by == self.owner_id
            reviewer = str(t.doc.meta.get("reviewer") or self.owner_id)
            msg = ""
            if status == "cut" and not is_owner:
                raise TaskError(f"Only {self.owner_id} can cut a task — say why in a log note or ask them.")
            if t.status in ("done", "cut") and status in ("done", "cut"):
                raise TaskError(f"{t.id} is already {t.status}.")
            if status == "done" and by not in (reviewer, self.owner_id):
                status = "review"
                msg = f"Only {reviewer} can mark done — moved to review instead."
            if status == "doing" and not is_owner:
                if not t.doc.sections.get("Done means", "").strip():
                    raise TaskError(f"{t.id} has no 'Done means' — write 2–4 acceptance checks before starting.")
                doing = [x for x in self.for_owner(t.owner) if x.status == "doing" and x.id != t.id]
                if len(doing) >= MAX_DOING:
                    raise TaskError(f"{t.owner} already has {MAX_DOING} tasks in doing "
                                    f"({', '.join(x.id for x in doing)}). Finish or park one first.")
            if status == "blocked" and not blocked_on:
                raise TaskError("blocked needs `blocked_on`: the person + the exact thing needed.")
            t.doc.meta["blocked_on"] = blocked_on if status == "blocked" else ""
            if t.status != status:
                t.doc.meta["status_since"] = today(self.tz).isoformat()
            t.doc.meta["status"] = status
            line = f"- {today(self.tz).isoformat()} — {by}: {status}" + (f" — {note}" if note else "")
            t.doc.add_line("Log", line, newest_first=True)
            if status == "done":
                t.doc.meta["done_on"] = today(self.tz).isoformat()
            if status in ("review", "done", "cut"):
                t.doc.meta.pop("rework", None)
            self.save(t)
            return t, msg

    def unblock_dependents(self, done_id: str, by: str) -> list[Task]:
        """Tasks blocked on `done_id` go back to todo when it is done."""
        out = []
        with self._lock:
            for t in self.all():
                if t.status == "blocked" and re.search(rf"\b{re.escape(done_id)}\b", t.blocked_on):
                    t2, _ = self.set_status(t.id, "todo", by=self.owner_id, note=f"unblocked: {done_id} is done")
                    out.append(t2)
        return out

    def mark_rework(self, task_id: str) -> Task:
        with self._lock:
            t = self.get(task_id)
            t.doc.meta["rework"] = True
            self.save(t)
            return t

    def add_log(self, task_id: str, by: str, text: str) -> Task:
        with self._lock:
            t = self.get(task_id)
            body = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[—-]\s*", "", " ".join(clean_text(text).split()))   # no double dates
            t.doc.add_line("Log", f"- {today(self.tz).isoformat()} — {by}: {body}", newest_first=True)
            self.save(t)
            return t

    def add_feedback(self, task_id: str, by: str, text: str) -> Task:
        with self._lock:
            t = self.get(task_id)
            t.doc.add_line("Feedback", f"- {today(self.tz).isoformat()} — {by}: {' '.join(clean_text(text).split())}")
            self.save(t)
            return t

    def set_done_means(self, task_id: str, by: str, checks: list[str]) -> Task:
        with self._lock:
            t = self.get(task_id)
            if isinstance(checks, str):
                checks = checks.splitlines()
            t.doc.sections["Done means"] = "\n".join(f"- [ ] {clean_text(c)}" for c in checks if clean_text(c))
            t.doc.add_line("Log", f"- {today(self.tz).isoformat()} — {by}: set done means", newest_first=True)
            self.save(t)
            return t

    def set_output(self, task_id: str, by: str, text: str) -> Task:
        with self._lock:
            t = self.get(task_id)
            body = " ".join(clean_text(text).split())
            if any(line.endswith(f"{by}: {body}") for line in t.doc.sections.get("Output", "").splitlines()):
                return t                                  # the same output twice is one line
            t.doc.add_line("Output", f"- {today(self.tz).isoformat()} — {by}: {body}")
            self.save(t)
            return t

    def clear_due(self, task_id: str, by: str) -> Task:
        with self._lock:
            t = self.get(task_id)
            if t.doc.meta.get("due"):
                t.doc.meta["due"] = ""
                t.doc.add_line("Log", f"- {today(self.tz).isoformat()} — {by}: due date removed", newest_first=True)
                self.save(t)
            return t

    def update_fields(self, task_id: str, by: str, **fields) -> Task:
        allowed = {"priority", "due", "title", "project", "owner", "goal"}
        with self._lock:
            t = self.get(task_id)
            changes = []
            for k, v in fields.items():
                if k not in allowed or v in (None, ""):
                    continue
                if k == "priority":
                    v = str(v).upper()
                    if v not in PRIORITIES:
                        raise TaskError(f"priority must be one of {PRIORITIES}")
                if k == "due":
                    d = parse_date(v, self.tz)
                    if not d:
                        raise TaskError(f"bad date: {v}")
                    v = d.isoformat()
                if k == "priority" and v == "P0" and by != self.owner_id and t.priority != "P0":
                    owner = str(fields.get("owner") or t.owner)
                    other = [x for x in self.for_owner(owner) if x.priority == "P0" and x.id != t.id]
                    if other:
                        raise TaskError(f"{owner} already has a P0 ({other[0].id}). One P0 per person — "
                                        f"ask {self.owner_id} to re-prioritise.")
                if k in ("title", "goal"):
                    v = " ".join(clean_text(v).split())
                t.doc.meta[k] = v
                changes.append(f"{k}={v}")
            if changes:
                t.doc.add_line("Log", f"- {today(self.tz).isoformat()} — {by}: set {', '.join(changes)}",
                               newest_first=True)
                self.save(t)
            return t

    # -- views ---------------------------------------------------------------
    def board(self) -> str:
        tasks = self.all()
        order = ["blocked", "doing", "review", "todo"]
        out = []
        for st in order:
            items = sorted([t for t in tasks if t.status == st], key=lambda t: (t.priority, t.id))
            if items:
                out.append(f"{st.upper()} ({len(items)})")
                out += [f"  {t.line(self.tz)}" for t in items]
        done = [t for t in tasks if t.status == "done"]
        if done:
            recent = sorted(done, key=lambda t: str(t.doc.meta.get("done_on", "")), reverse=True)[:5]
            out.append(f"DONE (last {len(recent)} of {len(done)})")
            out += [f"  {t.id} {t.title} — {t.owner} — {t.doc.meta.get('done_on', '')}" for t in recent]
        return "\n".join(out) or "Board is empty. Assign the first task with /assign."

    def person_status(self, member_id: str) -> tuple[str, str]:
        """Derive (status, one-line) for a person from their tasks."""
        mine = self.for_owner(member_id)
        if not mine:
            return "not started", "no open tasks"
        blocked = [t for t in mine if t.status == "blocked"]
        over = [t for t in mine if t.overdue(self.tz)]
        doing = [t for t in mine if t.status == "doing"]
        focus = (blocked or over or doing or sorted(mine, key=lambda t: t.priority))[0]
        if blocked:
            st = "blocked"
        elif over:
            st = "at risk"
        elif doing or any(t.status == "review" for t in mine):
            st = "on track"
        else:
            st = "not started"
        return st, focus.line(self.tz)
