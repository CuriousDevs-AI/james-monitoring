"""The company console: one process that runs everything.

    jm run   →  web console (setup, dashboard, chat, board, projects, team, approvals, reports, settings)
               + the team runtime + Telegram (if bots are connected) + the scheduler

The asyncio loop (runtime, Telegram, scheduler) lives in a background thread; the HTTP server hands work to it.
Localhost by default; every API call needs the key from the URL that `jm run` prints.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import re
import secrets
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import yaml

from .asks import AskError
from .chat import BACKCHANNEL, PROJECT_ROOM, TEAM_ROOM
from .commands import ack_assignment
from .config import ACTION_TYPES, LEVELS, ConfigError, load_config
from .fileio import path_lock, read_config, set_env, write_config
from .hub import Hub
from .llm import LLMError, LLMResult, make_llm
from .monitor import open_decisions, team_status_lines
from .runtime import Runtime
from .scheduler import Scheduler
from .setup import detect_git_identity, detect_timezone, scaffold, slug
from .tasks import PRIORITIES, TaskError
from .ui import TeamAdmin
from .util import normalize_tz, today

log = logging.getLogger("jm.server")

PROVIDERS = ("claude-code", "codex-cli", "opencode", "anthropic", "openai", "fake")


class _BrokenLLM:
    """Used when the model can't be created (e.g. `claude` not installed) so the console still works."""
    name = "unavailable"

    def __init__(self, err: str):
        self.err = err

    def complete(self, system, messages) -> LLMResult:
        raise LLMError(self.err)


def config_txn(fn):
    """Hold the config lock for the whole request: read → change → validate → write → reload is one step."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with path_lock(self.cfg_path):
            return fn(self, *a, **kw)
    return wrapper


class App:
    def __init__(self, base_dir: Path, telegram: bool = True):
        self.base = Path(base_dir).resolve()
        self.cfg_path = self.base / "config.yaml"
        self.telegram_enabled = telegram
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="jm-loop")
        self.thread.start()
        self.rt: Runtime | None = None
        self.hub: Hub | None = None
        self.gw = None
        self.slack = None
        self.sched: Scheduler | None = None
        self.admin: TeamAdmin | None = None
        self.llm_error = ""
        self.telegram_error = ""
        self.slack_error = ""
        self._lock = threading.RLock()

    @property
    def chat(self):
        return self.hub.chat if self.hub else None

    @property
    def pending(self) -> dict[str, int]:
        return self.hub.pending if self.hub else {}

    # -- lifecycle ----------------------------------------------------------------------
    def submit(self, coro, timeout: float | None = 600):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout) if timeout is not None else fut

    @property
    def ready(self) -> bool:
        return self.rt is not None

    def load(self) -> None:
        """(Re)build the team from config.yaml.

        The new runtime is built completely first and swapped in in one step, so requests never see a half-loaded
        app. The old runtime's work in flight (an agent mid-reply, a coding run) keeps running: it shares the
        per-person locks — one person never handles two messages at once — and its messages are forwarded to the
        new hub. Nothing waits for it, so saving Settings is instant even during a 30-minute coding run."""
        with self._lock:
            if not self.cfg_path.exists():
                if self.rt:
                    self.submit(self._stop_services(self.sched, self.gw, self.slack), timeout=60)
                self.rt = None
                return
            cfg = load_config(self.cfg_path)
            try:
                llm, llm_error = make_llm(cfg.llm), ""
            except LLMError as e:
                llm, llm_error = _BrokenLLM(str(e)), str(e)
            gw, tg_error, slack = None, "", None
            if self.telegram_enabled and any(m.bot_token for m in cfg.team) and cfg.monitor.bot_token:
                from .gateway import TelegramGateway
                try:
                    gw = TelegramGateway(cfg)
                    gw.build()
                except Exception as e:  # noqa: BLE001
                    gw, tg_error = None, str(e)
            if self.telegram_enabled and cfg.slack.configured:
                from .slack import SlackTransport
                slack = SlackTransport(cfg)
            hub = Hub()
            rt = Runtime(cfg, llm, bus=hub, shared=self.rt)
            hub.attach(rt)
            admin = TeamAdmin(self.cfg_path)

            old_hub, old = self.hub, (self.sched, self.gw, self.slack)
            if self.rt:                          # stop polling/schedule before the new ones start (no 409s)
                self.submit(self._stop_services(*old), timeout=60)
            if old_hub:
                old_hub.successor = hub
            (self.hub, self.rt, self.gw, self.slack, self.admin, self.llm_error, self.telegram_error,
             self.slack_error, self.sched) = (hub, rt, gw, slack, admin, llm_error, tg_error, "", None)
            self.submit(self._start_services(), timeout=120)

    async def _start_services(self) -> None:
        rt, hub = self.rt, self.hub
        if self.gw:
            try:
                await self.gw.start(rt, hub)
            except Exception as e:  # noqa: BLE001 - the console must keep working without Telegram
                self.telegram_error = str(e)
                log.exception("telegram failed to start")
                hub.remove("telegram")
                self.gw = None
        if self.slack:
            try:
                await self.slack.start(rt, hub)
            except Exception as e:  # noqa: BLE001 - …or without Slack
                self.slack_error = str(e)[:300]
                log.exception("slack failed to start")
                hub.remove("slack")
                self.slack = None

        async def digest(text):
            await rt.bus.send_owner(rt.cfg.monitor.id, "🛠 Work session:\n" + text)
        self.sched = Scheduler(rt, on_digest=digest)
        self.sched.start()

    async def _stop_services(self, sched=None, gw=None, slack=None, drain: bool = False) -> None:
        for what, stop in (("scheduler", sched and sched.stop), ("telegram", gw and gw.shutdown),
                           ("slack", slack and slack.shutdown)):
            if stop:
                try:
                    await asyncio.wait_for(stop(), 30)
                except Exception:  # noqa: BLE001 - a stuck channel must not block the reload
                    log.exception("stopping %s failed", what)
        if drain and self.rt:
            try:
                await asyncio.wait_for(self.rt.drain(), 30)
            except asyncio.TimeoutError:
                pass

    def raw(self) -> dict:
        return read_config(self.cfg_path)

    def save_raw(self, raw: dict) -> None:
        write_config(self.cfg_path, raw)          # validated, backed up (.bak), atomic

    # -- setup ----------------------------------------------------------------------------
    @config_txn
    def setup(self, b: dict) -> dict:
        if self.cfg_path.exists():
            raise ValueError("This folder already has a team. Use Settings to change it.")
        company = str(b.get("company", "")).strip()
        owner = str(b.get("owner", "")).strip()
        if not company or not owner:
            raise ValueError("Company and your name are required.")
        provider = str(b.get("provider") or "claude-code")
        from .config import KEY_ENV
        key_env = KEY_ENV.get(provider, "")
        mgr_name = str(b.get("manager_name") or "James").strip()
        mid = slug(mgr_name)
        if mid == re.sub(r"[^a-z0-9]+", "_", owner.lower()).strip("_"):
            raise ValueError("The manager can't have your name — pick another (e.g. James).")
        raw = {
            "company": company, "timezone": normalize_tz(str(b.get("timezone") or detect_timezone())),
            "owner": {"name": owner, "telegram_user_id": 0,
                      "git_name": str(b.get("git_name") or "").strip() or detect_git_identity()[0],
                      "git_email": str(b.get("git_email") or "").strip() or detect_git_identity()[1]},
            "llm": {"provider": provider, "model": str(b.get("model") or ""), "base_url": str(b.get("base_url") or ""),
                    "api_key_env": key_env, "max_tokens": 8000, **({"allow_free": True} if b.get("allow_free") else {})},
            "workspace": {"path": str(b.get("workspace") or "./team-workspace"), "push": bool(b.get("push"))},
            "telegram": {"group_chat_id": 0, "quiet_hours": ["22:00", "08:00"]},
            "monitor": {"daily_report": "18:30", "check_every_minutes": 60, "work_sessions": ["10:00", "15:00"],
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
            gn, ge = detect_git_identity()
            return {"setup_needed": True, "timezone": detect_timezone(), "folder": str(self.base),
                    "git_name": gn, "git_email": ge}
        cfg = self.rt.cfg
        s = self.rt.ws.state()
        return {
            "setup_needed": False, "company": cfg.company, "owner": cfg.owner_name, "owner_key": cfg.owner_key,
            "timezone": cfg.timezone, "manager": cfg.monitor.id, "model": f"{cfg.llm.provider}"
            f"{('/' + cfg.llm.model) if cfg.llm.model else ''}", "llm_error": self.llm_error,
            "telegram": {"connected": bool(self.gw), "error": self.telegram_error,
                         "health": {k: v for k, v in (s.get("telegram_health") or {}).items() if v},
                         "owner_id": cfg.owner_user_id, "group_id": cfg.group_chat_id,
                         "manager_token": bool(cfg.monitor.bot_token)},
            "slack": {"connected": bool(self.slack), "error": self.slack_error or (self.slack.error if self.slack else ""),
                      "tokens": bool(cfg.slack.bot_token and cfg.slack.app_token),
                      "owner_ids": cfg.slack.owner_user_ids, "channels": cfg.slack.channels,
                      "workspace": self.slack.team_name if self.slack else ""},
            "paused_all": bool(s.get("paused_all")), "paused": s.get("paused", []),
            "members": [{"id": m.id, "name": m.name, "role": m.role, "manager": m.monitor,
                         "projects": m.projects, "paused": m.id in s.get("paused", []),
                         "telegram": bool(m.bot_token), "model": self.rt.model_name(m),
                         "own_model": bool(m.llm)} for m in cfg.team],
            "projects": [self._project_json(p) for p in cfg.projects.values()],
            "priorities": PRIORITIES, "workspace": str(cfg.workspace_path),
            "permissions": cfg.permissions, "action_types": ACTION_TYPES, "backchannel": BACKCHANNEL,
            "git": {"name": cfg.git_author[0], "email": cfg.git_author[1]},
            "delivery": {"choice": cfg.default_channel_raw, "home": self.hub.home("team") if self.hub else "",
                         "connected": [t.name for t in (self.hub.transports if self.hub else []) if t.name in ("telegram", "slack")]},
            "github": {"enabled": cfg.github.enabled, "owner": cfg.github.owner, "repo": cfg.github.repo,
                       "project": cfg.github.project, "prs": cfg.github.prs, **{k: v for k, v in (s.get("github") or {}).items()
                                                                                if k in ("error", "last_sync", "url")}},
        }

    def _project_json(self, p) -> dict:
        tasks = [t for t in self.rt.tasks.all() if t.project == p.id and t.status != "cut"]   # cut ≠ progress
        tz = self.rt.cfg.timezone
        return {"id": p.id, "name": p.name or p.id, "description": p.description, "lead": p.lead,
                "status": p.status, "repo": p.repo, "members": [m.id for m in self.rt.cfg.project_members(p.id)],
                "telegram_chat_id": p.telegram_chat_id, "slack": bool(self.rt.cfg.slack.channels.get(PROJECT_ROOM + p.id)),
                "channel": p.channel, "home": self.hub.home(PROJECT_ROOM + p.id) if self.hub else "",
                "room": PROJECT_ROOM + p.id,
                "counts": {"total": len(tasks), "done": sum(1 for t in tasks if t.status == "done"),
                           "open": sum(1 for t in tasks if t.is_open),
                           "blocked": sum(1 for t in tasks if t.status == "blocked"),
                           "review": sum(1 for t in tasks if t.status == "review"),
                           "overdue": sum(1 for t in tasks if t.is_open and t.overdue(tz))}}

    def project_detail(self, pid: str) -> dict:
        cfg = self.rt.cfg
        p = cfg.projects.get(pid)
        if not p:
            raise ValueError(f"no project {pid}")
        people = []
        for m in cfg.project_members(pid):
            mine = [t for t in self.rt.tasks.for_owner(m.id) if t.project == pid]
            st = ("blocked" if any(t.status == "blocked" for t in mine) else
                  "at risk" if any(t.overdue(cfg.timezone) for t in mine) else
                  "on track" if any(t.status == "doing" for t in mine) else
                  "not started" if not mine else "queued")
            people.append({"id": m.id, "name": m.name, "role": m.role, "status": st, "open": len(mine),
                           "lead": m.id == p.lead, "other_projects": [x for x in m.projects if x != pid]})
        tasks = [self._task_json(t) for t in self.rt.tasks.all() if t.project == pid]
        return {**self._project_json(p), "people": people, "tasks": tasks,
                "last": (self.chat.since(PROJECT_ROOM + pid, -1) or [])[-3:]}

    @config_txn
    def project_members(self, pid: str, members: list[str]) -> dict:
        raw = self.raw()
        if pid not in (raw.get("projects") or {}):
            raise ValueError(f"no project {pid}")
        want = {str(x) for x in members}
        for m in raw.get("team", []):
            cur = [str(x) for x in (m.get("projects") or [])]
            if str(m.get("id")) in want and pid not in cur:
                cur.append(pid)
            elif str(m.get("id")) not in want and pid in cur:
                cur.remove(pid)
            if cur:
                m["projects"] = cur
            else:
                m.pop("projects", None)
        lead = raw["projects"][pid].get("lead")
        if lead and lead not in want:
            raw["projects"][pid]["lead"] = ""
        self.save_raw(raw)
        names = [m.name for m in self.rt.cfg.team if m.id in want]
        self.rt.ws.commit(f"project {pid}: team is {', '.join(names) or 'empty'}", author=self.rt.cfg.owner_name)
        self.load()
        return self._project_json(self.rt.cfg.projects[pid])

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
                        "error": rt.model_error(next((m.id for m in cfg.team if m.name == n), ""))}
                       for n, st, line in rows],
            "asks": [self._ask_json(a) for a in rt.asks.pending()],
            "decisions": open_decisions(rt.ws),
            "review": [self._task_json(t) for t in tasks if t.status == "review"],
            "blocked_on_you": [self._task_json(t) for t in self._blocked_on_owner()],
            "queued": len(rt.queued()),
            "attention": [self._task_json(t) for t in open_ if t.status == "blocked" or t.overdue(tz)],
            "critical": [self._task_json(t) for t in sorted(open_, key=lambda t: str(t.doc.meta.get("due") or "9999"))
                         if t.priority == "P0"][:5],
            "commits": rt.ws.git_log(12).splitlines(),
            "projects": [self._project_json(p) for p in cfg.projects.values()],
            "budget": cfg.daily_tokens_per_agent, "push_error": rt.ws.state().get("push_error", ""),
        }

    def pulse(self) -> dict:
        """Small and cheap, polled every few seconds: what changed, what's unread, what needs you."""
        rt = self.rt
        import hashlib
        h = hashlib.sha1()
        for d in ("tasks", "asks", "reports", "decisions"):
            for f in sorted((rt.ws.root / d).glob("*.md")):
                st = f.stat()
                h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        h.update(json.dumps(rt.ws.state().get("paused", [])).encode())
        rooms = {r: self.chat.last_index(r) for r in self.hub.rooms()}
        last_who = {}
        for r, i in rooms.items():
            if i >= 0:
                m = self.chat.since(r, i - 1)[-1:]
                last_who[r] = m[0]["who"] if m else ""
        pend = rt.asks.pending()
        review = [t for t in rt.tasks.all() if t.status == "review"]
        return {"version": h.hexdigest()[:16], "rooms": rooms, "last_who": last_who, "thinking": dict(self.pending),
                "asks": [a.id for a in pend], "red": sum(1 for a in pend if a.level == "red"),
                "needs_you": len(pend) + len(review) + len(self._blocked_on_owner()),
                "paused_all": bool(rt.ws.state().get("paused_all"))}

    def _blocked_on_owner(self) -> list:
        rt, cfg = self.rt, self.rt.cfg
        names = {cfg.owner_key, cfg.owner_name.lower()} | ({cfg.owner_name.split()[0].lower()}
                                                           if len(cfg.owner_name.split()[0]) >= 3 else set())
        pat = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n) + r")\b",
                         re.I)                               # whole words only: "Al" never matches "approval"
        return [t for t in rt.tasks.all() if t.status == "blocked" and pat.search(t.blocked_on)
                and "approval ask-" not in t.blocked_on.lower()]

    # -- connections: every model, login and channel in one place ---------------------------------
    def connections(self) -> dict:
        from . import connections as cx
        cfg, st = self.rt.cfg, self.rt.ws.state()
        hb = st.get("heartbeat", {})
        models = []
        for g in cx.providers_in_use(cfg):
            c = g["config"]
            models.append({**cx.check(c), "people": g["people"], "default": g["default"],
                           "base_url": c.base_url, "raw_provider": c.provider,
                           "failing": {mid: self.rt.model_error(mid) for mid in g["people"] if self.rt.model_error(mid)}})
        gname, gemail = cfg.git_author
        return {"models": models,
                "git": {"name": gname, "email": gemail, "ok": gemail != "jm@localhost",
                        "fix": "" if gemail != "jm@localhost" else "Set your git name and email (Company) so commits are yours."},
                "telegram": {"on": bool(self.gw), "error": self.telegram_error,
                             "health": {k: v for k, v in (st.get("telegram_health") or {}).items() if v},
                             "configured": bool(cfg.monitor.bot_token)},
                "slack": {"on": bool(self.slack), "configured": cfg.slack.configured,
                          "error": self.slack_error or (self.slack.error if self.slack else "")},
                "github": {"enabled": cfg.github.enabled, "error": (st.get("github") or {}).get("error", ""),
                           "last_sync": (st.get("github") or {}).get("last_sync", "")}}

    def connection_test(self, b: dict) -> dict:
        from . import connections as cx
        from .config import parse_llm
        if b.get("member"):
            m = self.rt.cfg.member(str(b["member"]))
            if not m:
                raise ValueError("no such person")
            conf = self.rt.cfg.llm_for(m)
        else:
            conf = parse_llm({k: v for k, v in b.items() if k in ("provider", "model", "base_url") and v},
                             {"provider": self.rt.cfg.llm.provider, "model": self.rt.cfg.llm.model,
                              "base_url": self.rt.cfg.llm.base_url, "api_key_env": self.rt.cfg.llm.api_key_env})
        r = cx.test(conf)
        if r["ok"]:                                            # working again: clear the old failures
            people = [x.id for x in self.rt.cfg.team if self.rt.cfg.llm_for(x) == conf]
            def fn(s):
                for mid in people:
                    if (s.get("heartbeat") or {}).get(mid):
                        s["heartbeat"][mid].update(error="", fails=0)
            self.rt.ws.update_state(fn)
        return r

    def test_model(self, b: dict) -> dict:
        """Setup/Settings: does this provider answer? (One tiny call.)"""
        from .config import parse_llm
        conf = parse_llm({k: v for k, v in b.items() if k in ("provider", "model", "base_url") and v})
        if b.get("api_key") and conf.api_key_env:
            os.environ[conf.api_key_env] = str(b["api_key"])       # the key you just typed, not an older one
        try:
            r = make_llm(conf).complete('Reply with exactly: {"reply": "pong", "actions": []}',
                                        [{"role": "user", "content": "ping"}])
        except LLMError as e:
            return {"ok": False, "error": str(e)[:300]}
        return {"ok": "pong" in r.text.lower(), "reply": r.text[:120], "tokens": r.total_tokens}

    def _task_json(self, t) -> dict:
        m = t.doc.meta
        return {"id": t.id, "title": t.title, "owner": t.owner, "priority": t.priority, "status": t.status,
                "due": m.get("due") or "", "project": t.project, "blocked_on": t.blocked_on,
                "overdue": t.overdue(self.rt.cfg.timezone), "updated": m.get("updated", ""),
                "done_on": m.get("done_on", "")}

    def _ask_json(self, a) -> dict:
        m = a.doc.meta
        return {"id": a.id, "from": a.requester, "summary": a.summary, "level": a.level, "status": a.status,
                "decided_by": m.get("decided_by", ""), "decided_at": m.get("decided_at", ""),
                "outcome": a.doc.sections.get("Outcome", ""),
                "task": m.get("task", ""), "deadline": m.get("deadline", ""), "kind": a.kind,
                "recommendation": m.get("recommendation", ""), "details": a.doc.sections.get("Details", ""),
                "created": m.get("created", "")}

    def task_detail(self, tid: str) -> dict:
        t = self.rt.tasks.get(tid)
        return {**self._task_json(t), "sections": t.doc.sections, "meta": {k: str(v) for k, v in t.doc.meta.items()},
                "github": self.rt.github.url_of(t.id) if self.rt.cfg.github.enabled else ""}

    # -- GitHub ---------------------------------------------------------------------------------
    def github_status(self) -> dict:
        from .github import Gh, GitHubError, GitHubSync
        try:
            return GitHubSync(self.rt.cfg, None, None, gh=Gh()).auth()
        except GitHubError as e:
            return {"ok": False, "error": str(e)}

    @config_txn
    def github_connect(self, b: dict) -> dict:
        """Point the mirror at a Project (or create one) and a repo for the task issues."""
        from .github import GitHubError, GitHubSync
        owner, repo = str(b.get("owner") or "").strip(), str(b.get("repo") or "").strip()
        if not owner or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("Give the owner (your login or org) and the repo for task issues as owner/name.")
        number = int(str(b.get("project") or "0").strip() or 0)
        url = ""
        try:
            probe = GitHubSync(self.rt.cfg, None, None)
            if not number:
                made = probe.create_project(owner, str(b.get("title") or f"{self.rt.cfg.company} team"))
                number, url = made["number"], made["url"]
        except GitHubError as e:
            raise ValueError(f"GitHub said: {e}") from None
        raw = self.raw()
        raw["github"] = {**(raw.get("github") or {}), "owner": owner, "repo": repo, "project": number,
                         "prs": bool(b.get("prs"))}
        self.save_raw(raw)
        self.load()
        try:
            proj = self.rt.github.project()                    # creates our fields on the board
            url = url or proj.get("url", "")
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Connected, but GitHub refused the Project: {e}") from None
        self.rt.ws.update_state(lambda s: s.setdefault("github", {}).__setitem__("url", url))
        return {"project": number, "url": url}

    def github_sync(self) -> dict:
        return self.submit(self.rt.sync_github(), timeout=600)

    def member_detail(self, mid: str) -> dict:
        m = self.rt.cfg.member(mid)
        if not m:
            raise ValueError(f"no member {mid}")
        ws = self.rt.ws
        skill = ws.root / "team" / m.id / "skill"
        llm = self.rt.cfg.llm_for(m)
        return {"id": m.id, "name": m.name, "role": m.role, "projects": m.projects, "manager": m.monitor,
                "llm": {"provider": llm.provider, "model": llm.model, "base_url": llm.base_url, "own": bool(m.llm),
                        "allow_free": llm.allow_free},
                "permissions": m.permissions,
                "persona": ws.read(f"team/{m.id}/persona.md"), "memory": ws.read(f"team/{m.id}/memory.md"),
                "log": ws.tail_log(m.id, 40), "status": self.rt.tasks.person_status(m.id),
                "skill_files": sorted(str(p.relative_to(skill)) for p in skill.rglob("*") if p.is_file())
                if skill.exists() else [],
                "tasks": [self._task_json(t) for t in self.rt.tasks.for_owner(m.id, open_only=False)]}

    # -- chat ------------------------------------------------------------------------------
    def chat_since(self, room: str, after: int) -> dict:
        return {"messages": self.chat.since(room, after), "thinking": self.pending.get(room, 0)}

    def chat_send(self, room: str, text: str) -> dict:
        msg = self.hub.receive(room, text, via="console")      # shown at once; raises for a bad room
        self.submit(self.hub.handle(room, msg, via="console"), timeout=None)
        return {"ok": True}

    def rooms(self) -> list[dict]:
        """Every room with its latest message — the chat sidebar in one call."""
        out = []
        for r in self.hub.rooms():
            last = self.chat.since(r, max(-1, self.chat.last_index(r) - 1))[-1:] if self.chat.last_index(r) >= 0 else []
            out.append({"room": r, "last": last[0] if last else None, "count": self.chat.last_index(r) + 1})
        return out

    # -- tasks -----------------------------------------------------------------------------
    def task_create(self, b: dict) -> dict:
        rt = self.rt
        title = " ".join(str(b.get("title") or "").split())
        if not title:
            raise ValueError("Give the task a title.")
        owner = str(b.get("owner") or "").lower()
        if not owner or not rt.cfg.member(owner):
            raise ValueError("Pick who owns this task.")
        pid = str(b.get("project") or "")
        if pid and pid not in rt.cfg.projects:
            raise ValueError(f"There's no project {pid}.")
        if pid and pid in rt.cfg.projects and owner not in [m.id for m in rt.cfg.project_members(pid)]:
            self.project_members(pid, [m.id for m in rt.cfg.project_members(pid)] + [owner])
            rt = self.rt
        dm = [x.strip() for x in (b.get("done_means") or []) if str(x).strip()]
        t = rt.tasks.create(title=title, owner=owner,
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
        if act == "status" and str(b.get("status")) == "done":
            act = "accept"                                    # done always means accepted: same path, same follow-ups
        if act == "status":
            t, msg = rt.tasks.set_status(tid, str(b.get("status")), by=by, note=str(b.get("note") or ""),
                                         blocked_on=str(b.get("blocked_on") or ""))
            if t.status == "blocked":
                self.submit(rt.on_blocked(t, by), timeout=30)
        elif act == "accept":
            msg = self.submit(rt.accept(tid, str(b.get("note") or "accepted")), timeout=60)
            t = rt.tasks.get(tid)
        elif act == "cut":
            t, msg = rt.tasks.set_status(tid, "cut", by=by, note=str(b.get("note") or ""))
        elif act == "feedback":
            msg = self.submit(rt.feedback(tid, str(b.get("text") or "")), timeout=60)
            t = rt.tasks.get(tid)
        elif act == "changes":
            msg = self.submit(rt.request_changes(tid, str(b.get("text") or "")), timeout=60)
            t = rt.tasks.get(tid)
        elif act == "edit":
            fields = {k: b.get(k) for k in ("priority", "due", "title", "owner", "project") if b.get(k)}
            if "due" in b and not b.get("due"):
                rt.tasks.clear_due(tid, by)
            if "owner" in fields and not rt.cfg.member(str(fields["owner"])):
                raise ValueError(f"There's nobody called {fields['owner']} on the team.")
            if "project" in fields and fields["project"] not in rt.cfg.projects:
                raise ValueError(f"There's no project {fields['project']}.")
            t, msg = rt.tasks.update_fields(tid, by, **fields), ""
        else:
            raise ValueError(f"unknown action {act}")
        rt.ws.commit(f"{tid}: {act} by {rt.cfg.owner_name}", author=rt.cfg.owner_name)
        return {**self._task_json(t), "message": msg if isinstance(msg, str) else ""}

    def pause(self, who: str, pause: bool) -> dict:
        msg = self.rt.set_paused(who, pause)
        if not pause:
            n = self.submit(self.rt.drain_queue(), timeout=30)
            if n:
                msg += f" · {n} queued message{'s' if n != 1 else ''} being answered"
        return {"message": msg}

    def decide(self, ask_id: str, decision: str, note: str = "") -> dict:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        return {"result": self.submit(self.rt.decide_ask(ask_id, decision, by=self.rt.cfg.owner_name, note=note,
                                                         via="console"))}

    # -- projects, members, decisions, settings --------------------------------------------
    @config_txn
    def project_save(self, b: dict) -> dict:
        pid = slug(str(b.get("id") or b.get("name") or ""))
        if not pid:
            raise ValueError("Project name is required.")
        raw = self.raw()
        if not b.get("id") and pid in (raw.get("projects") or {}):
            raise ValueError(f"There's already a project “{raw['projects'][pid].get('name') or pid}” — open it to "
                             f"edit it, or pick another name.")
        p = raw.setdefault("projects", {}).setdefault(pid, {"repo": "", "main_branch": "main"})
        for k in ("name", "description", "lead", "repo", "status"):
            if k in b:
                p[k] = str(b[k]).strip()
        if "channel" in b:
            ch = str(b.get("channel") or "").lower()
            if ch and ch not in ("telegram", "slack", "console"):
                raise ValueError("Channel is telegram, slack or console.")
            if ch:
                p["channel"] = ch
            else:
                p.pop("channel", None)
        if "telegram_chat_id" in b:
            v = str(b.get("telegram_chat_id") or "").strip()
            if v and not v.lstrip("-").isdigit():
                raise ValueError("Telegram chat id is a number, like -1001234567890.")
            if v:
                p["telegram_chat_id"] = int(v)
            else:
                p.pop("telegram_chat_id", None)
        members = [str(x) for x in b.get("members") or []]
        if p.get("lead") and members and p["lead"] not in members:
            members.append(p["lead"])
        for m in raw.get("team", []):
            if str(m.get("id")) in members and pid not in (m.get("projects") or []):
                m.setdefault("projects", []).append(pid)
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

    @config_txn
    def member_update(self, b: dict) -> dict:
        mid = str(b.get("id", ""))
        raw = self.raw()
        for m in raw.get("team", []):
            if str(m.get("id")) == mid:
                if b.get("role"):
                    m["role"] = str(b["role"]).strip()
                if "projects" in b:
                    if not isinstance(b["projects"], list):
                        raise ValueError("projects must be a list")
                    m["projects"] = [str(p).strip() for p in b["projects"] if str(p).strip()]
                    for pid, pr in (raw.get("projects") or {}).items():   # off a project → no longer its lead
                        if isinstance(pr, dict) and pr.get("lead") == mid and pid not in m["projects"]:
                            pr["lead"] = ""
                if "llm" in b:
                    want = b.get("llm") or {}
                    if not want.get("provider"):
                        m.pop("llm", None)                          # back to the company default
                    else:
                        if want["provider"] not in PROVIDERS:
                            raise ValueError(f"Unknown AI provider {want['provider']} (use {', '.join(PROVIDERS)}).")
                        m["llm"] = {k: str(want[k]) for k in ("provider", "model", "base_url") if want.get(k)}
                        if want.get("allow_free"):
                            m["llm"]["allow_free"] = True
                if "permissions" in b:
                    perms = {k: v for k, v in (b.get("permissions") or {}).items() if k in ACTION_TYPES and v in LEVELS}
                    if perms:
                        m["permissions"] = perms
                    else:
                        m.pop("permissions", None)
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
                "workspace": raw.get("workspace", {}), "executor": raw.get("executor", {}),
                "permissions": self.rt.cfg.permissions, "slack": {k: v for k, v in (raw.get("slack") or {}).items()},
                "sync": {"mirror_owner": self.rt.cfg.mirror_owner, "channel": self.rt.cfg.default_channel_raw}}

    @config_txn
    def settings_save(self, b: dict) -> dict:
        raw = self.raw()
        if b.get("company"):
            raw["company"] = str(b["company"]).strip()
        if b.get("timezone"):
            raw["timezone"] = normalize_tz(str(b["timezone"]))
        if b.get("owner_name"):
            raw.setdefault("owner", {})["name"] = str(b["owner_name"]).strip()
        for k in ("git_name", "git_email"):
            if k in b:
                v = str(b[k] or "").strip()
                if k == "git_email" and v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
                    raise ValueError("That git email doesn't look right.")
                raw.setdefault("owner", {})[k] = v
        mon = raw.setdefault("monitor", {})
        if b.get("daily_report"):
            mon["daily_report"] = str(b["daily_report"])
        if "work_sessions" in b:
            mon["work_sessions"] = [x.strip() for x in b["work_sessions"] if x.strip()]
        if b.get("budget"):
            raw.setdefault("budget", {})["daily_tokens_per_agent"] = int(b["budget"])
        llm = raw.setdefault("llm", {})
        if b.get("provider") and str(b["provider"]) != str(llm.get("provider") or ""):
            # only a *changed* provider resets its key variable — saving Settings never clobbers a custom one
            llm["provider"] = str(b["provider"])
            from .config import KEY_ENV
            llm["api_key_env"] = KEY_ENV.get(llm["provider"], "")
        if "allow_free" in b:
            llm["allow_free"] = bool(b["allow_free"])
        if "model" in b:
            llm["model"] = str(b["model"] or "")
        if "base_url" in b:
            llm["base_url"] = str(b["base_url"] or "")
        if b.get("api_key") and llm.get("api_key_env"):
            set_env(self.base / ".env", {llm["api_key_env"]: str(b["api_key"])})
        if "coding_tool" in b and b["coding_tool"] not in ("custom", "keep"):   # "custom": leave your own command
            raw.setdefault("executor", {})["command"] = {
                "claude": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"],
                "codex": ["codex", "exec", "--full-auto", "{prompt}"]}.get(b["coding_tool"], [])
        tg = raw.setdefault("telegram", {})
        for k in ("group_chat_id",):
            if k in b and str(b[k]).strip().lstrip("-").isdigit():
                tg[k] = int(b[k])
        if "owner_telegram_id" in b and str(b["owner_telegram_id"]).strip().isdigit():
            raw.setdefault("owner", {})["telegram_user_id"] = int(b["owner_telegram_id"])
        if isinstance(b.get("permissions"), dict):
            raw["permissions"] = {k: v for k, v in b["permissions"].items() if k in ACTION_TYPES and v in LEVELS}
        if "slack_owner_ids" in b:
            ids = [x.strip() for x in re.split(r"[,\s]+", str(b["slack_owner_ids"] or "")) if x.strip()]
            if any(not re.fullmatch(r"[UW][A-Z0-9]{6,}", x) for x in ids):
                raise ValueError("Slack member ids look like U012AB3CD (Profile → ⋮ → Copy member ID).")
            raw.setdefault("slack", {})["owner_user_ids"] = ids
        if "github_prs" in b and raw.get("github"):
            raw["github"]["prs"] = bool(b["github_prs"])
        if "channel" in b:
            ch = str(b["channel"] or "").lower()
            if ch and ch not in ("telegram", "slack", "console"):
                raise ValueError("Channel is telegram, slack or console.")
            raw.setdefault("sync", {})["channel"] = ch
        if "mirror_owner" in b:
            raw.setdefault("sync", {})["mirror_owner"] = bool(b["mirror_owner"])
        self.save_raw(raw)
        self.load()
        return {"ok": True}

    # -- slack connection from the console -------------------------------------------------
    @config_txn
    def slack_connect(self, bot_token: str, app_token: str) -> dict:
        from .slack import check_tokens
        r = check_tokens(bot_token.strip(), app_token.strip())
        if not r.get("ok"):
            return r
        sc = self.rt.cfg.slack
        set_env(self.base / ".env", {sc.bot_token_env: bot_token.strip(), sc.app_token_env: app_token.strip()})
        os.environ[sc.bot_token_env], os.environ[sc.app_token_env] = bot_token.strip(), app_token.strip()
        self.load()
        return r

    @config_txn
    def slack_provision(self) -> dict:
        from .slack import _client, provision
        cfg = self.rt.cfg
        if not cfg.slack.bot_token:
            raise ValueError("Connect Slack first (bot and app tokens).")
        if not cfg.slack.owner_user_ids:
            raise ValueError("Add your Slack member id first, so you are invited to the channels.")
        rooms = [TEAM_ROOM] + [PROJECT_ROOM + p for p in cfg.projects] + [m.id for m in cfg.team] + [BACKCHANNEL]
        try:
            made = provision(self.slack.web if self.slack and self.slack.web else _client(cfg.slack.bot_token), cfg, rooms)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Slack said no: {e} — check the app's scopes (docs/SLACK.md).") from e
        raw = self.raw()
        raw.setdefault("slack", {}).setdefault("channels", {}).update(made)
        self.save_raw(raw)
        self.rt.ws.commit("slack channels connected", author=cfg.owner_name)
        self.load()
        return {"channels": made}

    def slack_email(self, email: str) -> dict:
        from .slack import SlackError, lookup_email
        if not self.rt.cfg.slack.bot_token:
            raise ValueError("Connect Slack first (bot and app tokens).")
        try:
            uid = lookup_email(self.rt.cfg.slack.bot_token, email)
        except SlackError as e:
            raise ValueError(str(e)) from None
        with path_lock(self.cfg_path):
            raw = self.raw()
            ids = raw.setdefault("slack", {}).setdefault("owner_user_ids", [])
            if uid not in ids:
                ids.append(uid)
            self.save_raw(raw)
            self.load()
        return {"found": True, "id": uid}

    def slack_detect(self) -> dict:
        if not self.slack:
            raise ValueError("Slack isn't running yet — connect the tokens first.")
        code = getattr(self, "_slack_code", "")
        if not code:
            raise ValueError("Get a code first (Detect me shows it).")
        uid = self.slack.wait_for_unknown_user(90, code=code)
        if not uid:
            return {"found": False}
        with path_lock(self.cfg_path):
            raw = self.raw()
            ids = raw.setdefault("slack", {}).setdefault("owner_user_ids", [])
            if uid not in ids:
                ids.append(uid)
            self.save_raw(raw)
            self.load()
        return {"found": True, "id": uid}

    # -- telegram connection from the console ----------------------------------------------
    def telegram_manager(self, token: str) -> dict:
        check = self.admin.check_token(token)
        if not check.get("ok"):
            return check
        mgr = self.rt.cfg.monitor
        set_env(self.base / ".env", {mgr.bot_token_env: token})
        os.environ[mgr.bot_token_env] = token
        return check

    def telegram_code(self) -> dict:
        """A one-time code the owner sends to the manager's bot: only that message makes someone the owner."""
        self._tg_code = f"{secrets.randbelow(900000) + 100000}"
        return {"code": self._tg_code}

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
                    code = getattr(self, "_tg_code", "")
                    if not code:
                        raise ValueError("Get a code first (Detect me shows it).")
                    return await p.wait_for_owner(90, code=code)
                return await p.wait_for_group(cfg.owner_user_id, 90)
        found = asyncio.run(go())
        if not found:
            return {"found": False}
        with path_lock(self.cfg_path):
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
                if path in ("/api/model_login", "/api/connections/login"):
                    from . import connections as cx
                    return self._json(200, cx.login_state(q.get("provider", "")))
                if not app.ready:
                    return self._json(409, {"error": "setup needed"})
                routes = {
                    "/api/dashboard": lambda: app.dashboard(),
                    "/api/tasks": lambda: [app._task_json(t) for t in app.rt.tasks.all()],
                    "/api/task": lambda: app.task_detail(q.get("id", "")),
                    "/api/asks": lambda: [app._ask_json(a) for a in reversed(app.rt.asks.all())][:50],
                    "/api/chat": lambda: app.chat_since(q.get("room", TEAM_ROOM), int(q.get("after", -1))),
                    "/api/rooms": lambda: app.rooms(),
                    "/api/pulse": lambda: app.pulse(),
                    "/api/github/status": lambda: app.github_status(),
                    "/api/connections": lambda: app.connections(),
                    "/api/opencode/models": lambda: __import__("james_monitoring.connections", fromlist=["x"]).opencode_models(),
                    "/api/member": lambda: app.member_detail(q.get("id", "")),
                    "/api/project": lambda: app.project_detail(q.get("id", "")),
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
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._json(400, {"error": "bad Content-Length"})
            if n < 0 or n > 30_000_000:
                return self._json(413, {"error": "too large"})
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"error": "bad json"})
            if not isinstance(b, dict):
                return self._json(400, {"error": "expected a JSON object"})
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/setup":
                    return self._json(200, app.setup(b))
                if path == "/api/test_model":
                    return self._json(200, app.test_model(b))
                if path == "/api/model_check":                 # setup too: is this model ready? (no model call)
                    from . import connections as cx
                    from .config import parse_llm
                    return self._json(200, cx.check(parse_llm({k: v for k, v in b.items()
                                                               if k in ("provider", "model", "base_url", "allow_free")})))
                if path in ("/api/model_login", "/api/connections/login"):    # sign in (setup and Settings)
                    from . import connections as cx
                    return self._json(200, cx.login(str(b.get("provider", "")), app.base / ".jm-login",
                                                    target=str(b.get("target", "") or "")))
                if path == "/api/model_login/input":                  # the pasted code / key, or a key press
                    from . import connections as cx
                    return self._json(200, cx.login_input(str(b.get("provider", "")), text=str(b.get("text", "")),
                                                          key=str(b.get("key", ""))))
                if path == "/api/model_login/cancel":
                    from . import connections as cx
                    return self._json(200, cx.login_cancel(str(b.get("provider", ""))))
                if path == "/api/model_key":                          # an API key, straight into .env
                    env, key = str(b.get("env", "")).strip(), str(b.get("key", "")).strip()
                    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", env) or not key:
                        raise ValueError("Paste the key (and pick which variable it's for).")
                    set_env(app.base / ".env", {env: key})
                    return self._json(200, {"saved": env})
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
                    "/api/project/members": lambda: app.project_members(str(b.get("id", "")), list(b.get("members") or [])),
                    "/api/member": lambda: app.member_update(b),
                    "/api/decisions": lambda: app.decisions_save(str(b.get("text", ""))),
                    "/api/settings": lambda: app.settings_save(b),
                    "/api/upload": lambda: a.upload(str(b.get("filename", "SKILL.md")),
                                                    base64.b64decode(b.get("data", ""))),
                    "/api/upload_folder": lambda: a.upload_folder({str(f.get("path", "")): base64.b64decode(f.get("data", ""))
                                                                   for f in b.get("files") or []}, str(b.get("name", "folder"))),
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
                    "/api/pause": lambda: app.pause(str(b.get("who", "all")), bool(b.get("pause"))),
                    "/api/telegram/manager": lambda: app.telegram_manager(str(b.get("token", "")).strip()),
                    "/api/telegram/detect": lambda: app.telegram_detect(str(b.get("what", "owner"))),
                    "/api/telegram/code": lambda: app.telegram_code(),
                    "/api/github/connect": lambda: app.github_connect(b),
                    "/api/connections/test": lambda: app.connection_test(b),
                    "/api/github/sync": lambda: app.github_sync(),
                    "/api/slack/code": lambda: {"code": setattr(app, "_slack_code", f"{secrets.randbelow(900000) + 100000}")
                                               or app._slack_code},
                    "/api/slack/connect": lambda: app.slack_connect(str(b.get("bot_token", "")), str(b.get("app_token", ""))),
                    "/api/slack/provision": lambda: app.slack_provision(),
                    "/api/slack/detect": lambda: app.slack_detect(),
                    "/api/slack/email": lambda: app.slack_email(str(b.get("email", ""))),
                    "/api/restart": lambda: (app.load(), {"ok": True})[1],
                }
                fn = routes.get(path)
                if not fn:
                    return self._json(404, {"error": "not found"})
                return self._json(200, fn())
            except (ValueError, TaskError, AskError, ConfigError, FileNotFoundError) as e:
                return self._json(400, {"error": str(e)})
            except (AttributeError, TypeError, zipfile.BadZipFile) as e:   # malformed input, not a server fault
                return self._json(400, {"error": f"That request wasn't in the expected shape ({type(e).__name__})."})
            except (TimeoutError, concurrent.futures.TimeoutError):
                return self._json(504, {"error": "That's taking longer than expected — it continues in the "
                                                 "background; check back in a minute."})
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
        + (", Telegram connected" if app.gw else ", Telegram not connected")
        + (", Slack connected" if app.slack else ""))
    print(f"james-monitoring console: {url}\n{status}\nCtrl-C to stop.", flush=True)   # shows under Docker/systemd too
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
                app.submit(app._stop_services(app.sched, app.gw, app.slack, drain=True), timeout=90)
            except Exception:  # noqa: BLE001
                pass
