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
from .runtime import Event, Runtime
from .scheduler import Scheduler
from .setup import detect_git_identity, detect_timezone, scaffold, slug
from .tasks import PRIORITIES, TaskError
from .ui import TeamAdmin
from .util import normalize_tz, today

log = logging.getLogger("jm.server")

PROVIDERS = ("claude-code", "codex-cli", "opencode", "anthropic", "openai", "openrouter", "ollama", "fake")


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
        from .public import FailedKeys, PublicLink
        self.public = PublicLink()                # the console on a public https URL (Cloudflare Tunnel)
        self.failed_keys = FailedKeys()
        self.console_key = ""
        self.port = 0
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
            rt.console_link = self.console_link           # for /link in chat
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

    # -- the public link (Cloudflare Tunnel) ------------------------------------------------------
    def public_status(self) -> dict:
        st = self.public.status()
        st["named_url"] = self.rt.cfg.public_url if self.rt else ""
        st["link"] = f"{st['url']}/?k={self.console_key}" if st.get("url") else ""
        return st

    def public_set(self, on: bool) -> dict:
        if on:
            self.public.start(self.port, self.console_key, named_url=self.rt.cfg.public_url if self.rt else "")
        else:
            self.public.stop()
        st = self.public_status()
        if self.rt:
            self.rt._audit("system", self.rt.cfg.owner_name, "public link " + ("on" if st["on"] else "off"),
                           st.get("url", ""), st.get("error", ""))
        return st

    def console_link(self) -> str:
        """The link that opens the console as the founder: public if the tunnel is on, else this machine's."""
        base = self.public.url if self.public.on else f"http://localhost:{self.port}"
        return f"{base}/?k={self.console_key}"

    # -- who is asking, and what may they see -------------------------------------------------
    def owner_user(self) -> dict:
        cfg = self.rt.cfg if self.rt else None
        return {"id": cfg.owner_key if cfg else "owner", "name": cfg.owner_name if cfg else "Owner", "role": "owner",
                "client": ""}

    def user_for_key(self, key: str, console_key: str) -> dict | None:
        from .users import find_user
        if secrets.compare_digest(key or "", console_key):
            return self.owner_user()
        u = find_user(self.rt.cfg if self.rt else None, key)
        return {"id": u.id, "name": u.name, "role": u.role, "client": u.client} if u else None

    def private_rooms(self) -> set[str]:
        """The personal assistant's room: the founder's alone."""
        return {m.id for m in self.rt.cfg.assistants}

    def can_see_room(self, user: dict, room: str) -> bool:
        role = user["role"]
        if role == "owner":
            return True
        if role == "client" or room in self.private_rooms():
            return False
        if role == "admin":
            return True
        return room in (TEAM_ROOM, BACKCHANNEL) or room.startswith(PROJECT_ROOM)     # not the founder's 1:1s

    def can_write_room(self, user: dict, room: str) -> bool:
        from .users import can
        if room == BACKCHANNEL or not can(user, "chat") or not self.can_see_room(user, room):
            return False
        return user["role"] in ("owner", "admin") or room == TEAM_ROOM or room.startswith(PROJECT_ROOM)

    def can_see_project(self, user: dict, pid: str) -> bool:
        if user["role"] != "client":
            return True
        p = self.rt.cfg.projects.get(pid)
        return bool(p and user.get("client") and p.client == user["client"])

    def can_see_ask(self, user: dict, a) -> bool:
        """Requests from the personal assistant are the founder's alone."""
        return user["role"] == "owner" or a.requester not in self.private_rooms()

    def is_personal(self, t) -> bool:
        return any(m.id == t.owner for m in self.rt.cfg.assistants)

    def can_see_task(self, user: dict, t) -> bool:
        if user["role"] == "owner":
            return True
        if self.is_personal(t):
            return False
        return self.can_see_project(user, t.project) if user["role"] == "client" else True

    # -- sign-in links (owner only) -----------------------------------------------------------
    def users_list(self) -> list[dict]:
        from .users import ROLE_TEXT
        return [{"id": u.id, "name": u.name, "role": u.role, "client": u.client, "role_text": ROLE_TEXT.get(u.role, ""),
                 "has_link": bool(u.key_sha256)} for u in self.rt.cfg.users]

    @config_txn
    def users_save(self, b: dict) -> dict:
        """Add a person (returns their link once), change their role, give them a new link, or remove them."""
        from .users import hash_key, new_key               # roles and clients are validated by the config
        act = str(b.get("action") or "add")
        raw = self.raw()
        users = raw.setdefault("users", [])
        uid = slug(str(b.get("id") or b.get("name") or ""))
        key = ""
        if act == "add":
            name = " ".join(str(b.get("name") or "").split())
            if not name:
                raise ValueError("Give their name.")
            if any(str(u.get("id")) == uid for u in users):
                raise ValueError(f"There's already someone called {uid} — pick another name.")
            key = new_key()
            users.append({"id": uid, "name": name, "role": str(b.get("role") or "viewer"),
                          **({"client": str(b["client"])} if b.get("client") else {}), "key_sha256": hash_key(key)})
        else:
            u = next((x for x in users if str(x.get("id")) == uid), None)
            if not u:
                raise ValueError(f"No sign-in for {uid}.")
            if act == "remove":
                users.remove(u)
            elif act == "rotate":
                key = new_key()
                u["key_sha256"] = hash_key(key)
            elif act == "update":
                if b.get("role"):
                    u["role"] = str(b["role"])
                if "client" in b:
                    if b.get("client"):
                        u["client"] = str(b["client"])
                    else:
                        u.pop("client", None)
            else:
                raise ValueError(f"unknown action {act}")
        if not users:
            raw.pop("users", None)
        self.save_raw(raw)
        self.load()
        return {"users": self.users_list(), "id": uid, "key": key}

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
    def state(self, user: dict | None = None) -> dict:
        user = user or (self.owner_user() if self.ready else {"role": "owner"})
        if self.ready and user["role"] == "client":
            cfg = self.rt.cfg
            c = cfg.clients.get(user.get("client", ""))
            return {"setup_needed": False, "company": cfg.company, "me": user, "client": {"id": c.id, "name": c.name}
                    if c else None, "timezone": cfg.timezone}
        if not self.ready:
            gn, ge = detect_git_identity()
            return {"setup_needed": True, "timezone": detect_timezone(), "folder": str(self.base),
                    "git_name": gn, "git_email": ge}
        cfg = self.rt.cfg
        s = self.rt.ws.state()
        out = {
            "setup_needed": False, "company": cfg.company, "owner": cfg.owner_name, "owner_key": cfg.owner_key,
            "me": user,
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
                         "own_model": bool(m.llm), "department": m.department, "assistant": m.assistant}
                        for m in cfg.team],
            "departments": [{"id": d.id, "name": d.name, "head": d.head} for d in cfg.departments.values()],
            "clients": [{"id": c.id, "name": c.name} for c in cfg.clients.values()],
            "users": [{"id": u.id, "name": u.name, "role": u.role} for u in cfg.users],
            "public": ({k: v for k, v in self.public_status().items() if k != "link"} if user["role"] in ("owner", "admin")
                       else {"url": self.public.url if self.public.on else ""}),
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
        if user["role"] != "owner":             # the personal assistant is the founder's alone
            out["members"] = [m for m in out["members"] if not m["assistant"]]
        return out

    def _project_json(self, p) -> dict:
        tasks = [t for t in self.rt.tasks.all() if t.project == p.id and t.status != "cut"]   # cut ≠ progress
        tz = self.rt.cfg.timezone
        return {"id": p.id, "name": p.name or p.id, "description": p.description, "lead": p.lead, "client": p.client,
                "status": p.status, "repo": p.repo, "members": [m.id for m in self.rt.cfg.project_members(p.id)],
                "telegram_chat_id": p.telegram_chat_id, "slack": bool(self.rt.cfg.slack.channels.get(PROJECT_ROOM + p.id)),
                "channel": p.channel, "home": self.hub.home(PROJECT_ROOM + p.id) if self.hub else "",
                "room": PROJECT_ROOM + p.id,
                "counts": {"total": len(tasks), "done": sum(1 for t in tasks if t.status == "done"),
                           "open": sum(1 for t in tasks if t.is_open),
                           "blocked": sum(1 for t in tasks if t.status == "blocked"),
                           "review": sum(1 for t in tasks if t.status == "review"),
                           "overdue": sum(1 for t in tasks if t.is_open and t.overdue(tz))}}

    def project_detail(self, pid: str, user: dict | None = None) -> dict:
        if user and not self.can_see_project(user, pid):
            raise PermissionError("That project isn't part of your portal.")
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

    # -- project settings: this project's own model, rules and instructions — for everyone, or per person --------
    def project_settings(self, pid: str) -> dict:
        cfg = self.rt.cfg
        p = cfg.projects.get(pid)
        if not p:
            raise ValueError(f"no project {pid}")
        raw = (self.raw().get("projects") or {}).get(pid) or {}
        llm_view = lambda c: {"provider": c.provider, "model": c.model, "allow_free": c.allow_free}   # noqa: E731

        def perm_source(m, a):
            pa = p.agents.get(m.id)
            if pa and pa.permissions.get(a):
                return "person_project"
            if p.permissions.get(a):
                return "project"
            return "person" if m.permissions.get(a) else "company"
        people = []
        for m in cfg.project_members(pid):
            people.append({"id": m.id, "name": m.name, "role": m.role,
                           "model": self.rt.model_name(m, pid), "model_source": cfg.llm_source(m, pid),
                           "permissions": {a: {"level": cfg.permission(m, a, pid), "source": perm_source(m, a)}
                                           for a in ACTION_TYPES}})
        return {"id": pid, "llm": raw.get("llm") or {}, "permissions": raw.get("permissions") or {},
                "instructions": raw.get("instructions") or "", "agents": raw.get("agents") or {},
                "company": {"llm": llm_view(cfg.llm), "permissions": cfg.permissions}, "people": people}

    @config_txn
    def project_settings_save(self, b: dict) -> dict:
        """Save a project's settings. Anything left empty falls back (the person's own → the company's)."""
        pid = str(b.get("id") or "")
        raw = self.raw()
        p = (raw.get("projects") or {}).get(pid)
        if p is None:
            raise ValueError(f"no project {pid}")
        team = {str(m.get("id")) for m in raw.get("team", [])}

        def clean_llm(d):
            d = d or {}
            if not d.get("provider"):
                return None
            if d["provider"] not in PROVIDERS:
                raise ValueError(f"Unknown AI provider {d['provider']}.")
            out = {k: str(d[k]).strip() for k in ("provider", "model", "base_url") if str(d.get(k) or "").strip()}
            if d.get("allow_free"):
                out["allow_free"] = True
            if out["provider"] == "opencode" and "/" not in out.get("model", ""):
                raise ValueError("Pick an OpenCode model (provider/model) for this project.")
            return out

        def clean_perms(d):
            return {k: v for k, v in (d or {}).items() if k in ACTION_TYPES and v in LEVELS}

        def put(target, key, value):
            if value:
                target[key] = value
            else:
                target.pop(key, None)
        if "llm" in b:
            put(p, "llm", clean_llm(b.get("llm")))
        if "permissions" in b:
            put(p, "permissions", clean_perms(b.get("permissions")))
        if "instructions" in b:
            put(p, "instructions", str(b.get("instructions") or "").strip())
        if isinstance(b.get("agents"), dict):
            agents = {}
            for mid, a in b["agents"].items():
                if str(mid) not in team:
                    raise ValueError(f"{mid} isn't on the team.")
                a = a or {}
                entry = {}
                put(entry, "role", " ".join(str(a.get("role") or "").split()))
                put(entry, "llm", clean_llm(a.get("llm")))
                put(entry, "permissions", clean_perms(a.get("permissions")))
                put(entry, "instructions", str(a.get("instructions") or "").strip())
                if entry:
                    agents[str(mid)] = entry
            put(p, "agents", agents)
        self.save_raw(raw)                   # config.yaml (validated, .bak); the change is in the audit log
        self.load()
        return self.project_settings(pid)

    def remove_person(self, mid: str) -> dict:
        """Take someone off the team. Their open tasks go to the manager (logged), so nothing is left without an
        owner; their settings on projects and a department they headed are cleared; their files stay in git."""
        rt = self.rt
        m = rt.cfg.member(mid)
        if not m:
            raise ValueError(f"no member {mid}")
        name = self.admin.remove(m.id)["removed"]
        self.load()
        rt = self.rt
        mgr = rt.cfg.monitor
        moved = []
        for t in rt.tasks.all():                              # reviews they'd have done come back to you
            if t.is_open and str(t.doc.meta.get("reviewer") or "") == m.id:
                rt.tasks.update_fields(t.id, rt.owner_id, reviewer=rt.owner_id)
        for t in rt.tasks.for_owner(m.id):
            rt.tasks.update_fields(t.id, rt.owner_id, owner=mgr.id)
            rt.tasks.add_log(t.id, rt.owner_id, f"{name} left the team — handed to {mgr.name} to reassign")
            moved.append(t.id)
        rt.ws.commit(f"{name} left the team" + (f"; {', '.join(moved)} → {mgr.name}" if moved else ""),
                     author=rt.cfg.owner_name)
        if moved:
            self.submit(rt.bus.send_owner(mgr.id, f"{name} left the team. Their open tasks are with me now: "
                                                  f"{', '.join(moved)} — tell me who should take them."), timeout=None)
        return {"removed": name, "tasks_moved": moved}

    @config_txn
    def project_pause(self, pid: str, pause: bool) -> dict:
        """Pause a project: nobody works on it — its room's messages, its tasks and coding wait in the queue."""
        raw = self.raw()
        p = (raw.get("projects") or {}).get(pid)
        if p is None:
            raise ValueError(f"no project {pid}")
        name = p.get("name") or pid
        p["status"] = "paused" if pause else "active"
        self.save_raw(raw)
        self.rt.ws.write(f"projects/{pid}.md", re.sub(r"(?m)^Status: .*$", f"Status: {p['status']}",
                                                      self.rt.ws.read(f"projects/{pid}.md") or f"# {name}\n\nStatus: {p['status']}\n"))
        self.rt.ws.commit(f"project {pid} {'paused' if pause else 'resumed'}", author=self.rt.cfg.owner_name)
        self.load()
        msg = f"{name} is paused — nobody works on it; messages and tasks wait." if pause else f"{name} is running again."
        waiting = sum(1 for it in self.rt.queued() if self.rt.project_of(Event(**it["event"])) == pid) if not pause else 0
        if waiting:
            msg += f" {waiting} waiting message{'s' if waiting != 1 else ''} being answered."
        # the notice first: whoever answers what waited reads that the project is running again
        self.submit(self.hub.post(PROJECT_ROOM + pid, self.rt.cfg.monitor.id,
                                  ("⏸ " if pause else "▶️ ") + msg, kind="notice"), timeout=30)
        if not pause:
            self.submit(self.rt.drain_queue(), timeout=30)
        return {"message": msg, "project": self._project_json(self.rt.cfg.projects[pid])}

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

    def dashboard(self, user: dict | None = None) -> dict:
        user = user or self.owner_user()
        rt, cfg = self.rt, self.rt.cfg
        tz = cfg.timezone
        everything = rt.tasks.all()
        tasks = [t for t in everything if not self.is_personal(t)]      # company numbers: the company's work
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
            "asks": [self._ask_json(a) for a in rt.asks.pending() if self.can_see_ask(user, a)],
            "decisions": open_decisions(rt.ws),
            "review": [self._task_json(t) for t in tasks if t.status == "review"],
            "blocked_on_you": [self._task_json(t) for t in self._blocked_on_owner()] if user["role"] == "owner" else [],
            "queued": len(rt.queued()),
            "attention": [self._task_json(t) for t in open_ if t.status == "blocked" or t.overdue(tz)],
            "critical": [self._task_json(t) for t in sorted(open_, key=lambda t: str(t.doc.meta.get("due") or "9999"))
                         if t.priority == "P0"][:5],
            "commits": rt.ws.git_log(12).splitlines(),
            "projects": [self._project_json(p) for p in cfg.projects.values()],
            "budget": cfg.daily_tokens_per_agent, "push_error": rt.ws.state().get("push_error", ""),
            "departments": [{"id": d.id, "name": d.name, "head": d.head,
                             "people": [m.id for m in cfg.workers if m.department == d.id],
                             "open": sum(1 for t in open_ if cfg.member(t.owner) and cfg.member(t.owner).department == d.id),
                             "blocked": sum(1 for t in open_ if t.status == "blocked" and cfg.member(t.owner)
                                            and cfg.member(t.owner).department == d.id)}
                            for d in cfg.departments.values()],
            "my_day": self._my_day() if user["role"] == "owner" and cfg.assistants else None,
        }

    def _my_day(self) -> dict:
        """The founder's own list (from the personal assistant): personal tasks and today's reminders."""
        from .assistant import Reminders
        cfg, rt = self.rt.cfg, self.rt
        a = cfg.assistants[0]
        d = today(cfg.timezone).isoformat()
        mine = [self._task_json(t) for m in cfg.assistants for t in rt.tasks.for_owner(m.id)]
        return {"assistant": a.id, "name": a.name, "tasks": sorted(mine, key=lambda t: t["due"] or "9999")[:8],
                "reminders": [r for r in Reminders(rt).all() if r["at"][:10] <= d]}

    def pulse(self, user: dict | None = None) -> dict:
        """Small and cheap, polled every few seconds: what changed, what's unread, what needs you."""
        from .users import can
        user = user or self.owner_user()
        rt = self.rt
        import hashlib
        h = hashlib.sha1()
        for d in ("tasks", "asks", "reports", "decisions"):
            for f in sorted((rt.ws.root / d).glob("*.md")):
                st = f.stat()
                h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        h.update(json.dumps(rt.ws.state().get("paused", [])).encode())
        rooms = {r: self.chat.last_index(r) for r in self.hub.rooms() if self.can_see_room(user, r)}
        last_who = {}
        for r, i in rooms.items():
            if i >= 0:
                m = self.chat.since(r, i - 1)[-1:]
                last_who[r] = m[0]["who"] if m else ""
        pend = [a for a in rt.asks.pending() if self.can_see_ask(user, a)]
        review = [t for t in rt.tasks.all() if t.status == "review" and self.can_see_task(user, t)]
        approver = can(user, "approve")
        return {"version": h.hexdigest()[:16], "rooms": rooms, "last_who": last_who,
                "thinking": {k: v for k, v in self.pending.items() if k in rooms},
                "asks": [a.id for a in pend] if approver else [], "red": sum(1 for a in pend if a.level == "red") if approver else 0,
                "needs_you": (len(pend) + len(review) + (len(self._blocked_on_owner()) if user["role"] == "owner" else 0))
                if approver else 0,
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

    # -- Settings → Models ---------------------------------------------------------------------
    def models_catalog(self, refresh: bool = False) -> list:
        from . import connections as cx
        if refresh:
            cx._cache.clear()
        return cx._cached("catalog", 20 if not refresh else 0, cx.catalog)

    def models_team(self) -> list[dict]:
        """Each person: their model, whether it's ready, the last error, and their saved sessions."""
        from . import connections as cx
        cfg, rt = self.rt.cfg, self.rt
        sess = rt.sessions()
        checks: dict = {}
        out = []
        for m in cfg.team:
            c = cfg.llm_for(m)
            key = (c.provider, c.model, c.base_url, c.api_key_env, c.allow_free)
            if key not in checks:
                checks[key] = cx.check(c)
            chk = checks[key]
            out.append({"id": m.id, "name": m.name, "role": m.role, "provider": c.provider, "model": c.model,
                        "own": bool(m.llm), "allow_free": c.allow_free, "label": chk.get("label", c.provider),
                        "ok": chk["ok"], "detail": chk["detail"], "fix": chk.get("fix", ""),
                        "error": rt.model_error(m.id), "sessions": [x for x in sess if x["member"] == m.id]})
        return out

    @config_txn
    def models_assign(self, b: dict) -> dict:
        """Use a model for the whole company (default) or for chosen people."""
        from .config import parse_llm
        provider = str(b.get("provider") or "").strip()
        if provider not in PROVIDERS + ("openrouter", "ollama"):
            raise ValueError(f"Unknown provider {provider!r}.")
        llm = {"provider": provider}
        for k in ("model", "base_url", "api_key_env"):
            if str(b.get(k) or "").strip():
                llm[k] = str(b[k]).strip()
        if b.get("allow_free"):
            llm["allow_free"] = True
        if provider == "opencode" and "/" not in llm.get("model", ""):
            raise ValueError("Pick an OpenCode model (provider/model).")
        parse_llm(llm)                                          # validates
        who = b.get("who") if b.get("who") is not None else b.get("members")
        if isinstance(who, str) and who != "company":
            who = [who]
        if not who:
            raise ValueError("Choose who uses it — the company default or some people.")
        raw = self.raw()
        if who == "company":
            keep = {k: v for k, v in (raw.get("llm") or {}).items() if k in ("max_tokens", "temperature")}
            raw["llm"] = {**keep, **llm}
        else:
            if not isinstance(who, list):
                raise ValueError("`who` must be \"company\" or a list of people.")
            ids = {str(x) for x in who}
            known = {str(m.get("id")) for m in raw.get("team", [])}
            if ids - known:
                raise ValueError(f"I don't know {', '.join(sorted(ids - known))} — pick people from the team.")
            for m in raw.get("team", []):
                if str(m.get("id")) in ids:
                    m["llm"] = dict(llm)
        self.save_raw(raw)
        self.load()
        return {"ok": True, "team": self.models_team()}

    @config_txn
    def models_use_default(self, member: str) -> dict:
        raw = self.raw()
        for m in raw.get("team", []):
            if str(m.get("id")) == member:
                m.pop("llm", None)
        self.save_raw(raw)
        self.load()
        return {"ok": True}

    def models_health(self) -> dict:
        """What the console checks when it opens: every model in use, and OpenCode."""
        from . import connections as cx
        team = self.models_team()
        problems = {}
        for t in team:
            if not t["ok"] or t["error"]:
                k = (t["label"], t["model"], t["detail"], t["fix"])
                problems.setdefault(k, []).append(t["name"])
        cfg = self.rt.cfg
        for pid, p in cfg.projects.items():                     # models a project gives its people
            for m in cfg.project_members(pid):
                if cfg.llm_source(m, pid) not in ("project", "person_project"):
                    continue
                c = cfg.llm_for(m, pid)
                chk = cx.check(c)
                if not chk["ok"]:
                    k = (chk.get("label", c.provider), c.model, chk["detail"], chk.get("fix", ""))
                    problems.setdefault(k, []).append(f"{m.name} on {p.name or pid}")
        oc = cx._cached("opencode-info", 60, cx.opencode_info)
        return {"ok": not problems, "problems": [{"label": k[0], "model": k[1], "detail": k[2], "fix": k[3],
                                                  "who": v} for k, v in problems.items()],
                "opencode": {k: oc.get(k) for k in ("installed", "version", "credentials", "install")}}

    def test_model(self, b: dict) -> dict:
        """Setup/Settings: does this provider answer? (One tiny call.)"""
        from .config import parse_llm
        conf = parse_llm({k: v for k, v in b.items() if k in ("provider", "model", "base_url", "allow_free") and v})
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
                "done_on": m.get("done_on", ""), "reviewer": str(m.get("reviewer") or ""), "personal": self.is_personal(t),
                "depends_on": [str(x) for x in (m.get("depends_on") or [])]}

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
        rel = str(t.path.relative_to(self.rt.ws.root))
        deps = []
        for d in t.doc.meta.get("depends_on") or []:
            try:
                x = self.rt.tasks.get(str(d))
                deps.append({"id": x.id, "title": x.title, "status": x.status})
            except TaskError:
                deps.append({"id": str(d), "title": "(gone)", "status": ""})
        needed_by = [{"id": x.id, "title": x.title, "status": x.status} for x in self.rt.tasks.all()
                     if t.id in [str(d) for d in (x.doc.meta.get("depends_on") or [])]]
        return {**self._task_json(t), "sections": t.doc.sections, "meta": {k: str(v) for k, v in t.doc.meta.items()},
                "github": self.rt.github.url_of(t.id) if self.rt.cfg.github.enabled else "",
                "history": self.rt.ws.file_history(rel), "deps": deps, "needed_by": needed_by}

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

    def team_state(self, user: dict | None = None) -> dict:
        st = self.admin.state()
        if (user or {}).get("role", "owner") != "owner":
            private = self.private_rooms()
            st["members"] = [m for m in st.get("members", []) if m.get("id") not in private]
        return st

    def member_detail(self, mid: str, user: dict | None = None) -> dict:
        from .users import can
        out = self._member_detail(mid)
        if user and not can(user, "admin"):                   # memory and the private log are for admins
            out["memory"], out["log"] = "", ""
        return out

    def _member_detail(self, mid: str) -> dict:
        m = self.rt.cfg.member(mid)
        if not m:
            raise ValueError(f"no member {mid}")
        ws = self.rt.ws
        skill = ws.root / "team" / m.id / "skill"
        llm = self.rt.cfg.llm_for(m)
        return {"id": m.id, "name": m.name, "role": m.role, "projects": m.projects, "manager": m.monitor,
                "department": m.department, "assistant": m.assistant,
                "llm": {"provider": llm.provider, "model": llm.model, "base_url": llm.base_url, "own": bool(m.llm),
                        "allow_free": llm.allow_free},
                "permissions": m.permissions,
                "persona": ws.read(f"team/{m.id}/persona.md"), "memory": ws.read(f"team/{m.id}/memory.md"),
                "log": ws.tail_log(m.id, 40), "status": self.rt.tasks.person_status(m.id),
                "skill_files": sorted(str(p.relative_to(skill)) for p in skill.rglob("*") if p.is_file())
                if skill.exists() else [],
                "tasks": [self._task_json(t) for t in self.rt.tasks.for_owner(m.id, open_only=False)]}

    # -- search, audit, memory ----------------------------------------------------------------
    def search(self, q: str, user: dict | None = None) -> dict:
        from .search import search
        user = user or self.owner_user()
        rooms = [r for r in self.hub.rooms() if self.can_see_room(user, r)]
        members = self.rt.cfg.team if user["role"] == "owner" else self.rt.cfg.workers
        return search(self.rt, q, rooms, include_memory=user["role"] in ("owner", "admin"),
                      include_tasks=lambda t: self.can_see_task(user, t), members=members)

    def audit_list(self, q: dict) -> list[dict]:
        before = float(q.get("before") or 0)
        return self.rt.audit.entries(limit=min(int(q.get("limit") or 200), 1000), kind=q.get("kind", ""),
                                     who=q.get("who", ""), q=q.get("q", ""), before=before)

    def doc(self, rel: str) -> dict:
        """A team document (docs/, reports/, decisions/, projects/) — nothing outside them."""
        from .search import DOC_DIRS
        rel = str(rel or "").strip().lstrip("/")
        p = (self.rt.ws.root / rel).resolve()
        ok = any(p.is_relative_to((self.rt.ws.root / d).resolve()) for d in DOC_DIRS)
        if not ok or p.suffix != ".md" or not p.is_file():
            raise ValueError("That isn't a team document.")
        return {"path": rel, "text": p.read_text(errors="replace"), "history": self.rt.ws.file_history(rel, 10)}

    def playbook_get(self, mid: str) -> dict:
        m = self.rt.cfg.member(mid)
        if not m:
            raise ValueError(f"no member {mid}")
        done = sum(1 for t in self.rt.tasks.all() if t.owner == m.id and t.status == "done")
        return {"id": m.id, "lessons": self.rt.ws.lessons(m.id), "done": done}

    def playbook_edit(self, b: dict, by: str = "") -> dict:
        m = self.rt.cfg.member(str(b.get("id", "")))
        if not m:
            raise ValueError("no such person")
        if b.get("action") == "delete":
            self.rt.ws.forget_lesson(m.id, str(b.get("entry", "")))
        else:
            self.rt.ws.learn(m.id, str(b.get("text", "")), str(b.get("topic", "")), f"{by or self.rt.cfg.owner_name}")
        self.rt.ws.commit(f"{m.id}: playbook updated by {by or self.rt.cfg.owner_name}", author=by or self.rt.cfg.owner_name)
        return self.playbook_get(m.id)

    def memory_get(self, mid: str) -> dict:
        m = self.rt.cfg.member(mid)
        if not m:
            raise ValueError(f"no member {mid}")
        return {"id": m.id, "entries": self.rt.ws.memory_entries(m.id),
                "prompt_chars": len(self.rt.ws.memory(m.id))}

    def memory_edit(self, b: dict, by: str = "") -> dict:
        m = self.rt.cfg.member(str(b.get("id", "")))
        if not m:
            raise ValueError("no such person")
        act = str(b.get("action", ""))
        self.rt.ws.memory_edit(m.id, act, entry_id=str(b.get("entry", "")), text=str(b.get("text", "")),
                               pinned=bool(b.get("pinned")))
        self.rt.ws.commit(f"{m.id}: memory {act} by {by or self.rt.cfg.owner_name}", author=by or self.rt.cfg.owner_name)
        return self.memory_get(m.id)

    # -- notifications: everything that wants you, in one list ---------------------------------
    @staticmethod
    def _ts(v) -> float:
        from datetime import datetime
        try:
            d = datetime.fromisoformat(str(v))
            return d.timestamp()
        except (TypeError, ValueError):
            return 0.0

    def notifications(self, user: dict | None = None) -> dict:
        import hashlib
        import time as _t
        from .users import can
        from .util import in_quiet_hours
        user = user or self.owner_user()
        rt, cfg = self.rt, self.rt.cfg
        items: list[dict] = []
        add = lambda **k: items.append(k)                                  # noqa: E731
        if can(user, "approve"):
            for a in [x for x in rt.asks.pending() if self.can_see_ask(user, x)]:
                add(id=f"ask:{a.id}", kind="approval", tone="danger" if a.level == "red" else "warning",
                    title=a.summary, detail=f"{a.id} · from {rt.name_of(a.requester)}",
                    ts=self._ts(a.doc.meta.get("created")), link={"view": "approvals"})
            for t in rt.tasks.all():
                if t.status == "review" and self.can_see_task(user, t):
                    add(id=f"review:{t.id}:{t.doc.meta.get('status_since', '')}", kind="review", tone="warning",
                        title=f"{t.id} is ready for review", detail=f"{t.title} · {rt.name_of(t.owner)}",
                        ts=self._ts(t.doc.meta.get("status_since")), link={"task": t.id})
        if user["role"] == "owner":
            for t in self._blocked_on_owner():
                add(id=f"blocked:{t.id}:{t.doc.meta.get('status_since', '')}", kind="blocked", tone="danger",
                    title=f"{rt.name_of(t.owner)} is blocked on you", detail=f"{t.id} · {t.blocked_on}",
                    ts=self._ts(t.doc.meta.get("status_since")), link={"task": t.id})
        # mentions of you, and replies to what you wrote (last 7 days, rooms you can see)
        first = user["name"].split()[0].lower() if user["name"].split() else ""
        handles = {user["id"], first, re.sub(r"[^a-z0-9]+", "_", user["name"].lower()).strip("_")}
        pat = re.compile(r"(?<![\w@])@(" + "|".join(re.escape(x) for x in handles if len(x) >= 2) + r")\b", re.I) \
            if first else None                                  # a real @mention, not just your name in a sentence
        cut = _t.time() - 7 * 86400
        for room in self.hub.rooms():
            if not self.can_see_room(user, room):
                continue
            recent = [x for x in self.chat.since(room, -1, limit=300) if x.get("ts", 0) >= cut]
            mine = {x["i"] for x in recent if x.get("who") == user["id"]}
            for x in recent:
                if x.get("who") == user["id"] or x.get("kind") == "system":
                    continue
                replied = x.get("reply_to") in mine
                shared = room in (TEAM_ROOM, BACKCHANNEL) or room.startswith(PROJECT_ROOM)   # a 1:1 is all "to you"
                if replied or (shared and pat and pat.search(str(x.get("text", "")))):
                    add(id=f"msg:{room}:{x['i']}", kind="reply" if replied else "mention", tone="accent",
                        title=f"{rt.name_of(x.get('who', ''))} {'replied to you' if replied else 'mentioned you'}",
                        detail=" ".join(str(x.get("text", "")).split())[:160], ts=x.get("ts", 0),
                        link={"room": room, "i": x["i"]})
        if can(user, "admin"):
            hb = rt.ws.state().get("heartbeat", {})
            for mid, v in hb.items():
                if v.get("error") and cfg.member(mid):
                    add(id=f"model:{mid}:{hashlib.sha1(v['error'].encode()).hexdigest()[:8]}", kind="model",
                        tone="danger", title=f"{rt.name_of(mid)}'s AI model is failing", detail=v["error"][:160],
                        ts=self._ts(v.get("last")), link={"view": "settings", "tab": "models"})
            st = rt.ws.state()
            for key, label in (("push_error", "The team repo can't push"), ("commit_error", "Team repo commits fail")):
                if st.get(key):
                    add(id=f"sys:{key}:{hashlib.sha1(str(st[key]).encode()).hexdigest()[:8]}", kind="system",
                        tone="danger", title=label, detail=str(st[key])[:160], ts=_t.time(), link={"view": "activity"})
            for s in (st.get("stuck") or [])[-3:]:
                add(id=f"stuck:{s.get('member')}:{s.get('at')}", kind="system", tone="warning",
                    title=f"{rt.name_of(s.get('member', ''))} got stuck and was stopped", detail=s.get("room", ""),
                    ts=self._ts(s.get("at")), link={"view": "activity"})
        for p in sorted((rt.ws.root / "reports").glob("*.md"), reverse=True)[:2]:
            if p.stat().st_mtime >= cut:
                add(id=f"report:{p.name}", kind="report", tone="", title=f"Report {p.stem} is ready",
                    detail="The daily status report", ts=p.stat().st_mtime, link={"view": "reports"})
        read = set((rt.ws.state().get("notif_read") or {}).get(user["id"]) or [])
        items.sort(key=lambda x: -(x.get("ts") or 0))
        for it in items:
            it["read"] = it["id"] in read
        try:
            quiet = in_quiet_hours(cfg.timezone, cfg.quiet_hours)
        except Exception:  # noqa: BLE001
            quiet = False
        return {"items": items[:80], "unread": sum(1 for x in items if not x["read"]), "quiet": quiet,
                "quiet_hours": list(cfg.quiet_hours or [])}

    def notifications_read(self, b: dict, user: dict | None = None) -> dict:
        user = user or self.owner_user()
        ids = [x["id"] for x in self.notifications(user)["items"]] if b.get("all") else [str(x) for x in b.get("ids") or []]

        def fn(s):
            per = s.setdefault("notif_read", {})
            per[user["id"]] = (list(per.get(user["id"]) or []) + [i for i in ids if i not in (per.get(user["id"]) or [])])[-2000:]
        self.rt.ws.update_state(fn)
        return self.notifications(user)

    # -- system health ------------------------------------------------------------------------
    def health(self) -> dict:
        import shutil
        import time as _t
        rt, cfg = self.rt, self.rt.cfg
        st = rt.ws.state()
        checks = []
        ok = lambda name, good, detail, fix="": checks.append({"name": name, "ok": bool(good), "detail": detail,   # noqa: E731
                                                               "fix": "" if good else fix})
        t0 = _t.monotonic()
        try:
            async def _noop():
                return True
            self.submit(_noop(), timeout=3)
            ok("Team engine", True, f"responding ({int((_t.monotonic() - t0) * 1000)} ms)")
        except Exception as e:  # noqa: BLE001
            ok("Team engine", False, f"not responding: {e}", "Restart `jm run`.")
        running = bool(self.sched and self.sched._task and not self.sched._task.done())
        sched = st.get("sched") or {}
        ok("Scheduler", running, f"last checks {sched.get('checks', 'never')} · report {sched.get('report') or 'not yet'}",
           "Save Settings (it restarts the team) or restart `jm run`.")
        try:
            last = rt.ws._git("log", "-1", "--format=%cI").strip()
        except RuntimeError:
            last = ""
        ok("Team repo (git)", not st.get("commit_error"), f"last commit {last or 'none yet'}"
           + (f" · push: {st['push_error'][:120]}" if st.get("push_error") else ""),
           f"Commits fail: {st.get('commit_error', '')[:160]}")
        free = shutil.disk_usage(rt.ws.root).free
        ok("Disk space", free > 1_000_000_000, f"{free / 1e9:.1f} GB free", "Free some disk space — git and chat need it.")
        failing = {mid: v for mid, v in (st.get("heartbeat") or {}).items() if v.get("error") and cfg.member(mid)}
        ok("AI models", not failing, "all answering" if not failing else
           "; ".join(f"{rt.name_of(k)}: {v['error'][:80]}" for k, v in failing.items()), "Settings → Models.")
        ok("Telegram", bool(self.gw) or not cfg.monitor.bot_token,
           "connected" if self.gw else ("not set up" if not cfg.monitor.bot_token else self.telegram_error or "off"),
           "Settings → Telegram.")
        ok("Slack", bool(self.slack) or not cfg.slack.configured,
           "connected" if self.slack else ("not set up" if not cfg.slack.configured else self.slack_error or "off"),
           "Settings → Slack.")
        gh = st.get("github") or {}
        if cfg.github.enabled:
            ok("GitHub sync", not gh.get("error"), f"last sync {gh.get('last_sync') or 'never'}", gh.get("error", ""))
        now_ = _t.time()
        turns = [{"member": k, "name": rt.name_of(k), "room": v.get("room"), "seconds": int(now_ - v.get("since", now_))}
                 for k, v in dict(rt.turns).items()]
        slow = [x for x in turns if x["seconds"] > 300]
        ok("Replies in progress", not slow, f"{len(turns)} thinking now" + (
            f" · slow: {', '.join(x['name'] + ' ' + str(x['seconds'] // 60) + ' min' for x in slow)}" if slow else ""),
           "A turn over 9 minutes is stopped automatically.")
        q = rt.queued()
        ok("Queue", len(q) < 20, f"{len(q)} message(s) waiting for paused or over-budget people",
           "Resume people or raise the budget.")
        late = [a for a in rt.asks.pending() if a.deadline() and a.deadline().timestamp() < now_]
        ok("Approvals", not late, f"{len(rt.asks.pending())} pending" + (f", {len(late)} past their deadline" if late else ""),
           "Decide them on the Approvals page.")
        return {"ok": all(c["ok"] for c in checks), "checks": checks, "turns": turns,
                "stuck": (st.get("stuck") or [])[-10:], "version": __import__("james_monitoring").__version__}

    # -- Agent Studio -------------------------------------------------------------------------
    def studio_templates(self) -> dict:
        from .studio import SKILLS, TEMPLATES
        return {"templates": TEMPLATES, "skills": [{"id": k, **v} for k, v in SKILLS.items()],
                "departments": [{"id": d.id, "name": d.name} for d in self.rt.cfg.departments.values()]}

    def studio_try(self, b: dict) -> dict:
        """A throwaway conversation with a draft teammate: nothing is saved and no action runs."""
        from .config import parse_llm
        from .prompts import build_system
        from .runtime import parse_reply
        from .studio import persona_md
        cfg, rt = self.rt.cfg, self.rt
        persona = str(b.get("persona") or "") or persona_md(b)
        name, role = str(b.get("name") or "New teammate"), str(b.get("role") or "Teammate")
        conf = parse_llm({k: v for k, v in (b.get("llm") or {}).items() if v}, {
            "provider": cfg.llm.provider, "model": cfg.llm.model, "base_url": cfg.llm.base_url,
            "api_key_env": cfg.llm.api_key_env, "allow_free": cfg.llm.allow_free}) if (b.get("llm") or {}).get("provider") else cfg.llm
        system = build_system(company=cfg.company, today=today(cfg.timezone).isoformat(), charter=rt.ws.charter(),
                              persona=persona, memory="", member_name=name, member_role=role, roster=rt.roster(),
                              context="(This is a trial conversation before hiring — nothing you do is saved. "
                                      "Answer as you would on the job.)", owner_name=cfg.owner_name, can_run_code=False,
                              channel=f"a trial chat with {cfg.owner_name}")
        msgs = [{"role": x.get("role", "user"), "content": str(x.get("content", ""))} for x in (b.get("history") or [])
                if x.get("role") in ("user", "assistant")][-8:]
        msgs.append({"role": "user", "content": f"[trial chat from {cfg.owner_name}] {b.get('message', '')}"})
        try:
            res = make_llm(conf).complete(system, msgs)
        except LLMError as e:
            return {"ok": False, "error": str(e)[:300]}
        p = parse_reply(res.text)
        return {"ok": True, "reply": p.reply or res.text[:2000], "raw": res.text[:4000],
                "would_do": [rt.describe(cfg.monitor, a) for a in p.actions if a.get("type")],
                "tokens": res.total_tokens}

    def studio_hire(self, b: dict) -> dict:
        from .studio import persona_md
        name, role = " ".join(str(b.get("name") or "").split()), " ".join(str(b.get("role") or "").split())
        cfg = self.rt.cfg
        llm = b.get("llm") or {}                                    # check everything before anything is written
        if llm.get("provider"):
            if llm["provider"] not in PROVIDERS:
                raise ValueError(f"Unknown AI provider {llm['provider']}.")
            if llm["provider"] == "opencode" and "/" not in str(llm.get("model") or ""):
                raise ValueError("Pick an OpenCode model (provider/model).")
        if b.get("department") and b["department"] not in cfg.departments:
            if not b.get("department_name"):
                raise ValueError(f"There's no department {b['department']}.")
            b["department"] = self.departments_save({"name": b["department_name"]})["id"]   # new: create it
            cfg = self.rt.cfg
        bad = [p for p in (b.get("projects") or []) if p not in cfg.projects]
        if bad:
            raise ValueError(f"There's no project {', '.join(bad)}.")
        if b.get("assistant") and cfg.assistants:
            raise ValueError(f"You already have a personal assistant ({cfg.assistants[0].name}).")
        r = self.admin.add(name=name, role=role, token="",
                           projects=[] if b.get("assistant") else list(b.get("projects") or []))
        mid = r["id"]
        try:
            return self._studio_finish(mid, name, role, b, llm)
        except Exception:
            try:                                                    # never leave half a teammate behind
                self.admin.remove(mid)
            finally:
                self.load()
            raise

    def _studio_finish(self, mid: str, name: str, role: str, b: dict, llm: dict) -> dict:
        from .studio import persona_md
        with path_lock(self.cfg_path):
            raw = self.raw()
            for m in raw.get("team", []):
                if str(m.get("id")) == mid:
                    if b.get("department"):
                        m["department"] = str(b["department"])
                    if b.get("assistant"):
                        m["assistant"] = True
                    if llm.get("provider"):
                        m["llm"] = {k: str(llm[k]) for k in ("provider", "model", "base_url") if llm.get(k)}
                        if llm.get("allow_free"):
                            m["llm"]["allow_free"] = True
                    perms = {k: v for k, v in (b.get("permissions") or {}).items() if k in ACTION_TYPES and v in LEVELS}
                    if "code" not in (b.get("skills") or []):
                        perms.setdefault("run_code", "red")
                    if perms:
                        m["permissions"] = perms
            self.save_raw(raw)
        persona = str(b.get("persona") or "").strip() or persona_md({**b, "name": name, "role": role})
        self.load()
        self.rt.ws.write(f"team/{mid}/persona.md", persona.rstrip() + "\n")
        self.rt.ws.commit(f"{mid}: hired from the Agent Studio ({role})", author=self.rt.cfg.owner_name)
        return {"id": mid, "name": name}

    # -- departments and clients -----------------------------------------------------------------
    @config_txn
    def departments_save(self, b: dict) -> dict:
        raw = self.raw()
        deps = raw.setdefault("departments", {})
        act = str(b.get("action") or "save")
        did = slug(str(b.get("id") or b.get("name") or ""))
        if not did:
            raise ValueError("Give the department a name.")
        if act == "delete":
            deps.pop(did, None)
            for m in raw.get("team", []):
                if m.get("department") == did:
                    m.pop("department", None)
        else:
            d = deps.setdefault(did, {})
            if b.get("name"):
                d["name"] = " ".join(str(b["name"]).split())
            if "head" in b:
                d["head"] = str(b.get("head") or "")
            if isinstance(b.get("members"), list):
                want = {str(x) for x in b["members"]}
                for m in raw.get("team", []):
                    if str(m.get("id")) in want:
                        m["department"] = did
                    elif m.get("department") == did:
                        m.pop("department", None)
                if d.get("head") and d["head"] not in want:
                    want.add(d["head"])
                    for m in raw.get("team", []):
                        if str(m.get("id")) == d["head"]:
                            m["department"] = did
        if not deps:
            raw.pop("departments", None)
        self.save_raw(raw)
        self.load()
        return {"id": did}

    @config_txn
    def clients_save(self, b: dict) -> dict:
        raw = self.raw()
        cl = raw.setdefault("clients", {})
        act = str(b.get("action") or "save")
        cid = slug(str(b.get("id") or b.get("name") or ""))
        if not cid:
            raise ValueError("Give the client a name.")
        if act == "delete":
            if any(u.get("client") == cid for u in raw.get("users") or []):
                raise ValueError("Remove this client's sign-in links first (Settings → Sign-in links).")
            cl.pop(cid, None)
            for p in (raw.get("projects") or {}).values():
                if isinstance(p, dict) and p.get("client") == cid:
                    p.pop("client", None)
        else:
            c = cl.setdefault(cid, {})
            for k in ("name", "contact", "notes"):
                if k in b:
                    c[k] = str(b.get(k) or "").strip()
            if isinstance(b.get("projects"), list):
                want = {str(x) for x in b["projects"]}
                for pid, p in (raw.get("projects") or {}).items():
                    if not isinstance(p, dict):
                        continue
                    if pid in want:
                        p["client"] = cid
                    elif p.get("client") == cid:
                        p.pop("client", None)
        if not cl:
            raw.pop("clients", None)
        self.save_raw(raw)
        self.load()
        return {"id": cid}

    def clients(self) -> list[dict]:
        cfg = self.rt.cfg
        out = []
        for c in cfg.clients.values():
            ps = [self._project_json(p) for p in cfg.projects.values() if p.client == c.id]
            tot = sum(p["counts"]["total"] for p in ps)
            done = sum(p["counts"]["done"] for p in ps)
            out.append({"id": c.id, "name": c.name, "contact": c.contact, "notes": c.notes, "projects": ps,
                        "progress": round(100 * done / tot) if tot else 0,
                        "open": sum(p["counts"]["open"] for p in ps), "blocked": sum(p["counts"]["blocked"] for p in ps),
                        "users": [u.name for u in cfg.users if u.client == c.id]})
        return out

    def client_report(self, cid: str) -> dict:
        """A status report for a client, from the files (no model call) — only their projects, nothing internal
        (no owners' names on blockers, no chat, no costs)."""
        from datetime import timedelta
        cfg, tz = self.rt.cfg, self.rt.cfg.timezone
        c = cfg.clients.get(cid)
        if not c:
            raise ValueError(f"no client {cid}")
        d = today(tz)
        week = (d - timedelta(days=7)).isoformat()
        L = [f"# {c.name} — status {d.isoformat()}", "", f"Prepared by {cfg.company}.", ""]
        ps = [p for p in cfg.projects.values() if p.client == cid]
        if not ps:
            L.append("No projects yet.")
        for p in ps:
            ts = [t for t in self.rt.tasks.all() if t.project == p.id and t.status != "cut"]
            done = [t for t in ts if t.status == "done"]
            pct = round(100 * len(done) / len(ts)) if ts else 0
            L += [f"## {p.name or p.id}", f"Status: {p.status} · {len(done)}/{len(ts)} done ({pct}%)", ""]
            if p.description:
                L += [p.description, ""]
            recent = [t for t in done if str(t.doc.meta.get("done_on", "")) >= week]
            L += ["**Done this week**"] + ([f"- {t.title}" for t in recent] or ["- nothing yet"]) + [""]
            doing = [t for t in ts if t.status in ("doing", "review")]
            L += ["**In progress**"] + ([f"- {t.title}" + (f" (due {t.doc.meta.get('due')})" if t.doc.meta.get("due") else "")
                                        for t in doing] or ["- nothing right now"]) + [""]
            nxt = sorted([t for t in ts if t.status == "todo"], key=lambda t: (t.priority, str(t.doc.meta.get("due") or "9")))
            L += ["**Next up**"] + ([f"- {t.title}" for t in nxt[:5]] or ["- to be planned"]) + [""]
            blk = [t for t in ts if t.status == "blocked"]
            if blk:
                L += ["**Waiting on something**"] + [f"- {t.title}" for t in blk] + [""]
        return {"id": cid, "name": c.name, "text": "\n".join(L).rstrip() + "\n"}

    def portal(self, user: dict) -> dict:
        """What a client sees: their projects and progress, read only."""
        cfg = self.rt.cfg
        cid = user.get("client", "")
        c = cfg.clients.get(cid)
        if not c:
            raise ValueError("Your sign-in isn't linked to a client — ask your contact for a new link.")
        projects = []
        for p in cfg.projects.values():
            if p.client != cid:
                continue
            ts = [t for t in self.rt.tasks.all() if t.project == p.id and t.status != "cut"]
            projects.append({"id": p.id, "name": p.name or p.id, "description": p.description, "status": p.status,
                             "counts": self._project_json(p)["counts"],
                             "tasks": [{"title": t.title, "status": t.status, "due": t.doc.meta.get("due") or "",
                                        "done_on": t.doc.meta.get("done_on", "")} for t in ts]})
        return {"client": {"id": c.id, "name": c.name}, "company": cfg.company, "projects": projects,
                "report": self.client_report(cid)["text"]}

    # -- personal assistant --------------------------------------------------------------------
    def reminders(self) -> list[dict]:
        from .assistant import Reminders
        return Reminders(self.rt).all()

    def reminders_save(self, b: dict) -> list[dict]:
        from .assistant import Reminders
        r = Reminders(self.rt)
        if b.get("action") == "delete":
            r.delete(str(b.get("id", "")))
        else:
            who = str(b.get("member") or "") or (self.rt.cfg.assistants[0].id if self.rt.cfg.assistants else "")
            if not who or not self.rt.cfg.member(who):
                raise ValueError("Reminders come from the personal assistant — add one in the Agent Studio first.")
            r.add(who, str(b.get("at") or ""), str(b.get("text") or ""))
        return r.all()

    # -- chat ------------------------------------------------------------------------------
    def chat_since(self, room: str, after: int) -> dict:
        return {"messages": self.chat.since(room, after), "thinking": self.pending.get(room, 0)}

    def chat_send(self, room: str, text: str, user: dict | None = None, reply_to=None, files=None) -> dict:
        user = user or self.owner_user()
        if not self.can_write_room(user, room):
            raise PermissionError("You can read this room but not write in it.")
        saved = []
        if files:
            from .files import save
            if not isinstance(files, list) or len(files) > 10:
                raise ValueError("Attach up to 10 files at a time.")
            self.hub.check_room(room)
            for f in files:
                saved.append(save(self.rt.ws.root, room, str(f.get("name") or "file"), base64.b64decode(f.get("data") or "")))
            self.rt.ws.commit(f"files for {room}: " + ", ".join(x["name"] for x in saved), author=user["name"])
        who = "" if user["role"] == "owner" else user["id"]
        rt_ = int(reply_to) if reply_to not in (None, "") else None
        msg = self.hub.receive(room, text, via="console", who=who, reply_to=rt_,   # shown at once; raises for a bad room
                               files=saved or None)
        self.submit(self.hub.handle(room, msg, via="console"), timeout=None)
        return {"ok": True, "i": msg["i"]}

    def chat_thread(self, room: str, root: int) -> dict:
        return {"messages": self.chat.thread(room, int(root))}

    def rooms(self) -> list[dict]:
        """Every room with its latest message — the chat sidebar in one call."""
        out = []
        for r in self.hub.rooms():
            last = self.chat.since(r, max(-1, self.chat.last_index(r) - 1))[-1:] if self.chat.last_index(r) >= 0 else []
            out.append({"room": r, "last": last[0] if last else None, "count": self.chat.last_index(r) + 1})
        return out

    # -- tasks -----------------------------------------------------------------------------
    def task_create(self, b: dict, user: dict | None = None) -> dict:
        user = user or self.owner_user()
        rt = self.rt
        title = " ".join(str(b.get("title") or "").split())
        if not title:
            raise ValueError("Give the task a title.")
        owner = str(b.get("owner") or "").lower()
        if not owner or not rt.cfg.member(owner):
            raise ValueError("Pick who owns this task.")
        if rt.cfg.member(owner).assistant and user["role"] != "owner":
            raise PermissionError("That's the founder's personal assistant — pick someone on the team.")
        pid = str(b.get("project") or "")
        if pid and pid not in rt.cfg.projects:
            raise ValueError(f"There's no project {pid}.")
        if pid and pid in rt.cfg.projects and owner not in [m.id for m in rt.cfg.project_members(pid)]:
            if user["role"] not in ("owner", "admin"):
                raise PermissionError(f"{rt.cfg.member(owner).name} isn't on {rt.cfg.projects[pid].name or pid} — "
                                      f"an admin adds people to projects.")
            self.project_members(pid, [m.id for m in rt.cfg.project_members(pid)] + [owner])
            rt = self.rt
        dm = [x.strip() for x in (b.get("done_means") or []) if str(x).strip()]
        by = rt.owner_id if user["role"] in ("owner", "admin") else user["id"]
        t = rt.tasks.create(title=title, owner=owner,
                            created_by=by, priority=str(b.get("priority") or "P1"),
                            due=str(b.get("due") or "") or None, project=str(b.get("project") or ""),
                            done_means=dm, description=str(b.get("description") or ""))
        rt.ws.commit(f"assign {t.id} to {owner}" + (f" (by {user['name']})" if user["role"] != "owner" else ""),
                     author=user["name"])
        if b.get("notify", True):
            self.submit(ack_assignment(rt, owner, t.line(rt.cfg.timezone)), timeout=None)
        return self._task_json(t)

    def task_action(self, b: dict, user: dict | None = None) -> dict:
        user = user or self.owner_user()
        rt, tid, act = self.rt, str(b.get("id", "")), str(b.get("action", ""))
        by = rt.owner_id if user["role"] in ("owner", "admin") else user["id"]
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
            fields = {k: b.get(k) for k in ("priority", "due", "title", "owner", "project", "reviewer") if b.get(k)}
            if "depends_on" in b:
                fields["depends_on"] = b.get("depends_on") or []
            if "due" in b and not b.get("due"):
                rt.tasks.clear_due(tid, by)
            if "owner" in fields and not rt.cfg.member(str(fields["owner"])):
                raise ValueError(f"There's nobody called {fields['owner']} on the team.")
            if "owner" in fields and rt.cfg.member(str(fields["owner"])).assistant and user["role"] != "owner":
                raise PermissionError("That's the founder's personal assistant — pick someone on the team.")
            if "reviewer" in fields and fields["reviewer"] != rt.owner_id and not rt.cfg.member(str(fields["reviewer"])):
                raise ValueError("The reviewer must be you or someone on the team.")
            if "project" in fields and fields["project"] not in rt.cfg.projects:
                raise ValueError(f"There's no project {fields['project']}.")
            before = rt.tasks.get(tid).owner
            t, msg = rt.tasks.update_fields(tid, by, **fields), ""
            if t.owner != before:                          # reassigned: the new owner is told, and the old one
                new_name = rt.cfg.member(t.owner).name
                self.submit(ack_assignment(rt, t.owner, t.line(rt.cfg.timezone)), timeout=None)
                if rt.cfg.member(before):
                    self.submit(rt.bus.send_owner(before, f"↪ {t.id} moved to {new_name} — put anything useful "
                                                          f"in the task log for them."), timeout=None)
                msg = f"{t.id} reassigned to {new_name} — they've been told."
        else:
            raise ValueError(f"unknown action {act}")
        rt.ws.commit(f"{tid}: {act} by {user['name']}", author=user["name"])
        return {**self._task_json(t), "message": msg if isinstance(msg, str) else ""}

    def pause(self, who: str, pause: bool) -> dict:
        msg = self.rt.set_paused(who, pause)
        if not pause:
            n = self.submit(self.rt.drain_queue(), timeout=30)
            if n:
                msg += f" · {n} queued message{'s' if n != 1 else ''} being answered"
        return {"message": msg}

    def decide(self, ask_id: str, decision: str, note: str = "", user: dict | None = None) -> dict:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        by = (user or self.owner_user())["name"]
        return {"result": self.submit(self.rt.decide_ask(ask_id, decision, by=by, note=note, via="console"))}

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
        was_paused = p.get("status") == "paused"
        if "status" in b and str(b["status"]) not in ("active", "paused", "done"):
            raise ValueError("Status is active, paused or done.")
        for k in ("name", "description", "lead", "repo", "status"):
            if k in b:
                p[k] = str(b[k]).strip()
        if "client" in b:
            if b.get("client"):
                p["client"] = str(b["client"])
            else:
                p.pop("client", None)
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
        if was_paused and p.get("status") != "paused":
            self.submit(self.rt.drain_queue(), timeout=30)       # resumed: what waited is answered now
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
                if "department" in b:
                    if b.get("department"):
                        m["department"] = str(b["department"])
                    else:
                        m.pop("department", None)
                if "assistant" in b:
                    if b.get("assistant"):
                        m["assistant"] = True
                        m.pop("projects", None)                       # private: not on company projects
                    else:
                        m.pop("assistant", None)
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
# Every route and the permission it needs (see users.py). Anything not listed is refused.
GET_PERMS = {
    "/api/state": "any", "/api/dashboard": "read", "/api/tasks": "read", "/api/task": "read", "/api/asks": "read",
    "/api/chat": "read", "/api/rooms": "read", "/api/pulse": "read", "/api/thread": "read", "/api/member": "read",
    "/api/project": "read", "/api/team": "read", "/api/decisions": "read", "/api/reports": "read",
    "/api/report": "read", "/api/budget": "read", "/api/search": "read", "/api/memory": "read",
    "/api/notifications": "read", "/api/clients": "read", "/api/doc": "read", "/api/project/settings": "admin",
    "/api/file": "read", "/api/playbook": "read", "/api/client_report": "read",
    "/api/portal": "portal", "/api/github/status": "admin", "/api/models/catalog": "admin",
    "/api/models/team": "admin", "/api/models/health": "read", "/api/connections": "admin",
    "/api/opencode/models": "admin", "/api/settings": "admin", "/api/audit": "admin", "/api/audit.csv": "admin",
    "/api/health": "admin", "/api/studio/templates": "admin", "/api/reminders": "owner", "/api/users": "owner",
    "/api/model_login": "admin", "/api/connections/login": "admin", "/api/public": "owner",
}
POST_PERMS = {
    "/api/chat": "chat", "/api/tasks": "task", "/api/task": "task", "/api/ask": "approve", "/api/project/settings": "admin",
    "/api/project/pause": "admin", "/api/public": "owner", "/api/playbook": "admin",
    "/api/notifications/read": "read", "/api/projects": "admin", "/api/project/members": "admin",
    "/api/member": "admin", "/api/decisions": "admin", "/api/settings": "admin", "/api/upload": "admin",
    "/api/upload_folder": "admin", "/api/token": "admin", "/api/links": "admin", "/api/member_status": "admin",
    "/api/add": "admin", "/api/remove": "admin", "/api/report/run": "admin", "/api/work": "admin",
    "/api/pause": "admin", "/api/telegram/manager": "admin", "/api/telegram/detect": "admin",
    "/api/telegram/code": "admin", "/api/github/connect": "admin", "/api/models/assign": "admin",
    "/api/models/default": "admin", "/api/models/sessions/reset": "admin", "/api/connections/test": "admin",
    "/api/github/sync": "admin", "/api/slack/code": "admin", "/api/slack/connect": "admin",
    "/api/slack/provision": "admin", "/api/slack/detect": "admin", "/api/slack/email": "admin",
    "/api/restart": "admin", "/api/memory": "admin", "/api/studio/try": "admin", "/api/studio/hire": "admin",
    "/api/departments": "admin", "/api/clients": "admin", "/api/reminders": "owner", "/api/users": "owner",
    # before and after setup: models and keys (before setup only the founder's key exists)
    "/api/setup": "owner", "/api/test_model": "admin", "/api/model_check": "admin", "/api/model_login": "admin",
    "/api/connections/login": "admin", "/api/model_login/input": "admin", "/api/model_login/cancel": "admin",
    "/api/model_key": "admin",
}
# Console actions that are recorded in the audit log by the generic hook (decisions are recorded by the runtime,
# chat is its own record; probes and codes that change nothing — or carry secrets — are not recorded).
NO_AUDIT = {"/api/chat", "/api/ask", "/api/token", "/api/links", "/api/member_status", "/api/model_check",
            "/api/test_model", "/api/connections/test", "/api/model_login/input", "/api/slack/code",
            "/api/telegram/code", "/api/notifications/read", "/api/studio/try", "/api/upload", "/api/upload_folder",
            "/api/telegram/detect", "/api/slack/detect"}
AUDIT_LABEL = {"/api/tasks": "create task", "/api/task": "task", "/api/projects": "save project",
               "/api/project/members": "project team", "/api/project/settings": "project settings",
               "/api/project/pause": "pause/resume project", "/api/public": "public link", "/api/playbook": "edit playbook", "/api/member": "edit profile", "/api/decisions": "edit decisions",
               "/api/settings": "change settings", "/api/add": "add person", "/api/remove": "remove person",
               "/api/report/run": "write report", "/api/work": "run work session", "/api/pause": "pause/resume",
               "/api/models/assign": "assign model", "/api/models/default": "use default model",
               "/api/models/sessions/reset": "reset sessions", "/api/memory": "edit memory",
               "/api/studio/hire": "hire (Agent Studio)", "/api/departments": "departments", "/api/clients": "clients",
               "/api/reminders": "reminders", "/api/users": "sign-in links", "/api/model_key": "save API key",
               "/api/model_login": "model login", "/api/connections/login": "model login",
               "/api/github/connect": "connect GitHub", "/api/slack/connect": "connect Slack",
               "/api/telegram/manager": "connect Telegram", "/api/restart": "restart team", "/api/setup": "set up company"}


def make_handler(app: App, key: str):
    page = resources.files("james_monitoring").joinpath("templates", "console.html").read_text()
    from .users import can

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body: bytes, ctype, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def _visitor(self) -> str:
            # behind the tunnel every request comes from 127.0.0.1: Cloudflare says who the visitor is
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]

        def _user(self) -> dict | None:
            who = self._visitor()
            if app.failed_keys.blocked(who):
                return None
            u = app.user_for_key(self.headers.get("X-JM-Key", ""), key)
            if u is None:
                app.failed_keys.fail(who)
            return u

        def _refuse(self):
            if app.failed_keys.blocked(self._visitor()):
                return self._json(429, {"error": "Too many wrong keys — try again in a few minutes."})
            return self._json(403, {"error": "forbidden"})

        def _q(self) -> dict:
            from urllib.parse import parse_qs, urlparse
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def _file(self, q: dict):
            from .files import IMAGE_EXT, room_dir
            room, rel = q.get("room", ""), q.get("path", "")
            root = app.rt.ws.root.resolve()
            p = (root / rel).resolve()
            if not rel.startswith(room_dir(room) + "/") or not p.is_relative_to((root / room_dir(room)).resolve()) \
                    or not p.is_file():
                return self._json(404, {"error": "no such file"})
            ext = p.suffix.lower()
            inline = ext in IMAGE_EXT or ext == ".pdf"             # never inline HTML/SVG: it could run in the page
            ctype = IMAGE_EXT.get(ext) or ("application/pdf" if ext == ".pdf" else "application/octet-stream")
            name = re.sub(r'[^\w. -]', "_", p.name.split("-", 3)[-1])
            return self._send(200, p.read_bytes(), ctype,
                              {"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
                               "Content-Security-Policy": "sandbox"})

        def _deny(self, user, perm) -> bool:
            if perm == "any" or can(user, perm):
                return False
            self._json(403, {"error": "You don't have access to that — ask the founder." if user["role"] != "client"
                             else "That isn't part of your portal."})
            return True

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            user = self._user()
            if not user:
                return self._refuse()
            perm = GET_PERMS.get(path)
            if perm is None:
                return self._json(404, {"error": "not found"})
            if self._deny(user, perm):
                return
            q = self._q()
            try:
                if path == "/api/state":
                    return self._json(200, app.state(user))
                if path in ("/api/model_login", "/api/connections/login"):
                    from . import connections as cx
                    return self._json(200, cx.login_state(q.get("provider", "")))
                if not app.ready:
                    return self._json(409, {"error": "setup needed"})
                room = q.get("room", TEAM_ROOM)
                if path in ("/api/chat", "/api/thread", "/api/file") and not app.can_see_room(user, room):
                    return self._json(403, {"error": "That room is private."})
                if path == "/api/task" and not app.can_see_task(user, app.rt.tasks.get(q.get("id", ""))):
                    return self._json(403, {"error": "That task is private."})
                if path == "/api/memory" and not can(user, "admin"):
                    return self._json(403, {"error": "Memory is visible to the founder and admins."})
                if path in ("/api/member", "/api/memory", "/api/playbook") and q.get("id") in app.private_rooms() and user["role"] != "owner":
                    return self._json(403, {"error": "That profile is private."})
                if path == "/api/client_report" and user["role"] == "client" and q.get("id") != user.get("client"):
                    return self._json(403, {"error": "That isn't part of your portal."})
                if path == "/api/file":                           # an attachment, only for people who see its room
                    return self._file(q)
                if path == "/api/audit.csv":
                    body = app.rt.audit.csv(kind=q.get("kind", ""), who=q.get("who", ""), q=q.get("q", "")).encode()
                    return self._send(200, body, "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="audit-{today(app.rt.cfg.timezone)}.csv"'})
                routes = {
                    "/api/dashboard": lambda: app.dashboard(user),
                    "/api/tasks": lambda: [app._task_json(t) for t in app.rt.tasks.all() if app.can_see_task(user, t)],
                    "/api/task": lambda: app.task_detail(q.get("id", "")),
                    "/api/asks": lambda: [app._ask_json(a) for a in reversed(app.rt.asks.all()) if app.can_see_ask(user, a)][:50],
                    "/api/chat": lambda: app.chat_since(room, int(q.get("after", -1))),
                    "/api/thread": lambda: app.chat_thread(room, int(q.get("root", -1))),
                    "/api/rooms": lambda: [r for r in app.rooms() if app.can_see_room(user, r["room"])],
                    "/api/pulse": lambda: app.pulse(user),
                    "/api/github/status": lambda: app.github_status(),
                    "/api/models/catalog": lambda: app.models_catalog(q.get("refresh") == "1"),
                    "/api/models/team": lambda: app.models_team(),
                    "/api/models/health": lambda: app.models_health(),
                    "/api/connections": lambda: app.connections(),
                    "/api/opencode/models": lambda: __import__("james_monitoring.connections", fromlist=["x"]).opencode_models(),
                    "/api/member": lambda: app.member_detail(q.get("id", ""), user),
                    "/api/project": lambda: app.project_detail(q.get("id", ""), user),
                    "/api/team": lambda: app.team_state(user),
                    "/api/decisions": lambda: {"text": app.rt.ws.read("decisions/OPEN.md")},
                    "/api/reports": lambda: sorted((p.name for p in (app.rt.ws.root / "reports").glob("*.md")),
                                                   reverse=True),
                    "/api/report": lambda: {"text": app.rt.ws.read(f"reports/{Path(q.get('name', '')).name}")},
                    "/api/settings": lambda: app.settings(),
                    "/api/budget": lambda: app.rt.cmd_budget(),
                    "/api/search": lambda: app.search(q.get("q", ""), user),
                    "/api/memory": lambda: app.memory_get(q.get("id", "")),
                    "/api/doc": lambda: app.doc(q.get("path", "")),
                    "/api/playbook": lambda: app.playbook_get(q.get("id", "")),
                    "/api/project/settings": lambda: app.project_settings(q.get("id", "")),
                    "/api/notifications": lambda: app.notifications(user),
                    "/api/audit": lambda: app.audit_list(q),
                    "/api/health": lambda: app.health(),
                    "/api/studio/templates": lambda: app.studio_templates(),
                    "/api/clients": lambda: app.clients(),
                    "/api/client_report": lambda: app.client_report(q.get("id", "")),
                    "/api/portal": lambda: app.portal(user),
                    "/api/reminders": lambda: app.reminders(),
                    "/api/users": lambda: app.users_list(),
                    "/api/public": lambda: app.public_status(),
                }
                return self._json(200, routes[path]())
            except PermissionError as e:
                return self._json(403, {"error": str(e)})
            except (ValueError, TaskError, AskError, KeyError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("GET %s", path)
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            user = self._user()
            if not user:
                return self._refuse()
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
            perm = POST_PERMS.get(path)
            if perm is None:
                return self._json(404, {"error": "not found"})
            if self._deny(user, perm):
                return
            try:
                out = self._post(path, b, user)
                if out is None:
                    return
                if app.ready and path not in NO_AUDIT:
                    target = str(b.get("id") or b.get("member") or b.get("provider") or b.get("who") or "")
                    label = AUDIT_LABEL.get(path, path)
                    if path == "/api/task":
                        label = f"task {b.get('action', '')}".strip()
                    app.rt._audit("console", user["name"], label, target, b, via="console")
                return self._json(200, out)
            except PermissionError as e:
                return self._json(403, {"error": str(e)})
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

        def _post(self, path: str, b: dict, user: dict):
            if path == "/api/setup":
                return app.setup(b)
            if path == "/api/test_model":
                return app.test_model(b)
            if path == "/api/model_check":                 # setup too: is this model ready? (no model call)
                from . import connections as cx
                from .config import parse_llm
                return cx.check(parse_llm({k: v for k, v in b.items() if k in ("provider", "model", "base_url", "allow_free")}))
            if path in ("/api/model_login", "/api/connections/login"):    # sign in (setup and Settings)
                from . import connections as cx
                return cx.login(str(b.get("provider", "")), app.base / ".jm-login",
                                target=str(b.get("target", "") or ""), force=bool(b.get("force")))
            if path == "/api/model_login/input":                  # the pasted code / key, or a key press
                from . import connections as cx
                return cx.login_input(str(b.get("provider", "")), text=str(b.get("text", "")), key=str(b.get("key", "")))
            if path == "/api/model_login/cancel":
                from . import connections as cx
                return cx.login_cancel(str(b.get("provider", "")))
            if path == "/api/model_key":                          # an API key, straight into .env
                env, k = str(b.get("env", "")).strip(), str(b.get("key", "")).strip()
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", env) or not k:
                    raise ValueError("Paste the key (and pick which variable it's for).")
                set_env(app.base / ".env", {env: k})
                return {"saved": env}
            if not app.ready:
                self._json(409, {"error": "setup needed"})
                return None
            a = app.admin
            if path == "/api/task" and not can(user, "approve") and (
                    str(b.get("action")) in ("accept", "cut", "changes", "feedback") or str(b.get("status")) in ("done", "cut")):
                raise PermissionError("Only the founder or an admin can accept, cut, send back or give feedback on work "
                                      "(feedback becomes a binding correction).")
            if path in ("/api/member", "/api/studio/hire") and "assistant" in b and user["role"] != "owner":
                raise PermissionError("Only the founder can set up their personal assistant.")
            if path in ("/api/memory", "/api/playbook") and str(b.get("id", "")) in app.private_rooms() and user["role"] != "owner":
                raise PermissionError("That profile is private.")
            if path in ("/api/task",) and not app.can_see_task(user, app.rt.tasks.get(str(b.get("id", "")))):
                raise PermissionError("That task is private.")
            routes = {
                "/api/chat": lambda: app.chat_send(str(b.get("room", TEAM_ROOM)), str(b.get("text", "")), user,
                                                   b.get("reply_to"), b.get("files")),
                "/api/tasks": lambda: app.task_create(b, user),
                "/api/task": lambda: app.task_action(b, user),
                "/api/ask": lambda: app.decide(str(b.get("id", "")), str(b.get("decision", "")), str(b.get("note", "")),
                                               user),
                "/api/notifications/read": lambda: app.notifications_read(b, user),
                "/api/projects": lambda: app.project_save(b),
                "/api/project/members": lambda: app.project_members(str(b.get("id", "")), list(b.get("members") or [])),
                "/api/member": lambda: app.member_update(b),
                "/api/decisions": lambda: app.decisions_save(str(b.get("text", ""))),
                "/api/settings": lambda: app.settings_save(b),
                "/api/upload": lambda: a.upload(str(b.get("filename", "SKILL.md")), base64.b64decode(b.get("data", ""))),
                "/api/upload_folder": lambda: a.upload_folder({str(f.get("path", "")): base64.b64decode(f.get("data", ""))
                                                               for f in b.get("files") or []}, str(b.get("name", "folder"))),
                "/api/token": lambda: a.check_token(str(b.get("token", "")).strip()),
                "/api/links": lambda: a.check_links(str(b.get("token", "")).strip(), b.get("name", ""), b.get("role", "")),
                "/api/member_status": lambda: a.member_status(str(b.get("id", ""))),
                "/api/add": lambda: (a.add(name=str(b.get("name", "")), role=str(b.get("role", "")),
                                           token=str(b.get("token", "")), projects=list(b.get("projects") or []),
                                           upload_id=str(b.get("upload_id", ""))), app.load())[0],
                "/api/remove": lambda: app.remove_person(str(b.get("id", ""))),
                "/api/project/pause": lambda: app.project_pause(str(b.get("id", "")), bool(b.get("pause"))),
                "/api/report/run": lambda: {"file": app.submit(app.rt.run_daily_report())},
                "/api/work": lambda: {"digest": app.submit(app.rt.run_work_session(), timeout=1800)},
                "/api/pause": lambda: app.pause(str(b.get("who", "all")), bool(b.get("pause"))),
                "/api/telegram/manager": lambda: app.telegram_manager(str(b.get("token", "")).strip()),
                "/api/telegram/detect": lambda: app.telegram_detect(str(b.get("what", "owner"))),
                "/api/telegram/code": lambda: app.telegram_code(),
                "/api/github/connect": lambda: app.github_connect(b),
                "/api/models/assign": lambda: app.models_assign(b),
                "/api/models/default": lambda: app.models_use_default(str(b.get("member", ""))),
                "/api/models/sessions/reset": lambda: {"reset": app.rt.reset_sessions(str(b.get("member", "")),
                                                                                        str(b.get("room", "")))},
                "/api/connections/test": lambda: app.connection_test(b),
                "/api/github/sync": lambda: app.github_sync(),
                "/api/slack/code": lambda: {"code": setattr(app, "_slack_code", f"{secrets.randbelow(900000) + 100000}")
                                           or app._slack_code},
                "/api/slack/connect": lambda: app.slack_connect(str(b.get("bot_token", "")), str(b.get("app_token", ""))),
                "/api/slack/provision": lambda: app.slack_provision(),
                "/api/slack/detect": lambda: app.slack_detect(),
                "/api/slack/email": lambda: app.slack_email(str(b.get("email", ""))),
                "/api/restart": lambda: (app.load(), {"ok": True})[1],
                "/api/memory": lambda: app.memory_edit(b, user["name"]),
                "/api/playbook": lambda: app.playbook_edit(b, user["name"]),
                "/api/studio/try": lambda: app.studio_try(b),
                "/api/studio/hire": lambda: app.studio_hire(b),
                "/api/departments": lambda: app.departments_save(b),
                "/api/project/settings": lambda: app.project_settings_save(b),
                "/api/clients": lambda: app.clients_save(b),
                "/api/reminders": lambda: app.reminders_save(b),
                "/api/users": lambda: app.users_save(b),
                "/api/public": lambda: app.public_set(bool(b.get("on"))),
            }
            return routes[path]()
    return H


def serve(base_dir: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          telegram: bool = True, key: str | None = None, public: bool = False) -> None:
    app = App(base_dir, telegram=telegram)
    app.load()
    key = key or os.environ.get("JM_CONSOLE_KEY") or secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer((host, port), make_handler(app, key))
    app.console_key, app.port = key, srv.server_address[1]
    # A stop from Docker/systemd (SIGTERM) shuts down like Ctrl-C, so the tunnel and the team stop cleanly too.
    import atexit
    import signal
    atexit.register(app.public.stop)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = f"http://{shown}:{srv.server_address[1]}/?k={key}"
    status = "setup needed — finish it in the browser" if not app.ready else (
        f"{app.rt.cfg.company}: {len(app.rt.cfg.team)} people"
        + (", Telegram connected" if app.gw else ", Telegram not connected")
        + (", Slack connected" if app.slack else ""))
    print(f"james-monitoring console: {url}\n{status}\nCtrl-C to stop.", flush=True)   # shows under Docker/systemd too
    if host == "0.0.0.0":
        print("⚠️  Listening on all interfaces. Anyone with the link can control the team — prefer an SSH tunnel.")
    if public or (app.ready and app.rt.cfg.public_auto):
        threading.Thread(target=lambda: _announce_public(app), daemon=True, name="jm-public").start()
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
        app.public.stop()
        if app.ready:
            try:
                app.submit(app._stop_services(app.sched, app.gw, app.slack, drain=True), timeout=90)
            except Exception:  # noqa: BLE001
                pass


def _announce_public(app: App) -> None:
    st = app.public_set(True)
    if st.get("on"):
        print(f"🌐 Public console: {st['url']}/?k={app.console_key}\n   (anyone with this link is you — share sign-in "
              f"links from Settings for others)", flush=True)
    else:
        print(f"⚠️  Public link not started: {st.get('error')}", flush=True)
