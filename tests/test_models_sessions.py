"""Settings → Models and per-person CLI sessions that survive restarts."""
import stat

import pytest

from james_monitoring.config import LLMConfig, parse_config
from james_monitoring.llm import make_llm
from james_monitoring.runtime import Event, Runtime

from .conftest import make_raw

FAKE_CLAUDE = r'''
if [ "$1 $2" = "auth status" ]; then echo '{"loggedIn": true, "authMethod": "claude.ai"}'; exit 0; fi
log="$(dirname "$0")/calls.log"; echo "$@" >> "$log"; echo "--- stdin:" >> "$log"; cat >> "$log"; echo >> "$log"
sid=""; prev=""; for a in "$@"; do [ "$prev" = "--session-id" ] && sid="$a"; [ "$prev" = "--resume" ] && sid="$a"; prev="$a"; done
case "$*" in *"--resume gone-"*) echo '{"is_error":true,"result":"No conversation found with session ID"}'; exit 1;; esac
printf '{"type":"result","is_error":false,"session_id":"%s","result":"{\\"reply\\":\\"ok %s\\",\\"actions\\":[]}","usage":{"input_tokens":10,"output_tokens":5}}\n' "$sid" "$sid"
'''


def cli(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def runtime(tmp_path, llm=None):
    cfg = parse_config(make_raw(tmp_path, llm=llm or {"provider": "claude-code"}), base_dir=tmp_path)
    return Runtime(cfg, make_llm(cfg.llm))


async def test_a_person_keeps_one_session_per_room_across_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_CLAUDE_BIN", cli(tmp_path, "claude", FAKE_CLAUDE))
    rt = runtime(tmp_path)
    await rt.dispatch("sofia", Event("dm", "first message", sender="pankaj"))
    sid = rt._sessions()["sofia:sofia"]["id"]
    rt2 = runtime(tmp_path)                                          # restart
    out = await rt2.dispatch("sofia", Event("dm", "second message", sender="pankaj"))
    assert out == f"ok {sid}" and rt2._sessions()["sofia:sofia"]["calls"] == 2
    log = (tmp_path / "calls.log").read_text().split("--session-id")
    assert f"--resume {sid}" in log[-1] and "first message" not in log[-1].split("--- stdin:")[-1]   # only the new one
    await rt2.dispatch("sofia", Event("group", "hi all", sender="pankaj", project="site"))
    assert set(rt2._sessions()) == {"sofia:sofia", "sofia:p-site"}                     # one per room


async def test_sessions_start_fresh_when_the_model_changes_or_resume_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_CLAUDE_BIN", cli(tmp_path, "claude", FAKE_CLAUDE))
    rt = runtime(tmp_path)
    await rt.dispatch("sofia", Event("dm", "hello", sender="pankaj"))
    first = rt._sessions()["sofia:sofia"]["id"]
    rt._update_sessions(lambda d: d["sofia:sofia"].update(id="gone-123"))       # the CLI lost it
    out = await rt.dispatch("sofia", Event("dm", "again", sender="pankaj"))
    now = rt._sessions()["sofia:sofia"]["id"]
    assert now not in ("gone-123", first) and out == f"ok {now}"               # recovered with a new one
    rt.cfg.llm.model = "opus"                                                   # switched model
    assert rt.session_for(rt.cfg.member("sofia"), "sofia").id == ""
    assert rt.reset_sessions("sofia") == 1 and rt._sessions() == {}


def test_signing_in_again_asks_first(tmp_path, monkeypatch):
    from james_monitoring import connections as cx
    monkeypatch.setenv("JM_CLAUDE_BIN", cli(tmp_path, "claude", FAKE_CLAUDE))
    cx.login_cancel("claude-code")
    r = cx.login("claude-code", tmp_path / "logs")
    assert r["started"] is False and r["confirm"] and "replaces" in r["error"]


def test_models_module_assigns_and_reports(tmp_path, monkeypatch):
    from james_monitoring.server import App
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.delenv("OPENROUTER_API_KEY")                     # no key: health must say so
    app = App(tmp_path / "co", telegram=False)
    app.base.mkdir(parents=True)
    try:
        app.setup({"company": "A", "owner": "Maria", "provider": "fake"})
        app.admin.add(name="Riya", role="Backend", token="")
        app.load()
        app.models_assign({"provider": "ollama", "model": "llama3.1", "who": ["riya"]})
        riya = app.rt.cfg.member("riya")
        assert riya.llm.provider == "ollama" and riya.llm.base_url == "http://localhost:11434/v1" and riya.llm.api_key_env == ""
        team = {t["id"]: t for t in app.models_team()}
        assert team["riya"]["own"] and team["james"]["own"] is False
        with pytest.raises(ValueError, match="OpenCode model"):
            app.models_assign({"provider": "opencode", "model": "glm", "who": "company"})
        app.models_assign({"provider": "openrouter", "model": "z-ai/glm-4.6", "who": "company"})
        assert app.rt.cfg.llm.base_url == "https://openrouter.ai/api/v1" and app.rt.cfg.llm.api_key_env == "OPENROUTER_API_KEY"
        with pytest.raises(ValueError, match="Choose who"):          # never a silent company-wide change
            app.models_assign({"provider": "fake"})
        with pytest.raises(ValueError, match="don't know ghost"):
            app.models_assign({"provider": "fake", "who": ["ghost"]})
        app.models_assign({"provider": "fake", "members": "riya"})     # alias + single id, not split into letters
        assert app.rt.cfg.member("riya").llm.provider == "fake" and app.rt.cfg.llm.provider == "openrouter"
        app.models_use_default("riya")
        assert app.rt.cfg.member("riya").llm is None
        h = app.models_health()
        assert h["ok"] is False and h["problems"][0]["who"]                    # no OpenRouter key yet
    finally:
        app.submit(app._stop_services(app.sched, app.gw, app.slack), timeout=10)
        app.loop.call_soon_threadsafe(app.loop.stop)
