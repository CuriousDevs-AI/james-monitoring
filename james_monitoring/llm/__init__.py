"""Model adapters. The team never depends on one vendor: swap `llm.provider` in config.yaml.

    anthropic  -> Claude via the Anthropic API (API key)
    claude-code -> Claude via the Claude Code CLI — works with a Claude Pro/Max subscription, no API key
    codex-cli  -> Codex via the Codex CLI — works with a ChatGPT subscription, no API key
    opencode   -> any model through OpenCode (Claude, GPT, GLM, Gemini, free models) with OpenCode's logins
    openai     -> any OpenAI-compatible endpoint: OpenAI / Codex models, Ollama, OpenRouter, vLLM, LM Studio
    fake       -> deterministic replies for tests and dry runs
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import LLMConfig


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False          # the model hit its output limit: the reply is cut off
    cached_tokens: int = 0           # input tokens served from the provider's prompt cache (cheaper)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """What counts against the daily budget: cached input costs about a tenth of fresh input."""
        return self.input_tokens - self.cached_tokens + self.cached_tokens // 10 + self.output_tokens


class LLM(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        """messages: [{"role": "user"|"assistant", "content": str}, ...]"""
        ...


class LLMError(Exception):
    pass


def make_llm(cfg: LLMConfig) -> LLM:
    p = cfg.provider.lower()
    if p in ("anthropic", "claude"):
        from .anthropic_llm import AnthropicLLM
        return AnthropicLLM(cfg)
    if p in ("openai", "codex", "ollama", "openrouter", "openai-compatible"):
        from .openai_llm import OpenAILLM
        return OpenAILLM(cfg)
    if p in ("claude-code", "claude_code", "claude-cli", "subscription"):
        from .claude_code_llm import ClaudeCodeLLM
        return ClaudeCodeLLM(cfg)
    if p in ("codex-cli", "codex_cli", "chatgpt"):
        from .codex_cli_llm import CodexCLILLM
        return CodexCLILLM(cfg)
    if p in ("opencode", "open-code"):
        from .opencode_llm import OpenCodeLLM
        return OpenCodeLLM(cfg)
    if p == "fake":
        from .fake import FakeLLM
        return FakeLLM()
    raise LLMError(f"Unknown llm.provider `{cfg.provider}` (use claude-code | codex-cli | opencode | anthropic | openai | fake)")
