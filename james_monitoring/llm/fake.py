from __future__ import annotations

import json
from collections import deque

from . import LLMResult


class FakeLLM:
    """Scriptable model for tests / dry runs. Queue replies with .push(); default echoes a JSON reply."""
    name = "fake"

    def __init__(self):
        self.queue: deque[str] = deque()
        self.calls: list[tuple[str, list[dict]]] = []

    def push(self, reply: str | dict) -> None:
        self.queue.append(reply if isinstance(reply, str) else json.dumps(reply))

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        self.calls.append((system, messages))
        if self.queue:
            text = self.queue.popleft()
        else:
            last = messages[-1]["content"] if messages else ""
            text = json.dumps({"reply": f"(fake) received: {last[-200:]}", "actions": []})
        return LLMResult(text=text, input_tokens=len(system) // 4, output_tokens=len(text) // 4)
