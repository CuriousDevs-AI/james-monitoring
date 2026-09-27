"""The team workspace: a git repo that holds everything the team knows.

Layout:
    team/charter.md               shared: mission, goals, rules, permissions
    team/<id>/persona.md          who this member is (plain markdown, any model can read it)
    team/<id>/memory.md           durable notes + Pankaj's corrections
    team/<id>/log.md              agent<->agent messages and activity
    tasks/T-001-<slug>.md         one file per task
    asks/ASK-001.md               permission requests + outcome
    decisions/                    owner decisions
    reports/YYYY-MM-DD.md         daily reports
    .jm/state.json                runtime state (not committed)
"""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Config

GITIGNORE = ".jm/\n.env\n"


class Workspace:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root: Path = cfg.workspace_path
        self._git_lock = threading.Lock()
        self._state_lock = threading.Lock()

    # -- layout ------------------------------------------------------------
    def ensure(self) -> None:
        for sub in ["team", "tasks", "asks", "decisions", "reports", ".jm"]:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)
        elif ".jm/" not in gi.read_text():
            gi.write_text(gi.read_text().rstrip("\n") + "\n" + GITIGNORE)
        for m in self.cfg.team:
            (self.root / "team" / m.id).mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init", "-q")
            try:
                self._git("checkout", "-q", "-b", "main")
            except RuntimeError:
                pass

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def read(self, rel: str, default: str = "") -> str:
        p = self.root / rel
        return p.read_text() if p.exists() else default

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def append(self, rel: str, line: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(line.rstrip("\n") + "\n")

    # -- team files --------------------------------------------------------
    def persona(self, member_id: str) -> str:
        m = self.cfg.member(member_id)
        rel = (m.persona_file if m and m.persona_file else f"team/{member_id}/persona.md")
        return self.read(rel, default=f"You are {m.name if m else member_id}.")

    def charter(self) -> str:
        return self.read("team/charter.md")

    def memory(self, member_id: str, max_chars: int = 6000) -> str:
        text = self.read(f"team/{member_id}/memory.md")
        return text[-max_chars:]

    def remember(self, member_id: str, note: str) -> None:
        rel = f"team/{member_id}/memory.md"
        if not (self.root / rel).exists():
            self.write(rel, f"# Memory — {member_id}\n\nNewest last. Corrections from the owner are binding.\n\n")
        self.append(rel, f"- {date.today().isoformat()} — {note.strip()}")

    def log(self, member_id: str, line: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append(f"team/{member_id}/log.md", f"- {stamp} — {line.strip()}")

    def tail_log(self, member_id: str, n: int = 15) -> str:
        lines = self.read(f"team/{member_id}/log.md").splitlines()
        return "\n".join(lines[-n:])

    # -- git ---------------------------------------------------------------
    def _git(self, *args: str, cwd: Path | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def commit(self, message: str, author: str = "james-monitoring") -> bool:
        """Stage everything and commit. Returns True if a commit was made."""
        with self._git_lock:
            self._git("add", "-A")
            if not self._git("status", "--porcelain").strip():
                return False
            self._git("-c", f"user.name={author}", "-c", "user.email=jm@localhost",
                      "commit", "-q", "-m", message)
            if self.cfg.workspace_push:
                try:
                    self._git("push", "-q")
                except RuntimeError:
                    pass  # push failures must never stop the team; monitor reports them
            return True

    def git_log(self, n: int = 20, path: Path | None = None, since: str | None = None) -> str:
        args = ["log", f"-{n}", "--date=short", "--pretty=format:%h %ad %an: %s"]
        if since:
            args.append(f"--since={since}")
        try:
            return self._git(*args, cwd=path or self.root)
        except RuntimeError:
            return ""

    # -- runtime state (.jm/state.json, not committed) ----------------------
    def _state_path(self) -> Path:
        return self.root / ".jm" / "state.json"

    def state(self) -> dict[str, Any]:
        p = self._state_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}

    def update_state(self, fn) -> dict[str, Any]:
        with self._state_lock:
            s = self.state()
            fn(s)
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(s, indent=2, sort_keys=True))
            return s
