"""Chat rooms: the one record of every conversation, whichever channel it happened in.

Rooms:
    team            All hands (the whole company; the Telegram group / a Slack channel)
    p-<project>     a project's room (its team only)
    <member id>     the owner's 1:1 chat with that person
    backchannel     teammates talking to each other when it isn't about one project (read-only for the owner)

Stored in .jm/chat/<room>.jsonl (runtime data, not committed). Each message:
    {"i", "ts", "who", "text", "kind": msg|notice|ask|system|internal, "via"?: console|telegram|slack, "ask_id"?,
     "reply_to"?: i of the message answered, "thread"?: i of the thread's first message, "reply_quote"?: "Who: …"}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_CACHE: dict[str, tuple[int, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()

TEAM_ROOM = "team"
PROJECT_ROOM = "p-"                  # project rooms are "p-<project id>"
BACKCHANNEL = "backchannel"


class ChatStore:
    def __init__(self, root: Path):
        self.dir = Path(root) / ".jm" / "chat"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def _path(self, room: str) -> Path:
        safe = "".join(c for c in room.lower() if c.isalnum() or c in "_-") or "team"
        return self.dir / f"{safe}.jsonl"

    def _count(self, room: str) -> int:
        msgs = self._messages(room)                       # always from the file: several writers stay consistent
        return (msgs[-1].get("i", len(msgs) - 1) + 1) if msgs else 0

    def append(self, room: str, who: str, text: str, kind: str = "msg", **extra) -> dict:
        from .fileio import path_lock
        with self._lock, path_lock(self._path(room)):
            n = self._count(room)
            msg = {"i": n, "ts": time.time(), "who": who, "text": text, "kind": kind,
                   **{k: v for k, v in extra.items() if v not in (None, "")}}
            with self._path(room).open("a") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._counts[room] = n + 1
            return msg

    def _messages(self, room: str) -> list[dict]:
        """All messages of a room, read incrementally: only bytes added since the last read are parsed
        (shared by every ChatStore on the file, so a reload or `jm chat` next to `jm run` stays in sync)."""
        p = self._path(room)
        if not p.exists():
            return []
        key = str(p)
        with _CACHE_LOCK:
            size = p.stat().st_size
            offset, msgs = _CACHE.get(key, (0, []))
            if size < offset:                                 # file replaced/truncated: start over
                offset, msgs = 0, []
            if size > offset:
                with p.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read(size - offset)
                end = chunk.rfind(b"\n") + 1                  # only complete lines
                for line in chunk[:end].splitlines():
                    try:
                        msgs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                offset += end
                _CACHE[key] = (offset, msgs)
            return msgs

    def since(self, room: str, after: int = -1, limit: int = 200) -> list[dict]:
        msgs = self._messages(room)
        if after < 0:
            return msgs[-limit:]
        lo, hi = 0, len(msgs)                                 # messages are in index order: binary search
        while lo < hi:
            mid = (lo + hi) // 2
            if msgs[mid].get("i", -1) > after:
                hi = mid
            else:
                lo = mid + 1
        return msgs[lo:][-limit:]

    def get(self, room: str, i: int) -> dict | None:
        """One message by its index (None if there's no such message)."""
        if i is None or int(i) < 0:
            return None
        hit = self.since(room, int(i) - 1, limit=1_000_000)[:1]
        return hit[0] if hit and hit[0].get("i") == int(i) else None

    def thread(self, room: str, root: int) -> list[dict]:
        """A thread: its first message and every reply to it, in order."""
        msgs = self._messages(room)
        return [m for m in msgs if m.get("i") == root or m.get("thread") == root]

    def recent(self, room: str, n: int = 20, max_age_hours: float | None = None) -> list[dict]:
        msgs = self.since(room, -1, limit=n)
        if max_age_hours is not None:
            cut = time.time() - max_age_hours * 3600
            msgs = [m for m in msgs if m.get("ts", 0) >= cut]
        return msgs

    def last_index(self, room: str) -> int:
        with self._lock:
            return self._count(room) - 1
