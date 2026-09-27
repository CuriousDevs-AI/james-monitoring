"""Import a persona/skill from anything a person has: SKILL.md, a folder, or a packaged .skill/.zip.

The whole package is copied into the team repo (team/<id>/skill/), so the original file can be moved or
deleted afterwards. team/<id>/persona.md is the SKILL.md body the agents actually read.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

MAX_FILES = 200
MAX_BYTES = 5_000_000


class SkillError(ValueError):
    pass


@dataclass
class ImportedSkill:
    persona: str                                        # SKILL.md body (front matter stripped)
    files: dict[str, bytes] = field(default_factory=dict)   # relative path → bytes (the whole package)
    source: str = ""
    meta: dict = field(default_factory=dict)            # SKILL.md front matter

    def suggested_name(self) -> str:
        m = re.search(r"^#\s+([^\n—–-]+?)\s*[—–-]", self.persona, re.M)
        if m and len(m.group(1).split()) <= 3:
            return m.group(1).strip()
        m = re.search(r"\bAct as ([A-Z][\w]+)", str(self.meta.get("description", "")))
        if m:
            return m.group(1)
        raw = str(self.meta.get("name") or Path(self.source).stem)
        return raw.replace("_", "-").split("-")[0].strip().title() or "Member"

    def suggested_role(self) -> str:
        m = re.search(r"^#\s+[^\n—–]+?\s*[—–]\s*(.+)$", self.persona, re.M)
        if m:
            return m.group(1).strip()
        if self.meta.get("role"):
            return str(self.meta["role"]).strip()[:120]
        desc = " ".join(str(self.meta.get("description", "")).split())
        m = re.search(r"\bAct as [A-Z]\w+,\s*(?:the\s+)?(.+?)(?:\.|\s\(|$)", desc)
        if m:
            return m.group(1).strip()[:120]
        first = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0].rstrip(".")
        return first if 0 < len(first) <= 80 else ""


def _text(data: bytes, where: str) -> str:
    enc = "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        text = data.decode(enc)
    except UnicodeDecodeError:
        raise SkillError(f"{where} is not a text file (use SKILL.md, a folder, or a .skill/.zip)") from None
    if "\x00" in text:
        raise SkillError(f"{where} is not a text file")
    return text


def _split(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\n(.*?)\n---\n?(.*)\Z", text, re.S)
    if not m:
        return {}, text.strip() + "\n"
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), m.group(2).strip() + "\n"


def _safe(rel: str) -> str | None:
    p = PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or "__MACOSX" in p.parts or p.name.startswith("._") or p.name == ".DS_Store":
        return None
    return str(p)


def _from_files(files: dict[str, bytes], source: str) -> ImportedSkill:
    files = {k: v for k, v in files.items() if _safe(k)}
    if not files:
        raise SkillError(f"{source}: nothing usable inside")
    for wanted in ("skill.md", "persona.md"):
        hits = sorted((k for k in files if k.rsplit("/", 1)[-1].lower() == wanted), key=lambda k: k.count("/"))
        if hits:
            main = hits[0]
            break
    else:
        mds = [k for k in files if k.lower().endswith(".md")]
        if len(mds) == 1:
            main = mds[0]
        else:
            raise SkillError(f"{source}: no SKILL.md or persona.md inside")
    meta, body = _split(_text(files[main], f"{source}:{main}"))
    # Re-root the package at the folder that holds SKILL.md, so team/<id>/skill/SKILL.md is predictable.
    root = main.rsplit("/", 1)[0] + "/" if "/" in main else ""
    stored = {k[len(root):]: v for k, v in files.items() if k.startswith(root)}
    return ImportedSkill(persona=body, files=stored, source=source, meta=meta)


def from_bytes(filename: str, data: bytes) -> ImportedSkill:
    """An uploaded file (from the web UI) or a file read from disk."""
    if len(data) > MAX_BYTES:
        raise SkillError(f"{filename} is too large")
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = [i for i in z.infolist() if not i.is_dir()][:MAX_FILES]
            if sum(i.file_size for i in infos) > MAX_BYTES * 4:
                raise SkillError(f"{filename} unpacks too large")
            return _from_files({i.filename: z.read(i) for i in infos}, filename)
    name = filename.rsplit("/", 1)[-1] or "SKILL.md"
    _text(data, filename)                                   # validate it's text
    return _from_files({name if name.lower().endswith(".md") else "SKILL.md": data}, filename)


def from_path(path: str) -> ImportedSkill:
    p = Path(path.strip().strip("'\"")).expanduser()
    if p.is_dir():
        files = {}
        for f in sorted(p.rglob("*")):
            if f.is_file() and len(files) < MAX_FILES:
                files[str(f.relative_to(p)).replace("\\", "/")] = f.read_bytes()
        return _from_files(files, p.name)
    if not p.is_file():
        raise FileNotFoundError(f"{p} not found")
    return from_bytes(p.name, p.read_bytes())


def store(ws_root: Path, member_id: str, skill: ImportedSkill) -> None:
    """Copy the package into team/<id>/skill/ and write team/<id>/persona.md."""
    base = ws_root / "team" / member_id
    target = base / "skill"
    if target.exists():
        import shutil
        shutil.rmtree(target)
    for rel, data in skill.files.items():
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    base.mkdir(parents=True, exist_ok=True)
    (base / "persona.md").write_text(skill.persona)
