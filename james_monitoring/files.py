"""Files people attach in chat: documents, PDFs, images.

Stored in the team repo under files/<room>/ (committed — a client brief or a screenshot is part of the record).
What the agents get:
- text files (md, txt, csv, json, code…): the text, in the message;
- PDFs: the extracted text (with `pypdf`, an optional dependency: pip install "james-monitoring[files]"),
  saved next to the PDF as <name>.txt so it's also readable and searchable later;
- images: shown to models that can see (Anthropic and OpenAI-compatible APIs, Codex, OpenCode); for the others the
  agent is told an image is attached and asks for a description.
"""
from __future__ import annotations

import mimetypes
import re
import secrets
from datetime import datetime
from pathlib import Path

MAX_BYTES = 15_000_000
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".css", ".js",
            ".ts", ".tsx", ".jsx", ".py", ".go", ".rs", ".java", ".rb", ".php", ".sh", ".sql", ".log", ".ini", ".toml",
            ".env.example", ".c", ".h", ".cpp", ".swift", ".kt"}
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp"}
PER_FILE_CHARS = 20_000                # text of one file given to the agent
ALL_FILES_CHARS = 40_000


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    return "other"


def safe_name(name: str) -> str:
    base = Path(str(name or "file")).name
    stem, ext = Path(base).stem, Path(base).suffix.lower()
    stem = re.sub(r"[^\w.-]+", "-", stem).strip("-.")[:60] or "file"
    ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext or "") else ""
    return stem + ext


def room_dir(room: str) -> str:
    return "files/" + ("".join(c for c in room.lower() if c.isalnum() or c in "_-") or "team")


def save(ws_root: Path, room: str, name: str, data: bytes) -> dict:
    """Store one attachment. Returns its record for the chat message: {name, path, kind, size, pages?, text?}."""
    if not data:
        raise ValueError(f"{name} is empty.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{name} is {len(data) // 1_000_000} MB — the limit is {MAX_BYTES // 1_000_000} MB per file.")
    clean = safe_name(name)
    rel = f"{room_dir(room)}/{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}-{clean}"
    path = Path(ws_root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    meta = {"name": Path(str(name)).name[:120], "path": rel, "kind": kind_of(clean), "size": len(data)}
    if meta["kind"] == "pdf":
        text, pages, err = pdf_text(path)
        meta["pages"] = pages
        if text:
            path.with_name(path.name + ".txt").write_text(text)
            meta["text"] = rel + ".txt"
        elif err:
            meta["note"] = err
    return meta


def pdf_text(path: Path) -> tuple[str, int, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", 0, "PDF text can't be read here (install pypdf: pip install \"james-monitoring[files]\")"
    try:
        reader = PdfReader(str(path))
        parts = [(p.extract_text() or "").strip() for p in reader.pages]
        text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(parts) if t)
        return text, len(reader.pages), "" if text else "this PDF has no text layer (a scan?)"
    except Exception as e:  # noqa: BLE001 - a broken PDF is still attached, just unreadable
        return "", 0, f"couldn't read this PDF ({type(e).__name__})"


def for_agent(ws_root: Path, files: list[dict], can_see_images: bool) -> tuple[str, list[str]]:
    """(text to add to the message, image paths to show the model)."""
    root = Path(ws_root)
    parts, images, budget = [], [], ALL_FILES_CHARS
    for f in files or []:
        name, kind = f.get("name", "file"), f.get("kind", "other")
        p = root / str(f.get("path", ""))
        if kind == "image":
            if can_see_images and p.is_file():
                images.append(str(p))
                parts.append(f"📎 {name} (image — attached, you can see it)")
            else:
                parts.append(f"📎 {name} (image — your model can't see images: ask for a description if you need "
                             f"what's in it)")
            continue
        src = root / f["text"] if kind == "pdf" and f.get("text") else p if kind == "text" else None
        if src is not None and src.is_file() and budget > 0:
            text = src.read_text(errors="replace")
            cut = text[:min(PER_FILE_CHARS, budget)]
            budget -= len(cut)
            more = f"\n… ({len(text) - len(cut):,} more characters — read_file {f['path']}"
            more += ".txt)" if kind == "pdf" else ")"
            parts.append(f"📎 {name}" + (f" ({f.get('pages')} pages)" if f.get("pages") else "") +
                         f":\n<<<{name}\n{cut}\n>>>" + (more if len(cut) < len(text) else ""))
        else:
            why = f.get("note") or ("not a text file" if kind == "other" else "too much attached already")
            parts.append(f"📎 {name} ({why}; stored at {f.get('path')})")
    return ("\n\n# Attached files\n" + "\n\n".join(parts)) if parts else "", images


def media_type(path: str) -> str:
    return IMAGE_EXT.get(Path(path).suffix.lower()) or mimetypes.guess_type(path)[0] or "application/octet-stream"
