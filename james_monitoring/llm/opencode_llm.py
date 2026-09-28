"""Any model through OpenCode (`opencode run`): Claude, GPT/Codex, GLM, Gemini, local models — and OpenCode's
free models — with the logins OpenCode already holds (`opencode providers login`).

    llm: { provider: opencode, model: zhipuai/glm-4.6 }              # GLM
    llm: { provider: opencode, model: anthropic/claude-sonnet-4-5 }  # Claude via your OpenCode login
    llm: { provider: opencode, model: opencode/big-pickle, allow_free: true }   # free (see below)

Locked down on every call:
- an empty, isolated OpenCode config — none of your global MCP servers, plugins or permissions are loaded;
- no secrets in its environment (API keys, bot tokens stay in this process);
- paid models: every tool off and every permission denied — it can only read the prompt and answer.

OpenCode's free tier only answers stock OpenCode (tools on), so free models are opt-in (`allow_free: true`) and run
with a throwaway HOME in an empty folder; anything outside that folder is refused by OpenCode.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from ..config import LLMConfig
from . import LLMError, LLMResult
from ._proc import run, safe_env
from .claude_code_llm import _transcript, _workdir

LOCKED = json.dumps({"tools": {"*": False}, "permission": {"*": "deny"}, "mcp": {}})


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
            cache = t.get("cache") or {}
            tin += int(t.get("input", 0) or 0) + int(cache.get("read", 0) or 0) + int(cache.get("write", 0) or 0)
            cached += int(cache.get("read", 0) or 0)
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
                           "`jm connection login opencode` (free models need no login).")
        if "/" not in (cfg.model or ""):
            raise LLMError("OpenCode needs a model like provider/model — e.g. zhipuai/glm-4.6, "
                           "anthropic/claude-sonnet-4-5, or opencode/big-pickle (free). See `opencode models`.")
        self.cfg = cfg
        self.free = cfg.model.startswith("opencode/")
        if self.free and not cfg.allow_free:
            raise LLMError("Free OpenCode models run OpenCode's own agent, whose tools can't be switched off. "
                           "Turn on “Allow free models” for this model (they run isolated: empty folder, throwaway "
                           "home, no secrets) — or pick a paid model.")

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        prompt = (f"# Instructions (follow them exactly; do not use any tools)\n{system}\n\n# Conversation\n"
                  f"{_transcript(messages)}\n\nAnswer with the JSON object described above and nothing else.")
        base = _workdir("opencode")
        cwd = tempfile.mkdtemp(prefix="call-", dir=base)                 # empty: nothing to read or change
        cfg_home = os.path.join(base, "config")                          # empty config: no MCP, plugins, rules
        os.makedirs(cfg_home, exist_ok=True)
        if self.free:
            home = os.path.join(base, "free-home")                       # throwaway home, reused (it caches ~90MB)
            os.makedirs(home, exist_ok=True)
            env = safe_env(HOME=home, XDG_CONFIG_HOME=cfg_home, XDG_DATA_HOME=os.path.join(home, "data"),
                           XDG_STATE_HOME=os.path.join(home, "state"), XDG_CACHE_HOME=os.path.join(home, "cache"))
            cmd = [self.bin, "run", "--format", "json", "-m", self.cfg.model]
        else:
            env = safe_env(XDG_CONFIG_HOME=cfg_home, OPENCODE_CONFIG_CONTENT=LOCKED)
            cmd = [self.bin, "--pure", "run", "--format", "json", "-m", self.cfg.model]
        try:
            r = run(cmd, input=prompt, cwd=cwd, env=env, what="opencode",
                    timeout=int(os.environ.get("JM_OPENCODE_TIMEOUT", "180")))
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        text, tin, tout, cached, err = parse_events(r.stdout or "")
        if err or not text:
            noise = [ln for ln in (r.stderr or "").splitlines() if ln.strip() and "opencode-claude-auth" not in ln]
            detail = err or ("\n".join(noise)[-300:] if noise else "") or "no answer"
            hint = "" if self.free else (f" — is {self.cfg.model.split('/')[0]} logged in? "
                                         f"`jm connection login opencode`")
            raise LLMError(f"opencode ({self.cfg.model}) failed: {detail}{hint}")
        return LLMResult(text=text, input_tokens=tin, output_tokens=tout, cached_tokens=cached)
