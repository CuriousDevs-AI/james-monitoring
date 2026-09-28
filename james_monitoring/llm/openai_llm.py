from __future__ import annotations

import re

from ..config import LLMConfig
from . import LLMError, LLMResult

# Reasoning models (o1, o3, o4-mini, gpt-5…) take max_completion_tokens and only their default temperature.
_REASONING = re.compile(r"^(o\d|gpt-5)", re.I)


class OpenAILLM:
    """Any OpenAI-compatible chat endpoint (OpenAI, Ollama at http://localhost:11434/v1, OpenRouter, vLLM...)."""
    name = "openai"
    images = True                                    # sent inline; a text-only endpoint gets the message without them

    def __init__(self, cfg: LLMConfig):
        try:
            import openai
        except ImportError as e:  # pragma: no cover
            raise LLMError("pip install 'james-monitoring[openai]'") from e
        if not cfg.model:
            raise LLMError("llm.model is empty — set a model id in config.yaml")
        self.client = openai.OpenAI(api_key=cfg.api_key or "not-needed", base_url=cfg.base_url or None)
        self.cfg = cfg
        self.reasoning = bool(_REASONING.match(cfg.model.split("/")[-1]))
        self.json_mode = True                    # switched off if the endpoint doesn't support it

    def _kwargs(self) -> dict:
        kw: dict = {"model": self.cfg.model}
        if self.reasoning:
            kw["max_completion_tokens"] = self.cfg.max_tokens
        else:
            kw["max_tokens"] = self.cfg.max_tokens
            kw["temperature"] = self.cfg.temperature
        if self.json_mode:
            kw["response_format"] = {"type": "json_object"}
        return kw

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        from . import image_blocks
        msgs = [{"role": "system", "content": system},
                *({"role": m["role"], "content": str(m.get("content") or "") or "(empty)"} for m in messages)]
        imgs = image_blocks((messages[-1] if messages else {}).get("images") or [])
        if imgs:
            msgs[-1] = {"role": msgs[-1]["role"], "content": [
                {"type": "text", "text": msgs[-1]["content"]},
                *({"type": "image_url", "image_url": {"url": f"data:{mt};base64,{d}"}} for mt, d in imgs)]}
        for _attempt in range(4):
            try:
                r = self.client.chat.completions.create(messages=msgs, **self._kwargs())
                break
            except Exception as e:
                err = str(e)
                # Adapt once to what this endpoint/model accepts, then retry.
                if "max_completion_tokens" in err and not self.reasoning:
                    self.reasoning = True
                    continue
                if "response_format" in err and self.json_mode:
                    self.json_mode = False
                    continue
                if "temperature" in err and not self.reasoning:
                    self.reasoning = True
                    continue
                if imgs and re.search(r"image|vision|multimodal|content.*(array|list)", err, re.I):
                    imgs = []                                 # a text-only model: send the words, say so
                    text = msgs[-1]["content"][0]["text"] if isinstance(msgs[-1]["content"], list) else msgs[-1]["content"]
                    msgs[-1] = {"role": msgs[-1]["role"], "content": text + "\n\n(An image was attached, but this "
                                                                             "model can't see images.)"}
                    continue
                raise LLMError(f"OpenAI-compatible call failed: {e}") from e
        else:  # pragma: no cover
            raise LLMError("OpenAI-compatible call failed after adapting parameters")
        choice = r.choices[0]
        u = getattr(r, "usage", None)
        details = getattr(u, "prompt_tokens_details", None)
        return LLMResult(text=choice.message.content or "", input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                         output_tokens=getattr(u, "completion_tokens", 0) or 0,
                         truncated=getattr(choice, "finish_reason", "") == "length",
                         cached_tokens=int(getattr(details, "cached_tokens", 0) or 0) if details else 0)
