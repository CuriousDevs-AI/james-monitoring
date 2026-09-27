"""The team page: its operations (against a fake Telegram) and its HTTP layer (auth, routes)."""
import base64
import io
import json
import threading
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer

import pytest

from james_monitoring.config import load_config
from james_monitoring.setup import scaffold
from james_monitoring.ui import TeamAdmin

from .conftest import make_raw
from .fake_telegram import FakeTelegram

TOK = {"james": "11:JAMES", "riya": "22:RIYA"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("james_monitoring.telegram_setup.POLL_SECONDS", 0.05)
    tg = FakeTelegram({TOK["james"]: "acme_james_bot", TOK["riya"]: "acme_riya_bot"})
    tg.strict_dm = True
    raw = make_raw(tmp_path)
    raw["team"] = [raw["team"][0]]                       # just the manager
    raw["telegram"] = {"group_chat_id": -100, "api_base_url": tg.url}
    cfg = scaffold(raw, tokens={"james": TOK["james"]}, base_dir=tmp_path / "jm")
    yield TeamAdmin(cfg.path), tg, cfg
    tg.close()


def skill_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("riya-backend-lead/SKILL.md", "---\nname: riya-backend-lead\ndescription: Act as Riya\n---\n"
                                                 "# Riya — backend and APIs\nOwns the API.\n")
        z.writestr("riya-backend-lead/references/style.md", "Use Postgres.")
    return buf.getvalue()


def test_full_add_flow_and_skill_kept_after_original_deleted(setup, tmp_path):
    admin, tg, cfg = setup
    src = tmp_path / "riya.skill"
    src.write_bytes(skill_zip())
    up = admin.upload("riya.skill", src.read_bytes())
    assert up["name"] == "Riya" and up["role"] == "backend and APIs" and up["files"] == 2
    src.unlink()                                          # original is gone — must not matter

    assert admin.check_token("bad")["ok"] is False
    t = admin.check_token(TOK["riya"])
    assert t["ok"] and t["username"] == "acme_riya_bot"
    assert admin.check_links(TOK["riya"], "Riya", "backend") == {"in_group": False, "dm": False}
    tg.user_adds_bot_to_group(TOK["riya"])
    tg.started.add("acme_riya_bot")
    assert admin.check_links(TOK["riya"], "Riya", "backend") == {"in_group": True, "dm": True}

    admin.add(name="Riya", role="Backend lead", token=TOK["riya"], projects=["api"], upload_id=up["upload_id"])
    c = load_config(cfg.path)
    assert c.member("riya").projects == ["api"] and c.member("riya").bot_token == TOK["riya"]
    ws = c.workspace_path
    assert (ws / "team/riya/persona.md").read_text().startswith("# Riya — backend")
    assert (ws / "team/riya/skill/references/style.md").read_text() == "Use Postgres."
    assert (ws / "team/riya/skill/SKILL.md").exists()
    assert any("Riya joined" in s["text"] for s in tg.sent if s.get("chat_id") == -100)
    st = admin.state()
    riya = [m for m in st["members"] if m["id"] == "riya"][0]
    assert riya["skill_files"] == 2 and riya["has_token"]

    with pytest.raises(ValueError, match="already on the team"):
        admin.add(name="Riya", role="x", token=TOK["riya"])
    assert admin.remove("riya")["removed"] == "Riya"
    with pytest.raises(ValueError, match="manager"):
        admin.remove("james")


def test_console_http_requires_key_and_serves_page(tmp_path):
    from james_monitoring.server import App, make_handler
    app = App(tmp_path / "co", telegram=False)
    app.base.mkdir()
    app.load()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, "secret"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    hdr = {"X-JM-Key": "secret", "Content-Type": "application/json"}
    try:
        assert "Set up your company" in urllib.request.urlopen(base + "/").read().decode()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/state")
        assert e.value.code == 403
        st = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers=hdr)).read())
        assert st["setup_needed"] is True
        body = json.dumps({"company": "Acme", "owner": "Maria", "provider": "fake", "timezone": "Asia/Calcutta"}).encode()
        urllib.request.urlopen(urllib.request.Request(base + "/api/setup", data=body, method="POST", headers=hdr))
        st = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/state", headers=hdr)).read())
        assert st["company"] == "Acme" and st["timezone"] == "Asia/Kolkata"
        up = json.dumps({"filename": "riya.skill", "data": base64.b64encode(skill_zip()).decode()}).encode()
        r = urllib.request.urlopen(urllib.request.Request(base + "/api/upload", data=up, method="POST", headers=hdr))
        assert json.loads(r.read())["name"] == "Riya"
        bad = urllib.request.Request(base + "/api/tasks", data=b'{"title":"x","owner":"nobody"}', method="POST", headers=hdr)
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(bad)
        assert e.value.code == 400
    finally:
        srv.shutdown()
        app.submit(app._stop_services(), timeout=10)
