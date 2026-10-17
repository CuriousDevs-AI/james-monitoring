"""Explicit, bounded fallback for inference only. Never replay external actions or coding jobs."""
from dataclasses import replace
import re

from . import LLMError, is_transient, make_llm


def can_fallback(error: str) -> bool:
    # Authentication, policy and invalid requests must be fixed, not bypassed with another provider.
    if re.search(r"\b(400|401|403|404)\b|unauthori|forbidden|api.?key|not logged in|permission|"
                 r"invalid|not found|safety|content.?filter|guardrail", error, re.I):
        return False
    return is_transient(error) or bool(re.search(
        r"\b402\b|quota|insufficient.(credit|balance)|credit.*(exhaust|balance)|"
        r"usage limit|hit.*limit|reached.*limit|out of (tokens|credits)", error, re.I))


class FallbackLLM:
    """Stateless when fallback is enabled: CLI sessions must never miss turns answered by another model."""
    name = "fallback"
    sandbox = None

    def __init__(self, cfg):
        self.candidates = [replace(cfg, fallbacks=[]), *cfg.fallbacks]
        self.clients = {}

    def complete(self, system, messages):
        for i, cfg in enumerate(self.candidates):
            try:
                if i not in self.clients:
                    self.clients[i] = make_llm(cfg)
                client = self.clients[i]
                if hasattr(client, "sandbox"):
                    client.sandbox = self.sandbox
                result = client.complete(system, messages)
                result.model = cfg.provider + ("/" + cfg.model if cfg.model else "")
                result.fallback_used = i > 0
                return result
            except LLMError as e:
                if i == len(self.candidates) - 1 or not can_fallback(str(e)):
                    raise
        raise LLMError("No configured model answered.")  # pragma: no cover
