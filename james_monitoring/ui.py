"""`jm ui` — a local web page to build and manage the team.

Drop in persona/skill files (many at once) → names and roles are filled in → paste each bot token →
live ✓ for "bot is in the group" and "DM works" → Save. Remove people with a button.

Runs on 127.0.0.1 only, and every API call needs the secret key that is in the URL `jm ui` prints.
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import yaml

from . import skills as skillmod
from .config import load_config
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
    def upload(self, filename: str, data: bytes) -> dict:
        sk = skillmod.from_bytes(filename, data)
        uid = uuid.uuid4().hex
        self.uploads[uid] = sk
        return {"upload_id": uid, "name": sk.suggested_name(), "role": sk.suggested_role(),
                "lines": len(sk.persona.splitlines()), "files": len(sk.files), "source": filename,
                "preview": sk.persona[:600]}

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
        if not token.strip():
            raise ValueError("A bot token is required.")
        with self.lock:
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
            cfg = scaffold(raw, skills={mid: sk} if sk else None, tokens={mid: token.strip()},
                           base_dir=self.cfg_path.parent)
        if cfg.group_chat_id:
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


# -- HTTP --------------------------------------------------------------------------------
def make_handler(admin: TeamAdmin, key: str):
    page = resources.files("james_monitoring").joinpath("templates", "ui.html").read_text()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-JM-Key", ""), key)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            if path == "/api/state":
                return self._json(200, admin.state())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 30_000_000:
                return self._json(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/upload":
                    out = admin.upload(str(body.get("filename", "SKILL.md")), base64.b64decode(body.get("data", "")))
                elif path == "/api/token":
                    out = admin.check_token(str(body.get("token", "")).strip())
                elif path == "/api/links":
                    out = admin.check_links(str(body.get("token", "")).strip(), body.get("name", ""), body.get("role", ""))
                elif path == "/api/member_status":
                    out = admin.member_status(str(body.get("id", "")))
                elif path == "/api/add":
                    out = admin.add(name=str(body.get("name", "")), role=str(body.get("role", "")),
                                    token=str(body.get("token", "")), projects=list(body.get("projects") or []),
                                    upload_id=str(body.get("upload_id", "")))
                elif path == "/api/remove":
                    out = admin.remove(str(body.get("id", "")))
                else:
                    return self._json(404, {"error": "not found"})
            except (ValueError, FileNotFoundError) as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - show the error on the page instead of a dead request
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(200, out)
    return H


def serve(cfg_path: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    admin = TeamAdmin(cfg_path)
    admin.cfg()                                   # fail early on a broken config
    key = secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer((host, port), make_handler(admin, key))
    url = f"http://{host}:{srv.server_address[1]}/?k={key}"
    print(f"Team page: {url}\n(Ctrl-C to stop. Restart `jm run` after adding or removing people.)")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
