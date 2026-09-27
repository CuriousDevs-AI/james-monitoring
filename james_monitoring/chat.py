"""Chat rooms for the web console: one room per team member (like a DM) and a 'team' room (like the group).
Stored in .jm/chat/<room>.jsonl (runtime data, not committed)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

TEAM_ROOM = "team"


class ChatStore:
    def __init__(self, root: Path):
        self.dir = root / ".jm" / "chat"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def _path(self, room: str) -> Path:
        safe = "".join(c for c in room.lower() if c.isalnum() or c in "_-") or "team"
        return self.dir / f"{safe}.jsonl"

    def _count(self, room: str) -> int:
        if room not in self._counts:
            p = self._path(room)
            self._counts[room] = sum(1 for _ in p.open()) if p.exists() else 0
        return self._counts[room]

    def append(self, room: str, who: str, text: str, kind: str = "msg", **extra) -> dict:
        with self._lock:
            n = self._count(room)
            msg = {"i": n, "ts": time.time(), "who": who, "text": text, "kind": kind, **extra}
            with self._path(room).open("a") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._counts[room] = n + 1
            return msg

    def since(self, room: str, after: int = -1, limit: int = 200) -> list[dict]:
        p = self._path(room)
        if not p.exists():
            return []
        out = []
        with p.open() as f:
            for line in f:
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("i", -1) > after:
                    out.append(m)
        return out[-limit:]

    def last_index(self, room: str) -> int:
        with self._lock:
            return self._count(room) - 1


class WebBus:
    """Delivers what agents send to the owner into the console's chat rooms."""

    def __init__(self, chat: ChatStore):
        self.chat = chat

    async def send_owner(self, from_id: str, text: str, ask=None, urgent: bool = False) -> None:
        if ask is not None:
            self.chat.append(from_id, from_id, ask.card(), kind="ask", ask_id=ask.id)
        else:
            self.chat.append(from_id, from_id, text, kind="notice" if urgent else "msg")

    async def post_group(self, from_id: str, text: str, reply_to: int | None = None) -> None:
        self.chat.append(TEAM_ROOM, from_id, text)


class MultiBus:
    """Send to every connected channel (web console + Telegram). One failing channel never blocks the others."""

    def __init__(self, *buses):
        self.buses = [b for b in buses if b is not None]

    async def send_owner(self, from_id, text, ask=None, urgent=False):
        for b in self.buses:
            try:
                await b.send_owner(from_id, text, ask=ask, urgent=urgent)
            except Exception:  # noqa: BLE001
                pass

    async def post_group(self, from_id, text, reply_to=None):
        for b in self.buses:
            try:
                await b.post_group(from_id, text, reply_to=reply_to)
            except Exception:  # noqa: BLE001
                pass
