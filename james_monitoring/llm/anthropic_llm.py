from __future__ import annotations

from ..config import LLMConfig
from . import LLMError, LLMResult


def clean_messages(messages: list[dict]) -> list[dict]:
    """The API refuses empty turns and two turns in a row from the same side: fix both so one bad reply
    can't break a conversation forever."""
    out: list[dict] = []
    for m in messages:
        content = str(m.get("content") or "").strip() or "(empty)"
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + content}
        else:
            out.append({"role": m["role"], "content": content})
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(conversation start)"})
    return out


def _with_images(msgs: list[dict], original: list[dict]) -> list[dict]:
    from . import image_blocks
    imgs = image_blocks((original[-1] if original else {}).get("images") or [])
    if imgs and msgs and msgs[-1]["role"] == "user":
        msgs[-1] = {"role": "user", "content": [
            *({"type": "image", "source": {"type": "base64", "media_type": mt, "data": d}} for mt, d in imgs),
            {"type": "text", "text": msgs[-1]["content"]}]}
    return msgs


class AnthropicLLM:
    name = "anthropic"
    images = True                                    # can see images attached to the last message

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
                # The system prompt (charter, persona, memory, board) is the same across a conversation: cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=_with_images(clean_messages(messages), messages),
            )
        except Exception as e:
            raise LLMError(f"Anthropic call failed: {e}") from e
        text = "".join(getattr(b, "text", "") for b in r.content)
        u = getattr(r, "usage", None)
        cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        return LLMResult(text=text, input_tokens=(getattr(u, "input_tokens", 0) or 0)
                         + int(getattr(u, "cache_creation_input_tokens", 0) or 0) + cached,
                         output_tokens=getattr(u, "output_tokens", 0) or 0,
                         truncated=getattr(r, "stop_reason", "") == "max_tokens", cached_tokens=cached)
