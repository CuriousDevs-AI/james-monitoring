"""Claude through the Claude Code CLI (`claude -p`) — uses whatever the CLI is logged in with,
including a Claude Pro/Max subscription. No API key needed. Subscription usage limits apply.

Setup once on the server:  npm install -g @anthropic-ai/claude-code  &&  claude   (then /login)
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid

from ..config import LLMConfig
from . import LLMError, LLMResult
from ._proc import run, safe_env


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
    """One empty scratch folder per process (used when no sandbox folder is given)."""
    if kind not in _DIRS or not os.path.isdir(_DIRS[kind]):
        _DIRS[kind] = tempfile.mkdtemp(prefix=f"jm-{kind}-")
    return _DIRS[kind]


def base_dir(sandbox: str | None, kind: str) -> str:
    """Where a CLI keeps its per-person folders: the company's own sandbox (stable across restarts) or a temp dir."""
    d = os.path.join(sandbox, kind) if sandbox else _workdir(kind)
    os.makedirs(d, exist_ok=True)
    return d


def session_dir(sandbox: str | None, kind: str, key: str) -> str:
    """A stable, empty folder per session: the CLIs file sessions by folder, so resuming needs the same one."""
    import hashlib
    d = os.path.join(base_dir(sandbox, kind), "s", hashlib.sha1(key.encode()).hexdigest()[:16])
    os.makedirs(d, exist_ok=True)
    return d


def system_hash(system: str) -> str:
    import hashlib
    return hashlib.sha1(system.encode()).hexdigest()[:16]


def session_prompt(system: str, messages: list[dict], session, resume: bool) -> tuple[str, str]:
    """(prompt, system hash sent) for CLIs without a separate system prompt (Codex, OpenCode). A resumed session
    already holds the conversation: send only the new message — and the team prompt only if it changed."""
    h = system_hash(system)
    if resume and session.system_hash == h:
        return messages[-1]["content"], h
    if resume:
        return (f"# Updated instructions and context (replace the earlier ones; do not use any tools)\n{system}\n\n"
                f"# New message\n{messages[-1]['content']}\n\nAnswer with the JSON object described above."), h
    return (f"# Instructions (follow them exactly; do not use any tools)\n{system}\n\n# Conversation\n"
            f"{_transcript(messages)}\n\nAnswer with the JSON object described above and nothing else."), h


class ClaudeCodeLLM:
    name = "claude-code"
    sessions = True                                   # can keep a conversation going across calls and restarts

    def __init__(self, cfg: LLMConfig):
        self.bin = os.environ.get("JM_CLAUDE_BIN") or shutil.which("claude")
        if not self.bin:
            raise LLMError("`claude` CLI not found. Install it (npm install -g @anthropic-ai/claude-code), "
                           "run `claude` once and /login with your subscription.")
        self.cfg = cfg
        self.sandbox: str | None = None               # set by the runtime: <workspace>/.jm/sandbox

    def complete(self, system: str, messages: list[dict], session=None) -> LLMResult:
        resume = bool(session and session.id)
        try:
            return self._call(system, messages, session, resume)
        except LLMError as e:
            if resume and re.search(r"session|conversation", str(e), re.I):    # gone or unreadable → start fresh
                session.id = ""
                return self._call(system, messages, session, False)
            raise

    def _call(self, system: str, messages: list[dict], session, resume: bool) -> LLMResult:
        cwd = session_dir(self.sandbox, "claude", session.key) if session else base_dir(self.sandbox, "claude")
        tmp = os.path.join(base_dir(self.sandbox, "claude"), "tmp")
        os.makedirs(tmp, exist_ok=True)
        # The system prompt goes in a file: command lines are limited (128 KB per argument on Linux).
        fd, sys_file = tempfile.mkstemp(prefix="system-", suffix=".md", dir=tmp)
        with os.fdopen(fd, "w") as f:
            f.write(system)
        # No tools, no MCP servers, no user/project settings (so no hooks) — the team prompt is the only context.
        cmd = [self.bin, "-p", "--output-format", "json", "--system-prompt-file", sys_file,
               "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--setting-sources", "local", "--disable-slash-commands"]
        sid = ""
        if session:
            sid = session.id if resume else str(uuid.uuid4())
            cmd += ["--resume", sid] if resume else ["--session-id", sid]
        else:
            cmd.append("--no-session-persistence")
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        prompt = messages[-1]["content"] if resume else _transcript(messages)
        try:
            r = run(cmd, input=prompt, cwd=cwd, env=safe_env(),
                    timeout=int(os.environ.get("JM_CLAUDE_TIMEOUT", "180")), what="claude CLI")
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
                         truncated=data.get("stop_reason") == "max_tokens", cached_tokens=cached,
                         session_id=str(data.get("session_id") or sid), system_hash=system_hash(system))
