"""Hands for the agents: run a coding CLI (Claude Code, Codex, Aider, ...) on a git branch.

The command is plain config, e.g.
    executor.command: ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]
    executor.command: ["codex", "exec", "--full-auto", "{prompt}"]
so switching tools is a config change. Work always happens in a separate worktree on
branch jm/<task>; main is only touched by `merge()` after the owner approves.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Project


class ExecutorError(Exception):
    pass


@dataclass
class RunResult:
    ok: bool
    branch: str
    worktree: str
    commit: str
    diffstat: str
    output_tail: str


def _git(repo: Path, *args: str, timeout: float = 120) -> str:
    from .workspace import run_git
    try:
        return run_git(repo, *args, timeout=timeout).strip()
    except RuntimeError as e:
        raise ExecutorError(str(e)) from None


class Executor:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.executor_command)

    def project(self, project_id: str) -> Project:
        p = self.cfg.projects.get(project_id)
        if not p or not p.repo:
            raise ExecutorError(f"project `{project_id}` has no repo configured")
        if not (Path(p.repo) / ".git").exists():
            raise ExecutorError(f"{p.repo} is not a git repo")
        return p

    def branch_for(self, task_id: str) -> str:
        return f"jm/{task_id}"

    def _worktree(self, p: Project, task_id: str) -> Path:
        repo = Path(p.repo).resolve()
        return repo.parent / ".jm-worktrees" / f"{repo.name}-{task_id}"

    async def run(self, *, project_id: str, task_id: str, prompt: str) -> RunResult:
        if not self.enabled:
            raise ExecutorError("executor.command is not configured")
        p = self.project(project_id)
        repo = Path(p.repo).resolve()
        branch = self.branch_for(task_id)
        wt = self._worktree(p, task_id)
        if not wt.exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            existing = _git(repo, "branch", "--list", branch)
            if existing:
                _git(repo, "worktree", "add", str(wt), branch)
            else:
                _git(repo, "worktree", "add", "-b", branch, str(wt), p.main_branch)
        cmd = [c.replace("{prompt}", prompt) for c in self.cfg.executor_command]
        if not any("{prompt}" in c for c in self.cfg.executor_command):
            cmd.append(prompt)
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=wt, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
                                                    start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.executor_timeout_minutes * 60)
        except asyncio.TimeoutError:
            import os
            import signal
            try:                                               # the whole process group: no orphans left behind
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
            raise ExecutorError(f"coding agent timed out after {self.cfg.executor_timeout_minutes} min")
        text = (out or b"").decode(errors="replace")
        _git(wt, "add", "-A")
        commit = ""
        if _git(wt, "status", "--porcelain"):
            _git(wt, "-c", "user.name=james-monitoring", "-c", "user.email=jm@localhost", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--no-verify", "-m", f"{task_id}: changes by coding agent")
        try:
            commit = _git(wt, "rev-parse", "--short", "HEAD")
            diffstat = _git(repo, "diff", "--stat", f"{p.main_branch}...{branch}")
        except ExecutorError:
            diffstat = ""
        return RunResult(ok=proc.returncode == 0 and bool(diffstat), branch=branch, worktree=str(wt),
                         commit=commit, diffstat=diffstat or "(no changes)", output_tail=text[-1500:])

    def merge(self, project_id: str, branch: str) -> str:
        p = self.project(project_id)
        repo = Path(p.repo).resolve()
        current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != p.main_branch:
            raise ExecutorError(f"{repo} is on `{current}`, expected `{p.main_branch}` — not merging")
        if _git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise ExecutorError(f"{repo} has uncommitted changes — not merging")
        before = _git(repo, "rev-parse", "HEAD")
        try:
            _git(repo, "-c", "user.name=james-monitoring", "-c", "user.email=jm@localhost", "-c", "commit.gpgsign=false",
                 "merge", "--no-ff", "--no-verify", "-m", f"Merge {branch} (approved)", branch)
        except ExecutorError as e:
            try:                                              # never leave the real repo half-merged
                _git(repo, "merge", "--abort")
            except ExecutorError:
                _git(repo, "reset", "--hard", before)
            raise ExecutorError(f"merge conflict or error, rolled back: {str(e)[:300]}") from None
        sha = _git(repo, "rev-parse", "--short", "HEAD")
        if p.push:
            try:
                _git(repo, "push", "-q")
            except ExecutorError as e:
                raise ExecutorError(f"merged locally ({sha}) but the push failed: {e}") from None
        return sha
