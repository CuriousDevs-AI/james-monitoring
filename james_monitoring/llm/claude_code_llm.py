"""Claude through the Claude Code CLI (`claude -p`) — uses whatever the CLI is logged in with,
including a Claude Pro/Max subscription. No API key needed. Subscription usage limits apply.

Setup once on the server:  npm install -g @anthropic-ai/claude-code  &&  claude   (then /login)
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile

from ..config import LLMConfig
from . import LLMError, LLMResult


def _transcript(messages: list[dict]) -> str:
    """The CLI takes one prompt per call, so earlier turns are replayed as a transcript."""
    if len(messages) == 1:
        return messages[0]["content"]
    lines = ["Earlier in this conversation:"]
    for m in messages[:-1]:
        who = "You replied" if m["role"] == "assistant" else "Message"
        lines.append(f"--- {who} ---\n{m['content']}")
    lines.append("--- New message (answer this one) ---\n" + messages[-1]["content"])
    return "\n\n".join(lines)


_DIRS: dict[str, str] = {}


def _workdir(kind: str) -> str:
    """One empty scratch folder per process (not one per reload)."""
    if kind not in _DIRS or not os.path.isdir(_DIRS[kind]):
        _DIRS[kind] = tempfile.mkdtemp(prefix=f"jm-{kind}-")
    return _DIRS[kind]


class ClaudeCodeLLM:
    name = "claude-code"

    def __init__(self, cfg: LLMConfig):
        self.bin = os.environ.get("JM_CLAUDE_BIN") or shutil.which("claude")
        if not self.bin:
            raise LLMError("`claude` CLI not found. Install it (npm install -g @anthropic-ai/claude-code), "
                           "run `claude` once and /login with your subscription.")
        self.cfg = cfg
        # An empty working dir so no project CLAUDE.md or settings leak into the team's prompts.
        self.cwd = _workdir("claude")

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        # The system prompt goes in a file: command lines are limited (128 KB per argument on Linux).
        fd, sys_file = tempfile.mkstemp(prefix="system-", suffix=".md", dir=self.cwd)
        with os.fdopen(fd, "w") as f:
            f.write(system)
        cmd = [self.bin, "-p", "--output-format", "json", "--system-prompt-file", sys_file,
               "--tools", "", "--no-session-persistence"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        try:
            r = subprocess.run(cmd, input=_transcript(messages), capture_output=True, text=True, cwd=self.cwd,
                               stdin=None, timeout=int(os.environ.get("JM_CLAUDE_TIMEOUT", "180")))
        except subprocess.TimeoutExpired as e:
            raise LLMError("claude CLI timed out") from e
        finally:
            with contextlib.suppress(OSError):
                os.unlink(sys_file)
        out = (r.stdout or "").strip()
        try:
            data = json.loads(out.splitlines()[-1] if out else "{}")
        except json.JSONDecodeError:
            data = {}
        if r.returncode != 0 or data.get("is_error") or "result" not in data:
            detail = data.get("result") or (r.stderr or out or "no output").strip()[-400:]
            raise LLMError(f"claude CLI failed: {detail}")
        u = data.get("usage") or {}
        cached = int(u.get("cache_read_input_tokens", 0) or 0)
        tokens_in = int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0)) + cached
        return LLMResult(text=str(data["result"]), input_tokens=tokens_in, output_tokens=int(u.get("output_tokens", 0)),
                         truncated=data.get("stop_reason") == "max_tokens", cached_tokens=cached)
