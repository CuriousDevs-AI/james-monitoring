"""`jm init` — the setup wizard: AI → team → charter → git workspace → Telegram → monitoring."""
from __future__ import annotations

import os
import re
import subprocess
from importlib import resources
from pathlib import Path

import yaml

from .config import Config, load_config, parse_config
from .workspace import Workspace


def template(name: str) -> str:
    return resources.files("james_monitoring").joinpath("templates", name).read_text()


def strip_frontmatter(text: str) -> str:
    """Accept Claude SKILL.md files (YAML front matter) as personas."""
    m = re.match(r"\A---\n.*?\n---\n?(.*)\Z", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


def default_persona(member: dict, owner: str) -> str:
    if member.get("monitor"):
        return template("james.md").replace("{owner}", owner)
    owns = "\n".join(f"- {p}" for p in member.get("projects") or []) or f"- {member.get('role', '')}"
    return (template("persona.md").replace("{name}", member["name"]).replace("{role}", member.get("role", ""))
            .replace("{owns}", owns).replace("{owner}", owner))


def scaffold(raw: dict, *, personas: dict[str, str] | None = None, goals: list[str] | None = None,
             tokens: dict[str, str] | None = None, api_key: str = "", base_dir: Path = Path.cwd(),
             clone_url: str = "") -> Config:
    """Write config.yaml + .env and create the git workspace. Safe to re-run: never overwrites personas/charter."""
    base_dir = Path(base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = base_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))

    env_lines = []
    env_path = base_dir / ".env"
    existing = env_path.read_text() if env_path.exists() else ""
    cfg = parse_config(raw, base_dir=base_dir, path=cfg_path)
    if api_key and cfg.llm.api_key_env and f"{cfg.llm.api_key_env}=" not in existing:
        env_lines.append(f"{cfg.llm.api_key_env}={api_key}")
    for mid, tok in (tokens or {}).items():
        m = cfg.member(mid)
        if m and tok and f"{m.bot_token_env}=" not in existing:
            env_lines.append(f"{m.bot_token_env}={tok}")
    if env_lines:
        with env_path.open("a") as f:
            f.write(("\n" if existing and not existing.endswith("\n") else "") + "\n".join(env_lines) + "\n")
        os.chmod(env_path, 0o600)

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
    for m in raw.get("team", []):
        mid = str(m.get("id")).lower()
        rel = f"team/{mid}/persona.md"
        if not (ws.root / rel).exists():
            text = (personas or {}).get(mid) or default_persona(m, cfg.owner_name)
            ws.write(rel, text)
        mem = f"team/{mid}/memory.md"
        if not (ws.root / mem).exists():
            ws.write(mem, f"# Memory — {m.get('name', mid)}\n\nNewest last. Corrections from the owner are binding.\n\n")
    ws.commit("james-monitoring: team workspace set up", author="james-monitoring")
    return load_config(cfg_path)


# -- interactive wizard ---------------------------------------------------------------
def _ask(prompt: str, default: str = "", required: bool = False, secret: bool = False) -> str:
    import getpass
    while True:
        suffix = f" [{default}]" if default and not secret else ""
        raw = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        val = raw or default
        if val or not required:
            return val
        print("  required")


def wizard(base_dir: Path) -> Config:
    print("\n=== james-monitoring setup ===\n")
    print("Step 1/6 — AI (you can switch any time in config.yaml)")
    provider = _ask("  Provider: anthropic | openai (OpenAI, Codex, Ollama, OpenRouter...)", "anthropic")
    model = _ask("  Model id", required=True)
    base_url = _ask("  Base URL (blank for default; Ollama: http://localhost:11434/v1)", "")
    key_env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    api_key = _ask(f"  API key (saved to .env as {key_env}; blank to set later)", secret=True)

    print("\nStep 2/6 — Company and founder")
    company = _ask("  Company name", "CuriousDevs")
    owner = _ask("  Founder name", "Pankaj")
    tz = _ask("  Timezone", "Asia/Kolkata")
    goals = [g.strip() for g in _ask("  Top goals, separated by ';'", "").split(";") if g.strip()]

    print("\nStep 3/6 — Team. James (delivery manager) is added automatically.")
    team = [{"id": "james", "name": "James", "role": "Delivery manager and chief of staff", "monitor": True,
             "bot_token_env": "TG_TOKEN_JAMES"}]
    personas: dict[str, str] = {}
    n = int(_ask("  How many other team members?", "0") or 0)
    projects: dict[str, dict] = {}
    for i in range(n):
        print(f"  Member {i + 1}/{n}")
        name = _ask("    Name", required=True)
        mid = re.sub(r"[^a-z0-9_]", "", name.lower())
        role = _ask("    Role", required=True)
        projs = [p.strip() for p in _ask("    Projects (comma separated, optional)", "").split(",") if p.strip()]
        pfile = _ask("    Persona file to import (e.g. a SKILL.md; blank = generate)", "")
        if pfile and Path(pfile).expanduser().exists():
            personas[mid] = strip_frontmatter(Path(pfile).expanduser().read_text())
        team.append({"id": mid, "name": name, "role": role, "projects": projs,
                     "bot_token_env": f"TG_TOKEN_{mid.upper()}"})
        for p in projs:
            projects.setdefault(p, {"repo": "", "main_branch": "main"})

    print("\nStep 4/6 — Git workspace (tasks, memory, reports live here)")
    ws = _ask("  Local path for the team workspace", str(base_dir / "team-workspace"))
    clone = _ask("  Clone from a remote git URL? (blank = new local repo)", "")
    push = _ask("  Push to remote after each change? (y/n)", "n").lower().startswith("y")
    for p in projects:
        repo = _ask(f"  Local git repo for project `{p}` (blank = none; needed for code changes)", "")
        projects[p]["repo"] = repo

    print("\nStep 5/6 — Telegram. Create one bot per member with @BotFather.")
    print("  For James: BotFather → /setprivacy → Disable (so he can read the HQ group).")
    tokens = {}
    for m in team:
        tokens[m["id"]] = _ask(f"  Bot token for {m['name']} (blank to add later)", secret=True)
    owner_id = _ask("  Your Telegram user id (blank = find it later with /whoami)", "0")
    group_id = _ask("  HQ group chat id (blank = find it later with /groupid)", "0")

    print("\nStep 6/6 — Monitoring")
    report = _ask("  Daily report time (HH:MM)", "18:54")
    cap = _ask("  Daily token budget per agent", "300000")
    executor = _ask("  Coding CLI for code tasks: claude | codex | none", "none")
    exe_cmd = {"claude": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"],
               "codex": ["codex", "exec", "--full-auto", "{prompt}"]}.get(executor, [])

    raw = {
        "company": company, "timezone": tz,
        "owner": {"name": owner, "telegram_user_id": int(owner_id or 0)},
        "llm": {"provider": provider, "model": model, "base_url": base_url, "api_key_env": key_env, "max_tokens": 2000},
        "workspace": {"path": ws, "push": push},
        "telegram": {"group_chat_id": int(group_id or 0), "quiet_hours": ["22:00", "08:00"]},
        "monitor": {"daily_report": report, "check_every_minutes": 60, "stale_days": 7,
                    "blocked_escalate_days": 2, "ask_default_hours": 24},
        "budget": {"daily_tokens_per_agent": int(cap)},
        "executor": {"command": exe_cmd, "timeout_minutes": 30},
        "limits": {"max_agent_hops": 4},
        "projects": projects,
        "team": team,
    }
    cfg = scaffold(raw, personas=personas, goals=goals, tokens={k: v for k, v in tokens.items() if v},
                   api_key=api_key, base_dir=base_dir, clone_url=clone)
    print(f"\n✅ Done. Config: {cfg.path}\n   Workspace: {cfg.workspace_path}\n")
    print("Next:\n  1. Press /start on every bot in Telegram (so they can DM you).\n"
          "  2. Add all bots to your HQ group.\n  3. jm doctor\n  4. jm run\n  5. In the group: /onboard")
    return cfg


def add_member(cfg_path: Path, *, name: str, role: str, projects: list[str], persona_file: str = "",
               token: str = "") -> str:
    raw = yaml.safe_load(cfg_path.read_text())
    mid = re.sub(r"[^a-z0-9_]", "", name.lower())
    if any(str(m.get("id")).lower() == mid for m in raw.get("team", [])):
        raise ValueError(f"member `{mid}` already exists")
    member = {"id": mid, "name": name, "role": role, "projects": projects, "bot_token_env": f"TG_TOKEN_{mid.upper()}"}
    raw.setdefault("team", []).append(member)
    for p in projects:
        raw.setdefault("projects", {}).setdefault(p, {"repo": "", "main_branch": "main"})
    personas = {}
    if persona_file:
        personas[mid] = strip_frontmatter(Path(persona_file).expanduser().read_text())
    scaffold(raw, personas=personas, tokens={mid: token} if token else None, base_dir=cfg_path.parent)
    return mid
