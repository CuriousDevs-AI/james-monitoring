"""The team runtime: routes events to agents, calls the model, applies actions, enforces rules.

Transport-agnostic: Telegram (gateway.py) and the local CLI both talk to it through a `Bus`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol

from .asks import Ask, AskError, AskStore
from .assistant import REMIND_SPEC
from .chat import BACKCHANNEL, PROJECT_ROOM, TEAM_ROOM, ChatStore
from .config import Config, Member
from .executor import Executor, ExecutorError
from .fileio import atomic_write
from .llm import LLM, LLMError, is_transient, make_llm
from .monitor import build_report, checks, team_status_lines, write_report
from .prompts import build_system
from .tasks import TASK_ID, TaskError, TaskStore
from .util import now, today
from .workspace import Workspace

log = logging.getLogger("jm.runtime")

MAX_READ_ROUNDS = 2
MAX_FILE_CHARS = 60_000
READABLE_DIRS = ("docs", "tasks", "reports", "team", "asks", "decisions", "files")
HISTORY_TURNS = 8
TURN_SECONDS = 420
# A person's CLI session (Claude Code / Codex / OpenCode) is kept across calls and restarts, and started fresh when
# the model changes or it gets long or old (the fresh one is seeded with the recent conversation).
WATCHDOG_SECONDS = TURN_SECONDS + 120  # a turn still running after this is stopped (a hung CLI, a dead network)
RETRY_BACKOFF = (3, 10)             # seconds between retries of a model call that hit a temporary problem
# A CLI session (claude/codex/opencode) is resumed until the conversation itself gets long — measured by the last
# call's input (the whole conversation so far), not by adding up every call — or old, or the model changes.
SESSION_MAX_CONTEXT = 150_000       # tokens in the conversation (the last call's full input, cached included)
SESSION_MAX_CALLS = 200             # a safety net
SESSION_MAX_DAYS = 3                # model calls in one turn (retries, file reads, repair) stop after this                 # own recent exchanges per channel (the room transcript adds everyone else)
ROOM_CONTEXT = 16                 # recent messages of the current room shown to the agent
ELSEWHERE = 4                     # recent messages from each other room the agent is part of
ELSEWHERE_HOURS = 72
NO_GATE = {"ask_permission", "remember", "notify_owner", "read_file", "remind", "learn"}   # never need approval


# -- transport interface --------------------------------------------------------
class Bus(Protocol):
    async def send_owner(self, from_id: str, text: str, ask: Ask | None = None, urgent: bool = False) -> None: ...
    async def post_group(self, from_id: str, text: str, reply_to: int | None = None) -> None: ...


class ConsoleBus:
    """Prints instead of sending. Used by `jm chat` and tests."""

    def __init__(self, quiet: bool = False):
        self.sent: list[tuple[str, str, str]] = []   # (channel, from, text)
        self.quiet = quiet

    async def send_owner(self, from_id, text, ask=None, urgent=False):
        self.sent.append(("owner", from_id, text if not ask else ask.card()))
        if not self.quiet:
            print(f"\n[DM → owner] {from_id}: {text if not ask else ask.card() + '  [approve|reject]'}")

    async def post_group(self, from_id, text, reply_to=None):
        self.sent.append(("group", from_id, text))
        if not self.quiet:
            print(f"\n[HQ group] {from_id}: {text}")


@dataclass
class Event:
    source: str                  # dm | group | inbox | system
    text: str
    sender: str                  # "owner" or a member id
    hop: int = 0
    task: str = ""
    meta: dict = field(default_factory=dict)
    project: str = ""            # set when the message comes from a project room
    room: str = ""               # chat room it came from (see chat.py); derived when empty


# A document after the JSON: the path may contain spaces; the end marker must be alone on its line (so a document
# that merely mentions it isn't cut short).
_FILE_BLOCK = re.compile(r"^<<<FILE[ \t]+([^\n>]+?)[ \t]*>>>[ \t]*\n(.*?)\n?^<<<END(?:[ \t]+FILE)?>>>[ \t]*$", re.S | re.M)
_STR_FIELDS = ("title", "owner", "text", "summary", "details", "note", "path", "id", "task", "project", "to",
               "description", "log", "output", "blocked_on", "instructions", "recommendation", "goal", "due",
               "priority", "status", "level", "default", "why", "at", "lesson", "topic")


@dataclass
class Parsed:
    reply: str
    actions: list[dict]
    ok: bool                     # a JSON object was found (False: plain text or a cut-off reply)


def parse_reply(text: str) -> Parsed:
    """Tolerant extraction of {"reply", "actions"}.

    - JSON inside prose or code fences, or followed by more text, is found.
    - Long documents can come *after* the JSON as <<<FILE docs/x.md>>> … <<<END>>> blocks, so a big file never
      breaks the JSON. A write_file without content takes its block; a block without an action is saved anyway.
    - A reply cut off mid-JSON still yields its "reply" text (ok=False, so the caller can ask again)."""
    files: dict[str, str] = {}

    def keep(m):
        files[m.group(1).strip().lstrip("/")] = m.group(2)
        return ""
    t = _FILE_BLOCK.sub(keep, text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    # Every {"reply"/"actions"} object in the text is a candidate; a model sometimes quotes the format before its
    # real answer ('Format is {"reply": "..."}. Answer: {...}'), so the answer is the *last* one, and a placeholder
    # reply like "..." never beats a real one.
    dec = json.JSONDecoder()
    found: list[dict] = []
    i = 0
    while i < len(t):
        k = t.find("{", i)
        if k < 0:
            break
        try:
            cand, end = dec.raw_decode(t, k)
        except json.JSONDecodeError:
            i = k + 1
            continue
        if isinstance(cand, dict) and ("reply" in cand or "actions" in cand):
            found.append(cand)
            i = end
        else:
            i = k + 1
    real = [c for c in found if str(c.get("reply", "")).strip() not in ("", "...", "…", "<reply>")
            or c.get("actions")]
    obj = (real or found)[-1] if found else None
    if obj is None:
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)', t)
        if m and t.lstrip().startswith("{"):
            try:
                salvaged = json.loads('"' + m.group(1) + '"')
            except json.JSONDecodeError:
                salvaged = m.group(1)
            return Parsed(salvaged.strip(), _file_actions(files, []), False)
        return Parsed(t, _file_actions(files, []), False)
    actions = obj.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    actions = [a for a in actions if isinstance(a, dict) and a.get("type")]
    return Parsed(str(obj.get("reply") or "").strip(), _file_actions(files, actions), True)


def _norm_doc(path: str) -> str:
    path = (path or "").strip().lstrip("/")
    return path if path.startswith("docs/") else "docs/" + path


def _file_actions(files: dict[str, str], actions: list[dict]) -> list[dict]:
    if not files:
        return actions
    by_path = {_norm_doc(k): v for k, v in files.items()}
    used = set()
    for a in actions:
        if a.get("type") == "write_file" and not a.get("content"):
            key = _norm_doc(str(a.get("path") or ""))
            if key in by_path:
                a["content"] = by_path[key]
                used.add(key)
        elif a.get("type") == "write_file":
            used.add(_norm_doc(str(a.get("path") or "")))
    return actions + [{"type": "write_file", "path": k, "content": v} for k, v in by_path.items() if k not in used]


def parse_model_output(text: str) -> tuple[str, list[dict]]:
    p = parse_reply(text)
    return p.reply, p.actions


def normalize_action(a: dict) -> dict:
    """Models get types wrong ("hours": "24", "title": null, done_means as one string). Fix what can be fixed;
    anything else is reported for that one action — never for the whole turn."""
    out: dict = {"type": str(a.get("type", "")).strip().lower()}
    for k, v in a.items():
        if k == "type" or v is None:
            continue
        if k in _STR_FIELDS:
            v = v if isinstance(v, str) else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
            v = v.strip()
            if k in ("priority",):
                v = v.upper()
            if k in ("status", "level", "default", "owner", "to"):
                v = v.lower().lstrip("@")
            if v:
                out[k] = v
        elif k == "content":
            out[k] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2)
        elif k == "done_means":
            items = v if isinstance(v, list) else re.split(r"\n|;|\u2022", str(v))
            items = [re.sub(r"^\s*(?:[-*]|\[[ x]\])\s*", "", str(x)).strip() for x in items]
            out[k] = [x for x in items if x]
        elif k == "hours":
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
        else:
            out[k] = v
    return out


def _compact(actions: list[dict]) -> list[dict]:
    """Actions as kept in history: file bodies shortened so history stays small."""
    out = []
    for a in actions:
        a = dict(a)
        if isinstance(a.get("content"), str) and len(a["content"]) > 300:
            a["content"] = a["content"][:300] + f"… ({len(a['content'])} chars, saved)"
        out.append(a)
    return out


class Runtime:
    def __init__(self, cfg: Config, llm: LLM, bus: Bus | None = None, ws: Workspace | None = None,
                 shared: "Runtime | None" = None):
        """`shared`: the runtime this one replaces (a reload). Per-person locks and background work carry over,
        so nobody handles two messages at once while the old runtime finishes what it started."""
        self.cfg = cfg
        self.llm = llm
        self.bus: Bus = bus or ConsoleBus()
        self.ws = ws or Workspace(cfg)
        self.ws.ensure()
        self.owner_id = cfg.owner_key
        self.tasks = TaskStore(self.ws.root, cfg.timezone, owner_id=self.owner_id)
        self.asks = AskStore(self.ws.root, cfg.timezone, cfg.ask_default_hours)
        self.executor = Executor(cfg)
        self.executor.title_of = lambda tid: self.tasks.get(tid).title
        self._github = None
        self.chat = ChatStore(self.ws.root)
        from .audit import AuditLog
        self.audit = AuditLog(self.ws.root, cfg.timezone)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
        self._bg: set[asyncio.Task] = set()
        self._llms: dict[str, LLM] = {}
        self._coding: set[str] = set()                    # tasks with a coding run in progress
        self._gh_lock = asyncio.Lock()
        self._ask_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.turns: dict[str, dict] = {}                  # who is thinking right now (System health)
        if shared is not None:
            self._locks, self._bg, self.turns = shared._locks, shared._bg, shared.turns
            self._coding, self._ask_locks, self._gh_lock = shared._coding, shared._ask_locks, shared._gh_lock

    def llm_for(self, m: Member, project: str = "") -> LLM:
        """Each person can run on their own model (Claude, Codex, a local model…) — and on a project, on that
        project's model or their own model for that project. Default: the company's."""
        conf = self.cfg.llm_for(m, project)
        if conf is self.cfg.llm:
            return self._sandboxed(self.llm)
        key = f"{m.id}@{project}" if self.cfg.llm_source(m, project) in ("project", "person_project") else m.id
        if key not in self._llms:
            self._llms[key] = self._sandboxed(make_llm(conf))
        return self._llms[key]

    def _sandboxed(self, llm: LLM) -> LLM:
        """CLI models keep their per-person folders (and so their sessions) in the company's own sandbox."""
        if hasattr(llm, "sandbox") and not getattr(llm, "sandbox", None):
            d = self.ws.root / ".jm" / "sandbox"
            d.mkdir(parents=True, exist_ok=True)
            llm.sandbox = str(d)
        return llm

    # -- CLI sessions per person per room (survive restarts) ------------------------------
    def _sessions_path(self):
        return self.ws.root / ".jm" / "sessions.json"

    def _sessions(self) -> dict:
        try:
            return json.loads(self._sessions_path().read_text())
        except (OSError, ValueError):
            return {}

    def _update_sessions(self, fn) -> None:
        from .fileio import path_lock
        with path_lock(self._sessions_path()):
            data = self._sessions()
            fn(data)
            atomic_write(self._sessions_path(), json.dumps(data, indent=1))

    def session_for(self, m: Member, room: str, project: str = ""):
        """This person's session in this room — resumed if it's still the same model and not too long or old."""
        from .llm import Session
        key = f"{m.id}:{room}"
        rec = self._sessions().get(key) or {}
        fresh = (not rec.get("id") or rec.get("model") != self.model_name(m, project)
                 or int(rec.get("calls", 0)) >= SESSION_MAX_CALLS or int(rec.get("context", 0)) >= SESSION_MAX_CONTEXT
                 or (time.time() - float(rec.get("started", 0) or 0)) > SESSION_MAX_DAYS * 86400)
        return Session(key, "" if fresh else rec["id"], "" if fresh else rec.get("system_hash", ""))

    def _save_session(self, m: Member, sess, res, project: str = "") -> None:
        if not res.session_id:
            return
        model = self.model_name(m, project)

        def fn(data):
            rec = data.get(sess.key) or {}
            if rec.get("id") != res.session_id:
                rec = {"id": res.session_id, "started": time.time(), "calls": 0, "tokens": 0}
            rec.update(model=model, system_hash=res.system_hash, last=time.time(),
                       calls=int(rec.get("calls", 0)) + 1,
                       tokens=int(rec.get("tokens", 0)) + getattr(res, "billable_tokens", res.total_tokens),
                       context=int(res.input_tokens or 0))        # how long the conversation is now
            data[sess.key] = rec
        self._update_sessions(fn)
        sess.id, sess.system_hash = res.session_id, res.system_hash

    def sessions(self) -> list[dict]:
        """Every saved session: who, where, which model, how long — for Settings → Models."""
        out = []
        for key, rec in self._sessions().items():
            mid, _, room = key.partition(":")
            if not self.cfg.member(mid):
                continue
            out.append({"member": mid, "room": room, "model": rec.get("model", ""), "calls": rec.get("calls", 0),
                        "tokens": rec.get("tokens", 0), "started": rec.get("started"), "last": rec.get("last"),
                        "current": rec.get("model") == self.model_name(self.cfg.member(mid))})
        return sorted(out, key=lambda x: (x["member"], -(x["last"] or 0)))

    def reset_sessions(self, member: str = "", room: str = "") -> int:
        """Start fresh next time (e.g. after big persona changes). Memory, tasks and chat are untouched."""
        gone = []

        def fn(data):
            for key in list(data):
                mid, _, r = key.partition(":")
                if (not member or mid == member) and (not room or r == room):
                    gone.append(data.pop(key))
        self._update_sessions(fn)
        return len(gone)

    def model_error(self, member_id: str) -> str:
        """The last model error for this person — only if it came from the model they use *now*."""
        hb = (self.ws.state().get("heartbeat") or {}).get(member_id) or {}
        m = self.cfg.member(member_id)
        if not hb.get("error") or not m or hb.get("model", self.model_name(m)) not in self.models_of(m):
            return ""
        return hb["error"]

    def models_of(self, m: Member) -> set[str]:
        """Every model this person uses now: their own, and any a project gives them."""
        return {self.model_name(m)} | {self.model_name(m, p) for p in self.cfg.projects if m.id in
                                       [x.id for x in self.cfg.project_members(p)]}

    def model_name(self, m: Member, project: str = "") -> str:
        c = self.cfg.llm_for(m, project)
        if c.provider in ("opencode", "open-code"):
            return f"opencode:{c.model}"                  # OpenCode models already read "provider/model"
        return c.provider + (f"/{c.model}" if c.model else "")

    # -- state helpers ---------------------------------------------------------
    def project_paused(self, pid: str) -> bool:
        """A project on pause: nobody works on it (its room, its tasks, work sessions, coding) until it resumes."""
        p = self.cfg.projects.get(pid) if pid else None
        return bool(p and p.status == "paused")

    def action_project(self, a: dict, ev: "Event") -> str:
        """The project an action touches — the one it names, its task's, or the conversation's — whose rules apply."""
        if a.get("project") and a["project"] in self.cfg.projects:
            return a["project"]
        tid = a.get("task") or (a.get("id") if a.get("type") == "update_task" else "")
        if tid:
            try:
                pid = self.tasks.get(str(tid)).project
                if pid:
                    return pid
            except TaskError:
                pass
        return self.project_of(ev)

    def project_of(self, ev: "Event") -> str:
        """The project an event is about: its room's project, or its task's."""
        if ev.project:
            return ev.project
        if ev.room and ev.room.startswith(PROJECT_ROOM):
            return ev.room[len(PROJECT_ROOM):]
        if ev.task:
            try:
                return self.tasks.get(ev.task).project
            except TaskError:
                return ""
        return ""

    def paused(self, member_id: str) -> bool:
        s = self.ws.state()
        return bool(s.get("paused_all")) or member_id in (s.get("paused") or [])

    def set_paused(self, who: str, value: bool) -> str:
        def fn(s):
            s.setdefault("paused", [])
            if who == "all":
                s["paused_all"] = value
                if not value:
                    s["paused"] = []
            elif value and who not in s["paused"]:
                s["paused"].append(who)
            elif not value and who in s["paused"]:
                s["paused"].remove(who)
        self.ws.update_state(fn)
        return f"{'Paused' if value else 'Resumed'}: {who}"

    def _usage_today(self, member_id: str) -> int:
        return int(self.ws.state().get("usage", {}).get(today(self.cfg.timezone).isoformat(), {}).get(member_id, 0))

    def _add_usage(self, member_id: str, tokens: int, model: str = "") -> int:
        """Tokens per person per day (31 days kept) and per model, for the budget and the cost view."""
        day = today(self.cfg.timezone).isoformat()

        def fn(s):
            u = s.setdefault("usage", {})
            for k in sorted(u)[:-31]:
                del u[k]
            u.setdefault(day, {})
            u[day][member_id] = int(u[day].get(member_id, 0)) + tokens
            if model:
                bm = s.setdefault("usage_models", {})
                for k in sorted(bm)[:-31]:
                    del bm[k]
                bm.setdefault(day, {})
                bm[day][model] = int(bm[day].get(model, 0)) + tokens
        return int(self.ws.update_state(fn)["usage"][day][member_id])

    def _heartbeat(self, member_id: str, error: str = "", project: str = "") -> None:
        """Last contact per person; `fails` counts consecutive model failures (one timeout isn't an incident)."""
        m = self.cfg.member(member_id)
        model = self.model_name(m, project) if m else ""

        if error:
            prev_err = ((self.ws.state().get("heartbeat") or {}).get(member_id) or {}).get("error", "")
            if error != prev_err:                                 # a new failure, not the same one again
                self._audit("model", member_id, "model error", model, error)

        def fn(s):
            hb = s.setdefault("heartbeat", {})
            prev = hb.get(member_id) or {}
            same = prev.get("model", model) == model
            hb[member_id] = {"last": now(self.cfg.timezone).isoformat(timespec="minutes"), "error": error,
                             "model": model, "fails": (int(prev.get("fails", 0)) + 1 if same else 1) if error else 0}
        self.ws.update_state(fn)

    # -- messages that arrive while someone can't work (paused, over budget) -----------
    def _enqueue(self, m: Member, ev: Event, why: str) -> None:
        item = {"member": m.id, "why": why, "event": {k: getattr(ev, k) for k in
                                                      ("source", "text", "sender", "hop", "task", "project", "room",
                                                       "meta")}}
        self.ws.update_state(lambda s: s.setdefault("queue", []).append(item))

    def queued(self) -> list[dict]:
        return list(self.ws.state().get("queue") or [])

    async def drain_queue(self) -> int:
        """Deliver queued messages to everyone who can work again. Replies go to the room they came from."""
        ready: list[dict] = []

        def take(s):
            keep = []
            for it in s.get("queue") or []:
                mid = it.get("member", "")
                cap = self.cfg.daily_tokens_per_agent
                ev_ = it.get("event") or {}
                pid = self.project_of(Event(**ev_)) if ev_ else ""
                ok = (self.cfg.member(mid) and not self.paused(mid) and not (cap and self._usage_today(mid) >= cap)
                      and not self.project_paused(pid))
                (ready if ok else keep).append(it)
            s["queue"] = keep
        self.ws.update_state(take)
        for it in ready:
            self._spawn(self._answer_queued(it))
        return len(ready)

    async def _answer_queued(self, it: dict) -> None:
        ev = Event(**it["event"])
        ev.meta = {**(ev.meta or {}), "queued": True}
        m = self.cfg.member(it["member"])
        if not m:
            return
        if ev.source == "inbox":                          # a teammate's question: the answer goes back to them
            await self._deliver(m.id, ev)
            return
        reply = await self.dispatch(m.id, ev)
        room = self.room_of(m, ev)
        post = getattr(self.bus, "post_room", None)
        if ev.source == "system" and not post:
            return
        if post:
            why = str(it.get("why") or "")
            since = ("while " + why if " was " in why or why.startswith("I ") else f"while I was {why or 'away'}")
            await post(room, m.id, f"(answering what came in {since}) {reply}")
        else:
            await self.bus.send_owner(m.id, reply)

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """Wait for background work (agent-to-agent messages, coding runs). Used by CLI/tests."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # -- conversation history (survives restarts; .jm/ is not committed) ---------
    def _hist_path(self, key: str):
        return self.ws.root / ".jm" / "history" / f"{key.replace(':', '__')}.json"

    def _load_history(self, key: str, legacy: tuple[str, ...] = ()) -> deque:
        """History is kept per person *per room*: what Sofia said to you in your 1:1 — including approval
        follow-ups and work-session notes — is one thread, whatever triggered it. `legacy`: older keys to read
        once (history used to be kept per message source)."""
        if key not in self._history:
            d: deque = deque(maxlen=HISTORY_TURNS * 2)
            for k in (key, *legacy):
                p = self._hist_path(k)
                if p.exists():
                    try:
                        d.extend(x for x in json.loads(p.read_text()) if isinstance(x, dict) and x.get("content"))
                        break
                    except (json.JSONDecodeError, OSError):
                        continue
            self._history[key] = d
        return self._history[key]

    def _save_history(self, key: str, hist: deque) -> None:
        atomic_write(self._hist_path(key), json.dumps(list(hist)))

    # -- documents agents write and read (docs/ in the workspace) -----------------
    def _doc_path(self, rel: str):
        rel = (rel or "").strip().lstrip("/")
        if not rel.startswith("docs/"):
            rel = "docs/" + rel
        root = self.ws.root.resolve()                     # /tmp → /private/tmp etc.: compare resolved paths
        p = (root / rel).resolve()
        docs = (root / "docs").resolve()
        if docs not in p.parents:
            raise ValueError("files must stay inside docs/")
        return p, str(p.relative_to(root))

    def _resolve_readable(self, rel: str):
        """The file an agent may read, or None. Checked on the *resolved* path: `docs/../.git/config` is refused,
        and nothing under .git, .jm or any .env is ever readable."""
        root = self.ws.root.resolve()
        rel = (rel or "").strip().lstrip("/")
        m = re.fullmatch(r"(?:tasks/)?(T-\d{3,})(?:[-\w]*)?(?:\.md)?", rel)
        if m:                                              # "T-001", "tasks/T-001.md" → the real task file
            try:
                return self.tasks.get(m.group(1)).path.resolve()
            except TaskError:
                return None
        p = (root / rel).resolve()
        try:
            parts = p.relative_to(root).parts
        except ValueError:
            return None
        if not parts or parts[0] not in READABLE_DIRS or not p.is_file():
            return None
        if any(x in (".git", ".jm") or x.startswith(".env") for x in parts):
            return None
        return p

    def _read_files(self, reads: list[dict]) -> str:
        out = []
        for a in reads[:5]:
            rel = str(a.get("path", "")).strip().lstrip("/")
            p = self._resolve_readable(rel)
            if p:
                out.append(f"=== {rel} ===\n{p.read_text(errors='replace')[:MAX_FILE_CHARS]}")
            else:
                out.append(f"=== {rel} === (cannot read: only existing files under {', '.join(READABLE_DIRS)})")
        return "Files you asked for:\n\n" + "\n\n".join(out) + "\n\nNow give your final JSON answer."

    def list_docs(self, limit: int = 40) -> str:
        docs = self.ws.root / "docs"
        if not docs.exists():
            return ""
        files = sorted((p for p in docs.rglob("*") if p.is_file() and ".git" not in p.parts),
                       key=lambda p: p.stat().st_mtime)
        return "\n".join(f"- {p.relative_to(self.ws.root)}" for p in files[-limit:])        # the newest

    # -- context -----------------------------------------------------------------
    def roster(self) -> str:
        lines = [f"- {self.owner_id}: {self.cfg.owner_name} — founder (the owner)"]
        for m in self.cfg.workers:                        # the personal assistant isn't part of the company team
            proj = f" — projects: {', '.join(m.projects)}" if m.projects else ""
            dep = self.cfg.departments.get(m.department)
            lines.append(f"- {m.id}: {m.name} — {m.role}{proj}"
                         + (f" — {dep.name}" + (" (head)" if dep.head == m.id else "") if dep else ""))
        return "\n".join(lines)

    def projects_text(self) -> str:
        lines = []
        for pid, p in self.cfg.projects.items():
            lead = self.cfg.member(p.lead).name if p.lead and self.cfg.member(p.lead) else "-"
            extra = f" — {p.description}" if p.description else ""
            team = ", ".join(x.id for x in self.cfg.project_members(pid)) or "-"
            lines.append(f"- {pid}: {p.name or pid} [{p.status}] lead={lead} team={team}{' (code repo)' if p.repo else ''}{extra}")
        return "\n".join(lines)

    def project_channel(self, pid: str) -> str:
        """Channel description for a project room: the brief, who is on it and its slice of the board."""
        p = self.cfg.projects.get(pid)
        name = (p.name if p and p.name else pid)
        people = ", ".join(f"{x.name} ({x.role})" for x in self.cfg.project_members(pid)) or "nobody yet"
        lead = self.cfg.member(p.lead).name if p and p.lead and self.cfg.member(p.lead) else "-"
        tasks = [t for t in self.tasks.all() if t.project == pid and t.is_open]
        board = "\n".join(f"  {t.line(self.cfg.timezone)}" for t in tasks) or "  (no open tasks)"
        return (f"the #{name} project room — everyone on project `{pid}` reads it, keep it short and about this project.\n"
                f"Project brief: {(p.description if p else '') or '-'} · status {(p.status if p else 'active')} · lead {lead}\n"
                f"On this project: {people}\nOpen tasks in this project (create new ones with project `{pid}`):\n{board}")

    _STOP = set("""about after again also been before being could does doing done from have here into just like make
        more most much need only other over please some such than that their them then there these they this those
        through very want were what when where which while will with would your yours today tomorrow task tasks""".split())

    def experience(self, m: Member, ev: "Event", limit: int = 3) -> str:
        """Related past work of this person — finished tasks like this one (what they delivered, the founder's
        feedback) and documents they wrote — so experience carries over: the second landing page is better than
        the first. Plain word overlap over the files; no index to keep in sync."""
        words = lambda s: {w for w in re.findall(r"[a-z0-9]{4,}", str(s).lower()) if w not in self._STOP}   # noqa: E731
        query = ev.text
        if ev.task:
            try:
                cur = self.tasks.get(ev.task)
                query += " " + cur.title + " " + cur.doc.sections.get("Goal", "")
            except TaskError:
                pass
        q = words(query)
        if len(q) < 2:
            return ""
        scored = []
        for t in self.tasks.all():
            if t.owner != m.id or t.status != "done" or t.id == ev.task:
                continue
            hay = words(t.title + " " + t.doc.sections.get("Goal", "") + " " + t.doc.sections.get("Output", ""))
            hit = len(q & hay)
            if hit >= 2:
                scored.append((hit, str(t.doc.meta.get("done_on", "")), t))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        lines = []
        for _, _, t in scored[:limit]:
            out = " ".join(t.doc.sections.get("Output", "").split())[:220]
            fb = [x for x in t.doc.sections.get("Feedback", "").splitlines() if x.strip()]
            ok = next((ln.split(": done — ", 1)[1] for ln in t.doc.sections.get("Log", "").splitlines()
                       if ": done — " in ln and not ln.rstrip().endswith(": done — accepted")), "")
            lines.append(f"- {t.id} {t.title} (done {t.doc.meta.get('done_on', '')})" + (f" — delivered: {out}" if out else "")
                         + (f" — feedback: {' '.join(fb[-1].split())[:200]}" if fb else "")
                         + (f" — accepted with: {' '.join(ok.split())[:160]}" if ok and ok.strip() != "accepted" else ""))
        docs = []
        for p in sorted((self.ws.root / "docs").rglob("*.md")) if (self.ws.root / "docs").exists() else []:
            rel = str(p.relative_to(self.ws.root))
            try:
                head = p.read_text(errors="replace")[:1500]
            except OSError:
                continue
            hit = len(q & words(rel + " " + head))
            if hit >= 2:
                docs.append((hit, rel))
        docs.sort(reverse=True)
        lines += [f"- document {rel} (read_file it if useful)" for _, rel in docs[:2]]
        return ("## Your related past work — reuse what worked, avoid what was sent back\n" + "\n".join(lines)) if lines else ""

    def project_brief(self, m: Member, pid: str) -> str:
        """The project's own instructions and this person's role and instructions on it (project settings)."""
        p = self.cfg.projects.get(pid) if pid else None
        if not p:
            return ""
        pa = p.agents.get(m.id)
        parts = []
        if pa and pa.role:
            parts.append(f"On {p.name or pid} your role is: {pa.role}.")
        if p.instructions.strip():
            parts.append(f"Project instructions (everyone on {p.name or pid}):\n{p.instructions.strip()}")
        if pa and pa.instructions.strip():
            parts.append(f"Your instructions for {p.name or pid} (from {self.cfg.owner_name} — binding here):\n"
                         f"{pa.instructions.strip()}")
        return ("## This project's settings\n" + "\n\n".join(parts)) if parts else ""

    def context_for(self, m: Member) -> str:
        tz = self.cfg.timezone
        parts: list[str] = []
        if m.monitor:
            parts.append("## Board (all tasks)\n" + self.tasks.board(skip_owners={x.id for x in self.cfg.assistants}))
            parts.append("## People\n" + "\n".join(f"- {n}: {st} — {l}"
                                                   for n, st, l in team_status_lines(self.cfg, self.tasks)))
            pend = self.asks.pending()
            parts.append("## Pending permission requests\n" + ("\n".join(
                f"- {a.id} from {a.requester}: {a.summary}" for a in pend) or "- none"))
            od = self.ws.read("decisions/OPEN.md").strip()
            if od:
                parts.append("## Open decisions waiting on the owner (decisions/OPEN.md)\n" + od)
            if self.cfg.projects:
                parts.append("## Projects\n" + self.projects_text())
            parts.append("## Recent team repo commits\n" + (self.ws.git_log(15) or "- none"))
            for pid, p in self.cfg.projects.items():
                if p.repo:
                    from pathlib import Path
                    gl = self.ws.git_log(8, path=Path(p.repo))
                    if gl:
                        parts.append(f"## Recent commits — project {pid}\n{gl}")
        else:
            mine = self.tasks.for_owner(m.id, open_only=False)
            open_ = [t for t in mine if t.is_open]
            done = [t for t in mine if not t.is_open][-3:]
            parts.append("## Your open tasks\n" + ("\n".join(t.summary() for t in open_) or "- none"))
            if done:
                parts.append("## Your recently closed tasks\n" + "\n".join(t.line(tz) for t in done))
            waiting = [t for t in self.tasks.all() if t.is_open and t.status == "blocked"
                       and any(x.id == m.id for x in self.people_in(t.blocked_on))]
            if waiting:
                parts.append("## Others blocked on you\n" + "\n".join(t.line(tz) for t in waiting))
            mine_asks = [a for a in self.asks.all() if a.requester == m.id][-5:]
            if mine_asks:
                parts.append("## Your permission requests\n" + "\n".join(
                    f"- {a.id}: {a.summary} — {a.status}" for a in mine_asks))
            if self.cfg.projects:
                parts.append("## Projects\n" + self.projects_text())
            mine_p = [pid for pid in m.projects if pid in self.cfg.projects]
            if mine_p:
                parts.append("## Your projects and teammates\n" + "\n".join(
                    f"- {pid}: " + (", ".join(x.name for x in self.cfg.project_members(pid) if x.id != m.id) or "just you")
                    for pid in mine_p))
        if m.assistant:
            from .assistant import Reminders
            rems = Reminders(self).all(m.id)
            parts.append("## Reminders you've set\n" + ("\n".join(f"- {r['at'].replace('T', ' ')}: {r['text']}"
                                                                   for r in rems) or "- none"))
            pend, review = self.asks.pending(), [t for t in self.tasks.all() if t.status == "review"]
            parts.append("## What the company needs from the founder\n"
                         + "\n".join([f"- approval {a.id}: {a.summary}" for a in pend[:5]]
                                     + [f"- review {t.id}: {t.title}" for t in review[:5]] or ["- nothing"]))
        docs = self.list_docs()
        if docs:
            parts.append("## Team documents (read with read_file)\n" + docs)
        recent = self.ws.tail_log(m.id, 10)
        if recent:
            parts.append("## Your recent activity log\n" + recent)
        return "\n\n".join(parts)

    # -- what was said (shared across console, Telegram and Slack) ---------------------
    def room_of(self, m: Member, ev: Event) -> str:
        if ev.room:
            return ev.room
        if ev.project:
            return PROJECT_ROOM + ev.project
        return {"dm": m.id, "group": TEAM_ROOM}.get(ev.source, m.id)

    def _who(self, who: str) -> str:
        if who == self.owner_id:
            return f"{self.cfg.owner_name} (founder)"
        mm = self.cfg.member(who)
        if mm:
            return mm.name
        u = next((x for x in self.cfg.users if x.id == who), None)
        return f"{u.name} ({u.role}, a human colleague — not the founder)" if u else who

    def name_of(self, who: str) -> str:
        """A plain display name for anyone who can write in a room."""
        if who == self.owner_id:
            return self.cfg.owner_name
        mm = self.cfg.member(who)
        if mm:
            return mm.name
        u = next((x for x in self.cfg.users if x.id == who), None)
        return u.name if u else who

    def note_stuck(self, member_id: str, room: str) -> None:
        """The hub's watchdog stopped a turn that hung: remembered for System health and the audit log."""
        self._audit("system", member_id, "turn stopped (stuck)", room)
        self.ws.update_state(lambda s: s.setdefault("stuck", []).append(
            {"member": member_id, "room": room, "at": now(self.cfg.timezone).isoformat(timespec="seconds")}) or
            s.__setitem__("stuck", s["stuck"][-20:]))

    def _line(self, msg: dict) -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        t = datetime.fromtimestamp(msg.get("ts", 0), ZoneInfo(self.cfg.timezone)).strftime("%d %b %H:%M")
        text = " ".join(str(msg.get("text", "")).split())
        if len(text) > 400:
            text = text[:400] + "…"
        tag = {"ask": " [approval card]", "system": " [system]", "internal": " [teammates]"}.get(msg.get("kind"), "")
        if msg.get("files"):
            text += " [📎 " + ", ".join(f"{f.get('name')} ({f.get('path')})" for f in msg["files"]) + "]"
        if msg.get("reply_quote"):
            tag += f" (replying to “{str(msg['reply_quote'])[:80]}”)"
        return f"- {t} {self._who(msg.get('who', ''))}{tag}: {text}"

    def room_title(self, room: str) -> str:
        if room == TEAM_ROOM:
            return "All hands"
        if room == BACKCHANNEL:
            return "Team backchannel"
        if room.startswith(PROJECT_ROOM):
            p = self.cfg.projects.get(room[len(PROJECT_ROOM):])
            return f"#{p.name if p else room[len(PROJECT_ROOM):]} room"
        mm = self.cfg.member(room)
        return f"1:1 {self.cfg.owner_name} ↔ {mm.name if mm else room}"

    def conversation_context(self, m: Member, ev: Event) -> str:
        """The current room's recent messages (everyone's, from every channel) + a glance at the member's other rooms,
        so a project's people all know what was said there — like a real team channel."""
        room = self.room_of(m, ev)
        parts = []
        here = self.chat.recent(room, ROOM_CONTEXT + 1)
        if here and here[-1].get("who") == ev.sender and here[-1].get("text", "").strip() == ev.text.strip():
            here = here[:-1]                  # that's the message being answered right now
        if here:
            parts.append(f"## Recent messages in {self.room_title(room)} (oldest first)\n"
                         + "\n".join(self._line(x) for x in here[-ROOM_CONTEXT:]))
        if m.monitor:
            others = [TEAM_ROOM, BACKCHANNEL] + [PROJECT_ROOM + p for p in self.cfg.projects] + \
                     [x.id for x in self.cfg.workers if x.id != m.id]
        else:
            others = [m.id, TEAM_ROOM] + [PROJECT_ROOM + p for p in m.projects if p in self.cfg.projects]
        glance, budget = [], 5000
        for r in others:
            if r == room:
                continue
            msgs = self.chat.recent(r, ELSEWHERE, max_age_hours=ELSEWHERE_HOURS)
            if not msgs:
                continue
            block = f"### {self.room_title(r)}\n" + "\n".join(self._line(x) for x in msgs)
            if len(block) > budget:
                break
            budget -= len(block)
            glance.append(block)
        if glance:
            parts.append("## Elsewhere lately (other rooms you are in)\n" + "\n".join(glance))
        return "\n\n".join(parts)

    # -- main entry --------------------------------------------------------------
    async def dispatch(self, member_id: str, ev: Event) -> str:
        m = self.cfg.member(member_id)
        if not m:
            return f"Unknown team member `{member_id}`."
        cap = self.cfg.daily_tokens_per_agent
        why = "paused" if self.paused(m.id) else "over today's budget" if cap and self._usage_today(m.id) >= cap else ""
        pid = self.project_of(ev)
        if not why and self.project_paused(pid):
            p = self.cfg.projects[pid]
            self._enqueue(m, ev, f"{p.name or pid} was paused")
            return (f"⏸ {p.name or pid} is paused — this waits in the queue and is answered when you resume the "
                    f"project.")
        if why:
            # Paused means paused — owner messages, teammates' questions and system follow-ups all wait in the
            # queue and are handled on /resume (or when the budget resets). Nothing is lost, nothing runs.
            self._enqueue(m, ev, why)
            return ("⏸ I'm paused — your message is queued and I'll answer it when you /resume me."
                    if why == "paused" else
                    f"💸 I've used today's budget ({cap:,} tokens) — your message is queued; I'll answer when "
                    f"the budget resets or you raise it.")

        async with self._locks[m.id]:
            if cap and self._usage_today(m.id) >= cap:    # re-checked: others may have spent it while we waited
                self._enqueue(m, ev, "over today's budget")
                return f"💸 I've used today's budget ({cap:,} tokens) — your message is queued."
            room = self.room_of(m, ev)
            self.turns[m.id] = {"room": room, "since": time.time(), "source": ev.source}
            try:     # watchdog — counted from when they start, not while they wait for their previous turn
                return await asyncio.wait_for(self._turn(m, ev), WATCHDOG_SECONDS)
            except asyncio.TimeoutError:
                self.note_stuck(m.id, room)
                self.reset_sessions(m.id, room)            # the hung call may still hold that session: start fresh
                return (f"⚠️ I got stuck on this and stopped after {max(1, WATCHDOG_SECONDS // 60)} minute(s) — please send "
                        f"it again (if it keeps happening, check my model in Settings → AI models).")
            finally:
                self.turns.pop(m.id, None)

    async def _turn(self, m: Member, ev: Event) -> str:
        cap = self.cfg.daily_tokens_per_agent
        # Owner messages that mention a task are feedback on that task — captured in git, not lost in chat.
        is_question = ev.text.rstrip().endswith("?")
        if ev.sender == self.owner_id and ev.source in ("dm", "group") and not is_question \
                and ev.meta.get("primary", True):
            for tid in set(TASK_ID.findall(ev.text)):
                try:
                    self.tasks.add_feedback(tid, self.cfg.owner_name, ev.text)
                except TaskError:
                    pass
            self.ws.log(m.id, f"{self.cfg.owner_name} ({ev.source}): {ev.text[:300]}")
        elif ev.source == "inbox":
            self.ws.log(m.id, f"from {ev.sender}: {ev.text[:300]}")

        channel = {"dm": f"private chat with {self.cfg.owner_name}",
                   "group": "the team HQ group (keep it short; the whole team reads it)",
                   "inbox": f"internal message from teammate `{ev.sender}` (not visible to the owner)",
                   "system": "a system event"}.get(ev.source, ev.source)
        if ev.project:
            channel = self.project_channel(ev.project)
        if m.assistant and ev.source == "dm":
            channel = (f"private chat with {self.cfg.owner_name} — you are their personal assistant. This chat and "
                       f"your tasks are private to them: never share them with the team. Keep their day organised: "
                       f"personal tasks (create_task with owner = you), reminders (remind), and delegate company work "
                       f"to the team with create_task / message_agent when they ask.")
        context = self.context_for(m)
        brief = self.project_brief(m, ev.project)
        if brief:
            context = brief + "\n\n" + context
        past = self.experience(m, ev)
        if past:
            context += "\n\n" + past
        convo = self.conversation_context(m, ev)
        if convo:
            context += "\n\n" + convo
        gated = {a: self.cfg.permission(m, a, ev.project) for a in ("create_task", "update_task", "write_file", "message_agent",
                                                        "post_group", "post_room", "run_code")}
        system = build_system(
            company=self.cfg.company, today=today(self.cfg.timezone).isoformat(),
            charter=self.ws.charter(), persona=self.ws.persona(m.id), memory=self.ws.memory(m.id),
            member_name=m.name, member_role=m.role, roster=self.roster(), context=context,
            owner_name=self.cfg.owner_name, can_run_code=self.executor.enabled and not m.monitor,
            channel=channel, needs_approval=[a for a, lv in gated.items() if lv == "red"
                                             and (a != "run_code" or (self.executor.enabled and not m.monitor))],
            extra_actions=REMIND_SPEC if m.assistant else "", playbook=self.ws.playbook(m.id))
        try:
            llm = self.llm_for(m, ev.project)
        except LLMError as e:
            self._heartbeat(m.id, error=str(e)[:200], project=ev.project)
            return f"⚠️ My AI model isn't available right now: {e}"
        room = self.room_of(m, ev)
        hkey = f"{m.id}:{room}"
        legacy = ((f"{m.id}:dm",) if room == m.id else (f"{m.id}:group",) if room == TEAM_ROOM else
                  (f"{m.id}:project:{room[len(PROJECT_ROOM):]}",) if room.startswith(PROJECT_ROOM) else ())
        hist = self._load_history(hkey, legacy)
        who = self.cfg.owner_name if ev.sender == self.owner_id else self._who(ev.sender)
        where = f"project {ev.project}" if ev.project else ev.source
        quote = f", replying to “{ev.meta['reply_quote']}”" if ev.meta.get("reply_quote") else ""
        user_msg = f"[{where} from {who}{quote}] {ev.text}"
        files = ev.meta.get("files") or []
        attached, images = ("", [])
        if files:                                        # documents, PDFs and images attached to this message
            from .files import for_agent
            attached, images = for_agent(self.ws.root, files, bool(getattr(llm, "images", False)))
        messages = [*hist, {"role": "user", "content": user_msg + attached, **({"images": images} if images else {})}]
        if files:                                        # history keeps the names, not the whole text again
            user_msg += " [📎 " + ", ".join(f.get("name", "file") for f in files) + "]"

        last_error = ""
        deadline = time.monotonic() + TURN_SECONDS           # one turn never holds this person's lock for long

        sess = self.session_for(m, room, ev.project) if getattr(llm, "sessions", False) else None

        async def call(msgs):
            nonlocal last_error
            if time.monotonic() > deadline:
                last_error = f"the turn took longer than {TURN_SECONDS // 60} minutes"
                return None
            for attempt in range(len(RETRY_BACKOFF) + 1):
                try:
                    if sess is not None:
                        res = await asyncio.to_thread(llm.complete, system, msgs, sess)
                        self._save_session(m, sess, res, ev.project)   # resumed next time, even after a restart
                    else:
                        res = await asyncio.to_thread(llm.complete, system, msgs)
                    break
                except LLMError as e:
                    last_error = " ".join(str(e).split())[:160]
                    wait = RETRY_BACKOFF[attempt] if attempt < len(RETRY_BACKOFF) else None
                    if wait is not None and is_transient(str(e)) and time.monotonic() + wait < deadline:
                        log.warning("llm hiccup for %s (retry in %ss): %s", m.id, wait, last_error)
                        await asyncio.sleep(wait)                 # rate limit / overload / network: try again
                        continue
                    log.error("llm failed for %s: %s", m.id, e)
                    self._heartbeat(m.id, error=str(e)[:200], project=ev.project)
                    return None
            self._heartbeat(m.id, project=ev.project)
            billable = getattr(res, "billable_tokens", res.total_tokens)
            used = self._add_usage(m.id, billable, self.model_name(m, ev.project))
            if cap and used >= 0.8 * cap and used - billable < 0.8 * cap:
                await self.bus.send_owner(self.cfg.monitor.id, f"💸 {m.name} used 80% of today's token budget.")
            return res

        parsed, retried = None, False
        for _round in range(MAX_READ_ROUNDS + 2):
            res = await call(messages)
            if res is None:
                # Said in the first person: this message appears in the chat *as* this teammate.
                told = "I've flagged it" if m.monitor else f"{self.cfg.monitor.name} has been told"
                return (f"⚠️ I couldn't think right now (model error{': ' + last_error if last_error else ''}). "
                        f"{told} — please try again in a moment.")
            parsed = parse_reply(res.text)
            if (not parsed.ok or res.truncated) and not retried:
                retried = True                              # one retry: say exactly what went wrong
                why = ("Your reply was cut off at the length limit. Keep the JSON short and put long documents "
                       "AFTER it as <<<FILE docs/…md>>> … <<<END>>> blocks (split very long ones)."
                       if res.truncated else "That wasn't the JSON object.")
                messages = [*messages, {"role": "assistant", "content": res.text[:2000]},
                            {"role": "user", "content": f"{why} Reply again with ONLY the JSON object "
                                                        f'{{"reply": ..., "actions": [...]}}.'}]
                continue
            reads = [a for a in parsed.actions if a.get("type") == "read_file"]
            if not reads or _round >= MAX_READ_ROUNDS:
                parsed.actions = [a for a in parsed.actions if a.get("type") != "read_file"]
                break
            # Give the model the files it asked for, then let it answer for real.
            messages = [*messages, {"role": "assistant", "content": res.text},
                        {"role": "user", "content": self._read_files(reads)}]
        reply, actions = parsed.reply, parsed.actions

        notes, failed = await self._apply_all(m, actions, ev)
        if failed and not ev.meta.get("no_repair"):
            # One repair round: the agent sees exactly what failed and can fix it (or explain) — no false "done".
            fix_msgs = [*messages, {"role": "assistant", "content": json.dumps({"reply": reply,
                                                                                "actions": _compact(actions)})},
                        {"role": "user", "content": "Results of your actions:\n" + "\n".join(notes) +
                         "\n\nSome actions FAILED. Return corrected actions for the failed ones only (or none), and a "
                         "reply that tells the truth about what happened."}]
            res = await call(fix_msgs)
            if res is not None:
                fixed = parse_reply(res.text)
                if fixed.ok:
                    fixed_actions = [a for a in fixed.actions if a.get("type") != "read_file"]
                    more, _ = await self._apply_all(m, fixed_actions, ev)
                    notes += more
                    actions += fixed_actions
                    reply = fixed.reply or reply
        reply = self._fix_ids(reply, notes)
        reply = reply or ("(no reply)" if not actions else "")
        hist.append({"role": "user", "content": user_msg})
        hist.append({"role": "assistant", "content": json.dumps({"reply": reply, "actions": _compact(actions)},
                                                                ensure_ascii=False)
                     + (("\n[results] " + " | ".join(notes)) if notes else "")})
        self._save_history(hkey, hist)
        if ev.source == "inbox":
            self.ws.log(m.id, f"replied to {ev.sender}: {reply[:300]}")
        what = {"dm": f"replied to {who}", "group": f"replied to {who} in the group", "inbox": f"answered {who}",
                "system": "picked up an update"}.get(ev.source, f"{ev.source} from {who}")
        n = len(actions)
        await asyncio.to_thread(self.ws.commit, what + (f" · {n} change{'s' if n != 1 else ''}" if n else ""),
                                m.name)
        out = reply
        if notes:
            out = (out + "\n\n" if out else "") + "\n".join(notes)
        return out or "(no reply)"

    def _fix_ids(self, reply: str, notes: list[str]) -> str:
        """Models guess the id of a task they're creating ("Created T-002") before it exists. A task id in the
        reply that doesn't exist becomes the id that was really created (if it's clear which), else "the new task"."""
        real = {t.id for t in self.tasks.all()}
        bad = [x for x in dict.fromkeys(TASK_ID.findall(reply or "")) if x not in real]
        if not bad:
            return reply
        created = [x for n in notes for x in re.findall(r"(T-\d{3,}) created", n)]
        for i, x in enumerate(bad):
            reply = re.sub(rf"\b{x}\b", created[i] if len(created) == len(bad) else "the new task", reply)
        return reply

    async def _apply_all(self, m: Member, actions: list[dict], ev: Event) -> tuple[list[str], bool]:
        """Apply each action on its own: one bad action is reported and the rest still run."""
        notes, failed = [], False
        for raw in actions:
            try:
                a = normalize_action(raw)
                note = await self._apply(m, a, ev)
                if note:
                    notes.append(note)
            except (TaskError, AskError, ExecutorError, ValueError, KeyError, TypeError, AttributeError) as e:
                failed = True
                notes.append(f"⚠️ {raw.get('type')}: {e}")
            except Exception as e:  # noqa: BLE001 - never let one action take the turn down
                failed = True
                log.exception("action %s failed", raw.get("type"))
                notes.append(f"⚠️ {raw.get('type')}: unexpected error ({type(e).__name__})")
        return notes, failed

    # -- actions -----------------------------------------------------------------
    def describe(self, m: Member, a: dict) -> str:
        """One line the owner can approve or reject."""
        t = a.get("type", "")
        if t == "create_task":
            return f"create task “{a.get('title', '')}” for {a.get('owner') or m.id} ({a.get('priority', 'P1')})"
        if t == "update_task":
            what = ", ".join(f"{k}={a[k]}" for k in ("status", "priority", "due", "blocked_on") if a.get(k))
            return f"update {a.get('id')}" + (f": {what}" if what else "")
        if t == "write_file":
            return f"write {a.get('path')} ({len(str(a.get('content', '')))} chars)"
        if t == "message_agent":
            return f"message {a.get('to')}: {str(a.get('text', ''))[:200]}"
        if t == "post_group":
            return f"post in All hands: {str(a.get('text', ''))[:300]}"
        if t == "post_room":
            return f"post in #{a.get('project')}: {str(a.get('text', ''))[:300]}"
        if t == "run_code":
            return f"run the coding agent on {a.get('task')} in {a.get('project')}: {str(a.get('instructions', ''))[:200]}"
        if t == "remind":
            return f"remind {self.cfg.owner_name} at {a.get('at')}: {str(a.get('text', ''))[:200]}"
        if t == "learn":
            return f"learn: {str(a.get('lesson', ''))[:200]}"
        return t

    async def _apply(self, m: Member, a: dict, ev: Event) -> str:
        t = a["type"]
        level = ("green" if t in NO_GATE or ev.meta.get("approved")
                 else self.cfg.permission(m, t, self.action_project(a, ev)))
        if level == "red":
            ask = self.asks.create(requester=m.id, summary=f"{m.name} wants to {self.describe(m, a)}",
                                   details=str(a.get("why") or a.get("description") or ""), level="red",
                                   task=str(a.get("task") or a.get("id") or ""), kind="action",
                                   payload={"action": a, "project": ev.project, "room": ev.room})
            post = getattr(self.bus, "post", None)
            if post and ev.room and ev.room.startswith(PROJECT_ROOM):   # asked in a project room → the card is there
                await post(ev.room, m.id, ask.card(self.cfg.monitor.name), kind="ask", ask=ask)
            else:
                await self.bus.send_owner(m.id, ask.summary, ask=ask, urgent=True)
            return f"🙋 {ask.id}: waiting for {self.cfg.owner_name} to approve — {self.describe(m, a)}"
        note = await self._do(m, a, ev)
        self._audit("agent", m.id, t, str(a.get("id") or a.get("task") or a.get("path") or a.get("to") or ""),
                    self.describe(m, a) + (" (approved)" if ev.meta.get("approved") else ""))
        if level == "yellow":
            await self.bus.send_owner(m.id, f"🟡 FYI — {m.name} did: {self.describe(m, a)}")
        return note

    async def _internal(self, room: str, who: str, text: str) -> None:
        """Teammates talking to each other: visible to the owner (and to the project's people) in the room."""
        post = getattr(self.bus, "post_room", None)
        if post:
            try:
                await post(room, who, text, kind="internal")
            except Exception:  # noqa: BLE001
                log.exception("could not record internal message")

    def _handoff_room(self, ev: Event, task_id: str = "") -> str:
        pid = ev.project
        if not pid and task_id:
            try:
                pid = self.tasks.get(task_id).project
            except TaskError:
                pid = ""
        return PROJECT_ROOM + pid if pid and pid in self.cfg.projects else BACKCHANNEL

    async def _do(self, m: Member, a: dict, ev: Event) -> str:
        t = a["type"]
        if t == "learn":
            source = str(a.get("task") or ev.task or "")
            added = self.ws.learn(m.id, str(a.get("lesson") or ""), str(a.get("topic") or ""), source)
            return f"📚 learned: {a.get('lesson')}" if added else ""
        if t == "remind":
            if not m.assistant:
                raise ValueError("only the founder's personal assistant sets reminders — use notify_owner")
            from .assistant import Reminders
            r = Reminders(self).add(m.id, str(a.get("at") or ""), str(a.get("text") or ""))
            return f"⏰ reminder set for {r['at'].replace('T', ' ')[:16]}: {r['text']}"
        if t == "create_task":
            owner = (a.get("owner") or m.id).lower().lstrip("@")
            if owner != self.owner_id:
                om = self.cfg.member(owner) or next((x for x in self.cfg.team if x.name.lower() == owner or
                                                     x.name.lower().split()[0] == owner.split()[0]), None)
                if not om:
                    raise ValueError(f"unknown owner `{owner}` (team: {', '.join(x.id for x in self.cfg.team)})")
                owner = om.id                              # always the id: "Marcus Chen" → marcus
            if a.get("project") and a["project"] not in self.cfg.projects:
                raise ValueError(f"unknown project `{a['project']}` (projects: {', '.join(self.cfg.projects) or 'none'})")
            created_by = self.owner_id if (ev.sender == self.owner_id and ev.source in ("dm", "group")) else m.id
            task = self.tasks.create(title=a.get("title", "untitled"), owner=owner, created_by=created_by,
                                     priority=a.get("priority", "P1"), due=a.get("due"),
                                     project=a.get("project", "") or ev.project, goal=a.get("goal", ""),
                                     done_means=a.get("done_means") or [], description=a.get("description", ""))
            if owner not in (m.id, self.owner_id):
                await self._internal(self._handoff_room(ev, task.id), m.id,
                                     f"📝 assigned {task.id} to {self.cfg.member(owner).name}: {task.title}")
                self._spawn(self._deliver(owner, Event("inbox", f"New task assigned to you: {task.line(self.cfg.timezone)}",
                                                       sender=m.id, hop=ev.hop + 1, task=task.id,
                                                       project=task.project)))
            return f"📝 {task.id} created → {owner}"
        if t == "update_task":
            tid = a["id"]
            msgs = []
            task0 = self.tasks.get(tid)
            changes = [k for k in ("status", "priority", "due", "done_means", "output") if a.get(k)]
            if task0.owner != m.id and not m.monitor and changes:
                # Someone else's task: add a note or message them — only they (or the founder) change it.
                if a.get("log"):
                    self.tasks.add_log(tid, m.id, a["log"])
                raise TaskError(f"{task0.id} belongs to {self._who(task0.owner)} — you can add a log note or "
                                f"message them; you can't change its {', '.join(changes)}.")
            fields = {k: a.get(k) for k in ("priority", "due") if a.get(k)}
            if fields:
                self.tasks.update_fields(tid, m.id, **fields)
            if a.get("done_means"):
                self.tasks.set_done_means(tid, m.id, list(a["done_means"]))
            if a.get("log"):
                self.tasks.add_log(tid, m.id, a["log"])
            if a.get("output"):
                self.tasks.set_output(tid, m.id, a["output"])
            if a.get("status"):
                task, msg = self.tasks.set_status(tid, a["status"], by=m.id,
                                                  blocked_on=a.get("blocked_on", ""))
                msgs.append(f"🔄 {task.id} → {task.status}")
                if msg:
                    msgs.append(msg)
                if task.status == "review":
                    await self.bus.send_owner(m.id, f"👀 {task.id} is ready for your review: {task.title}\n"
                                                    f"Output: {a.get('output') or 'see task file'}\n"
                                                    f"Accept with /accept {task.id} or reply with feedback.")
                if task.status == "blocked":
                    await self.on_blocked(task, m.id, hop=ev.hop + 1)
                if task.status == "done":
                    await self.on_done(task, hop=ev.hop + 1)
            return " · ".join(msgs)
        if t == "ask_permission":
            summary = a.get("summary", "").strip()
            if not summary:
                raise ValueError("ask_permission needs a one-line summary")
            same = self.asks.find_pending(m.id, summary, a.get("task", ""))
            if same:
                return f"🙋 already waiting for {self.cfg.owner_name}: {same.id}"
            # An agent's request never approves itself: no auto-approve, and at least an hour to answer.
            default = "reject" if a.get("default") == "reject" else "wait"
            ask = self.asks.create(requester=m.id, summary=summary, details=a.get("details", ""),
                                   level=a.get("level", "red"), task=a.get("task", ""), default=default,
                                   hours=a.get("hours"), recommendation=a.get("recommendation", ""))
            await self._hold_task_for(ask)
            await self.bus.send_owner(m.id, ask.summary, ask=ask, urgent=ask.level == "red")
            return f"🙋 {ask.id} sent to {self.cfg.owner_name}"
        if t == "remember":
            note = a.get("note", "")
            if not note.strip():
                raise ValueError("remember needs a note")
            # Only the founder's own words can become a pinned, binding correction — never a teammate's message,
            # a document or a GitHub comment (that would let injected text make itself permanent).
            from_owner = ev.sender == self.owner_id and ev.source in ("dm", "group")
            flag = str(a.get("correction", "")).strip().lower()
            pinned = from_owner and (flag in ("true", "1", "yes") or (
                flag not in ("false", "0", "no") and
                bool(re.search(r"\b(don'?t|do not|never|always|instead|wrong|not like|stop|use)\b", ev.text, re.I))))
            self.ws.remember(m.id, note, pinned=pinned)
            return ""
        if t == "message_agent":
            to = (a.get("to") or "").lower()
            if to == self.owner_id:
                await self.bus.send_owner(m.id, a.get("text", ""))
                return ""
            target = self.cfg.member(to)
            if not target or target.id == m.id:
                raise ValueError(f"unknown teammate `{to}`")
            if target.assistant:
                raise ValueError(f"{target.name} is {self.cfg.owner_name}'s personal assistant, not reachable by "
                                 f"the team — message {self.cfg.owner_name} instead")
            self.ws.log(m.id, f"to {target.id}: {a.get('text', '')[:300]}")
            # the personal assistant's hand-offs are private: they go in the teammate's own 1:1, not the backchannel
            room = target.id if m.assistant else self._handoff_room(ev, a.get("task", ""))
            await self._internal(room, m.id, f"→ {target.name}: {a.get('text', '')}")
            origin = ev.meta.get("origin_room") or self.room_of(m, ev)
            self._spawn(self._deliver(target.id, Event("inbox", a.get("text", ""), sender=m.id, hop=ev.hop + 1,
                                                       task=a.get("task", ""),
                                                       project=room[len(PROJECT_ROOM):] if room != BACKCHANNEL else "",
                                                       room=room, meta={"reply_to": m.id, "origin_room": origin})))
            return f"✉️ sent to {target.name}"
        if t == "notify_owner":
            await self.bus.send_owner(m.id, a.get("text", ""))
            return ""
        if t == "post_group":
            await self.bus.post_group(m.id, a.get("text", ""))
            return ""
        if t == "post_room":
            pid = str(a.get("project") or ev.project or "")
            if pid not in self.cfg.projects:
                raise ValueError(f"no project `{pid}`")
            post = getattr(self.bus, "post_room", None)
            if post:
                await post(PROJECT_ROOM + pid, m.id, str(a.get("text", "")))
            return ""
        if t == "write_file":
            if not str(a.get("content", "")).strip():
                raise ValueError("write_file had no content — put the document in a <<<FILE path>>> … <<<END>>> "
                                 "block after the JSON (the existing file was left as it was)")
            content = str(a.get("content", ""))
            if len(content) > MAX_FILE_CHARS:
                raise ValueError(f"file too large (max {MAX_FILE_CHARS} chars)")
            p, rel = self._doc_path(str(a.get("path", "")))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content.rstrip("\n") + "\n")
            if a.get("task"):
                self.tasks.set_output(a["task"], m.id, rel)
            self.ws.log(m.id, f"wrote {rel}")
            return f"📄 saved {rel}"
        if t == "run_code":
            if m.monitor:
                raise ValueError(f"{m.name} coordinates the team and does not write code")
            tid, pid = a.get("task", ""), a.get("project", "")
            if self.project_paused(pid):
                raise ValueError(f"{pid} is paused — no coding runs until it's resumed")
            self.executor.project(pid)            # validate now, run in background
            self.tasks.get(tid)
            if tid in self._coding:
                raise ValueError(f"a coding run for {tid} is already going — wait for it")
            self._coding.add(tid)
            self._spawn(self._run_code(m, tid, pid, a.get("instructions", "")))
            return f"🛠 coding started on branch {self.executor.branch_for(tid)} ({pid})"
        raise ValueError(f"unknown action `{t}`")

    # -- the loops that make it a company --------------------------------------------
    def people_in(self, text: str) -> list[Member]:
        """Team members named in free text ("waiting on Marcus for the API URL")."""
        low = (text or "").lower()
        return [x for x in self.cfg.team
                if re.search(rf"\b({re.escape(x.id)}|{re.escape(x.name.lower())})\b", low)]

    async def on_blocked(self, task, by: str, hop: int = 0) -> None:
        """Whoever a task is blocked on hears about it at once — and their answer comes back (see _deliver).
        Once per blocker per task per day, and within the hop limit, so two agents can't ping-pong forever."""
        if self.owner_id in task.blocked_on.lower() or self.cfg.owner_name.lower() in task.blocked_on.lower():
            await self.bus.send_owner(task.owner if self.cfg.member(task.owner) else self.cfg.monitor.id,
                                      f"🚧 {task.id} is blocked on you: {task.blocked_on}")
        for x in self.people_in(task.blocked_on):
            if x.id == task.owner or x.id == self.owner_id:
                continue
            key, day = f"{task.id}:{x.id}", today(self.cfg.timezone).isoformat()
            if (self.ws.state().get("blocked_told") or {}).get(key) == day:
                continue
            self.ws.update_state(lambda s: s.setdefault("blocked_told", {}).__setitem__(key, day))
            owner = self._who(task.owner)
            self._spawn(self._deliver(x.id, Event(
                "inbox", f"{owner} is blocked on you for {task.id} ({task.title}): {task.blocked_on}. "
                         f"Unblock them now — give them what they need (your reply goes straight to them), or "
                         f"say when you will.", sender=task.owner if self.cfg.member(task.owner) else by,
                task=task.id, project=task.project, hop=hop,
                room=PROJECT_ROOM + task.project if task.project in self.cfg.projects else BACKCHANNEL,
                meta={"reply_to": task.owner if self.cfg.member(task.owner) else ""})))

    async def on_done(self, task, hop: int = 0) -> None:
        """A task is done: tasks that waited on it go back to todo and their owners are told."""
        for t in self.tasks.unblock_dependents(task.id, by=self.owner_id):
            if self.cfg.member(t.owner):
                self._spawn(self._deliver(t.owner, Event(
                    "inbox", f"{task.id} ({task.title}) is done, so {t.id} is unblocked and back in todo. "
                             f"Pick it up.", sender=self.cfg.monitor.id, task=t.id, project=t.project, hop=hop)))

    async def _hold_task_for(self, ask: Ask) -> None:
        """While a request waits, its task shows as blocked on the owner (and goes back when decided)."""
        tid = str(ask.doc.meta.get("task") or "")
        if not tid:
            return
        try:
            t = self.tasks.get(tid)
        except TaskError:
            return
        if not t.is_open or t.status == "blocked":
            return
        ask.doc.meta.setdefault("payload", {})["prev_status"] = t.status
        from .fileio import atomic_write as _aw
        _aw(ask.path, ask.doc.render())
        self.tasks.set_status(tid, "blocked", by=self.owner_id,
                              blocked_on=f"{self.cfg.owner_name} — approval {ask.id}", note=f"waiting on {ask.id}")

    def _release_task_for(self, ask: Ask) -> None:
        tid = str(ask.doc.meta.get("task") or "")
        if not tid:
            return
        try:
            t = self.tasks.get(tid)
        except TaskError:
            return
        if t.status == "blocked" and ask.id in t.blocked_on:
            prev = (ask.doc.meta.get("payload") or {}).get("prev_status") or "todo"
            prev = prev if prev in ("todo", "doing", "review") else "todo"
            try:                                          # back under the owner's own rules (WIP ≤ 2, Done means)
                self.tasks.set_status(tid, prev, by=t.owner, note=f"{ask.id} {ask.status}")
            except TaskError:
                self.tasks.set_status(tid, "todo", by=self.owner_id, note=f"{ask.id} {ask.status}")

    async def request_changes(self, task_id: str, text: str, by: str | None = None) -> str:
        """The owner sends reviewed work back: feedback saved (task + binding memory), task back to doing,
        the owner of the task is told now and reworks it first in the next work session."""
        text = (text or "").strip()
        if not text:
            raise ValueError("Say what needs to change.")
        t = self.tasks.add_feedback(task_id, self.cfg.owner_name, text)
        self.ws.remember(t.owner, f"Feedback on {t.id}: {text}", pinned=True)
        if t.status in ("review", "done", "todo", "blocked"):
            t, _ = self.tasks.set_status(t.id, "doing", by=self.owner_id, note="changes requested")
        self.tasks.mark_rework(t.id)
        await asyncio.to_thread(self.ws.commit, f"{t.id}: changes requested", self.cfg.owner_name)
        if self.cfg.member(t.owner):
            async def tell():
                reply = await self.dispatch(t.owner, Event(
                    "system", f"{self.cfg.owner_name} reviewed {t.id} ({t.title}) and wants changes: {text}\n"
                              f"Rework it now (write the corrected output, move it back to review when it meets "
                              f"the feedback), and record what you'll do differently next time with learn. Reply in "
                              f"one line: what you'll change.", sender="system", task=t.id,
                    project=t.project))
                await self.bus.send_owner(t.owner, reply)
            self._spawn(tell())
        return f"↩️ {t.id} back to {self._who(t.owner)} with your changes"

    async def _deliver(self, to: str, ev: Event) -> None:
        """Agent-to-agent message with a hop limit so agents can't loop forever."""
        if ev.hop > self.cfg.max_agent_hops:
            await self.bus.send_owner(self.cfg.monitor.id,
                                      f"🔁 Loop stopped: {ev.sender} → {to} exceeded {self.cfg.max_agent_hops} "
                                      f"exchanges{f' on {ev.task}' if ev.task else ''}. Last message: {ev.text[:300]}")
            return
        try:
            reply = await self.dispatch(to, ev)
            real = bool(reply) and not reply.startswith(("⏸", "💸", "⚠️")) and reply != "(no reply)"
            if real and ev.room:
                await self._internal(ev.room, to, f"↩ {self._who(ev.sender)}: {reply}")
            back = ev.meta.get("reply_to")
            if real and back and self.cfg.member(back):
                # Close the loop: the answer goes back to whoever asked (they may be mid-task waiting for it).
                self._spawn(self._deliver(back, Event(
                    "inbox", f"{self._who(to)} replied: {reply}", sender=to, hop=ev.hop + 1, task=ev.task,
                    project=ev.project, room=ev.room,
                    meta={"is_reply": True, "origin_room": ev.meta.get("origin_room", "")})))
            origin = ev.meta.get("origin_room")
            if real and ev.meta.get("is_reply") and origin and origin != ev.room and origin != BACKCHANNEL:
                # …and what they do with it is said where the conversation started (e.g. your 1:1).
                post = getattr(self.bus, "post_room", None)
                if post:
                    await post(origin, to, reply)
        except Exception as e:  # never let background work crash the runtime
            log.exception("deliver failed")
            self._heartbeat(to, error=str(e)[:200])

    async def _run_code(self, m: Member, task_id: str, project_id: str, instructions: str) -> None:
        try:
            await self._run_code_inner(m, task_id, project_id, instructions)
        finally:
            self._coding.discard(task_id)

    async def _run_code_inner(self, m: Member, task_id: str, project_id: str, instructions: str) -> None:
        task = self.tasks.get(task_id)
        prompt = (f"You are working on task {task.id}: {task.title}\n"
                  f"Goal: {task.doc.sections.get('Goal', '')}\n"
                  f"Done means:\n{task.doc.sections.get('Done means', '')}\n\n"
                  f"Instructions from {m.name}:\n{instructions}\n\n"
                  f"Make the change in this repository. Do not push. Keep the change focused.")
        try:
            r = await self.executor.run(project_id=project_id, task_id=task_id, prompt=prompt)
        except ExecutorError as e:
            self.tasks.add_log(task_id, m.id, f"coding run failed: {e}")
            await asyncio.to_thread(self.ws.commit, f"{task_id}: coding run failed", m.name)
            await self.bus.send_owner(m.id, f"⚠️ Coding run for {task_id} failed: {e}")
            return
        self.tasks.set_output(task_id, m.id, f"branch {r.branch} @ {r.commit} ({project_id})")
        if r.diffstat == "(no changes)":
            self.tasks.add_log(task_id, m.id, "coding run produced no changes")
            await asyncio.to_thread(self.ws.commit, f"{task_id}: coding run, no changes", m.name)
            await self.bus.send_owner(m.id, f"ℹ️ Coding run for {task_id} made no changes.\n{r.output_tail[-600:]}")
            return
        pr = ""
        if self.cfg.github.prs:
            try:
                pr = await asyncio.to_thread(self.executor.open_pr, project_id, task_id, r.branch,
                                             f"{task.title}\n\nDone means:\n{task.doc.sections.get('Done means', '')}\n\n"
                                             f"Coded by {m.name} (AI) for {task_id}.\n\n```\n{r.diffstat}\n```")
                self.tasks.set_output(task_id, m.id, f"pull request {pr}")
            except ExecutorError as e:
                self.tasks.add_log(task_id, m.id, f"no pull request: {e}")
        ask = self.asks.create(requester=m.id, summary=f"Merge {pr or r.branch} into {project_id} main ({task_id})",
                               details=f"{r.diffstat}\n\n" + (f"Pull request: {pr}" if pr else f"Worktree: {r.worktree}"),
                               level="red", task=task_id, kind="merge",
                               payload={"project": project_id, "branch": r.branch, "pr": pr})
        self.tasks.set_status(task_id, "review", by=m.id, note=f"code ready on {r.branch}")
        await asyncio.to_thread(self.ws.commit, f"{task_id}: code ready on {r.branch}", m.name)
        await self.bus.send_owner(m.id, ask.summary, ask=ask, urgent=True)

    # -- decisions ----------------------------------------------------------------
    async def decide_ask(self, ask_id: str, decision: str, by: str, note: str = "", via: str = "") -> str:
        """One decision per request, even when Approve is tapped twice or in two channels at once."""
        async with self._ask_locks[ask_id.upper()]:
            out = await self._decide_ask(ask_id, decision, by, note, via)
            try:
                summary = self.asks.get(ask_id).summary
            except Exception:  # noqa: BLE001
                summary = ""
            self._audit("decision", by, decision, ask_id.upper(), {"summary": summary, "note": note}, via=via)
            return out

    def _audit(self, kind: str, who: str, action: str, target: str = "", detail="", via: str = "") -> None:
        """Never lets a full disk or a bad value break the work being recorded."""
        try:
            self.audit.record(kind, who, action, target, detail, via)
        except Exception:  # noqa: BLE001
            log.exception("audit record failed")

    async def _decide_ask(self, ask_id: str, decision: str, by: str, note: str = "", via: str = "") -> str:
        pre = self.asks.get(ask_id)
        if pre.status != "pending":
            raise AskError(f"{pre.id} is already {pre.status}")
        merged = ""
        if pre.kind == "merge" and decision == "approved" and pre.status == "pending":
            # Merge first; only a merge that worked makes the request "approved". A failed merge is rolled back
            # and the request stays open.
            p = pre.doc.meta.get("payload") or {}
            try:
                if p.get("pr"):
                    sha = await asyncio.to_thread(self.executor.merge_pr, p["project"], p["pr"])
                else:
                    sha = await asyncio.to_thread(self.executor.merge, p["project"], p["branch"])
            except (ExecutorError, KeyError) as e:
                raise AskError(f"{pre.id} is still open — the merge didn't happen: {e}. Fix it and approve again, "
                               f"or reject.") from None
            merged = f" — merged {p['branch']} into {p['project']} ({sha})"
            if pre.doc.meta.get("task"):
                try:
                    done, _ = self.tasks.set_status(pre.doc.meta["task"], "done", by=self.owner_id,
                                                    note=f"merged {p['branch']} @ {sha}")
                    await self.on_done(done)               # whatever waited on it is unblocked
                except TaskError:
                    pass
        ask = self.asks.decide(ask_id, decision, by=by, note=note)
        result = f"{ask.id} {decision}{merged}"
        pr = (ask.doc.meta.get("payload") or {}).get("pr")
        if ask.kind == "merge" and decision == "rejected" and pr:
            try:
                await asyncio.to_thread(self.executor.close_pr, ask.doc.meta["payload"]["project"], pr,
                                        f"Rejected by {by}" + (f": {note}" if note else ""))
                result += " — pull request closed"
            except (ExecutorError, KeyError) as e:
                result += f" — couldn't close the PR: {e}"
        if not merged:
            self._release_task_for(ask)
        if ask.kind == "action" and decision == "approved":
            p = ask.doc.meta.get("payload") or {}
            m = self.cfg.member(ask.requester)
            if m and isinstance(p.get("action"), dict):
                try:
                    done = await self._do(m, p["action"], Event("system", f"approved {ask.id}", sender="system",
                                                                  project=p.get("project", ""), room=p.get("room", ""),
                                                                  meta={"approved": ask.id}))
                    result += f" — done{': ' + done if done else ''}"
                except (TaskError, AskError, ExecutorError, ValueError, KeyError) as e:
                    result += f" — but it failed: {e}"
        await asyncio.to_thread(self.ws.commit, f"{ask.id}: {decision} by {by}", self.cfg.owner_name)
        decided = getattr(self.bus, "ask_decided", None)
        if decided:
            await decided(ask, f"{result} by {by}", via)
        post = getattr(self.bus, "post_room", None)
        if post and self.cfg.member(ask.requester):      # every channel shows the card is settled
            icon = "✅" if decision == "approved" else "❌"
            await post(ask.requester, ask.requester, f"{icon} {result} by {by}" + (f" — {note}" if note else ""),
                       kind="system", via=via)
        if ask.requester and self.cfg.member(ask.requester):
            self._spawn(self._notify_requester(ask, decision, note))
        return result

    async def _notify_requester(self, ask: Ask, decision: str, note: str) -> None:
        reply = await self.dispatch(ask.requester, Event(
            "system", f"{ask.id} ('{ask.summary}') was {decision} by {self.cfg.owner_name}"
                      + (f" with note: {note}" if note else "") + ". Continue accordingly and tell the owner "
                      "in one line what you will do next.", sender="system", task=ask.doc.meta.get("task", "")))
        await self.bus.send_owner(ask.requester, reply)

    async def apply_ask_defaults(self) -> list[str]:
        out = []
        for a in self.asks.due_for_default():
            if a.default in ("approve", "reject") and a.level != "red":
                decision = "approved" if a.default == "approve" else "rejected"
                out.append(await self.decide_ask(a.id, decision, by="default (no reply)"))
        return out

    # -- owner commands (transport-independent) -----------------------------------
    def cmd_status(self) -> str:
        rows = team_status_lines(self.cfg, self.tasks)
        icon = {"on track": "🟢", "at risk": "🟠", "blocked": "🔴", "not started": "⚪"}
        return "\n".join(f"{icon.get(st, '•')} {n} — {st} — {line}" for n, st, line in rows)

    def cmd_assign(self, args: str) -> tuple[str, str | None]:
        """/assign <who> "<title>" [P0|P1|P2] [due:MM-DD|YYYY-MM-DD] [project:<id>]"""
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError('usage: /assign <who> "<title>" P1 due:10-03 project:<id>')
        who, rest = parts[0].lstrip("@").lower(), parts[1]
        pr = re.search(r"(?<!\S)P[0-2](?!\S)", rest, re.I)
        due = re.search(r"(?<!\S)due:(\S+)", rest)
        proj = re.search(r"(?<!\S)project:(\S+)", rest)
        title = rest
        for mm in (pr, due, proj):
            if mm:
                title = title.replace(mm.group(0), " ")
        title = " ".join(title.split()).strip().strip('"').strip()
        if not title:
            raise ValueError("task title is empty")
        member = self.cfg.member(who)
        if not member:
            raise ValueError(f"unknown member `{who}`")
        task = self.tasks.create(title=title, owner=member.id, created_by=self.owner_id,
                                 priority=pr.group(0).upper() if pr else "P1", due=due.group(1) if due else None,
                                 project=proj.group(1) if proj else (member.projects[0] if member.projects else ""))
        self.ws.commit(f"assign {task.id} to {member.id}", author=self.cfg.owner_name)
        return f"📝 {task.line(self.cfg.timezone)}", member.id

    async def accept(self, task_id: str, note: str = "accepted") -> str:
        task, _ = self.tasks.set_status(task_id, "done", by=self.owner_id, note=note or "accepted")
        await asyncio.to_thread(self.ws.commit, f"{task.id} accepted", self.cfg.owner_name)
        await self.on_done(task)
        m = self.cfg.member(task.owner)
        if m and self.cfg.reflect and not self.paused(m.id):
            async def reflect():                         # the job teaches: what made this good, for next time
                reply = await self.dispatch(m.id, Event(
                    "system", f"{self.cfg.owner_name} accepted {task.id} ({task.title})"
                              + (f": {note}" if note and note != "accepted" else "") + ". Look back at how you did it. If "
                              f"there's something reusable — what worked, what to do the same way next time — record "
                              f"1–2 lessons with learn. Nothing worth keeping? No actions. Reply in one line.",
                    sender="system", task=task.id, project=task.project, meta={"no_repair": True}))
                if "📚" in (reply or ""):                  # you see what they took from it
                    await self.bus.send_owner(m.id, reply)
            self._spawn(reflect())
        return f"✅ {task.id} done — {task.title}"

    async def cmd_accept(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if not parts:
            raise ValueError("usage: /accept T-001 [note]")
        return await self.accept(parts[0], parts[1] if len(parts) > 1 else "accepted")

    async def feedback(self, task_id: str, text: str) -> str:
        """Feedback never goes nowhere: on work in review it sends the task back (request changes); otherwise it
        is saved (task + binding memory) and the owner of the task hears it now."""
        text = (text or "").strip()
        if not text:
            raise ValueError("Write the feedback first.")
        t = self.tasks.get(task_id)
        if t.status == "review":
            return await self.request_changes(task_id, text)
        t = self.tasks.add_feedback(task_id, self.cfg.owner_name, text)
        self.ws.remember(t.owner, f"Feedback on {t.id}: {text}", pinned=True)
        await asyncio.to_thread(self.ws.commit, f"feedback on {t.id}", self.cfg.owner_name)
        if self.cfg.member(t.owner) and t.is_open:
            async def tell():
                reply = await self.dispatch(t.owner, Event(
                    "system", f"{self.cfg.owner_name}'s feedback on {t.id} ({t.title}): {text}\nTake it into "
                              f"account now. Reply in one line: what you'll do differently.", sender="system",
                    task=t.id, project=t.project))
                await self.bus.send_owner(t.owner, reply)
            self._spawn(tell())
        return f"🗒 feedback saved on {t.id}, in {self._who(t.owner)}'s memory, and sent to them"

    async def cmd_feedback(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("usage: /feedback T-001 <text>")
        return await self.feedback(parts[0], parts[1])

    async def cmd_changes(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("usage: /changes T-001 <what to change>")
        return await self.request_changes(parts[0], parts[1])

    def cmd_cut(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if not parts:
            raise ValueError("usage: /cut T-001 [reason]")
        task, _ = self.tasks.set_status(parts[0], "cut", by=self.owner_id, note=parts[1] if len(parts) > 1 else "")
        self.ws.commit(f"{task.id} cut", author=self.cfg.owner_name)
        return f"✂️ {task.id} cut"

    def cmd_asks(self) -> str:
        pend = self.asks.pending()
        return "\n\n".join(a.card(self.cfg.monitor.name) for a in pend) or "No pending asks."

    def cmd_budget(self) -> str:
        cap = self.cfg.daily_tokens_per_agent
        return "\n".join(f"{m.name}: {self._usage_today(m.id):,} / {cap:,} tokens today" for m in self.cfg.team)

    def cmd_log(self, who: str) -> str:
        m = self.cfg.member(who)
        if not m:
            raise ValueError(f"unknown member `{who}`")
        return self.ws.tail_log(m.id, 20) or f"No activity logged for {m.name} yet."

    def cmd_report(self) -> str:
        return build_report(self.cfg, self.ws, self.tasks, self.asks)

    # -- GitHub Project mirror ------------------------------------------------------------
    @property
    def github(self):
        if self._github is None:
            from .github import GitHubSync
            self._github = GitHubSync(self.cfg, self.tasks, self.ws)
        return self._github

    async def sync_github(self) -> dict:
        """Your edits on the GitHub board come in (through the rules), then every changed task goes out."""
        from .github import GitHubError
        if not self.cfg.github.enabled:
            return {"pulled": 0, "pushed": 0}
        if self._gh_lock.locked():                          # one sync at a time (scheduler + "Sync now")
            return {"pulled": 0, "pushed": 0, "busy": True}
        async with self._gh_lock:
            return await self._sync_github()

    async def _sync_github(self) -> dict:
        from .github import GitHubError
        try:
            changes = await asyncio.to_thread(self.github.pull)
            applied = []
            for c in changes:
                applied.append(await self._apply_github(c))
            pushed = await asyncio.to_thread(self.github.push)
        except GitHubError as e:
            self.ws.update_state(lambda s: s.setdefault("github", {}).__setitem__("error", str(e)[:300]))
            log.error("github sync failed: %s", e)
            return {"error": str(e)}
        self.ws.update_state(lambda s: s.setdefault("github", {}).update(
            error="", last_sync=now(self.cfg.timezone).isoformat(timespec="seconds")))
        if applied:
            await asyncio.to_thread(self.ws.commit, "changes from the GitHub board", self.cfg.owner_name)
        return {"pulled": len(applied), "pushed": pushed, "notes": [x for x in applied if x]}

    async def _apply_github(self, c) -> str:
        """One change you made on GitHub, applied exactly as if you had done it in the console."""
        from .github import STAGES
        gh = self.github
        try:
            t = self.tasks.get(c.task)
        except TaskError:
            return ""
        try:
            if c.field == "comment":
                return await self.feedback(t.id, c.value)
            if c.field == "stage":
                if c.value == t.status or (c.source == "status" and c.value == "doing"
                                           and t.status in ("doing", "blocked", "review")):
                    note = ""                              # "In Progress" covers doing/blocked/review
                elif c.value == "done":
                    note = await self.accept(t.id, "accepted on GitHub")
                elif c.value == "cut":
                    self.tasks.set_status(t.id, "cut", by=self.owner_id, note="cut on GitHub")
                    note = f"✂️ {t.id} cut"
                elif c.value == "doing" and t.status == "review":
                    note = await self.request_changes(t.id, "Moved back to Doing on GitHub — see the issue for what "
                                                            "to change, or ask me.")
                elif c.value == "blocked":
                    t2, _ = self.tasks.set_status(t.id, "blocked", by=self.owner_id,
                                                  blocked_on=t.blocked_on or f"{self.cfg.owner_name} — see GitHub")
                    await self.on_blocked(t2, self.owner_id)
                    note = f"🔄 {t.id} → blocked"
                else:
                    self.tasks.set_status(t.id, c.value, by=self.owner_id, note="moved on GitHub")
                    note = f"🔄 {t.id} → {c.value}"
                if c.source == "status":
                    gh.mark_pulled(t.id, "status", c.raw)       # Stage still shows the old value: the next push fixes it
                else:
                    gh.mark_pulled(t.id, "stage", STAGES.get(c.value, ""))
                return note
            if c.field == "owner":
                m = self.cfg.member(c.value.strip()) or next(
                    (x for x in self.cfg.team if x.name.lower() == c.value.strip().lower()), None)
                if not m:
                    raise ValueError(f"nobody called {c.value!r} on the team")
                self.tasks.update_fields(t.id, self.owner_id, owner=m.id)
            elif c.field == "project":
                if c.value and c.value not in self.cfg.projects:
                    raise ValueError(f"no project {c.value!r}")
                self.tasks.update_fields(t.id, self.owner_id, project=c.value)
            elif c.field == "blocker":
                if t.status == "blocked" and c.value:
                    t2, _ = self.tasks.set_status(t.id, "blocked", by=self.owner_id, blocked_on=c.value)
                    await self.on_blocked(t2, self.owner_id)
            elif c.field in ("priority", "due") and c.value:
                self.tasks.update_fields(t.id, self.owner_id, **{c.field: c.value})
            gh.mark_pulled(t.id, c.field, c.value)
            return f"{t.id} {c.field} → {c.value}"
        except (TaskError, ValueError) as e:
            shown = c.raw if c.source == "status" else (STAGES.get(c.value, c.value) if c.field == "stage" else c.value)
            gh.restore(t.id, "status" if c.source == "status" else c.field, shown)   # next push puts it back
            self.ws.log(self.cfg.monitor.id, f"GitHub change refused on {t.id} ({c.field}={c.value}): {e}")
            return f"⚠️ {t.id}: {e}"

    def backup_runtime(self, keep: int = 14) -> str:
        """.jm/ (chats, conversation memory, state) isn't in git: snapshot it daily (last `keep` days)."""
        import zipfile
        root = self.ws.root / ".jm"
        dest = root / "backups"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{today(self.cfg.timezone).isoformat()}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in root.rglob("*"):
                parts = f.relative_to(root).parts
                if f.is_file() and "backups" not in parts and "sandbox" not in parts and not f.name.endswith(".lock"):
                    z.write(f, f.relative_to(root))
        for old in sorted(dest.glob("*.zip"))[:-keep]:
            old.unlink()
        return str(path)

    async def run_reminders(self, at=None) -> int:
        """Send every reminder whose time has come (the scheduler calls this each tick)."""
        from .assistant import Reminders
        rem = Reminders(self)
        due = rem.due(at or now(self.cfg.timezone))
        sent = 0
        for r in due:
            who = r.get("member") if self.cfg.member(r.get("member", "")) else self.cfg.monitor.id
            try:
                await self.bus.send_owner(who, f"⏰ Reminder: {r['text']}", urgent=True)
            except Exception:  # noqa: BLE001 - kept: it's tried again on the next tick
                log.exception("reminder %s not delivered", r.get("id"))
                continue
            rem.delete(r["id"])                           # removed only once it has been sent
            sent += 1
        return sent

    async def run_brief(self) -> int:
        from .assistant import brief
        for m in self.cfg.assistants:
            if not self.paused(m.id):
                await self.bus.send_owner(m.id, brief(self, m))
        return len(self.cfg.assistants)

    async def run_daily_report(self) -> str:
        try:
            await asyncio.to_thread(self.backup_runtime)
        except Exception:  # noqa: BLE001 - a failed backup must not stop the report
            log.exception("runtime backup failed")
        rel, text = await asyncio.to_thread(write_report, self.cfg, self.ws, self.tasks, self.asks)
        await self.bus.post_group(self.cfg.monitor.id, text)
        head = text.split("## Headline", 1)[-1].strip().split("\n", 1)[0] if "## Headline" in text else ""
        await self.bus.send_owner(self.cfg.monitor.id, f"📊 Today's report is in All hands ({rel}).\n{head}")
        return rel

    async def run_checks(self) -> None:
        for line in await self.apply_ask_defaults():
            await self.bus.send_owner(self.cfg.monitor.id, f"⏱ {line}")
        await self.drain_queue()                      # e.g. yesterday's over-budget messages
        mgr = self.cfg.monitor
        for alert in checks(self.cfg, self.ws, self.tasks, self.asks):
            if alert.owner:
                await self.bus.send_owner(mgr.id, alert.text, urgent=alert.incident)
            if alert.incident:
                await self.bus.post_group(mgr.id, f"🚨 INCIDENT — {alert.text}")
            if alert.nudge and self.cfg.member(alert.nudge) and not self.paused(alert.nudge):
                # The manager chases it himself — the owner isn't the only one who hears about late work.
                self._spawn(self._deliver(alert.nudge, Event(
                    "inbox", f"{mgr.name} here: {alert.text}. What's the plan? Update the task (status, log, date) "
                             f"or tell me exactly what you need.", sender=mgr.id, task=alert.task,
                    meta={"reply_to": mgr.id})))

    def blocked_on_member(self, m: Member) -> list:
        return [t for t in self.tasks.all() if t.is_open and t.status == "blocked" and t.owner != m.id
                and not self.project_paused(t.project) and any(x.id == m.id for x in self.people_in(t.blocked_on))]

    def next_work(self, m: Member) -> tuple[str, object] | None:
        """What a person does first: unblock others → rework asked for by the owner → their top task."""
        waiting = self.blocked_on_member(m)
        if waiting:
            return "unblock", waiting[0]
        mine = [t for t in self.tasks.for_owner(m.id) if t.status in ("todo", "doing")
                and not self.project_paused(t.project)]          # paused projects wait
        rework = [t for t in mine if t.doc.meta.get("rework")]
        if rework:
            return "rework", rework[0]
        if mine:
            return "work", sorted(mine, key=lambda t: (t.priority, t.status != "doing", t.id))[0]
        return None

    async def run_work_session(self) -> str:
        """Each member with open work gets a work session — the team moves without being asked; then the manager
        does a round of the board. Returns a one-line-per-person digest (for the owner, sent silently).
        Coding runs started here keep going in the background: the session doesn't wait for them."""
        lines = []

        async def one(m: Member) -> None:
            if m.monitor or self.paused(m.id):
                return
            nxt = self.next_work(m)
            if not nxt:
                return
            kind, top = nxt
            if kind == "unblock":
                text = (f"Work session. {self._who(top.owner)} is blocked on you for {top.id} ({top.title}): "
                        f"{top.blocked_on}. Unblock them first: give them what they need with message_agent (or do "
                        f"your part and log it). Reply in one line: what you did.")
            else:
                fb = "\n".join(top.doc.sections.get("Feedback", "").strip().splitlines()[-3:])
                text = (f"Work session. {'Rework' if kind == 'rework' else 'Make real progress on'} {top.id} "
                        f"({top.title}) now" + (f" — the founder's latest feedback:\n{fb}\n" if kind == "rework"
                                               else ": ")
                        + "write the actual output with write_file (or run_code), update the task "
                          "(status/log/output). If you can't proceed, set it to blocked and name who you need and "
                          "what exactly. Reply in one line: what you did and what's next.")
            reply = await self.dispatch(m.id, Event("system", text, sender="system", task=top.id, project=top.project))
            lines.append(f"• {m.name} ({top.id}{', unblocking' if kind == 'unblock' else ''}"
                         f"{', rework' if kind == 'rework' else ''}): {reply.splitlines()[0][:200] if reply else '-'}")
        await asyncio.gather(*(one(m) for m in self.cfg.team))
        mgr = self.cfg.monitor
        if not self.paused(mgr.id) and any(t.is_open for t in self.tasks.all()):
            reply = await self.dispatch(mgr.id, Event(
                "system", "Manager's round after the work session. Look at the board and what people just did. "
                          "Chase what's stuck: message_agent the person (not the founder) with a specific ask; "
                          "fix obviously wrong priorities or dates on tasks you manage. Only notify_owner for "
                          "decisions only the founder can make. Reply in one line.", sender="system"))
            lines.append(f"• {mgr.name} (round): {reply.splitlines()[0][:200] if reply else '-'}")
        return "\n".join(sorted(lines))

    def onboarding_messages(self) -> list[tuple[str, str]]:
        """(member_id, text) — the day-one 'we are one team' intros. Deterministic, no model call."""
        jm = self.cfg.monitor
        roster = "\n".join(f"• {m.name} — {m.role}" for m in self.cfg.workers)
        msgs = [(jm.id, f"👋 Welcome to {self.cfg.company} HQ.\n\nWe are one team. {self.cfg.owner_name} is the founder; "
                        f"their word is final.\n\nThe team:\n{roster}\n\nHow we work:\n"
                        f"• This group is for things that concern everyone: announcements, @all status, big news.\n"
                        f"• Work instructions go in each person's 1:1 chat.\n"
                        f"• Every task lives in git with an owner, a priority and a date.\n"
                        f"• 🔴 money, public, production, deleting, legal → we ask {self.cfg.owner_name} first.\n"
                        f"• I post the daily report at {self.cfg.daily_report}.")]
        for m in self.cfg.workers:
            if m.id == jm.id:
                continue
            proj = f" I work on: {', '.join(m.projects)}." if m.projects else ""
            msgs.append((m.id, f"Hi team — {m.name}, {m.role}.{proj} DM me for work; I'll post here only for "
                               f"things everyone needs."))
        return msgs
