"""Configuration: config.yaml (non-secret) + environment / .env (secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass
class Member:
    id: str
    name: str
    role: str
    bot_token_env: str = ""
    monitor: bool = False          # True for James (delivery manager / router)
    projects: list[str] = field(default_factory=list)
    persona_file: str = ""         # relative to workspace; default team/<id>/persona.md

    @property
    def bot_token(self) -> str:
        return os.environ.get(self.bot_token_env, "") if self.bot_token_env else ""


@dataclass
class Project:
    id: str
    repo: str = ""                 # local path of the project's git repo (optional)
    main_branch: str = "main"
    push: bool = False


@dataclass
class LLMConfig:
    provider: str = "anthropic"    # anthropic | openai (any OpenAI-compatible: OpenAI, Ollama, OpenRouter, vLLM) | fake
    model: str = ""
    base_url: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 2000
    temperature: float = 0.3

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


@dataclass
class Config:
    company: str
    timezone: str
    owner_name: str
    owner_user_id: int
    extra_user_ids: list[int]
    llm: LLMConfig
    workspace_path: Path
    workspace_push: bool
    group_chat_id: int
    telegram_api_base: str
    quiet_hours: tuple[str, str] | None
    daily_report: str
    check_every_minutes: int
    work_sessions: list[str]
    stale_days: int
    blocked_escalate_days: int
    ask_default_hours: int
    daily_tokens_per_agent: int
    executor_command: list[str]
    executor_timeout_minutes: int
    max_agent_hops: int
    team: list[Member]
    projects: dict[str, Project]
    path: Path | None = None

    # -- helpers ---------------------------------------------------------
    def member(self, member_id: str) -> Member | None:
        member_id = member_id.lower().lstrip("@")
        for m in self.team:
            if m.id == member_id or m.name.lower() == member_id:
                return m
        return None

    @property
    def monitor(self) -> Member:
        for m in self.team:
            if m.monitor:
                return m
        return self.team[0]

    def is_authorized(self, user_id: int | None) -> bool:
        return user_id is not None and (user_id == self.owner_user_id or user_id in self.extra_user_ids)


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _get(d: dict, path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"Config not found: {path}. Run `jm init` first.")
    load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text()) or {}
    return parse_config(raw, base_dir=path.parent, path=path)


def parse_config(raw: dict, base_dir: Path | None = None, path: Path | None = None) -> Config:
    base_dir = base_dir or Path.cwd()
    team_raw = raw.get("team") or []
    if not team_raw:
        raise ConfigError("config: `team` must list at least one member")
    team: list[Member] = []
    seen: set[str] = set()
    for t in team_raw:
        mid = str(t.get("id") or t.get("name", "")).strip().lower()
        if not mid:
            raise ConfigError("config: every team member needs an `id`")
        if mid in seen or mid == "all":
            raise ConfigError(f"config: duplicate or reserved member id `{mid}`")
        seen.add(mid)
        team.append(Member(
            id=mid,
            name=str(t.get("name") or mid.title()),
            role=str(t.get("role") or ""),
            bot_token_env=str(t.get("bot_token_env") or f"TG_TOKEN_{mid.upper()}"),
            monitor=bool(t.get("monitor", False)),
            projects=[str(p) for p in (t.get("projects") or [])],
            persona_file=str(t.get("persona_file") or ""),
        ))
    if sum(1 for m in team if m.monitor) > 1:
        raise ConfigError("config: only one member can have `monitor: true`")

    projects = {}
    for pid, p in (raw.get("projects") or {}).items():
        p = p or {}
        projects[str(pid)] = Project(id=str(pid), repo=str(p.get("repo") or ""),
                                     main_branch=str(p.get("main_branch") or "main"),
                                     push=bool(p.get("push", False)))

    llm = raw.get("llm") or {}
    provider = str(llm.get("provider") or "anthropic")
    default_key_env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, "")
    qh = _get(raw, "telegram.quiet_hours")
    ws = Path(str(_get(raw, "workspace.path", "./team-workspace"))).expanduser()
    if not ws.is_absolute():
        ws = (base_dir / ws).resolve()

    cmd = _get(raw, "executor.command", [])
    if isinstance(cmd, str):
        import shlex
        cmd = shlex.split(cmd)

    return Config(
        company=str(raw.get("company") or "My Company"),
        timezone=str(raw.get("timezone") or "UTC"),
        owner_name=str(_get(raw, "owner.name", "Owner")),
        owner_user_id=int(_get(raw, "owner.telegram_user_id", 0) or 0),
        extra_user_ids=[int(x) for x in (_get(raw, "owner.extra_user_ids", []) or [])],
        llm=LLMConfig(
            provider=provider,
            model=str(llm.get("model") or ""),
            base_url=str(llm.get("base_url") or ""),
            api_key_env=str(llm.get("api_key_env") or default_key_env),
            max_tokens=int(llm.get("max_tokens") or 2000),
            temperature=float(llm.get("temperature", 0.3)),
        ),
        workspace_path=ws,
        workspace_push=bool(_get(raw, "workspace.push", False)),
        group_chat_id=int(_get(raw, "telegram.group_chat_id", 0) or 0),
        telegram_api_base=str(_get(raw, "telegram.api_base_url", "") or "").rstrip("/"),
        quiet_hours=(str(qh[0]), str(qh[1])) if qh and len(qh) == 2 else None,
        daily_report=str(_get(raw, "monitor.daily_report", "18:54")),
        check_every_minutes=int(_get(raw, "monitor.check_every_minutes", 60)),
        work_sessions=[str(x) for x in (_get(raw, "monitor.work_sessions", []) or [])],
        stale_days=int(_get(raw, "monitor.stale_days", 7)),
        blocked_escalate_days=int(_get(raw, "monitor.blocked_escalate_days", 2)),
        ask_default_hours=int(_get(raw, "monitor.ask_default_hours", 24)),
        daily_tokens_per_agent=int(_get(raw, "budget.daily_tokens_per_agent", 300000)),
        executor_command=[str(c) for c in cmd],
        executor_timeout_minutes=int(_get(raw, "executor.timeout_minutes", 30)),
        max_agent_hops=int(_get(raw, "limits.max_agent_hops", 4)),
        team=team,
        projects=projects,
        path=path,
    )
