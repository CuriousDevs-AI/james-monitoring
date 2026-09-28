"""Configuration: config.yaml (non-secret) + environment / .env (secrets)."""
from __future__ import annotations

import os
import re
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
    monitor: bool = False          # True for the manager who monitors and routes (e.g. "James")
    projects: list[str] = field(default_factory=list)
    persona_file: str = ""         # relative to workspace; default team/<id>/persona.md
    llm: "LLMConfig | None" = None  # this person's own model (e.g. Marcus on Codex, Sofia on Claude); None = company default
    permissions: dict[str, str] = field(default_factory=dict)   # per-person overrides of Config.permissions

    @property
    def bot_token(self) -> str:
        return os.environ.get(self.bot_token_env, "") if self.bot_token_env else ""


@dataclass
class Project:
    id: str
    repo: str = ""                 # local path of the project's git repo (optional)
    main_branch: str = "main"
    push: bool = False
    name: str = ""
    description: str = ""
    lead: str = ""                 # member id
    status: str = "active"         # active | paused | done
    telegram_chat_id: int = 0      # optional Telegram group for this project's room
    channel: str = ""              # telegram | slack | console — where this project's room lives ("" = company default)


@dataclass
class LLMConfig:
    provider: str = "anthropic"    # anthropic | openai (any OpenAI-compatible: OpenAI, Ollama, OpenRouter, vLLM) | fake
    model: str = ""
    base_url: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 8000
    temperature: float = 0.3
    allow_free: bool = False       # OpenCode free models (opencode/…): opt-in, they run OpenCode's own agent

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


CLI_PROVIDERS = ("claude-code", "claude_code", "claude-cli", "subscription", "codex-cli", "codex_cli", "opencode",
                 "open-code", "fake")
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
           "codex": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}

# What an agent may do on its own. green = just do it · yellow = do it and tell the owner · red = ask first
# (the action waits as an Approve/Reject card and runs only after the owner approves).
CHANNELS = ("telegram", "slack", "console")
RESERVED_IDS = ("all", "everyone", "team", "here", "channel", "backchannel", "system", "owner")   # room/mention names
ACTION_TYPES = ["create_task", "update_task", "write_file", "message_agent", "post_group", "post_room", "run_code"]
LEVELS = ("green", "yellow", "red")
DEFAULT_PERMISSIONS = {**{a: "green" for a in ACTION_TYPES},
                       "run_code": "red"}      # a coding agent runs commands on this machine: ask first by default


@dataclass
class SlackConfig:
    bot_token_env: str = "SLACK_BOT_TOKEN"     # xoxb-… (posts, reads channels)
    app_token_env: str = "SLACK_APP_TOKEN"     # xapp-… (Socket Mode: no public URL needed)
    owner_user_ids: list[str] = field(default_factory=list)   # Slack member ids allowed to command the team
    channels: dict[str, str] = field(default_factory=dict)    # console room → Slack channel id

    @property
    def bot_token(self) -> str:
        return os.environ.get(self.bot_token_env, "") if self.bot_token_env else ""

    @property
    def app_token(self) -> str:
        return os.environ.get(self.app_token_env, "") if self.app_token_env else ""

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.app_token)


@dataclass
class GitHubConfig:
    """Mirror the task board into a GitHub Project (issues + fields), and open code changes as PRs.
    Uses the `gh` CLI, so everything on GitHub is done as the owner's own account."""
    owner: str = ""                # user or org login that owns the Project ("@me" works too)
    repo: str = ""                 # owner/name — where task issues live (e.g. the team repo)
    project: int = 0               # Project number (github.com/users/<owner>/projects/<number>)
    sync_minutes: int = 2
    prs: bool = False              # code tasks: push the branch and open a PR as the owner; approve = merge the PR

    @property
    def enabled(self) -> bool:
        return bool(self.owner and self.repo and self.project)


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
    permissions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    slack: SlackConfig = field(default_factory=SlackConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    git_name: str = ""             # the owner's git identity: every commit/PR the team makes is theirs
    git_email: str = ""
    mirror_owner: bool = True      # show the owner's messages in every channel (console ↔ Telegram ↔ Slack)
    default_channel_raw: str = ""  # telegram | slack | console — where All hands and the 1:1s live

    # -- helpers ---------------------------------------------------------
    def project_members(self, pid: str) -> list[Member]:
        """People assigned to a project (a person can be on several)."""
        return [m for m in self.team if pid in m.projects]

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

    @property
    def owner_key(self) -> str:
        """Stable id for the owner in tasks/asks (e.g. 'maria_lopez')."""
        import re
        return re.sub(r"[^a-z0-9]+", "_", self.owner_name.lower()).strip("_") or "owner"

    def is_authorized(self, user_id: int | None) -> bool:
        return user_id is not None and (user_id == self.owner_user_id or user_id in self.extra_user_ids)

    def permission(self, member: Member, action: str) -> str:
        """green | yellow | red for this person and action type."""
        return member.permissions.get(action) or self.permissions.get(action) or "green"

    def llm_for(self, member: Member) -> "LLMConfig":
        return member.llm or self.llm

    def channel_for(self, room: str, connected: tuple[str, ...] = ()) -> str:
        """The one outside channel a room lives on: "telegram", "slack" or "" (console only).
        Project setting → company setting (sync.channel) → whatever is connected (Telegram first)."""
        ch = ""
        if room.startswith("p-"):
            p = self.projects.get(room[2:])
            ch = p.channel if p and p.channel in CHANNELS else ""
        ch = ch or (self.default_channel_raw if self.default_channel_raw in CHANNELS else "")
        if not ch:
            ch = "telegram" if "telegram" in connected else "slack" if "slack" in connected else "console"
        return "" if ch == "console" else ch

    @property
    def git_author(self) -> tuple[str, str]:
        """(name, email) for every commit the team makes — the owner's, so work shows up as theirs."""
        return (self.git_name or self.owner_name or "james-monitoring", self.git_email or "jm@localhost")


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
        raise ConfigError(f"No team here yet ({path} not found). Run `jm run` in this folder to set one up in the "
                          f"browser (or `jm init` for the terminal wizard), or pass -c path/to/config.yaml.")
    load_dotenv(path.parent / ".env")
    raw = yaml.safe_load(path.read_text()) or {}
    return parse_config(raw, base_dir=path.parent, path=path)


def _levels(d: Any, where: str) -> dict[str, str]:
    out = {}
    for k, v in (d or {}).items():
        v = str(v).lower()
        if v not in LEVELS:
            raise ConfigError(f"config: {where}.{k} must be green, yellow or red (got `{v}`)")
        out[str(k)] = v
    return out


def parse_llm(llm: dict, base: dict | None = None) -> LLMConfig:
    """`llm` is merged over `base` (the company default). Changing the provider resets model/url/key."""
    merged = dict(base or {})
    if llm.get("provider") and str(llm["provider"]) != str(merged.get("provider") or ""):
        merged = {k: v for k, v in merged.items() if k in ("max_tokens", "temperature")}
    merged.update({k: v for k, v in llm.items() if v not in (None, "")})
    provider = str(merged.get("provider") or "anthropic")
    if provider == "openrouter" and not merged.get("base_url"):
        merged["base_url"] = "https://openrouter.ai/api/v1"
    if provider == "ollama":
        merged.setdefault("base_url", "http://localhost:11434/v1")
        merged.setdefault("api_key_env", "")                 # local: no key
    if provider in CLI_PROVIDERS:
        key_env = ""                                       # the CLI holds the login
    elif "api_key_env" in merged:
        key_env = str(merged.get("api_key_env") or "")     # whatever the user chose (OPENROUTER_API_KEY, …)
    else:
        key_env = KEY_ENV.get(provider, "")
    return LLMConfig(
        provider=provider,
        model=str(merged.get("model") or ""),
        base_url=str(merged.get("base_url") or ""),
        api_key_env=key_env,
        max_tokens=max(int(merged.get("max_tokens") or 8000), 4000),   # below ~4k, documents get cut off
        temperature=float(merged.get("temperature", 0.3)),
        allow_free=bool(merged.get("allow_free", False)),
    )


def parse_config(raw: dict, base_dir: Path | None = None, path: Path | None = None) -> Config:
    base_dir = base_dir or Path.cwd()
    llm_raw = raw.get("llm") or {}
    team_raw = raw.get("team") or []
    if not team_raw:
        raise ConfigError("config: `team` must list at least one member")
    team: list[Member] = []
    seen: set[str] = set()
    for t in team_raw:
        mid = str(t.get("id") or t.get("name", "")).strip().lower()
        if not mid:
            raise ConfigError("config: every team member needs an `id`")
        if mid in seen or mid in RESERVED_IDS or mid.startswith("p-"):
            raise ConfigError(f"config: duplicate or reserved member id `{mid}` (reserved: {', '.join(RESERVED_IDS)})")
        seen.add(mid)
        team.append(Member(
            id=mid,
            name=str(t.get("name") or mid.title()),
            role=str(t.get("role") or ""),
            bot_token_env=str(t.get("bot_token_env") or f"TG_TOKEN_{mid.upper()}"),
            monitor=bool(t.get("monitor", False)),
            projects=[str(p) for p in (t.get("projects") or [])],
            persona_file=str(t.get("persona_file") or ""),
            llm=parse_llm(t["llm"], llm_raw) if isinstance(t.get("llm"), dict) and t["llm"].get("provider") else None,
            permissions=_levels(t.get("permissions"), f"team.{mid}.permissions"),
        ))
    if sum(1 for m in team if m.monitor) > 1:
        raise ConfigError("config: only one member can have `monitor: true`")

    projects = {}
    for pid, p in (raw.get("projects") or {}).items():
        p = p or {}
        projects[str(pid)] = Project(id=str(pid), repo=str(p.get("repo") or ""),
                                     main_branch=str(p.get("main_branch") or "main"),
                                     push=bool(p.get("push", False)), name=str(p.get("name") or pid),
                                     description=str(p.get("description") or ""), lead=str(p.get("lead") or ""),
                                     status=str(p.get("status") or "active"),
                                     telegram_chat_id=int(p.get("telegram_chat_id") or 0),
                                     channel=str(p.get("channel") or "").lower())

    sl = raw.get("slack") or {}
    slack = SlackConfig(bot_token_env=str(sl.get("bot_token_env") or "SLACK_BOT_TOKEN"),
                        app_token_env=str(sl.get("app_token_env") or "SLACK_APP_TOKEN"),
                        owner_user_ids=[str(x) for x in (sl.get("owner_user_ids") or [])],
                        channels={str(k): str(v) for k, v in (sl.get("channels") or {}).items() if v})
    qh = _get(raw, "telegram.quiet_hours")
    ws = Path(str(_get(raw, "workspace.path", "./team-workspace"))).expanduser()
    ws = (ws if ws.is_absolute() else base_dir / ws).resolve()        # always resolved (/tmp → /private/tmp)

    cmd = _get(raw, "executor.command", [])
    if isinstance(cmd, str):
        import shlex
        cmd = shlex.split(cmd)

    from .util import normalize_tz
    try:
        tz = normalize_tz(str(raw.get("timezone") or "UTC"))
    except ValueError as e:
        raise ConfigError(f"config: {e}") from None
    import re as _re

    def hhmm(v, where):
        v = str(v).strip()
        m = _re.fullmatch(r"(\d{1,2}):(\d{2})", v)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ConfigError(f"config: {where} must be a 24-hour time like 18:30 (got `{v}`)")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    daily_report = hhmm(_get(raw, "monitor.daily_report", "18:30"), "monitor.daily_report")
    work_sessions = sorted({hhmm(x, "monitor.work_sessions") for x in (_get(raw, "monitor.work_sessions", []) or [])})
    if qh and len(qh) == 2:
        qh = [hhmm(qh[0], "telegram.quiet_hours"), hhmm(qh[1], "telegram.quiet_hours")]
    owner_name = str(_get(raw, "owner.name", "Owner"))
    owner_key = re.sub(r"[^a-z0-9]+", "_", owner_name.lower()).strip("_") or "owner"
    clash = next((m for m in team if m.id == owner_key), None)
    if clash:
        raise ConfigError(f"config: team member id `{clash.id}` is the same as the owner's ({owner_name}) — give "
                          f"{clash.name} a different id (e.g. `{clash.id}_ai`)")
    return Config(
        company=str(raw.get("company") or "My Company"),
        timezone=tz,
        owner_name=str(_get(raw, "owner.name", "Owner")),
        owner_user_id=int(_get(raw, "owner.telegram_user_id", 0) or 0),
        extra_user_ids=[int(x) for x in (_get(raw, "owner.extra_user_ids", []) or [])],
        llm=parse_llm(llm_raw),
        workspace_path=ws,
        workspace_push=bool(_get(raw, "workspace.push", False)),
        group_chat_id=int(_get(raw, "telegram.group_chat_id", 0) or 0),
        telegram_api_base=str(_get(raw, "telegram.api_base_url", "") or "").rstrip("/"),
        quiet_hours=(str(qh[0]), str(qh[1])) if qh and len(qh) == 2 else None,
        daily_report=daily_report,
        check_every_minutes=max(5, int(_get(raw, "monitor.check_every_minutes", 60))),
        work_sessions=work_sessions,
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
        permissions={**DEFAULT_PERMISSIONS, **_levels(raw.get("permissions"), "permissions")},
        slack=slack,
        github=GitHubConfig(owner=str(_get(raw, "github.owner", "") or ""), repo=str(_get(raw, "github.repo", "") or ""),
                            project=int(_get(raw, "github.project", 0) or 0),
                            sync_minutes=max(1, int(_get(raw, "github.sync_minutes", 2) or 2)),
                            prs=bool(_get(raw, "github.prs", False))),
        git_name=str(_get(raw, "owner.git_name", "") or ""),
        git_email=str(_get(raw, "owner.git_email", "") or ""),
        mirror_owner=bool(_get(raw, "sync.mirror_owner", True)),
        default_channel_raw=str(_get(raw, "sync.channel", "") or "").lower(),
    )
