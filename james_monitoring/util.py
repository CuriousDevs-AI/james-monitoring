from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


# Old names some systems (macOS, older Linux) still report, mapped to the names Python's tz database knows.
TZ_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata", "Asia/Saigon": "Asia/Ho_Chi_Minh", "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar", "Asia/Chongqing": "Asia/Shanghai", "Asia/Macao": "Asia/Macau",
    "Europe/Kiev": "Europe/Kyiv", "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Indianapolis": "America/Indiana/Indianapolis", "America/Louisville": "America/Kentucky/Louisville",
    "Pacific/Truk": "Pacific/Chuuk", "Pacific/Ponape": "Pacific/Pohnpei", "Atlantic/Faeroe": "Atlantic/Faroe",
    "US/Eastern": "America/New_York", "US/Central": "America/Chicago", "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles", "IST": "Asia/Kolkata",
}


def normalize_tz(name: str) -> str:
    """Return a timezone name that ZoneInfo accepts, or raise ValueError."""
    name = (name or "UTC").strip()
    for candidate in (name, TZ_ALIASES.get(name, "")):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
            return candidate
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError / ValueError
            continue
    raise ValueError(f"Unknown timezone '{name}'. Use a name like Asia/Kolkata, Europe/Madrid, America/New_York.")


def now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def today(tz: str) -> date:
    return now(tz).date()


def parse_date(value, tz: str = "UTC") -> date | None:
    """Accepts YYYY-MM-DD, MM-DD (this year, or next year if already passed long ago), date objects."""
    if value in (None, "", "-"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", s)
    if m:
        t = today(tz)
        d = date(t.year, int(m.group(1)), int(m.group(2)))
        if d < t - timedelta(days=180):
            d = date(t.year + 1, d.month, d.day)
        return d
    m = re.fullmatch(r"\+(\d+)d", s)
    if m:
        return today(tz) + timedelta(days=int(m.group(1)))
    return None


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def in_quiet_hours(tz: str, quiet: tuple[str, str] | None) -> bool:
    if not quiet:
        return False
    t = now(tz).time()
    start, end = parse_hhmm(quiet[0]), parse_hhmm(quiet[1])
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "task"


def chunk(text: str, size: int = 3900) -> list[str]:
    """Split text for Telegram's 4096-char limit, on line boundaries where possible."""
    if len(text) <= size:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts
