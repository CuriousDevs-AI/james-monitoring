"""The audit log: who did what, when, from where — people in the console, decisions from any channel, what agents
did on their own, and model failures.

One JSON line per event in the team repo (audit/YYYY-MM.jsonl), committed with everything else — so it has git's
history behind it and can't be quietly rewritten. Secrets never go in: known secret fields are dropped and long
text is cut.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path

from .fileio import path_lock

SECRET_KEYS = re.compile(r"token|key|secret|password|code|data|files|persona|text", re.I)
KINDS = ("console", "decision", "agent", "model", "system", "login")


_URL_CREDS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)     # https://user:token@host → https://***@host
_TOKENS = re.compile(r"\b(sk-[\w-]{8,}|xox[abpr]-[\w-]{8,}|xapp-[\w-]{8,}|gh[pousr]_\w{16,}|u_[\w-]{20,}|\d{6,}:[\w-]{30,})")


def _scrub(v):
    """Drop secret-looking keys at every level; hide credentials in URLs and anything shaped like a token."""
    if isinstance(v, dict):
        return {k: (x[:120] + ("…" if len(x) > 120 else "") if k == "text" and isinstance(x, str) else _scrub(x))
                for k, x in v.items() if not SECRET_KEYS.search(str(k)) or k in ("note", "text")}
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    if isinstance(v, str):
        return _TOKENS.sub("***", _URL_CREDS.sub(r"\1***@", v))
    return v


def _clean(detail) -> str:
    detail = _scrub(detail)
    if isinstance(detail, dict):
        keep = {k: (json.dumps(v, ensure_ascii=False)[:160] if isinstance(v, (dict, list)) else v)
                for k, v in detail.items()}
        detail = ", ".join(f"{k}={v}" for k, v in keep.items() if v not in ("", None, [], {}))
    return " ".join(str(detail or "").split())[:400]


def _cell(v) -> str:
    """No spreadsheet formulas from a task title or a message (=, +, -, @ at the start)."""
    v = str(v if v is not None else "")
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


class AuditLog:
    def __init__(self, root: Path, tz: str = "UTC"):
        self.dir = Path(root) / "audit"
        self.tz = tz

    def record(self, kind: str, who: str, action: str, target: str = "", detail="", via: str = "") -> dict:
        from .util import now
        ts = time.time()
        ev = {"ts": ts, "at": now(self.tz).isoformat(timespec="seconds"), "kind": kind, "who": who,
              "action": action, "target": _clean(target), "detail": _clean(detail), "via": via}
        path = self.dir / f"{ev['at'][:7]}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path_lock(path):
            with path.open("a") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def entries(self, limit: int = 200, kind: str = "", who: str = "", q: str = "", before: float = 0) -> list[dict]:
        """Newest first, filtered."""
        out: list[dict] = []
        files = sorted(self.dir.glob("*.jsonl"), reverse=True) if self.dir.exists() else []
        needle = (q or "").lower().strip()
        for p in files:
            for line in reversed(p.read_text(errors="replace").splitlines()):
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if before and ev.get("ts", 0) >= before:
                    continue
                if kind and ev.get("kind") != kind:
                    continue
                if who and ev.get("who") != who:
                    continue
                if needle and needle not in json.dumps(ev, ensure_ascii=False).lower():
                    continue
                out.append(ev)
                if len(out) >= limit:
                    return out
        return out

    def csv(self, **filters) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["at", "kind", "who", "action", "target", "detail", "via"])
        for ev in self.entries(limit=100_000, **filters):
            w.writerow([_cell(ev.get(k, "")) for k in ("at", "kind", "who", "action", "target", "detail", "via")])
        return buf.getvalue()
