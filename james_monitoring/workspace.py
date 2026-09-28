"""The team workspace: a git repo that holds everything the team knows.

Layout:
    team/charter.md               shared: mission, goals, rules, permissions
    team/<id>/persona.md          who this member is (plain markdown, any model can read it)
    team/<id>/memory.md           durable notes + the owner's corrections
    team/<id>/log.md              agent<->agent messages and activity
    tasks/T-001-<slug>.md         one file per task
    asks/ASK-001.md               permission requests + outcome
    decisions/                    owner decisions
    reports/YYYY-MM-DD.md         daily reports
    .jm/state.json                runtime state (not committed)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .fileio import atomic_write, path_lock

log = logging.getLogger("jm.workspace")

# Git must never wait for a human: no password prompts, no SSH questions, no editors, no signing pinentry.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "SSH_ASKPASS": "echo", "GCM_INTERACTIVE": "never",
           "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=15", "GIT_EDITOR": "true"}


def run_git(cwd: Path, *args: str, timeout: float = 60) -> str:
    env = {**os.environ, **GIT_ENV}
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args[:2])} timed out after {int(timeout)}s") from None
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout

GITIGNORE = ".jm/\n.env\n*.lock\n"


class Workspace:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root: Path = cfg.workspace_path
        self._push_guard = threading.Lock()
        self._pushing = False
        self._push_again = False

    # -- layout ------------------------------------------------------------
    def ensure(self) -> None:
        for sub in ["team", "tasks", "asks", "decisions", "reports", ".jm"]:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE)
        else:
            have = gi.read_text().splitlines()
            missing = [x for x in GITIGNORE.splitlines() if x not in have]
            if missing:
                gi.write_text(gi.read_text().rstrip("\n") + "\n" + "\n".join(missing) + "\n")
        for stray in self.root.glob("team/*/*.lock"):        # left by older versions
            stray.unlink(missing_ok=True)
            try:
                self._git("rm", "-q", "--cached", "--ignore-unmatch", str(stray.relative_to(self.root)))
            except RuntimeError:
                pass
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
        atomic_write(self.root / rel, text)

    def append(self, rel: str, line: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(line.rstrip("\n") + "\n")

    def _now(self) -> datetime:
        """The company's clock (config timezone), not the server's — logs and reports agree on what "today" is."""
        from .util import now
        return now(self.cfg.timezone)

    # -- team files --------------------------------------------------------
    def persona(self, member_id: str) -> str:
        m = self.cfg.member(member_id)
        rel = (m.persona_file if m and m.persona_file else f"team/{member_id}/persona.md")
        return self.read(rel, default=f"You are {m.name if m else member_id}.")

    def charter(self) -> str:
        return self.read("team/charter.md")

    # Memory file: "## Corrections" (from the owner — binding, never trimmed), "## Notes" (newest kept), and
    # anything the owner writes by hand (other headings, paragraphs) — kept verbatim and always in the prompt.
    CORR, NOTES = "## Corrections (binding — never trimmed)", "## Notes"

    def _memory_sections(self, member_id: str) -> tuple[list[str], list[str], list[str]]:
        """(corrections, notes, everything else verbatim) — nothing in the file is ever dropped."""
        corr, notes, other, cur = [], [], [], None
        for line in self.read(f"team/{member_id}/memory.md").splitlines():
            if line.startswith("## Corrections"):
                cur = corr
                continue
            if line.startswith("## Notes"):
                cur = notes
                continue
            if line.startswith("## ") or line.startswith("# "):
                cur = None
            target = other if cur is None else cur
            if cur is not None and not line.startswith("- ") and cur and line.strip():
                cur[-1] += "\n" + line                    # a wrapped line belongs to the entry above
            elif cur is None or line.strip():
                target.append(line)
        return corr, notes, other

    def memory(self, member_id: str, max_chars: int = 6000) -> str:
        """What goes in the prompt: every correction and everything written by hand, then as many of the newest
        notes as fit — whole entries only."""
        corr, notes, other = self._memory_sections(member_id)
        extra = "\n".join(x for x in other if not x.startswith("# Memory") and "Newest last." not in x).strip()
        out = ([self.CORR, *corr] if corr else []) + ([extra] if extra else [])
        budget = max_chars - sum(len(x) + 1 for x in out)
        kept: list[str] = []
        for line in reversed(notes):
            if len(line) + 1 > budget:
                break
            kept.insert(0, line)
            budget -= len(line) + 1
        if kept:
            out += [self.NOTES + (f" (newest {len(kept)} of {len(notes)})" if len(kept) < len(notes) else ""), *kept]
        return "\n".join(out)

    def remember(self, member_id: str, note: str, pinned: bool = False) -> None:
        """Add one entry: the file is only ever *inserted into*, so hand-written parts are never touched."""
        note = " ".join(str(note or "").split())
        if not note:
            return
        rel = f"team/{member_id}/memory.md"
        with path_lock(self.root / rel):
            lines = self.read(rel).splitlines() or [f"# Memory — {member_id}", "",
                                                    "Newest last. Corrections from the owner are binding.", ""]
            head = self.CORR if pinned else self.NOTES
            prefix = "## Corrections" if pinned else "## Notes"
            if pinned and any(ln.split(" — ", 1)[-1].strip() == note for ln in lines if ln.startswith("- ")):
                return                                    # exactly this correction is already pinned
            entry = f"- {self._now().date().isoformat()} — {note}"
            idx = next((i for i, ln in enumerate(lines) if ln.startswith(prefix)), None)
            if idx is None:
                lines += ["", head, entry]
            else:
                end = next((j for j in range(idx + 1, len(lines)) if lines[j].startswith("#")), len(lines))
                while end > idx + 1 and not lines[end - 1].strip():
                    end -= 1
                lines.insert(end, entry)
            self.write(rel, "\n".join(lines).rstrip("\n") + "\n")

    def log(self, member_id: str, line: str) -> None:
        stamp = self._now().strftime("%Y-%m-%d %H:%M")
        self.append(f"team/{member_id}/log.md", f"- {stamp} — {line.strip()}")

    def tail_log(self, member_id: str, n: int = 15) -> str:
        lines = self.read(f"team/{member_id}/log.md").splitlines()
        return "\n".join(lines[-n:])

    # -- git ---------------------------------------------------------------
    def _git(self, *args: str, cwd: Path | None = None, timeout: float = 60) -> str:
        return run_git(cwd or self.root, *args, timeout=timeout)

    def commit(self, message: str, author: str = "james-monitoring") -> bool:
        """Stage everything and commit. Returns True if a commit was made.

        One lock per repo (threads *and* processes), no prompts, no signing. A failed commit never breaks the
        caller: the change is on disk, the error is recorded and shown by the manager's checks, and the next
        commit picks it up."""
        try:
            with path_lock(self.root / ".jm" / "git"):
                self._git("add", "-A")
                if not self._git("status", "--porcelain").strip():
                    return False
                name, email = self.cfg.git_author
                who = author if author and author not in (name, self.cfg.owner_name, "james-monitoring") else ""
                self._git("-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false",
                          "commit", "-q", "--no-verify", "-m", f"{who}: {message}" if who else message)
        except RuntimeError as e:
            log.error("commit failed: %s", e)
            self.update_state(lambda s: s.__setitem__("commit_error", str(e)[:300]))
            return False
        if self.state().get("commit_error"):
            self.update_state(lambda s: s.pop("commit_error", None))
        if self.cfg.workspace_push:
            self.push_later()
        return True

    def push_later(self) -> None:
        """Push in the background, one push at a time: a slow or dead remote never freezes the team."""
        with self._push_guard:
            if self._pushing:
                self._push_again = True
                return
            self._pushing = True

        def run():
            while True:
                self.push()
                with self._push_guard:
                    if not self._push_again:
                        self._pushing = False
                        return
                    self._push_again = False
        threading.Thread(target=run, daemon=True, name="jm-push").start()

    def wait_push(self, timeout: float = 30) -> None:
        """Wait for a background push to finish (shutdown, tests)."""
        import time
        end = time.time() + timeout
        while self._pushing and time.time() < end:
            time.sleep(0.05)

    def push(self) -> bool:
        """Push to the remote. First push to an empty remote sets the upstream. Failures never stop the
        team; they are recorded in state and shown by the manager's checks."""
        err = ""
        try:
            self._git("push", "-q", timeout=120)
        except RuntimeError:
            try:
                self._git("push", "-q", "-u", "origin", "HEAD", timeout=120)
            except RuntimeError as e:
                err = str(e)[:200]
        self.update_state(lambda s: s.__setitem__("push_error", err))
        return not err

    def git_log(self, n: int = 20, path: Path | None = None, since: str | None = None) -> str:
        args = ["log", f"-{n}", "--date=short", "--pretty=format:%h %ad %s"]          # who did it is in the subject
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
        for candidate in (p, p.with_name("state.json.bak")):
            if not candidate.exists():
                continue
            try:
                data = json.loads(candidate.read_text())
                if isinstance(data, dict):
                    if candidate != p:
                        log.error("state.json was unreadable; recovered from state.json.bak")
                    return data
            except (json.JSONDecodeError, OSError):
                log.error("%s is unreadable", candidate)
        return {}

    def update_state(self, fn) -> dict[str, Any]:
        p = self._state_path()
        with path_lock(p):
            s = self.state()
            fn(s)
            text = json.dumps(s, indent=2, sort_keys=True)
            if p.exists():
                try:
                    json.loads(p.read_text())
                    atomic_write(p.with_name("state.json.bak"), p.read_text())
                except (json.JSONDecodeError, OSError):
                    pass                                     # never back up a broken file over a good one
            atomic_write(p, text)
            return s
