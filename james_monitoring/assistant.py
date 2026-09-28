"""Personal assistant mode: one teammate (`assistant: true`) who works for the founder personally.

- Private: its 1:1 room and its tasks (personal tasks) are the founder's alone — no teammate, admin or client sees
  them, and they stay out of the company board, report and @all.
- Reminders: the assistant (or the founder in Settings) sets them; they arrive in the assistant's chat, and on the
  phone through whatever channel that room lives on.
- A morning brief at `monitor.daily_brief`: personal tasks due, today's reminders, and what the company needs from
  the founder — from the files, no model call.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from .util import now, today

REMIND_SPEC = """\
- {"type":"remind","at":"YYYY-MM-DD HH:MM | HH:MM | in 30m | in 2h | in 3d","text":"<what to remind the founder of>"}
  Sets a reminder: at that time it arrives in this chat (and on their phone). Use the founder's timezone.
"""


def parse_when(value: str, tz: str, base: datetime | None = None) -> datetime:
    """'2026-10-02 09:30', '18:00' (today, or tomorrow if passed), 'in 45m', 'in 2h', 'in 1d', 'tomorrow 9:00'."""
    from zoneinfo import ZoneInfo
    base = base or now(tz)
    v = " ".join(str(value or "").lower().split())
    m = re.fullmatch(r"in (\d+)\s*(m|min|mins|minutes?|h|hrs?|hours?|d|days?)", v)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        return base + {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    m = re.fullmatch(r"(tomorrow\s+)?(\d{1,2}):(\d{2})", v)
    if m:
        hh, mm = int(m.group(2)), int(m.group(3))
        if hh > 23 or mm > 59:
            raise ValueError(f"bad time {value}")
        at = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if m.group(1):
            at += timedelta(days=1)
        elif at <= base:
            at += timedelta(days=1)
        return at
    try:
        at = datetime.fromisoformat(v.replace(" ", "T"))
    except ValueError:
        raise ValueError(f"I can't read the time “{value}” — use YYYY-MM-DD HH:MM, HH:MM or 'in 2h'.") from None
    return at if at.tzinfo else at.replace(tzinfo=ZoneInfo(tz))


class Reminders:
    """Stored in the runtime state (.jm/state.json): they're operational, not company records."""

    def __init__(self, rt):
        self.rt = rt

    def all(self, member: str = "") -> list[dict]:
        items = list(self.rt.ws.state().get("reminders") or [])
        return sorted([r for r in items if not member or r.get("member") == member], key=lambda r: r.get("at", ""))

    def add(self, member: str, when: str, text: str) -> dict:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("A reminder needs its text.")
        at = parse_when(when, self.rt.cfg.timezone)
        if at < now(self.rt.cfg.timezone) - timedelta(minutes=1):
            raise ValueError(f"{at:%Y-%m-%d %H:%M} is in the past.")
        item = {"id": "R-" + secrets.token_hex(3), "member": member, "at": at.isoformat(timespec="minutes"),
                "text": text[:500], "done": False}
        self.rt.ws.update_state(lambda s: s.setdefault("reminders", []).append(item))
        return item

    def delete(self, rid: str) -> None:
        def fn(s):
            s["reminders"] = [r for r in (s.get("reminders") or []) if r.get("id") != rid]
        self.rt.ws.update_state(fn)

    def due(self, at: datetime) -> list[dict]:
        """Reminders whose time has come; marked done in the same step (each fires once)."""
        out: list[dict] = []

        def fn(s):
            keep = []
            for r in s.get("reminders") or []:
                try:
                    when = datetime.fromisoformat(r["at"])
                except (KeyError, ValueError):
                    continue
                if when <= at:
                    out.append(r)
                elif when > at - timedelta(days=60):
                    keep.append(r)
            s["reminders"] = keep
        self.rt.ws.update_state(fn)
        return out


def brief(rt, m) -> str:
    """The morning brief for the founder, from the assistant."""
    cfg, tz = rt.cfg, rt.cfg.timezone
    d = today(tz)
    mine = [t for t in rt.tasks.for_owner(m.id) if t.is_open]
    due = [t for t in mine if t.due(tz) and t.due(tz) <= d]
    later = [t for t in mine if t not in due]
    rems = [r for r in Reminders(rt).all(m.id) if r["at"][:10] == d.isoformat()]
    pend = rt.asks.pending()
    review = [t for t in rt.tasks.all() if t.status == "review"]
    lines = [f"☀️ Good morning, {cfg.owner_name.split()[0]} — your day ({d:%a %d %b})"]
    lines += ["", "Due today or late:"] + ([f"• {t.id} {t.title}" + (" ⚠️ late" if t.overdue(tz) else "")
                                           for t in due] or ["• nothing"])
    if rems:
        lines += ["", "Reminders today:"] + [f"• {r['at'][11:16]} — {r['text']}" for r in rems]
    if later:
        lines += ["", f"Also on your list ({len(later)}):"] + [f"• {t.id} {t.title}" for t in later[:5]]
    if pend or review:
        lines += ["", "The company needs you:"]
        if pend:
            lines.append(f"• {len(pend)} approval(s) waiting — {pend[0].summary}" + (" …" if len(pend) > 1 else ""))
        if review:
            lines.append(f"• {len(review)} task(s) to review — {review[0].id} {review[0].title}")
    return "\n".join(lines)
