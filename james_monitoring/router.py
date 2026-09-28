"""Who should handle a message? Pure functions so they are easy to test."""
from __future__ import annotations

import re

from .config import Config

_MENTION = re.compile(r"(?<![\w.+-])@([A-Za-z0-9_]+)")        # not the "@" inside an email address
# The whole message must *be* a status request ("@all status", "give me an update", "standup?") — "@all update the
# README by Friday" is an instruction and goes to the agents.
_STATUS = re.compile(r"^(?:(?:please|pls|quick|give|share|post|send|me|us|your|an?|the|daily)\s+)*"
                     r"(?:status(?:\s+update)?|updates?|standup|progress|kya\s+chal\s+raha(?:\s+hai)?)"
                     r"(?:\s+(?:please|pls|update|report|now|today))*$", re.I)
ALL_WORDS = ("all", "everyone", "team", "here", "channel")


def mentions(text: str, cfg: Config, usernames: dict[str, str] | None = None) -> tuple[bool, list[str]]:
    """(is_all, member ids explicitly @mentioned — by id, first name or Telegram bot username)."""
    usernames = {k: v.lower() for k, v in (usernames or {}).items()}
    by_username = {v: k for k, v in usernames.items()}
    ids: list[str] = []
    for tok in _MENTION.findall(text):
        t = tok.lower()
        if t in ALL_WORDS:
            return True, [m.id for m in cfg.workers]
        mid = by_username.get(t)
        m = cfg.member(mid) if mid else cfg.member(t)
        if m and m.assistant:                          # the personal assistant is only reached in its own chat
            continue
        if not m and t in cfg.departments:             # @engineering → everyone in that department
            for x in cfg.workers:
                if x.department == t and x.id not in ids:
                    ids.append(x.id)
            continue
        if m and m.id not in ids:
            ids.append(m.id)
    return False, ids


def group_targets(text: str, cfg: Config, usernames: dict[str, str] | None = None) -> tuple[bool, list[str]]:
    """Returns (is_all, member_ids). No mention → the manager (monitor) handles it."""
    is_all, ids = mentions(text, cfg, usernames)
    return is_all, ids or [cfg.monitor.id]


def room_targets(room: str, text: str, cfg: Config, usernames: dict[str, str] | None = None) -> tuple[bool, list[str]]:
    """Who answers an owner message in a room. Returns (is_all, member_ids).

    DM → that person · All hands → @all / @names / the manager ·
    project room → @all = the project's team, @names, otherwise the lead (or the manager)."""
    from .chat import PROJECT_ROOM, TEAM_ROOM
    if room == TEAM_ROOM:
        return group_targets(text, cfg, usernames)
    if room.startswith(PROJECT_ROOM):
        pid = room[len(PROJECT_ROOM):]
        members = [m.id for m in cfg.project_members(pid)]
        is_all, ids = mentions(text, cfg, usernames)
        if is_all:
            return True, members
        if ids:
            return False, ids
        p = cfg.projects.get(pid)
        lead = p.lead if p else ""
        return False, [lead] if lead in members else [cfg.monitor.id]
    m = cfg.member(room)
    return False, [m.id] if m else []


def is_status_request(text: str) -> bool:
    """'@all status' style messages get an instant answer from the task board (no model call)."""
    stripped = " ".join(_MENTION.sub("", text).strip(" ?!.,:-").split())
    return bool(_STATUS.match(stripped))
