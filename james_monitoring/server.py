"""The company console: one process that runs everything.

    jm run   →  web console (setup, dashboard, chat, board, projects, team, approvals, reports, settings)
               + the team runtime + Telegram (if bots are connected) + the scheduler

The asyncio loop (runtime, Telegram, scheduler) lives in a background thread; the HTTP server hands work to it.
Localhost by default; every API call needs the key from the URL that `jm run` prints.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import yaml

from .asks import AskError
from .chat import TEAM_ROOM, ChatStore, MultiBus, WebBus
from .commands import ack_assignment, run_command
from .config import ConfigError, load_config
from .llm import LLMError, LLMResult, make_llm
from .monitor import open_decisions, team_status_lines
from .router import group_targets, is_status_request
from .runtime import Event, Runtime
from .scheduler import Scheduler
from .setup import detect_timezone, scaffold, slug
from .tasks import PRIORITIES, TaskError
from .ui import TeamAdmin
from .util import normalize_tz, today

log = logging.getLogger("jm.server")


class _BrokenLLM:
    """Used when the model can't be created (e.g. `claude` not installed) so the console still works."""
    name = "unavailable"

    def __init__(self, err: str):
        self.err = err

    def complete(self, system, messages) -> LLMResult:
        raise LLMError(self.err)


class App:
    def __init__(self, base_dir: Path, telegram: bool = True):
        self.base = Path(base_dir).resolve()
        self.cfg_path = self.base / "config.yaml"
        self.telegram_enabled = telegram
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="jm-loop")
        self.thread.start()
        self.rt: Runtime | None = None
        self.gw = None
        self.sched: Scheduler | None = None
        self.chat: ChatStore | None = None
        self.admin: TeamAdmin | None = None
        self.llm_error = ""
        self.telegram_error = ""
        self.pending: dict[str, int] = {}          # room → agents still thinking
        self._lock = threading.RLock()

    # -- lifecycle ----------------------------------------------------------------------
    def submit(self, coro, timeout: float | None = 600):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout) if timeout is not None else fut

    @property
    def ready(self) -> bool:
        return self.rt is not None

    def load(self) -> None:
        with self._lock:
            if self.rt:
                self.submit(self._stop_services())
            self.rt = None
            if not self.cfg_path.exists():
                return
            cfg = load_config(self.cfg_path)
            try:
                llm = make_llm(cfg.llm)
                self.llm_error = ""
            except LLMError as e:
                llm, self.llm_error = _BrokenLLM(str(e)), str(e)
            self.chat = ChatStore(cfg.workspace_path)
            web = WebBus(self.chat)
            self.gw, self.telegram_error = None, ""
            if self.telegram_enabled and any(m.bot_token for m in cfg.team) and cfg.monitor.bot_token:
                from .gateway import TelegramGateway
                try:
                    gw = TelegramGateway(cfg)
                    gw.build()
                    gw.chat = self.chat
                    self.gw = gw
                except Exception as e:  # noqa: BLE001
                    self.telegram_error = str(e)
            rt = Runtime(cfg, llm, bus=MultiBus(web, self.gw))
            self.admin = TeamAdmin(self.cfg_path)
            self.rt = rt
            self.submit(self._start_services())

    async def _start_services(self) -> None:
        if self.gw:
            try:
                await self.gw.start(self.rt)
            except Exception as e:  # noqa: BLE001 - the console must keep working without Telegram
                self.telegram_error = str(e)
                log.exception("telegram failed to start")
                self.gw = None
                self.rt.bus = MultiBus(WebBus(self.chat))

        async def digest(text):
            await self.rt.bus.send_owner(self.rt.cfg.monitor.id, "🛠 Work session:\n" + text)
        self.sched = Scheduler(self.rt, on_digest=digest)
        self.sched.start()

    async def _stop_services(self) -> None:
        if self.sched:
            await self.sched.stop()
        if self.gw:
            await self.gw.shutdown()
        if self.rt:
            await self.rt.drain()

    def raw(self) -> dict:
        return yaml.safe_load(self.cfg_path.read_text()) or {}

    def save_raw(self, raw: dict) -> None:
        self.cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))

    # -- setup ----------------------------------------------------------------------------
    def setup(self, b: dict) -> dict:
        if self.cfg_path.exists():
            raise ValueError("This folder already has a team. Use Settings to change it.")
        company = str(b.get("company", "")).strip()
        owner = str(b.get("owner", "")).strip()
        if not company or not owner:
            raise ValueError("Company and your name are required.")
        provider = str(b.get("provider") or "claude-code")
        key_env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "")
        mgr_name = str(b.get("manager_name") or "James").strip()
        mid = slug(mgr_name)
        raw = {
            "company": company, "timezone": normalize_tz(str(b.get("timezone") or detect_timezone())),
            "owner": {"name": owner, "telegram_user_id": 0},
            "llm": {"provider": provider, "model": str(b.get("model") or ""), "base_url": str(b.get("base_url") or ""),
                    "api_key_env": key_env, "max_tokens": 2000},
            "workspace": {"path": str(b.get("workspace") or "./team-workspace"), "push": bool(b.get("push"))},
            "telegram": {"group_chat_id": 0, "quiet_hours": ["22:00", "08:00"]},
            "monitor": {"daily_report": "18:30", "check_every_minutes": 60, "work_sessions": [],
                        "stale_days": 7, "blocked_escalate_days": 2, "ask_default_hours": 24},
            "budget": {"daily_tokens_per_agent": 300000},
            "executor": {"command": [], "timeout_minutes": 30},
            "limits": {"max_agent_hops": 4},
            "projects": {},
            "team": [{"id": mid, "name": mgr_name, "role": str(b.get("manager_role") or
                                                                  "Delivery manager and chief of staff"),
                      "monitor": True, "bot_token_env": f"TG_TOKEN_{mid.upper()}"}],
        }
        goals = [g.strip() for g in str(b.get("goals") or "").split("\n") if g.strip()]
        scaffold(raw, goals=goals, api_key=str(b.get("api_key") or ""), base_dir=self.base,
                 clone_url=str(b.get("clone_url") or "").strip())
        self.load()
        return {"ok": True}

    # -- read views ----------------------------------------------------------------------
    def state(self) -> dict:
        if not self.ready:
            return {"setup_needed": True, "timezone": detect_timezone(), "folder": str(self.base)}
        cfg = self.rt.cfg
        s = self.rt.ws.state()
        return {
            "setup_needed": False, "company": cfg.company, "owner": cfg.owner_name, "owner_key": cfg.owner_key,
            "timezone": cfg.timezone, "manager": cfg.monitor.id, "model": f"{cfg.llm.provider}"
            f"{('/' + cfg.llm.model) if cfg.llm.model else ''}", "llm_error": self.llm_error,
            "telegram": {"connected": bool(self.gw), "error": self.telegram_error,
                         "owner_id": cfg.owner_user_id, "group_id": cfg.group_chat_id,
                         "manager_token": bool(cfg.monitor.bot_token)},
            "paused_all": bool(s.get("paused_all")), "paused": s.get("paused", []),
            "members": [{"id": m.id, "name": m.name, "role": m.role, "manager": m.monitor,
                         "projects": m.projects, "paused": m.id in s.get("paused", []),
                         "telegram": bool(m.bot_token)} for m in cfg.team],
            "projects": [{"id": p.id, "name": p.name or p.id, "description": p.description, "lead": p.lead,
                          "status": p.status, "repo": p.repo} for p in cfg.projects.values()],
            "priorities": PRIORITIES, "workspace": str(cfg.workspace_path),
        }

    def dashboard(self) -> dict:
        rt, cfg = self.rt, self.rt.cfg
        tz = cfg.timezone
        tasks = rt.tasks.all()
        open_ = [t for t in tasks if t.is_open]
        rows = team_status_lines(cfg, rt.tasks)
        counts = {k: sum(1 for r in rows if r[1] == k) for k in ["on track", "at risk", "blocked", "not started"]}
        usage = rt.ws.state().get("usage", {}).get(today(tz).isoformat(), {})
        hb = rt.ws.state().get("heartbeat", {})
        return {
            "counts": counts,
            "tasks": {"open": len(open_), "done": sum(1 for t in tasks if t.status == "done"),
                      "review": sum(1 for t in tasks if t.status == "review"),
                      "blocked": sum(1 for t in tasks if t.status == "blocked"),
                      "overdue": sum(1 for t in open_ if t.overdue(tz))},
            "people": [{"name": n, "status": st, "line": line,
                        "id": next((m.id for m in cfg.team if m.name == n), ""),
                        "tokens": usage.get(next((m.id for m in cfg.team if m.name == n), ""), 0),
                        "error": (hb.get(next((m.id for m in cfg.team if m.name == n), "")) or {}).get("error", "")}
                       for n, st, line in rows],
            "asks": [self._ask_json(a) for a in rt.asks.pending()],
            "decisions": open_decisions(rt.ws),
            "review": [self._task_json(t) for t in tasks if t.status == "review"],
            "attention": [self._task_json(t) for t in open_ if t.status == "blocked" or t.overdue(tz)],
            "critical": [self._task_json(t) for t in sorted(open_, key=lambda t: str(t.doc.meta.get("due") or "9999"))
                         if t.priority == "P0"][:5],
            "commits": rt.ws.git_log(12).splitlines(),
            "budget": cfg.daily_tokens_per_agent, "push_error": rt.ws.state().get("push_error", ""),
        }

    def _task_json(self, t) -> dict:
        m = t.doc.meta
        return {"id": t.id, "title": t.title, "owner": t.owner, "priority": t.priority, "status": t.status,
                "due": m.get("due") or "", "project": t.project, "blocked_on": t.blocked_on,
                "overdue": t.overdue(self.rt.cfg.timezone), "updated": m.get("updated", ""),
                "done_on": m.get("done_on", "")}

    def _ask_json(self, a) -> dict:
        m = a.doc.meta
        return {"id": a.id, "from": a.requester, "summary": a.summary, "level": a.level, "status": a.status,
                "task": m.get("task", ""), "deadline": m.get("deadline", ""), "kind": a.kind,
                "recommendation": m.get("recommendation", ""), "details": a.doc.sections.get("Details", ""),
                "created": m.get("created", "")}

    def task_detail(self, tid: str) -> dict:
        t = self.rt.tasks.get(tid)
        return {**self._task_json(t), "sections": t.doc.sections, "meta": {k: str(v) for k, v in t.doc.meta.items()}}

    def member_detail(self, mid: str) -> dict:
        m = self.rt.cfg.member(mid)
        if not m:
            raise ValueError(f"no member {mid}")
        ws = self.rt.ws
        skill = ws.root / "team" / m.id / "skill"
        return {"id": m.id, "name": m.name, "role": m.role, "projects": m.projects, "manager": m.monitor,
                "persona": ws.read(f"team/{m.id}/persona.md"), "memory": ws.read(f"team/{m.id}/memory.md"),
                "log": ws.tail_log(m.id, 40), "status": self.rt.tasks.person_status(m.id),
                "skill_files": sorted(str(p.relative_to(skill)) for p in skill.rglob("*") if p.is_file())
                if skill.exists() else [],
                "tasks": [self._task_json(t) for t in self.rt.tasks.for_owner(m.id, open_only=False)]}

    # -- chat ------------------------------------------------------------------------------
    def chat_since(self, room: str, after: int) -> dict:
        return {"messages": self.chat.since(room, after), "thinking": self.pending.get(room, 0)}

    def chat_send(self, room: str, text: str) -> dict:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        cfg = self.rt.cfg
        if room != TEAM_ROOM and not cfg.member(room):
            raise ValueError(f"no such room {room}")
        owner = self.rt.owner_id
        self.chat.append(room, owner, text)
        if text.startswith("/"):
            self.submit(self._command(room, text), timeout=None)
        elif room == TEAM_ROOM:
            self.submit(self._team_message(text), timeout=None)
        else:
            self.submit(self._dm(room, text), timeout=None)
        return {"ok": True}

    async def _dm(self, mid: str, text: str) -> None:
        self.pending[mid] = self.pending.get(mid, 0) + 1
        try:
            reply = await self.rt.dispatch(mid, Event("dm", text, sender=self.rt.owner_id))
            self.chat.append(mid, mid, reply)
        except Exception as e:  # noqa: BLE001
            self.chat.append(mid, mid, f"⚠️ {e}", kind="notice")
        finally:
            self.pending[mid] -= 1

    async def _team_message(self, text: str) -> None:
        cfg = self.rt.cfg
        is_all, targets = group_targets(text, cfg, self.gw.usernames if self.gw else None)
        if is_all and is_status_request(text):
            for m in cfg.team:
                st, line = self.rt.tasks.person_status(m.id)
                self.chat.append(TEAM_ROOM, m.id, f"{st} — {line}")
            return

        async def one(mid):
            self.pending[TEAM_ROOM] = self.pending.get(TEAM_ROOM, 0) + 1
            try:
                reply = await self.rt.dispatch(mid, Event("group", text, sender=self.rt.owner_id))
                self.chat.append(TEAM_ROOM, mid, reply)
            except Exception as e:  # noqa: BLE001
                self.chat.append(TEAM_ROOM, mid, f"⚠️ {e}", kind="notice")
            finally:
                self.pending[TEAM_ROOM] -= 1
        await asyncio.gather(*(one(t) for t in targets))

    async def _command(self, room: str, text: str) -> None:
        cmd, _, args = text[1:].partition(" ")
        speaker = room if room != TEAM_ROOM else self.rt.cfg.monitor.id
        try:
            out = await run_command(self.rt, cmd.lower().split("@")[0], args.strip(), speaker,
                                    private=room != TEAM_ROOM)
        except (ValueError, TaskError, AskError) as e:
            out = f"⚠️ {e}"
        self.chat.append(room, speaker, out, kind="system")

    # -- tasks -----------------------------------------------------------------------------
    def task_create(self, b: dict) -> dict:
        rt = self.rt
        owner = str(b.get("owner", "")).lower()
        if not rt.cfg.member(owner):
            raise ValueError("Pick who owns this task.")
        dm = [x.strip() for x in (b.get("done_means") or []) if str(x).strip()]
        t = rt.tasks.create(title=str(b.get("title", "")).strip() or "untitled", owner=owner,
                            created_by=rt.owner_id, priority=str(b.get("priority") or "P1"),
                            due=str(b.get("due") or "") or None, project=str(b.get("project") or ""),
                            done_means=dm, description=str(b.get("description") or ""))
        rt.ws.commit(f"assign {t.id} to {owner}", author=rt.cfg.owner_name)
        if b.get("notify", True):
            self.submit(ack_assignment(rt, owner, t.line(rt.cfg.timezone)), timeout=None)
        return self._task_json(t)

    def task_action(self, b: dict) -> dict:
        rt, tid, act = self.rt, str(b.get("id", "")), str(b.get("action", ""))
        by = rt.owner_id
        if act == "status":
            t, msg = rt.tasks.set_status(tid, str(b.get("status")), by=by, note=str(b.get("note") or ""),
                                         blocked_on=str(b.get("blocked_on") or ""))
        elif act == "accept":
            t, msg = rt.tasks.set_status(tid, "done", by=by, note=str(b.get("note") or "accepted"))
        elif act == "cut":
            t, msg = rt.tasks.set_status(tid, "cut", by=by, note=str(b.get("note") or ""))
        elif act == "feedback":
            text = str(b.get("text") or "").strip()
            if not text:
                raise ValueError("Write the feedback first.")
            t, msg = rt.tasks.add_feedback(tid, rt.cfg.owner_name, text), ""
            rt.ws.remember(t.owner, f"Feedback on {t.id}: {text}")
        elif act == "edit":
            t, msg = rt.tasks.update_fields(tid, by, **{k: b.get(k) for k in ("priority", "due", "title", "owner",
                                                                          "project") if b.get(k)}), ""
        else:
            raise ValueError(f"unknown action {act}")
        rt.ws.commit(f"{tid}: {act} by {rt.cfg.owner_name}", author=rt.cfg.owner_name)
        return {**self._task_json(t), "message": msg}

    def decide(self, ask_id: str, decision: str, note: str = "") -> dict:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        return {"result": self.submit(self.rt.decide_ask(ask_id, decision, by=self.rt.cfg.owner_name, note=note))}

    # -- projects, members, decisions, settings --------------------------------------------
    def project_save(self, b: dict) -> dict:
        pid = slug(str(b.get("id") or b.get("name") or ""))
        if not pid:
            raise ValueError("Project name is required.")
        raw = self.raw()
        p = raw.setdefault("projects", {}).setdefault(pid, {"repo": "", "main_branch": "main"})
        for k in ("name", "description", "lead", "repo", "status"):
            if k in b:
                p[k] = str(b[k]).strip()
        if p.get("repo") and not (Path(p["repo"]).expanduser() / ".git").exists():
            raise ValueError(f"{p['repo']} is not a git repo (leave it blank if there is no code yet).")
        lead = p.get("lead")
        if lead:
            for m in raw.get("team", []):
                if str(m.get("id")) == lead and pid not in (m.get("projects") or []):
                    m.setdefault("projects", []).append(pid)
        self.save_raw(raw)
        self.rt.ws.write(f"projects/{pid}.md", f"# {p.get('name') or pid}\n\nStatus: {p.get('status', 'active')}\n"
                                               f"Lead: {lead or '-'}\n\n{p.get('description', '')}\n")
        self.rt.ws.commit(f"project {pid} saved", author=self.rt.cfg.owner_name)
        self.load()
        return {"id": pid}

    def member_update(self, b: dict) -> dict:
        mid = str(b.get("id", ""))
        raw = self.raw()
        for m in raw.get("team", []):
            if str(m.get("id")) == mid:
                if b.get("role"):
                    m["role"] = str(b["role"]).strip()
                if "projects" in b:
                    m["projects"] = [p.strip() for p in b["projects"] if p.strip()]
                break
        else:
            raise ValueError(f"no member {mid}")
        self.save_raw(raw)
        if isinstance(b.get("persona"), str) and b["persona"].strip():
            self.rt.ws.write(f"team/{mid}/persona.md", b["persona"].rstrip() + "\n")
        self.rt.ws.commit(f"{mid}: profile updated", author=self.rt.cfg.owner_name)
        self.load()
        return {"id": mid}

    def decisions_save(self, text: str) -> dict:
        self.rt.ws.write("decisions/OPEN.md", text.rstrip() + "\n")
        self.rt.ws.commit("decisions updated", author=self.rt.cfg.owner_name)
        return {"open": open_decisions(self.rt.ws)}

    def settings(self) -> dict:
        raw = self.raw()
        return {"company": raw.get("company"), "timezone": raw.get("timezone"), "owner": raw.get("owner", {}),
                "llm": {k: v for k, v in (raw.get("llm") or {}).items()}, "monitor": raw.get("monitor", {}),
                "budget": raw.get("budget", {}), "telegram": raw.get("telegram", {}),
                "workspace": raw.get("workspace", {}), "executor": raw.get("executor", {})}

    def settings_save(self, b: dict) -> dict:
        raw = self.raw()
        if b.get("company"):
            raw["company"] = str(b["company"]).strip()
        if b.get("timezone"):
            raw["timezone"] = normalize_tz(str(b["timezone"]))
        if b.get("owner_name"):
            raw.setdefault("owner", {})["name"] = str(b["owner_name"]).strip()
        mon = raw.setdefault("monitor", {})
        if b.get("daily_report"):
            mon["daily_report"] = str(b["daily_report"])
        if "work_sessions" in b:
            mon["work_sessions"] = [x.strip() for x in b["work_sessions"] if x.strip()]
        if b.get("budget"):
            raw.setdefault("budget", {})["daily_tokens_per_agent"] = int(b["budget"])
        llm = raw.setdefault("llm", {})
        if b.get("provider"):
            llm["provider"] = str(b["provider"])
            llm["api_key_env"] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(llm["provider"], "")
        if "model" in b:
            llm["model"] = str(b["model"] or "")
        if "base_url" in b:
            llm["base_url"] = str(b["base_url"] or "")
        if b.get("api_key") and llm.get("api_key_env"):
            from .setup import _append_env
            _append_env(self.base / ".env", {llm["api_key_env"]: str(b["api_key"])})
        if "coding_tool" in b:
            raw.setdefault("executor", {})["command"] = {
                "claude": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"],
                "codex": ["codex", "exec", "--full-auto", "{prompt}"]}.get(b["coding_tool"], [])
        tg = raw.setdefault("telegram", {})
        for k in ("group_chat_id",):
            if k in b and str(b[k]).strip().lstrip("-").isdigit():
                tg[k] = int(b[k])
        if "owner_telegram_id" in b and str(b["owner_telegram_id"]).strip().isdigit():
            raw.setdefault("owner", {})["telegram_user_id"] = int(b["owner_telegram_id"])
        self.save_raw(raw)
        self.load()
        return {"ok": True}

    # -- telegram connection from the console ----------------------------------------------
    def telegram_manager(self, token: str) -> dict:
        from .setup import _append_env
        check = self.admin.check_token(token)
        if not check.get("ok"):
            return check
        mgr = self.rt.cfg.monitor
        _append_env(self.base / ".env", {mgr.bot_token_env: token})
        os.environ[mgr.bot_token_env] = token
        return check

    def telegram_detect(self, what: str) -> dict:
        if self.gw:
            raise ValueError("Telegram is already running. Stop and restart to re-detect.")
        cfg = self.rt.cfg
        token = cfg.monitor.bot_token
        if not token:
            raise ValueError("Connect the manager's bot first.")
        from .telegram_setup import TelegramProbe

        async def go():
            async with TelegramProbe(token, cfg.telegram_api_base) as p:
                if what == "owner":
                    return await p.wait_for_owner(90)
                return await p.wait_for_group(cfg.owner_user_id, 90)
        found = asyncio.run(go())
        if not found:
            return {"found": False}
        raw = self.raw()
        if what == "owner":
            raw.setdefault("owner", {})["telegram_user_id"] = found.id
        else:
            raw.setdefault("telegram", {})["group_chat_id"] = found.id
        self.save_raw(raw)
        return {"found": True, "id": found.id, "name": getattr(found, "name", "") or getattr(found, "title", "")}


# -- HTTP -----------------------------------------------------------------------------------
def make_handler(app: App, key: str):
    page = resources.files("james_monitoring").joinpath("templates", "console.html").read_text()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body: bytes, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def _ok(self):
            return secrets.compare_digest(self.headers.get("X-JM-Key", ""), key)

        def _q(self) -> dict:
            from urllib.parse import parse_qs, urlparse
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if not self._ok():
                return self._json(403, {"error": "forbidden"})
            q = self._q()
            try:
                if path == "/api/state":
                    return self._json(200, app.state())
                if not app.ready:
                    return self._json(409, {"error": "setup needed"})
                routes = {
                    "/api/dashboard": lambda: app.dashboard(),
                    "/api/tasks": lambda: [app._task_json(t) for t in app.rt.tasks.all()],
                    "/api/task": lambda: app.task_detail(q.get("id", "")),
                    "/api/asks": lambda: [app._ask_json(a) for a in reversed(app.rt.asks.all())][:50],
                    "/api/chat": lambda: app.chat_since(q.get("room", TEAM_ROOM), int(q.get("after", -1))),
                    "/api/member": lambda: app.member_detail(q.get("id", "")),
                    "/api/team": lambda: app.admin.state(),
                    "/api/decisions": lambda: {"text": app.rt.ws.read("decisions/OPEN.md")},
                    "/api/reports": lambda: sorted((p.name for p in (app.rt.ws.root / "reports").glob("*.md")),
                                                   reverse=True),
                    "/api/report": lambda: {"text": app.rt.ws.read(f"reports/{Path(q.get('name', '')).name}")},
                    "/api/settings": lambda: app.settings(),
                    "/api/budget": lambda: app.rt.cmd_budget(),
                }
                fn = routes.get(path)
                if not fn:
                    return self._json(404, {"error": "not found"})
                return self._json(200, fn())
            except (ValueError, TaskError, AskError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s", path)
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            if not self._ok():
                return self._json(403, {"error": "forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 30_000_000:
                return self._json(413, {"error": "too large"})
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/setup":
                    return self._json(200, app.setup(b))
                if not app.ready:
                    return self._json(409, {"error": "setup needed"})
                a = app.admin
                routes = {
                    "/api/chat": lambda: app.chat_send(str(b.get("room", TEAM_ROOM)), str(b.get("text", ""))),
                    "/api/tasks": lambda: app.task_create(b),
                    "/api/task": lambda: app.task_action(b),
                    "/api/ask": lambda: app.decide(str(b.get("id", "")), str(b.get("decision", "")),
                                                   str(b.get("note", ""))),
                    "/api/projects": lambda: app.project_save(b),
                    "/api/member": lambda: app.member_update(b),
                    "/api/decisions": lambda: app.decisions_save(str(b.get("text", ""))),
                    "/api/settings": lambda: app.settings_save(b),
                    "/api/upload": lambda: a.upload(str(b.get("filename", "SKILL.md")),
                                                    base64.b64decode(b.get("data", ""))),
                    "/api/token": lambda: a.check_token(str(b.get("token", "")).strip()),
                    "/api/links": lambda: a.check_links(str(b.get("token", "")).strip(), b.get("name", ""),
                                                        b.get("role", "")),
                    "/api/member_status": lambda: a.member_status(str(b.get("id", ""))),
                    "/api/add": lambda: (a.add(name=str(b.get("name", "")), role=str(b.get("role", "")),
                                               token=str(b.get("token", "")),
                                               projects=list(b.get("projects") or []),
                                               upload_id=str(b.get("upload_id", ""))), app.load())[0],
                    "/api/remove": lambda: (a.remove(str(b.get("id", ""))), app.load())[0],
                    "/api/report/run": lambda: {"file": app.submit(app.rt.run_daily_report())},
                    "/api/work": lambda: {"digest": app.submit(app.rt.run_work_session(), timeout=1800)},
                    "/api/pause": lambda: {"message": app.rt.set_paused(str(b.get("who", "all")), bool(b.get("pause")))},
                    "/api/telegram/manager": lambda: app.telegram_manager(str(b.get("token", "")).strip()),
                    "/api/telegram/detect": lambda: app.telegram_detect(str(b.get("what", "owner"))),
                    "/api/restart": lambda: (app.load(), {"ok": True})[1],
                }
                fn = routes.get(path)
                if not fn:
                    return self._json(404, {"error": "not found"})
                return self._json(200, fn())
            except (ValueError, TaskError, AskError, ConfigError, FileNotFoundError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("POST %s", path)
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
    return H


def serve(base_dir: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          telegram: bool = True, key: str | None = None) -> None:
    app = App(base_dir, telegram=telegram)
    app.load()
    key = key or os.environ.get("JM_CONSOLE_KEY") or secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer((host, port), make_handler(app, key))
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{shown}:{srv.server_address[1]}/?k={key}"
    status = "setup needed — finish it in the browser" if not app.ready else (
        f"{app.rt.cfg.company}: {len(app.rt.cfg.team)} people"
        + (", Telegram connected" if app.gw else ", Telegram not connected (chat in the console)"))
    print(f"james-monitoring console: {url}\n{status}\nCtrl-C to stop.")
    if host == "0.0.0.0":
        print("⚠️  Listening on all interfaces. Anyone with the link can control the team — prefer an SSH tunnel.")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if app.ready:
            try:
                app.submit(app._stop_services(), timeout=30)
            except Exception:  # noqa: BLE001
                pass
