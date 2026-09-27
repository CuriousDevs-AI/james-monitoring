from __future__ import annotations

from ..config import LLMConfig
from . import LLMError, LLMResult


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, cfg: LLMConfig):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("pip install 'james-monitoring[anthropic]'") from e
        if not cfg.model:
            raise LLMError("llm.model is empty — set a current Claude model id in config.yaml")
        kwargs = {"api_key": cfg.api_key or None}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.cfg = cfg

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        try:
            r = self.client.messages.create(
                model=self.cfg.model, max_tokens=self.cfg.max_tokens, temperature=self.cfg.temperature,
                system=system, messages=messages,
            )
        except Exception as e:
            raise LLMError(f"Anthropic call failed: {e}") from e
        text = "".join(getattr(b, "text", "") for b in r.content)
        u = getattr(r, "usage", None)
        return LLMResult(text=text, input_tokens=getattr(u, "input_tokens", 0) or 0,
                         output_tokens=getattr(u, "output_tokens", 0) or 0)
