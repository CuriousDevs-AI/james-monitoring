"""Search everything the team has: every chat room, every task, the team's documents and everyone's memory.

Plain full-text (every word must appear, any order, case-insensitive) over the files — no index to keep in sync,
so a message written a second ago is found. Callers pass the rooms the viewer may see.
"""
from __future__ import annotations

import re
from pathlib import Path

DOC_DIRS = ("docs", "reports", "decisions", "projects")
PER_KIND = 25


def _terms(q: str) -> list[str]:
    return [t for t in re.split(r"\s+", (q or "").lower().strip()) if t]


def _hit(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return all(t in low for t in terms)


def snippet(text: str, terms: list[str], width: int = 160) -> str:
    """The part of `text` around the first match, on one line."""
    flat = " ".join(str(text).split())
    low = flat.lower()
    pos = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, pos - width // 3)
    out = flat[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(flat) else "")


def search(rt, q: str, rooms: list[str], include_memory: bool = True, include_tasks=None, members=None) -> dict:
    """{"messages", "tasks", "docs", "memory"} — newest first, at most PER_KIND of each."""
    terms = _terms(q)
    out: dict[str, list] = {"messages": [], "tasks": [], "docs": [], "memory": []}
    if not terms or len("".join(terms)) < 2:
        return out
    for room in rooms:
        for m in reversed(rt.chat.since(room, -1, limit=100_000)):
            if _hit(str(m.get("text", "")), terms):
                out["messages"].append({"room": room, "i": m.get("i"), "who": m.get("who", ""), "ts": m.get("ts", 0),
                                        "kind": m.get("kind", "msg"), "text": snippet(m.get("text", ""), terms)})
    out["messages"].sort(key=lambda x: -x["ts"])
    out["messages"] = out["messages"][:PER_KIND * 2]
    for t in rt.tasks.all():
        if include_tasks is not None and not include_tasks(t):
            continue
        body = "\n".join(f"{k}: {v}" for k, v in t.doc.sections.items() if v)
        hay = f"{t.id} {t.title} {t.owner} {t.blocked_on}\n{body}"
        if _hit(hay, terms):
            where = t.title if _hit(f"{t.id} {t.title}", terms) else body
            out["tasks"].append({"id": t.id, "title": t.title, "owner": t.owner, "status": t.status,
                                 "updated": str(t.doc.meta.get("updated", "")), "text": snippet(where, terms)})
    out["tasks"].sort(key=lambda x: x["updated"], reverse=True)
    out["tasks"] = out["tasks"][:PER_KIND]
    root: Path = rt.ws.root
    for d in DOC_DIRS:
        for p in sorted((root / d).rglob("*.md")) if (root / d).exists() else []:
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            rel = str(p.relative_to(root))
            if _hit(rel + "\n" + text, terms):
                line = next((ln for ln in text.splitlines() if _hit(ln, terms)), text)
                out["docs"].append({"path": rel, "mtime": p.stat().st_mtime, "text": snippet(line, terms)})
    out["docs"].sort(key=lambda x: -x["mtime"])
    out["docs"] = out["docs"][:PER_KIND]
    if include_memory:
        for m in (members if members is not None else rt.cfg.team):
            for e in rt.ws.memory_entries(m.id):
                if _hit(e["text"], terms):
                    out["memory"].append({"member": m.id, "pinned": e["pinned"], "date": e["date"],
                                          "text": snippet(e["text"], terms)})
        out["memory"] = out["memory"][:PER_KIND]
    return out
