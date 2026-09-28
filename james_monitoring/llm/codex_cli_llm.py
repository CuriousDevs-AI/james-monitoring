"""Codex through the Codex CLI (`codex exec`) — uses whatever the CLI is logged in with, including a
ChatGPT subscription. No API key needed.

Setup once on the server:  npm install -g @openai/codex  &&  codex login

Each call is a fresh, read-only, throw-away session in an empty folder: the team's prompt is the only context,
and the model can't touch any files. (Code changes go through the executor, on a branch, after approval.)
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
from .claude_code_llm import _transcript, _workdir


def _usage(stdout: str) -> tuple[int, int]:
    """Sum token usage from `codex exec --json` events (any event that carries a `usage` object)."""
    tin = tout = 0
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = ev.get("usage") if isinstance(ev, dict) else None
        if not isinstance(u, dict) and isinstance(ev, dict) and isinstance(ev.get("msg"), dict):
            u = ev["msg"].get("usage") or (ev["msg"].get("info") or {}).get("total_token_usage")
        if isinstance(u, dict):
            tin += int(u.get("input_tokens", 0) or 0)
            tout += int(u.get("output_tokens", 0) or 0)
    return tin, tout


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
               "--json", "--output-last-message", str(out_file), "--cd", self.cwd]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        cmd.append("-")                                    # read the prompt from stdin
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=self.cwd,
                               timeout=int(os.environ.get("JM_CODEX_TIMEOUT", "300")))
        except subprocess.TimeoutExpired as e:
            raise LLMError("codex CLI timed out") from e
        text = out_file.read_text().strip() if out_file.exists() else ""
        out_file.unlink(missing_ok=True)
        if r.returncode != 0 or not text:
            detail = (r.stderr or r.stdout or "no output").strip()[-400:]
            raise LLMError(f"codex CLI failed: {detail}")
        tin, tout = _usage(r.stdout or "")
        return LLMResult(text=text, input_tokens=tin, output_tokens=tout)
