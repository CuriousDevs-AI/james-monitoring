"""Model adapters. The team never depends on one vendor: swap `llm.provider` in config.yaml.

    anthropic  -> Claude via the Anthropic API (API key)
    claude-code -> Claude via the Claude Code CLI — works with a Claude Pro/Max subscription, no API key
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

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


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
    if p == "fake":
        from .fake import FakeLLM
        return FakeLLM()
    raise LLMError(f"Unknown llm.provider `{cfg.provider}` (use anthropic | claude-code | openai | fake)")
