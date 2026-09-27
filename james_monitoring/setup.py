"""Team setup: `jm init` and `jm add-member`.

Nothing is hardcoded to any company. The flow, with every Telegram link verified live:
  1. basics (company, founder, timezone, goals)       4. the manager bot → detects YOU and your GROUP
  2. AI (any provider/model)                          5. each member: name → persona/SKILL.md → bot →
  3. git workspace (new or cloned)                       in the group ✓ → DM works ✓ → intro posted
                                                      6. monitoring (report time, work sessions, budget)
"""
from __future__ import annotations

import os
import re
import subprocess
from importlib import resources
from pathlib import Path
from typing import Protocol

import yaml

from .config import Config, load_config, parse_config
from .workspace import Workspace


# -- I/O (console for humans, scripted for tests) ----------------------------------------
class IO(Protocol):
    def ask(self, prompt: str, default: str = "", required: bool = False, secret: bool = False) -> str: ...
    def confirm(self, prompt: str, default: bool = True) -> bool: ...
    def say(self, text: str) -> None: ...


class ConsoleIO:
    def ask(self, prompt, default="", required=False, secret=False):
        import getpass
        while True:
            suffix = f" [{default}]" if default and not secret else ""
            raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
            val = raw or default
            if val or not required:
                return val
            print("  (required)")

    def confirm(self, prompt, default=True):
        d = "Y/n" if default else "y/N"
        raw = input(f"{prompt} [{d}]: ").strip().lower()
        return default if not raw else raw.startswith("y")

    def say(self, text):
        print(text)


# -- helpers ---------------------------------------------------------------------------
def template(name: str) -> str:
    return resources.files("james_monitoring").joinpath("templates", name).read_text()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_").replace("-", "_")) or "member"


def detect_timezone() -> str:
    tz = os.environ.get("TZ", "")
    if "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"


def strip_frontmatter(text: str) -> str:
    """Accept Claude SKILL.md files (YAML front matter) as personas."""
    m = re.match(r"\A---\n.*?\n---\n?(.*)\Z", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


class PersonaError(ValueError):
    pass


def _read_text(data: bytes, where: str) -> str:
    enc = "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        text = data.decode(enc)
    except UnicodeDecodeError:
        raise PersonaError(f"{where} is not a text file (use SKILL.md, a folder, or a .skill/.zip)") from None
    if "\x00" in text:
        raise PersonaError(f"{where} is not a text file")
    return text


def _from_archive(p: Path) -> str:
    """A packaged skill (.skill / .zip): use the SKILL.md inside (shallowest one wins)."""
    import zipfile
    with zipfile.ZipFile(p) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        for wanted in ("skill.md", "persona.md"):
            hits = sorted((n for n in names if n.rsplit("/", 1)[-1].lower() == wanted), key=lambda n: n.count("/"))
            if hits:
                return strip_frontmatter(_read_text(z.read(hits[0]), f"{p.name}:{hits[0]}"))
    raise PersonaError(f"{p.name} is an archive but has no SKILL.md or persona.md inside")


def load_persona(path: str) -> str:
    """A persona file, a SKILL.md, a folder containing one, or a packaged .skill / .zip."""
    import zipfile
    p = Path(path.strip().strip("'\"")).expanduser()
    if p.is_dir():
        for name in ("SKILL.md", "persona.md", "README.md"):
            if (p / name).exists():
                p = p / name
                break
        else:
            raise FileNotFoundError(f"no SKILL.md or persona.md in {p}")
    if not p.is_file():
        raise FileNotFoundError(f"{p} not found")
    if zipfile.is_zipfile(p):
        return _from_archive(p)
    return strip_frontmatter(_read_text(p.read_bytes(), p.name))


def default_persona(member: dict, owner: str) -> str:
    if member.get("monitor"):
        return (template("manager.md").replace("{name}", member["name"])
                .replace("{role}", member.get("role", "")).replace("{owner}", owner))
    owns = "\n".join(f"- {p}" for p in member.get("projects") or []) or f"- {member.get('role', '')}"
    return (template("persona.md").replace("{name}", member["name"]).replace("{role}", member.get("role", ""))
            .replace("{owns}", owns).replace("{owner}", owner))


def _append_env(env_path: Path, pairs: dict[str, str]) -> None:
    existing = env_path.read_text() if env_path.exists() else ""
    lines = [f"{k}={v}" for k, v in pairs.items() if v and f"\n{k}=" not in "\n" + existing]
    if lines:
        with env_path.open("a") as f:
            f.write(("\n" if existing and not existing.endswith("\n") else "") + "\n".join(lines) + "\n")
        os.chmod(env_path, 0o600)
    for k, v in pairs.items():
        if v:
            os.environ[k] = v


def scaffold(raw: dict, *, personas: dict[str, str] | None = None, goals: list[str] | None = None,
             tokens: dict[str, str] | None = None, api_key: str = "", base_dir: Path = Path.cwd(),
             clone_url: str = "") -> Config:
    """Write config.yaml + .env and create/refresh the git workspace. Re-runnable: never overwrites
    existing personas, memory or charter."""
    base_dir = Path(base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = base_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    cfg = parse_config(raw, base_dir=base_dir, path=cfg_path)
    env = {}
    if api_key and cfg.llm.api_key_env:
        env[cfg.llm.api_key_env] = api_key
    for mid, tok in (tokens or {}).items():
        m = cfg.member(mid)
        if m and tok:
            env[m.bot_token_env] = tok
    _append_env(base_dir / ".env", env)

    ws_path = cfg.workspace_path
    if clone_url and not (ws_path / ".git").exists():
        ws_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", clone_url, str(ws_path)], check=True)
    ws = Workspace(cfg)
    ws.ensure()
    if not (ws.root / "team/charter.md").exists():
        g = "\n".join(f"{i}. {x}" for i, x in enumerate(goals or [], 1)) or "1. (set your goals here)"
        ws.write("team/charter.md", template("charter.md").replace("{company}", cfg.company)
                 .replace("{owner}", cfg.owner_name).replace("{goals}", g))
    if not (ws.root / "decisions/OPEN.md").exists():
        ws.write("decisions/OPEN.md", f"# Open decisions for {cfg.owner_name}\n\n"
                                      "Numbered items here appear in every report until you remove them.\n")
    for m in raw.get("team", []):
        mid = str(m.get("id")).lower()
        rel = f"team/{mid}/persona.md"
        given = (personas or {}).get(mid)
        if given:
            ws.write(rel, given)                                   # explicitly provided → use it
        elif not (ws.root / rel).exists():
            ws.write(rel, default_persona(m, cfg.owner_name))      # never overwrite an edited persona
        mem = f"team/{mid}/memory.md"
        if not (ws.root / mem).exists():
            ws.write(mem, f"# Memory — {m.get('name', mid)}\n\nNewest last. Corrections from the owner are binding.\n\n")
    ws.commit("team setup", author="james-monitoring")
    return load_config(cfg_path)


# -- the interactive flow ---------------------------------------------------------------
BOTFATHER = ("  In Telegram open @BotFather → /newbot → give it a name and a username → copy the token.")


async def _connect_bot(io: IO, label: str, api_base: str):
    """Ask for a token until it works. Returns (token, BotInfo)."""
    from .telegram_setup import TelegramProbe
    io.say(BOTFATHER)
    while True:
        token = io.ask(f"  Bot token for {label}", required=True, secret=True)
        try:
            async with TelegramProbe(token, api_base) as p:
                info = await p.info()
            io.say(f"  ✅ Connected: @{info.username}")
            return token, info
        except Exception as e:  # noqa: BLE001 - show any Telegram error and retry
            io.say(f"  ❌ That token didn't work ({e}). Try again.")


async def _wait(io: IO, what: str, coro_factory, timeout: float) -> bool:
    while True:
        io.say(f"  ⏳ Waiting for {what} ...")
        if await coro_factory(timeout):
            io.say(f"  ✅ {what[0].upper() + what[1:]} — confirmed")
            return True
        if not io.confirm(f"  Not detected yet. Keep waiting for {what}?", True):
            io.say("  ⚠️ Skipped — fix later, then check with `jm doctor --ping`.")
            return False


async def setup_manager(io: IO, *, company: str, api_base: str = "", timeout: float = 300) -> tuple[dict, str, str, int, int]:
    """The manager's bot also discovers the owner's Telegram id and the team group."""
    from .telegram_setup import TelegramProbe
    io.say("\nThe manager watches everyone, routes messages and reports to you.")
    name = io.ask("  Manager's name", "James")
    role = io.ask("  Manager's role", "Delivery manager and chief of staff")
    persona = ""
    ppath = io.ask("  Persona / SKILL.md path for the manager (blank = built-in)", "")
    while ppath:
        try:
            persona = load_persona(ppath)
            io.say(f"  ✅ Persona loaded ({len(persona.splitlines())} lines)")
            break
        except (FileNotFoundError, PersonaError, OSError) as e:
            ppath = io.ask(f"  ❌ {e}. Path again (blank = built-in)", "")
    token, info = await _connect_bot(io, name, api_base)
    async with TelegramProbe(token, api_base) as p:
        if not info.reads_groups:
            io.say(f"  ⚠️ @{info.username} can't read group messages yet. In @BotFather: /setprivacy → "
                   f"@{info.username} → Disable.")
            if await _wait(io, "privacy mode to be disabled",
                           lambda t: p.wait_until(lambda: _reads(p), t), timeout):
                pass
        io.say(f"\n  Open https://t.me/{info.username} in Telegram and press Start (or send 'hi').")
        owner = None
        while not owner:
            owner = await p.wait_for_owner(timeout)
            if owner and not io.confirm(f"  Detected {owner.name}"
                                        f"{' (@' + owner.username + ')' if owner.username else ''}, id {owner.id}. "
                                        f"Is this you?", True):
                owner = None
            if not owner:
                manual = io.ask("  Enter your Telegram user id manually (blank = keep waiting)", "")
                if manual.strip().lstrip("-").isdigit():
                    owner = type("P", (), {"id": int(manual), "name": "you", "username": ""})()
        io.say(f"  ✅ Owner: {owner.name} ({owner.id})")
        io.say(f"\n  Create a Telegram group for your team (e.g. '{company} HQ'), add @{info.username} to it, "
               f"and send any message in the group.")
        group = None
        while not group:
            group = await p.wait_for_group(owner.id, timeout)
            if group and not io.confirm(f"  Detected group '{group.title}' ({group.id}). Use it?", True):
                group = None
            if not group:
                manual = io.ask("  Enter the group id manually (blank = keep waiting)", "")
                if manual.strip().lstrip("-").isdigit():
                    group = type("G", (), {"id": int(manual), "title": "HQ"})()
        io.say(f"  ✅ Group: {group.title} ({group.id})")
        await p.can_dm(owner.id, f"✅ {name} is connected. I'll set up the rest of the team now.")
    mid = slug(name)
    member = {"id": mid, "name": name, "role": role, "monitor": True, "bot_token_env": f"TG_TOKEN_{mid.upper()}"}
    return member, persona, token, owner.id, group.id


async def _reads(p) -> bool:
    return (await p.info()).reads_groups


async def setup_member(io: IO, cfg: Config, *, projects: dict, api_base: str = "", timeout: float = 300,
                       taken: set[str] | None = None) -> tuple[dict, str, str]:
    """name → role → persona/SKILL.md → projects → bot → in the group ✓ → DM works ✓ → intro in the group."""
    from .telegram_setup import TelegramProbe
    taken = taken or {m.id for m in cfg.team}
    while True:
        name = io.ask("\n  Team member's name", required=True)
        mid = slug(name)
        if mid in taken or mid == "all":
            io.say(f"  ❌ '{mid}' is already used. Pick another name.")
            continue
        break
    role = io.ask(f"  {name}'s role", required=True)
    persona = ""
    while True:
        ppath = io.ask(f"  Persona / SKILL.md path for {name} (file or folder; blank = generate one)", "")
        if not ppath:
            break
        try:
            persona = load_persona(ppath)
            io.say(f"  ✅ Persona loaded ({len(persona.splitlines())} lines)")
            break
        except (FileNotFoundError, PersonaError, OSError) as e:
            io.say(f"  ❌ {e}")
    projs = [x.strip() for x in io.ask(f"  Projects {name} works on (comma separated, optional)", "").split(",")
             if x.strip()]
    for pr in projs:
        if pr not in projects:
            repo = io.ask(f"  Local git repo for project '{pr}' (blank = none; needed for code tasks)", "")
            projects[pr] = {"repo": repo, "main_branch": "main"}

    token, info = await _connect_bot(io, name, api_base)
    async with TelegramProbe(token, api_base) as p:
        if cfg.group_chat_id:
            io.say(f"  Add @{info.username} to your team group.")
            await _wait(io, f"@{info.username} to join the group",
                        lambda t: p.wait_until(lambda: p.in_group(cfg.group_chat_id), t), timeout)
        if cfg.owner_user_id:
            io.say(f"  Open https://t.me/{info.username} and press Start, so {name} can message you.")
            hello = f"✅ {name} here — {role}. Message me in this chat for work."
            await _wait(io, f"your DM with {name}",
                        lambda t: p.wait_until(lambda: p.can_dm(cfg.owner_user_id, hello), t), timeout)
        if cfg.group_chat_id:
            await p.post(cfg.group_chat_id, f"👋 {name} joined the team — {role}.")
    member = {"id": mid, "name": name, "role": role, "bot_token_env": f"TG_TOKEN_{mid.upper()}"}
    if projs:
        member["projects"] = projs
    return member, persona, token


async def wizard(base_dir: Path, io: IO | None = None, *, api_base: str = "", timeout: float = 300) -> Config:
    io = io or ConsoleIO()
    base_dir = Path(base_dir).resolve()
    io.say("\n=== james-monitoring: set up your team ===")

    io.say("\nStep 1/6 — Basics")
    company = io.ask("  Company / project name", required=True)
    owner = io.ask("  Your name (the founder/owner)", required=True)
    tz = io.ask("  Timezone", detect_timezone())
    goals = [g.strip() for g in io.ask("  Top goals, separated by ';' (optional)", "").split(";") if g.strip()]

    io.say("\nStep 2/6 — AI model (switch any time in config.yaml)")
    io.say("  claude-code = your Claude Pro/Max subscription via the `claude` CLI (no API key)\n"
           "  anthropic   = Anthropic API key · openai = any OpenAI-compatible API (OpenAI, Codex, Ollama, OpenRouter)")
    provider = io.ask("  Provider: claude-code | anthropic | openai", "claude-code")
    if provider == "claude-code":
        model = io.ask("  Model (blank = the CLI's default, e.g. sonnet / opus)", "")
        base_url, key_env, api_key = "", "", ""
        import shutil
        if not shutil.which("claude"):
            io.say("  ⚠️ `claude` CLI not found on this machine. Install: npm install -g @anthropic-ai/claude-code,\n"
                   "     then run `claude` once and /login with your subscription.")
    else:
        model = io.ask("  Model id", required=True)
        base_url = io.ask("  API base URL (blank = provider default; Ollama: http://localhost:11434/v1)", "")
        key_env = "ANTHROPIC_API_KEY" if provider.startswith("anthropic") else "OPENAI_API_KEY"
        api_key = io.ask(f"  API key (stored in .env as {key_env}; blank = set later)", "", secret=True)

    io.say("\nStep 3/6 — Git workspace (tasks, personas, memory, reports live here)")
    ws = io.ask("  Workspace folder", str(base_dir / "team-workspace"))
    clone = io.ask("  Clone it from a git URL? (blank = start a new repo)", "")
    push = io.confirm("  Push to the remote after every change?", bool(clone))

    io.say("\nStep 4/6 — Telegram: the manager bot")
    manager, m_persona, m_token, owner_id, group_id = await setup_manager(io, company=company, api_base=api_base,
                                                                         timeout=timeout)
    raw = {
        "company": company, "timezone": tz,
        "owner": {"name": owner, "telegram_user_id": owner_id},
        "llm": {"provider": provider, "model": model, "base_url": base_url, "api_key_env": key_env,
                "max_tokens": 2000},
        "workspace": {"path": ws, "push": push},
        "telegram": {"group_chat_id": group_id, "quiet_hours": ["22:00", "08:00"],
                     **({"api_base_url": api_base} if api_base else {})},
        "monitor": {"daily_report": "18:30", "check_every_minutes": 60, "work_sessions": [],
                    "stale_days": 7, "blocked_escalate_days": 2, "ask_default_hours": 24},
        "budget": {"daily_tokens_per_agent": 300000},
        "executor": {"command": [], "timeout_minutes": 30},
        "limits": {"max_agent_hops": 4},
        "projects": {},
        "team": [manager],
    }
    cfg = scaffold(raw, personas={manager["id"]: m_persona} if m_persona else None, goals=goals,
                   tokens={manager["id"]: m_token}, api_key=api_key, base_dir=base_dir, clone_url=clone)

    io.say("\nStep 5/6 — Your team. For each person: name → persona/SKILL.md → bot → group ✓ → DM ✓")
    while io.confirm("\n  Add a team member?", True):
        member, persona, token = await setup_member(io, cfg, projects=raw["projects"], api_base=api_base,
                                                    timeout=timeout)
        raw["team"].append(member)
        cfg = scaffold(raw, personas={member["id"]: persona} if persona else None, tokens={member["id"]: token},
                       base_dir=base_dir)
        io.say(f"  ✅ {member['name']} added ({len(raw['team'])} people on the team)")

    io.say("\nStep 6/6 — Monitoring")
    raw["monitor"]["daily_report"] = io.ask("  Daily report time (HH:MM)", "18:30")
    sessions = io.ask("  Work sessions — times the team works on its own (comma separated; blank = only when asked)",
                      "10:00,15:00")
    raw["monitor"]["work_sessions"] = [x.strip() for x in sessions.split(",") if x.strip()]
    raw["budget"]["daily_tokens_per_agent"] = int(io.ask("  Daily token budget per person", "300000"))
    exe = io.ask("  Coding tool for code tasks: claude | codex | none", "none")
    raw["executor"]["command"] = {"claude": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"],
                                  "codex": ["codex", "exec", "--full-auto", "{prompt}"]}.get(exe, [])
    cfg = scaffold(raw, base_dir=base_dir)

    io.say(f"\n✅ {company}'s team is set up: {', '.join(m.name for m in cfg.team)}.")
    io.say(f"   Config: {cfg.path}\n   Workspace: {cfg.workspace_path}")
    io.say("\nNext: `jm doctor --ping`, then `jm run`. In the group, type /onboard.")
    return cfg


async def add_member_interactive(cfg_path: Path, io: IO | None = None, *, timeout: float = 300) -> str:
    io = io or ConsoleIO()
    cfg = load_config(cfg_path)
    raw = yaml.safe_load(Path(cfg_path).read_text())
    raw.setdefault("projects", {})
    member, persona, token = await setup_member(io, cfg, projects=raw["projects"], api_base=cfg.telegram_api_base,
                                                timeout=timeout)
    raw.setdefault("team", []).append(member)
    scaffold(raw, personas={member["id"]: persona} if persona else None, tokens={member["id"]: token},
             base_dir=Path(cfg_path).parent)
    io.say(f"✅ {member['name']} added. Restart `jm run` to bring them online.")
    return member["id"]


def add_member(cfg_path: Path, *, name: str, role: str, projects: list[str], persona_file: str = "",
               token: str = "") -> str:
    """Non-interactive add (no Telegram checks) — for scripts."""
    raw = yaml.safe_load(Path(cfg_path).read_text())
    mid = slug(name)
    if any(str(m.get("id")).lower() == mid for m in raw.get("team", [])) or mid == "all":
        raise ValueError(f"member `{mid}` already exists")
    member = {"id": mid, "name": name, "role": role, "bot_token_env": f"TG_TOKEN_{mid.upper()}"}
    if projects:
        member["projects"] = projects
    raw.setdefault("team", []).append(member)
    for p in projects:
        raw.setdefault("projects", {}).setdefault(p, {"repo": "", "main_branch": "main"})
    personas = {mid: load_persona(persona_file)} if persona_file else None
    scaffold(raw, personas=personas, tokens={mid: token} if token else None, base_dir=Path(cfg_path).parent)
    return mid


def remove_member(cfg_path: Path, member_id: str) -> str:
    """Take someone off the team. Their files stay in git history (team/<id>/ is kept)."""
    raw = yaml.safe_load(Path(cfg_path).read_text())
    team = raw.get("team", [])
    target = [m for m in team if str(m.get("id")).lower() == member_id.lower()]
    if not target:
        raise ValueError(f"no member `{member_id}`")
    if target[0].get("monitor"):
        raise ValueError("can't remove the manager — make someone else the manager first")
    raw["team"] = [m for m in team if m is not target[0]]
    Path(cfg_path).write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    return target[0].get("name", member_id)
