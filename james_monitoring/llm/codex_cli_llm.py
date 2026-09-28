"""Codex through the Codex CLI (`codex exec`) — uses whatever the CLI is logged in with, including a
ChatGPT subscription. No API key needed.

Setup once on the server:  npm install -g @openai/codex  &&  codex login

Each call is a fresh, throw-away session in an empty folder with the shell, browser, apps, MCP servers and user
config switched off: the team's prompt is the only context, and the model can't read or change files. (Code changes go through the executor, on a branch, after approval.)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import LLMConfig
from . import LLMError, LLMResult
from ._proc import run, safe_env
from .claude_code_llm import _transcript, _workdir


def _usage(stdout: str) -> tuple[int, int, int]:
    """(input, output, cached) from `codex exec --json`: the final turn.completed usage (cumulative counters from
    other events are ignored so nothing is counted twice)."""
    last = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("usage"), dict):
            last = ev["usage"]
    if not last:
        return 0, 0, 0
    return (int(last.get("input_tokens", 0) or 0), int(last.get("output_tokens", 0) or 0),
            int(last.get("cached_input_tokens", 0) or 0))


# Everything that lets the model act on this machine is switched off: it can only read the prompt and answer.
DISABLE = ("shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external", "computer_use",
           "in_app_browser", "view_image", "sleep_tool", "tool_suggest", "skill_mcp_dependency_install")


class CodexCLILLM:
    name = "codex-cli"

    def __init__(self, cfg: LLMConfig):
        self.bin = os.environ.get("JM_CODEX_BIN") or shutil.which("codex")
        if not self.bin:
            raise LLMError("`codex` CLI not found. Install it (npm install -g @openai/codex) and run `codex login`.")
        self.cfg = cfg
        self.cwd = tempfile.mkdtemp(prefix="call-", dir=_workdir("codex"))

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        prompt = (f"{system}\n\n# Conversation\n{_transcript(messages)}\n\n"
                  "Answer with the JSON object described above and nothing else.")
        fd, out_name = tempfile.mkstemp(prefix="last-", suffix=".txt", dir=self.cwd)   # one per call: agents run in parallel
        os.close(fd)
        out_file = Path(out_name)
        cmd = [self.bin, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never",
               "--ignore-user-config", "--ignore-rules", "--json", "--output-last-message", str(out_file),
               "--cd", self.cwd]
        for f in DISABLE:
            cmd += ["--disable", f]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd.append("-")                                    # read the prompt from stdin
        try:
            r = run(cmd, input=prompt, cwd=self.cwd, env=safe_env(),
                    timeout=int(os.environ.get("JM_CODEX_TIMEOUT", "180")), what="codex CLI")
            text = out_file.read_text().strip() if out_file.exists() else ""
        finally:
            out_file.unlink(missing_ok=True)
        if r.returncode != 0 or not text:
            detail = (r.stderr or r.stdout or "no output").strip()[-400:]
            raise LLMError(f"codex CLI failed: {detail}")
        tin, tout, cached = _usage(r.stdout or "")
        return LLMResult(text=text, input_tokens=tin, output_tokens=tout, cached_tokens=cached)
