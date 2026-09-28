"""Any model through OpenCode (`opencode run`): Claude, GPT/Codex, GLM, Gemini, local models — and OpenCode's
free models — with the logins OpenCode already holds (`opencode providers login`).

    llm: { provider: opencode, model: opencode/big-pickle }          # free
    llm: { provider: opencode, model: zhipuai/glm-4.6 }              # GLM
    llm: { provider: opencode, model: anthropic/claude-sonnet-4-5 }  # Claude via your OpenCode login

Paid providers run with OpenCode's tools denied (no shell, no edits, no web) — the team's prompt is the only
context. OpenCode's free tier only answers stock OpenCode, so free models (`opencode/…`) run unmodified in an
empty folder, and the prompt tells them not to use tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from ..config import LLMConfig
from . import LLMError, LLMResult
from .claude_code_llm import _transcript, _workdir

LOCKED = json.dumps({"permission": {"edit": "deny", "bash": "deny", "webfetch": "deny", "external_directory": "deny"}})


def parse_events(stdout: str) -> tuple[str, int, int, int, str]:
    """(text, input tokens, output tokens, cached tokens, error) from `opencode run --format json`."""
    texts, tin, tout, cached, err = [], 0, 0, 0, ""
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = ev.get("part") or {}
        if ev.get("type") == "text" and part.get("text"):
            texts.append(part["text"])
        elif ev.get("type") == "step_finish":
            t = part.get("tokens") or {}
            tin += int(t.get("input", 0) or 0) + int((t.get("cache") or {}).get("read", 0) or 0)
            cached += int((t.get("cache") or {}).get("read", 0) or 0)
            tout += int(t.get("output", 0) or 0) + int(t.get("reasoning", 0) or 0)
        elif ev.get("type") == "error":
            e = ev.get("error") or {}
            err = (e.get("data") or {}).get("message") or e.get("name") or "error"
    return "".join(texts).strip(), tin, tout, cached, err


class OpenCodeLLM:
    name = "opencode"

    def __init__(self, cfg: LLMConfig):
        self.bin = os.environ.get("JM_OPENCODE_BIN") or shutil.which("opencode")
        if not self.bin:
            raise LLMError("`opencode` CLI not found. Install it (npm install -g opencode-ai) and log in with "
                           "`opencode providers login` (free models need no login).")
        if "/" not in (cfg.model or ""):
            raise LLMError("OpenCode needs a model like provider/model — e.g. opencode/big-pickle (free), "
                           "zhipuai/glm-4.6, anthropic/claude-sonnet-4-5. See `opencode models`.")
        self.cfg = cfg
        self.free = cfg.model.startswith("opencode/")

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        prompt = (f"# Instructions (follow them exactly; do not use any tools)\n{system}\n\n# Conversation\n"
                  f"{_transcript(messages)}\n\nAnswer with the JSON object described above and nothing else.")
        cwd = tempfile.mkdtemp(prefix="call-", dir=_workdir("opencode"))      # empty: nothing to read or change
        env = {**os.environ}
        if not self.free:
            env["OPENCODE_CONFIG_CONTENT"] = LOCKED
        try:
            r = subprocess.run([self.bin, "run", "--format", "json", "-m", self.cfg.model], input=prompt,
                               capture_output=True, text=True, cwd=cwd, env=env,
                               timeout=int(os.environ.get("JM_OPENCODE_TIMEOUT", "240")))
        except subprocess.TimeoutExpired as e:
            raise LLMError("opencode timed out") from e
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        text, tin, tout, cached, err = parse_events(r.stdout or "")
        if err or not text:
            detail = err or (r.stderr or "").strip()[-300:] or "no answer"
            hint = "" if self.free else f" — is {self.cfg.model.split('/')[0]} logged in? `opencode providers login`"
            raise LLMError(f"opencode ({self.cfg.model}) failed: {detail}{hint}")
        return LLMResult(text=text, input_tokens=tin, output_tokens=tout, cached_tokens=cached)
