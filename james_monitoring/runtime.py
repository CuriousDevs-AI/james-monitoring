"""The team runtime: routes events to agents, calls the model, applies actions, enforces rules.

Transport-agnostic: Telegram (gateway.py) and the local CLI both talk to it through a `Bus`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Protocol

from .asks import Ask, AskError, AskStore
from .config import Config, Member
from .executor import Executor, ExecutorError
from .llm import LLM, LLMError
from .monitor import build_report, checks, team_status_lines, write_report
from .prompts import build_system
from .tasks import TASK_ID, TaskError, TaskStore
from .util import now, today
from .workspace import Workspace

log = logging.getLogger("jm.runtime")

MAX_READ_ROUNDS = 2
MAX_FILE_CHARS = 60_000
READABLE_DIRS = ("docs", "tasks", "reports", "team", "asks", "decisions")


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


def parse_model_output(text: str) -> tuple[str, list[dict]]:
    """Tolerant JSON extraction: models sometimes wrap JSON in prose or code fences."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    candidates = [t]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        candidates.append(t[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            reply = str(obj.get("reply") or "").strip()
            actions = obj.get("actions") or []
            if not isinstance(actions, list):
                actions = []
            return reply, [a for a in actions if isinstance(a, dict) and a.get("type")]
    return text.strip(), []


class Runtime:
    def __init__(self, cfg: Config, llm: LLM, bus: Bus | None = None, ws: Workspace | None = None):
        self.cfg = cfg
        self.llm = llm
        self.bus: Bus = bus or ConsoleBus()
        self.ws = ws or Workspace(cfg)
        self.ws.ensure()
        self.owner_id = cfg.owner_key
        self.tasks = TaskStore(self.ws.root, cfg.timezone, owner_id=self.owner_id)
        self.asks = AskStore(self.ws.root, cfg.timezone, cfg.ask_default_hours)
        self.executor = Executor(cfg)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=12))
        self._bg: set[asyncio.Task] = set()

    # -- state helpers ---------------------------------------------------------
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

    def _add_usage(self, member_id: str, tokens: int) -> int:
        day = today(self.cfg.timezone).isoformat()

        def fn(s):
            u = s.setdefault("usage", {})
            for k in [k for k in u if k != day]:   # keep only today
                del u[k]
            u.setdefault(day, {})
            u[day][member_id] = int(u[day].get(member_id, 0)) + tokens
        return int(self.ws.update_state(fn)["usage"][day][member_id])

    def _heartbeat(self, member_id: str, error: str = "") -> None:
        def fn(s):
            hb = s.setdefault("heartbeat", {})
            hb[member_id] = {"last": now(self.cfg.timezone).isoformat(timespec="minutes"), "error": error}
        self.ws.update_state(fn)

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def drain(self) -> None:
        """Wait for background work (agent-to-agent messages, coding runs). Used by CLI/tests."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # -- conversation history (survives restarts; .jm/ is not committed) ---------
    def _load_history(self, key: str) -> deque:
        if key not in self._history:
            d: deque = deque(maxlen=12)
            p = self.ws.root / ".jm" / "history" / f"{key.replace(':', '__')}.json"
            if p.exists():
                try:
                    d.extend(json.loads(p.read_text()))
                except json.JSONDecodeError:
                    pass
            self._history[key] = d
        return self._history[key]

    def _save_history(self, key: str, hist: deque) -> None:
        p = self.ws.root / ".jm" / "history" / f"{key.replace(':', '__')}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(list(hist)))

    # -- documents agents write and read (docs/ in the workspace) -----------------
    def _doc_path(self, rel: str):
        rel = (rel or "").strip().lstrip("/")
        if not rel.startswith("docs/"):
            rel = "docs/" + rel
        p = (self.ws.root / rel).resolve()
        docs = (self.ws.root / "docs").resolve()
        if docs not in p.parents:
            raise ValueError("files must stay inside docs/")
        return p, str(p.relative_to(self.ws.root))

    def _read_files(self, reads: list[dict]) -> str:
        root = self.ws.root.resolve()
        out = []
        for a in reads[:5]:
            rel = str(a.get("path", "")).strip().lstrip("/")
            p = (root / rel).resolve()
            ok = (root in p.parents and rel.split("/")[0] in READABLE_DIRS and ".jm" not in p.parts
                  and p.is_file())
            if ok:
                out.append(f"=== {rel} ===\n{p.read_text(errors='replace')[:MAX_FILE_CHARS]}")
            else:
                out.append(f"=== {rel} === (cannot read: only existing files under {', '.join(READABLE_DIRS)})")
        return "Files you asked for:\n\n" + "\n\n".join(out) + "\n\nNow give your final JSON answer."

    def list_docs(self, limit: int = 40) -> str:
        docs = self.ws.root / "docs"
        if not docs.exists():
            return ""
        files = sorted(p for p in docs.rglob("*") if p.is_file())
        return "\n".join(f"- {p.relative_to(self.ws.root)}" for p in files[-limit:])

    # -- context -----------------------------------------------------------------
    def roster(self) -> str:
        lines = [f"- {self.owner_id}: {self.cfg.owner_name} — founder (the owner)"]
        for m in self.cfg.team:
            proj = f" — projects: {', '.join(m.projects)}" if m.projects else ""
            lines.append(f"- {m.id}: {m.name} — {m.role}{proj}")
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

    def context_for(self, m: Member) -> str:
        tz = self.cfg.timezone
        parts: list[str] = []
        if m.monitor:
            parts.append("## Board (all tasks)\n" + self.tasks.board())
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
            waiting = [t for t in self.tasks.all() if t.is_open and m.id in t.blocked_on.lower()]
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
        docs = self.list_docs()
        if docs:
            parts.append("## Team documents (read with read_file)\n" + docs)
        recent = self.ws.tail_log(m.id, 10)
        if recent:
            parts.append("## Your recent activity log\n" + recent)
        return "\n\n".join(parts)

    # -- main entry --------------------------------------------------------------
    async def dispatch(self, member_id: str, ev: Event) -> str:
        m = self.cfg.member(member_id)
        if not m:
            return f"Unknown team member `{member_id}`."
        if self.paused(m.id) and ev.source != "system":
            return f"⏸ {m.name} is paused. Send /resume to continue."
        cap = self.cfg.daily_tokens_per_agent
        if cap and self._usage_today(m.id) >= cap:
            return f"💸 {m.name} hit today's budget ({cap:,} tokens). Raise budget.daily_tokens_per_agent or wait."

        async with self._locks[m.id]:
            # Owner messages that mention a task are feedback on that task — captured in git, not lost in chat.
            is_question = ev.text.rstrip().endswith("?")
            if ev.sender == self.owner_id and ev.source in ("dm", "group") and not is_question:
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
            system = build_system(
                company=self.cfg.company, today=today(self.cfg.timezone).isoformat(),
                charter=self.ws.charter(), persona=self.ws.persona(m.id), memory=self.ws.memory(m.id),
                member_name=m.name, member_role=m.role, roster=self.roster(), context=self.context_for(m),
                owner_name=self.cfg.owner_name, can_run_code=self.executor.enabled and not m.monitor,
                channel=channel)
            hkey = f"{m.id}:project:{ev.project}" if ev.project else f"{m.id}:{ev.source}"
            hist = self._load_history(hkey)
            who = self.cfg.owner_name if ev.sender == self.owner_id else ev.sender
            where = f"project {ev.project}" if ev.project else ev.source
            user_msg = f"[{where} from {who}] {ev.text}"
            messages = [*hist, {"role": "user", "content": user_msg}]
            reply, actions, raw = "", [], ""
            for _round in range(MAX_READ_ROUNDS + 1):
                try:
                    res = await asyncio.to_thread(self.llm.complete, system, messages)
                except LLMError as e:
                    log.exception("llm failed for %s", m.id)
                    self._heartbeat(m.id, error=str(e)[:200])
                    return f"⚠️ {m.name} couldn't think right now (model error). {self.cfg.monitor.name} has flagged it."
                self._heartbeat(m.id)
                used = self._add_usage(m.id, res.total_tokens)
                if cap and used >= 0.8 * cap and used - res.total_tokens < 0.8 * cap:
                    await self.bus.send_owner(self.cfg.monitor.id, f"💸 {m.name} used 80% of today's token budget.")
                raw = res.text
                reply, actions = parse_model_output(raw)
                reads = [a for a in actions if a.get("type") == "read_file"]
                if not reads or _round == MAX_READ_ROUNDS:
                    actions = [a for a in actions if a.get("type") != "read_file"]
                    break
                # Give the model the files it asked for, then let it answer for real.
                messages = [*messages, {"role": "assistant", "content": raw},
                            {"role": "user", "content": self._read_files(reads)}]
            hist.append({"role": "user", "content": user_msg})
            hist.append({"role": "assistant", "content": raw})
            self._save_history(hkey, hist)
            notes = []
            for a in actions:
                try:
                    note = await self._apply(m, a, ev)
                    if note:
                        notes.append(note)
                except (TaskError, AskError, ExecutorError, ValueError, KeyError) as e:
                    notes.append(f"⚠️ {a.get('type')}: {e}")
            if ev.source == "inbox":
                self.ws.log(m.id, f"replied to {ev.sender}: {reply[:300]}")
            what = {"dm": f"replied to {who}", "group": f"replied to {who} in the group", "inbox": f"answered {who}",
                    "system": "picked up an update"}.get(ev.source, f"{ev.source} from {who}")
            n = len(actions)
            self.ws.commit(what + (f" · {n} change{'s' if n != 1 else ''}" if n else ""),
                           author=m.name)
        out = reply
        if notes:
            out = (out + "\n\n" if out else "") + "\n".join(notes)
        return out or "(no reply)"

    # -- actions -----------------------------------------------------------------
    async def _apply(self, m: Member, a: dict, ev: Event) -> str:
        t = a["type"]
        if t == "create_task":
            owner = (a.get("owner") or m.id).lower()
            if owner != self.owner_id and not self.cfg.member(owner):
                raise ValueError(f"unknown owner `{owner}`")
            created_by = self.owner_id if (ev.sender == self.owner_id and ev.source in ("dm", "group")) else m.id
            task = self.tasks.create(title=a.get("title", "untitled"), owner=owner, created_by=created_by,
                                     priority=a.get("priority", "P1"), due=a.get("due"),
                                     project=a.get("project", "") or ev.project, goal=a.get("goal", ""),
                                     done_means=a.get("done_means") or [], description=a.get("description", ""))
            if owner not in (m.id, self.owner_id):
                self._spawn(self._deliver(owner, Event("inbox", f"New task assigned to you: {task.line(self.cfg.timezone)}",
                                                       sender=m.id, hop=ev.hop + 1, task=task.id)))
            return f"📝 {task.id} created → {owner}"
        if t == "update_task":
            tid = a["id"]
            msgs = []
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
                if task.status == "blocked" and self.owner_id in task.blocked_on.lower():
                    await self.bus.send_owner(m.id, f"🚧 {task.id} is blocked on you: {task.blocked_on}")
            return " · ".join(msgs)
        if t == "ask_permission":
            ask = self.asks.create(requester=m.id, summary=a.get("summary", ""), details=a.get("details", ""),
                                   level=a.get("level", "red"), task=a.get("task", ""),
                                   default=a.get("default", "wait"), hours=a.get("hours"),
                                   recommendation=a.get("recommendation", ""))
            await self.bus.send_owner(m.id, ask.summary, ask=ask, urgent=ask.level == "red")
            return f"🙋 {ask.id} sent to {self.cfg.owner_name}"
        if t == "remember":
            self.ws.remember(m.id, a.get("note", ""))
            return ""
        if t == "message_agent":
            to = (a.get("to") or "").lower()
            if to == self.owner_id:
                await self.bus.send_owner(m.id, a.get("text", ""))
                return ""
            target = self.cfg.member(to)
            if not target or target.id == m.id:
                raise ValueError(f"unknown teammate `{to}`")
            self.ws.log(m.id, f"to {target.id}: {a.get('text', '')[:300]}")
            self._spawn(self._deliver(target.id, Event("inbox", a.get("text", ""), sender=m.id, hop=ev.hop + 1,
                                                       task=a.get("task", ""))))
            return f"✉️ sent to {target.name}"
        if t == "notify_owner":
            await self.bus.send_owner(m.id, a.get("text", ""))
            return ""
        if t == "post_group":
            await self.bus.post_group(m.id, a.get("text", ""))
            return ""
        if t == "write_file":
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
            self.executor.project(pid)            # validate now, run in background
            self.tasks.get(tid)
            self._spawn(self._run_code(m, tid, pid, a.get("instructions", "")))
            return f"🛠 coding started on branch {self.executor.branch_for(tid)} ({pid})"
        raise ValueError(f"unknown action `{t}`")

    async def _deliver(self, to: str, ev: Event) -> None:
        """Agent-to-agent message with a hop limit so agents can't loop forever."""
        if ev.hop > self.cfg.max_agent_hops:
            await self.bus.send_owner(self.cfg.monitor.id,
                                      f"🔁 Loop stopped: {ev.sender} → {to} exceeded {self.cfg.max_agent_hops} "
                                      f"exchanges{f' on {ev.task}' if ev.task else ''}. Last message: {ev.text[:300]}")
            return
        try:
            await self.dispatch(to, ev)
        except Exception as e:  # never let background work crash the runtime
            log.exception("deliver failed")
            self._heartbeat(to, error=str(e)[:200])

    async def _run_code(self, m: Member, task_id: str, project_id: str, instructions: str) -> None:
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
            self.ws.commit(f"{task_id}: coding run failed", author=m.name)
            await self.bus.send_owner(m.id, f"⚠️ Coding run for {task_id} failed: {e}")
            return
        self.tasks.set_output(task_id, m.id, f"branch {r.branch} @ {r.commit} ({project_id})")
        if r.diffstat == "(no changes)":
            self.tasks.add_log(task_id, m.id, "coding run produced no changes")
            self.ws.commit(f"{task_id}: coding run, no changes", author=m.name)
            await self.bus.send_owner(m.id, f"ℹ️ Coding run for {task_id} made no changes.\n{r.output_tail[-600:]}")
            return
        ask = self.asks.create(requester=m.id, summary=f"Merge {r.branch} into {project_id} main ({task_id})",
                               details=f"{r.diffstat}\n\nWorktree: {r.worktree}", level="red", task=task_id,
                               kind="merge", payload={"project": project_id, "branch": r.branch})
        self.tasks.set_status(task_id, "review", by=m.id, note=f"code ready on {r.branch}")
        self.ws.commit(f"{task_id}: code ready on {r.branch}", author=m.name)
        await self.bus.send_owner(m.id, ask.summary, ask=ask, urgent=True)

    # -- decisions ----------------------------------------------------------------
    async def decide_ask(self, ask_id: str, decision: str, by: str, note: str = "") -> str:
        ask = self.asks.decide(ask_id, decision, by=by, note=note)
        result = f"{ask.id} {decision}"
        if ask.kind == "merge" and decision == "approved":
            p = ask.doc.meta.get("payload") or {}
            try:
                sha = self.executor.merge(p["project"], p["branch"])
                result += f" — merged {p['branch']} into {p['project']} ({sha})"
                if ask.doc.meta.get("task"):
                    self.tasks.set_status(ask.doc.meta["task"], "done", by=self.owner_id,
                                          note=f"merged {p['branch']} @ {sha}")
            except ExecutorError as e:
                result += f" — but merge failed: {e}"
        self.ws.commit(f"{ask.id}: {decision} by {by}", author=self.cfg.owner_name)
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

    def cmd_accept(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if not parts:
            raise ValueError("usage: /accept T-001 [note]")
        task, _ = self.tasks.set_status(parts[0], "done", by=self.owner_id, note=parts[1] if len(parts) > 1 else "accepted")
        self.ws.commit(f"{task.id} accepted", author=self.cfg.owner_name)
        return f"✅ {task.id} done — {task.title}"

    def cmd_feedback(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            raise ValueError("usage: /feedback T-001 <text>")
        task = self.tasks.add_feedback(parts[0], self.cfg.owner_name, parts[1])
        self.ws.remember(task.owner, f"Feedback on {task.id}: {parts[1]}")
        self.ws.commit(f"feedback on {task.id}", author=self.cfg.owner_name)
        return f"🗒 feedback saved on {task.id} and in {task.owner}'s memory"

    def cmd_cut(self, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
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

    async def run_daily_report(self) -> str:
        rel, text = write_report(self.cfg, self.ws, self.tasks, self.asks)
        await self.bus.post_group(self.cfg.monitor.id, text)
        return rel

    async def run_checks(self) -> None:
        for line in await self.apply_ask_defaults():
            await self.bus.send_owner(self.cfg.monitor.id, f"⏱ {line}")
        for alert in checks(self.cfg, self.ws, self.tasks, self.asks):
            await self.bus.send_owner(self.cfg.monitor.id, alert.text, urgent=alert.incident)
            if alert.incident:
                await self.bus.post_group(self.cfg.monitor.id, f"🚨 INCIDENT — {alert.text}")

    async def run_work_session(self) -> str:
        """Each member with open work gets a work session — the team moves without being asked.
        Returns a one-line-per-person digest (for the owner, sent silently)."""
        lines = []

        async def one(m: Member) -> None:
            if m.monitor or self.paused(m.id):
                return
            ready = [t for t in self.tasks.for_owner(m.id) if t.status in ("todo", "doing")]
            if not ready:
                return
            top = sorted(ready, key=lambda t: (t.priority, t.status != "doing", t.id))[0]
            reply = await self.dispatch(m.id, Event(
                "system", f"Work session. Make real progress on {top.id} ({top.title}) now: write the actual "
                          f"output with write_file (or run_code), update the task (status/log/output). If you "
                          f"can't proceed, set it to blocked and name who you need. Reply in one line: what you "
                          f"did and what's next.", sender="system", task=top.id))
            lines.append(f"• {m.name} ({top.id}): {reply.splitlines()[0][:200] if reply else '-'}")
        await asyncio.gather(*(one(m) for m in self.cfg.team))
        await self.drain()
        return "\n".join(sorted(lines))

    def onboarding_messages(self) -> list[tuple[str, str]]:
        """(member_id, text) — the day-one 'we are one team' intros. Deterministic, no model call."""
        jm = self.cfg.monitor
        roster = "\n".join(f"• {m.name} — {m.role}" for m in self.cfg.team)
        msgs = [(jm.id, f"👋 Welcome to {self.cfg.company} HQ.\n\nWe are one team. {self.cfg.owner_name} is the founder; "
                        f"his word is final.\n\nThe team:\n{roster}\n\nHow we work:\n"
                        f"• This group is for things that concern everyone: announcements, @all status, big news.\n"
                        f"• Work instructions go in each person's 1:1 chat.\n"
                        f"• Every task lives in git with an owner, a priority and a date.\n"
                        f"• 🔴 money, public, production, deleting, legal → we ask {self.cfg.owner_name} first.\n"
                        f"• I post the daily report at {self.cfg.daily_report}.")]
        for m in self.cfg.team:
            if m.id == jm.id:
                continue
            proj = f" I work on: {', '.join(m.projects)}." if m.projects else ""
            msgs.append((m.id, f"Hi team — {m.name}, {m.role}.{proj} DM me for work; I'll post here only for "
                               f"things everyone needs."))
        return msgs
