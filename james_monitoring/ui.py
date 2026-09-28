"""Team operations behind the console's Team page: import personas, check bot tokens and Telegram links,
add and remove people."""
from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path

import yaml

from . import skills as skillmod
from .config import load_config
from .fileio import path_lock
from .setup import remove_member, scaffold, slug


class TeamAdmin:
    """The operations behind the page. Kept separate from HTTP so it is easy to test."""

    def __init__(self, cfg_path: Path):
        self.cfg_path = Path(cfg_path).resolve()
        self.lock = threading.Lock()
        self.uploads: dict[str, skillmod.ImportedSkill] = {}
        self.usernames: dict[str, str] = {}
        self.dm_ok: set[str] = set()

    # -- helpers ---------------------------------------------------------------------
    def cfg(self):
        return load_config(self.cfg_path)

    def raw(self) -> dict:
        return yaml.safe_load(self.cfg_path.read_text()) or {}

    def _probe(self, token: str):
        from .telegram_setup import TelegramProbe
        return TelegramProbe(token, self.cfg().telegram_api_base)

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    # -- read ------------------------------------------------------------------------------
    def state(self) -> dict:
        cfg = self.cfg()
        ws = cfg.workspace_path
        members = []
        for m in cfg.team:
            persona = ws / "team" / m.id / "persona.md"
            skill_dir = ws / "team" / m.id / "skill"
            members.append({
                "id": m.id, "name": m.name, "role": m.role, "projects": m.projects, "manager": m.monitor,
                "has_token": bool(m.bot_token), "token_env": m.bot_token_env,
                "username": self.usernames.get(m.id, ""),
                "persona_lines": len(persona.read_text().splitlines()) if persona.exists() else 0,
                "skill_files": sum(1 for p in skill_dir.rglob("*") if p.is_file()) if skill_dir.exists() else 0,
            })
        return {"company": cfg.company, "owner": cfg.owner_name, "owner_id": cfg.owner_user_id,
                "group_id": cfg.group_chat_id, "timezone": cfg.timezone, "workspace": str(ws),
                "model": f"{cfg.llm.provider}{('/' + cfg.llm.model) if cfg.llm.model else ''}",
                "members": members, "projects": sorted(cfg.projects)}

    # -- persona upload ------------------------------------------------------------------
    def _keep(self, sk) -> dict:
        uid = uuid.uuid4().hex
        self.uploads[uid] = sk
        return {"upload_id": uid, "name": sk.suggested_name(), "role": sk.suggested_role(),
                "lines": len(sk.persona.splitlines()), "files": len(sk.files), "source": sk.source,
                "preview": sk.persona[:600]}

    def upload(self, filename: str, data: bytes) -> dict:
        """One file. A zip with several people returns the first as usual plus all of them in "items"."""
        items = [self._keep(sk) for sk in skillmod.many_from_bytes(filename, data)]
        return {**items[0], "items": items}

    def upload_folder(self, files: dict[str, bytes], name: str = "folder") -> dict:
        """A whole folder picked in the browser: one person per sub-folder with a SKILL.md (or per .md file)."""
        if not files:
            raise ValueError("The folder is empty.")
        return {"items": [self._keep(sk) for sk in skillmod.split_packages(files, name)]}

    # -- telegram checks -----------------------------------------------------------------
    def check_token(self, token: str) -> dict:
        async def go():
            async with self._probe(token) as p:
                return await p.info()
        try:
            info = self._run(go())
        except Exception as e:  # noqa: BLE001 - any Telegram error means "not a working token"
            return {"ok": False, "error": f"That token didn't work: {e}"}
        return {"ok": True, "username": info.username, "reads_groups": info.reads_groups}

    def check_links(self, token: str, name: str = "", role: str = "") -> dict:
        cfg = self.cfg()

        async def go():
            async with self._probe(token) as p:
                in_group = await p.in_group(cfg.group_chat_id) if cfg.group_chat_id else False
                dm = token in self.dm_ok
                if not dm and cfg.owner_user_id:
                    hello = f"✅ {name or 'Your new teammate'} here{' — ' + role if role else ''}. Message me in this chat for work."
                    dm = await p.can_dm(cfg.owner_user_id, hello)
                return in_group, dm
        try:
            in_group, dm = self._run(go())
        except Exception as e:  # noqa: BLE001
            return {"in_group": False, "dm": False, "error": str(e)}
        if dm:
            self.dm_ok.add(token)
        return {"in_group": in_group, "dm": dm}

    def member_status(self, member_id: str) -> dict:
        cfg = self.cfg()
        m = cfg.member(member_id)
        if not m or not m.bot_token:
            return {"ok": False, "in_group": False, "username": ""}

        async def go():
            async with self._probe(m.bot_token) as p:
                info = await p.info()
                return info, (await p.in_group(cfg.group_chat_id) if cfg.group_chat_id else False)
        try:
            info, in_group = self._run(go())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "in_group": False, "username": "", "error": str(e)}
        self.usernames[m.id] = info.username
        return {"ok": True, "in_group": in_group, "username": info.username,
                "reads_groups": info.reads_groups, "manager": m.monitor}

    # -- write ---------------------------------------------------------------------------
    def add(self, *, name: str, role: str, token: str, projects: list[str] | None = None,
            upload_id: str = "") -> dict:
        name, role = name.strip(), role.strip()
        if not name or not role:
            raise ValueError("Name and role are required.")
        with self.lock, path_lock(self.cfg_path):
            raw = self.raw()
            mid = slug(name)
            taken = {str(m.get("id")).lower() for m in raw.get("team", [])}
            if mid in taken or mid == "all":
                raise ValueError(f"'{mid}' is already on the team — use a different name.")
            member = {"id": mid, "name": name, "role": role, "bot_token_env": f"TG_TOKEN_{mid.upper()}"}
            projects = [p.strip() for p in (projects or []) if p.strip()]
            if projects:
                member["projects"] = projects
                for p in projects:
                    raw.setdefault("projects", {}).setdefault(p, {"repo": "", "main_branch": "main"})
            raw.setdefault("team", []).append(member)
            sk = self.uploads.pop(upload_id, None) if upload_id else None
            cfg = scaffold(raw, skills={mid: sk} if sk else None,
                           tokens={mid: token.strip()} if token.strip() else None, base_dir=self.cfg_path.parent)
        if cfg.group_chat_id and token.strip():
            async def intro():
                async with self._probe(token.strip()) as p:
                    await p.post(cfg.group_chat_id, f"👋 {name} joined the team — {role}.")
            try:
                self._run(intro())
            except Exception:  # noqa: BLE001 - the member is saved either way
                pass
        return {"id": mid, "name": name}

    def remove(self, member_id: str) -> dict:
        with self.lock:
            return {"removed": remove_member(self.cfg_path, member_id)}
