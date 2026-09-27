"""The setup flow against a fake Telegram: nothing hardcoded, every link verified."""
import threading

import pytest

from james_monitoring.config import load_config
from james_monitoring.setup import add_member, load_persona, remove_member, wizard

from .fake_telegram import FakeTelegram


class ScriptedIO:
    def __init__(self, answers):
        self.answers = list(answers)
        self.out = []

    def _next(self, prompt):
        assert self.answers, f"no scripted answer for: {prompt}"
        return self.answers.pop(0)

    def ask(self, prompt, default="", required=False, secret=False):
        a = self._next(prompt)
        return default if a is None else a

    def confirm(self, prompt, default=True):
        a = self._next(prompt)
        return default if a is None else a

    def say(self, text):
        self.out.append(text)


TOK = {"mgr": "10:MGR", "riya": "20:RIYA", "omar": "30:OMAR"}


@pytest.fixture
def tg():
    t = FakeTelegram({TOK["mgr"]: "acme_manager_bot", TOK["riya"]: "acme_riya_bot", TOK["omar"]: "acme_omar_bot"})
    t.strict_dm = True
    yield t
    t.close()


def later(delay, fn):
    threading.Timer(delay, fn).start()


async def test_full_setup_for_any_company(tmp_path, tg, monkeypatch):
    monkeypatch.setattr("james_monitoring.telegram_setup.POLL_SECONDS", 0.1)
    skill = tmp_path / "skills" / "riya"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: riya\ndescription: x\n---\n# Riya — backend\nOwns the API.\n")

    # What the human does in Telegram, while the wizard waits:
    tg.user_says(TOK["mgr"], "hi")                                  # presses Start on the manager bot
    tg.started.add("acme_manager_bot")
    tg.user_adds_bot_to_group(TOK["mgr"], group_id=-4242, title="Acme HQ")
    later(1.0, lambda: tg.user_adds_bot_to_group(TOK["riya"], group_id=-4242))   # adds Riya's bot a bit later
    later(1.5, lambda: tg.started.add("acme_riya_bot"))                          # presses Start on Riya's bot
    tg.user_adds_bot_to_group(TOK["omar"], group_id=-4242)
    tg.started.add("acme_omar_bot")

    io = ScriptedIO([
        "Acme Robotics", "Maria Lopez", "Europe/Madrid", "Ship v1; First customer",        # basics
        "openai", "some-model", "http://localhost:11434/v1", "",                           # AI
        str(tmp_path / "ws"), "", False,                                                   # workspace
        "Nova", "Chief of staff", "",                                                      # manager (not James!)
        TOK["mgr"],                                                                        # manager token
        True,                                                                              # "is this you?"
        True,                                                                              # "use this group?"
        True, "Riya", "Backend lead", str(skill), "api", "",                               # member 1
        "bad-token", TOK["riya"],                                                          # bad token → retry
        True, "Omar", "Designer", "", "",                                                  # member 2, generated persona
        TOK["omar"],
        False,                                                                             # no more members
        "17:00", "09:30", "100000", "none",                                                # monitoring
    ])
    cfg = await wizard(tmp_path / "jm", io, api_base=tg.url, timeout=5)

    assert cfg.company == "Acme Robotics" and cfg.owner_name == "Maria Lopez" and cfg.owner_key == "maria_lopez"
    assert cfg.timezone == "Europe/Madrid" and cfg.owner_user_id == 111 and cfg.group_chat_id == -4242
    assert [m.name for m in cfg.team] == ["Nova", "Riya", "Omar"] and cfg.monitor.name == "Nova"
    assert cfg.member("riya").projects == ["api"] and cfg.work_sessions == ["09:30"]
    ws = cfg.workspace_path
    assert (ws / "team/riya/persona.md").read_text().startswith("# Riya — backend")
    assert "# Nova — Chief of staff" in (ws / "team/nova/persona.md").read_text()
    assert "# Omar — Designer" in (ws / "team/omar/persona.md").read_text()
    assert "1. Ship v1" in (ws / "team/charter.md").read_text()
    env = (tmp_path / "jm/.env").read_text()
    assert f"TG_TOKEN_RIYA={TOK['riya']}" in env and "bad-token" not in env
    # every DM and group intro really happened
    assert any(s["bot"] == "acme_riya_bot" and s["chat_id"] == 111 and "Riya here" in s["text"] for s in tg.sent)
    assert any(s["bot"] == "acme_omar_bot" and s["chat_id"] == -4242 and "Omar joined" in s["text"] for s in tg.sent)
    assert any("That token didn't work" in o for o in io.out)
    # no company-specific leftovers anywhere in the generated workspace
    blob = " ".join(p.read_text() for p in ws.rglob("*.md"))
    for word in ("CuriousDevs", "Pankaj", "James", "Kolkata"):
        assert word not in blob, word


def test_persona_loading(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: a\n---\nbody\n")
    assert load_persona(str(d)) == "body\n"
    assert load_persona(str(d / "SKILL.md")) == "body\n"
    with pytest.raises(FileNotFoundError):
        load_persona(str(tmp_path / "missing.md"))


def test_scripted_add_and_remove(tmp_path):
    from .conftest import make_raw
    from james_monitoring.setup import scaffold
    raw = make_raw(tmp_path)
    cfg = scaffold(raw, base_dir=tmp_path / "jm")
    add_member(cfg.path, name="Riya Shah", role="QA", projects=["api"])
    c2 = load_config(cfg.path)
    assert c2.member("riya_shah").role == "QA" and "api" in c2.projects
    assert remove_member(cfg.path, "riya_shah") == "Riya Shah"
    assert load_config(cfg.path).member("riya_shah") is None
    with pytest.raises(ValueError, match="manager"):
        remove_member(cfg.path, "james")


def test_persona_from_packaged_skill_and_bad_files(tmp_path):
    import zipfile
    from james_monitoring.setup import PersonaError
    pkg = tmp_path / "james-delivery-manager.skill"
    with zipfile.ZipFile(pkg, "w") as z:
        z.writestr("james-delivery-manager/SKILL.md", "---\nname: james\n---\n# James — delivery\nBody\n")
        z.writestr("james-delivery-manager/references/notes.md", "extra")
        z.writestr("__MACOSX/james-delivery-manager/._SKILL.md", b"\xba\xba")
    assert load_persona(str(pkg)) == "# James — delivery\nBody\n"
    assert load_persona(f"'{pkg}' ") == "# James — delivery\nBody\n"          # dragged-in path with quotes
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(PersonaError, match="no SKILL.md"):
        load_persona(str(empty))
    binary = tmp_path / "photo.png"
    binary.write_bytes(bytes([0x89, 0xba, 0xff, 0x00, 0xfe]) * 50)
    with pytest.raises(PersonaError, match="not a text file"):
        load_persona(str(binary))
