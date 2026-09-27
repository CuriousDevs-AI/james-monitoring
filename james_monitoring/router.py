"""Who should handle a group message? Pure functions so they are easy to test."""
from __future__ import annotations

import re

from .config import Config

_MENTION = re.compile(r"@([A-Za-z0-9_]+)")
_STATUS = re.compile(r"\b(status|update|updates|standup|kya\s+chal\s+raha)\b", re.I)


def group_targets(text: str, cfg: Config, usernames: dict[str, str] | None = None) -> tuple[bool, list[str]]:
    """Returns (is_all, member_ids). No mention → the monitor (James) handles it."""
    usernames = {k: v.lower() for k, v in (usernames or {}).items()}
    by_username = {v: k for k, v in usernames.items()}
    ids: list[str] = []
    for tok in _MENTION.findall(text):
        t = tok.lower()
        if t in ("all", "everyone", "team"):
            return True, [m.id for m in cfg.team]
        mid = by_username.get(t)
        m = cfg.member(mid) if mid else cfg.member(t)
        if m and m.id not in ids:
            ids.append(m.id)
    return False, ids or [cfg.monitor.id]


def is_status_request(text: str) -> bool:
    """Short '@all status' style messages get an instant answer from the task board (no model call)."""
    stripped = _MENTION.sub("", text).strip(" ?!.,")
    return bool(_STATUS.search(stripped)) and len(stripped) <= 60
