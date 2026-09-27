from __future__ import annotations

from ..config import LLMConfig
from . import LLMError, LLMResult


class OpenAILLM:
    """Any OpenAI-compatible chat endpoint (OpenAI, Ollama at http://localhost:11434/v1, OpenRouter, vLLM...)."""
    name = "openai"

    def __init__(self, cfg: LLMConfig):
        try:
            import openai
        except ImportError as e:  # pragma: no cover
            raise LLMError("pip install 'james-monitoring[openai]'") from e
        if not cfg.model:
            raise LLMError("llm.model is empty — set a model id in config.yaml")
        self.client = openai.OpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url or None)
        self.cfg = cfg

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        try:
            r = self.client.chat.completions.create(
                model=self.cfg.model, max_tokens=self.cfg.max_tokens, temperature=self.cfg.temperature,
                messages=[{"role": "system", "content": system}, *messages],
            )
        except Exception as e:
            raise LLMError(f"OpenAI-compatible call failed: {e}") from e
        text = r.choices[0].message.content or ""
        u = getattr(r, "usage", None)
        return LLMResult(text=text, input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                         output_tokens=getattr(u, "completion_tokens", 0) or 0)
