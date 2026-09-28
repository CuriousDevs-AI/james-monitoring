"""Connections: is each model installed, logged in and answering — and can it be fixed from the console."""
import stat
import time

from james_monitoring import connections as cx
from james_monitoring.config import LLMConfig


def cli(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_claude_logged_out_then_in(tmp_path, monkeypatch):
    state = tmp_path / "logged"
    monkeypatch.setenv("JM_CLAUDE_BIN", cli(tmp_path, "claude", f'''
if [ "$1 $2" = "auth status" ]; then
  if [ -f {state} ]; then echo '{{"loggedIn": true, "authMethod": "claude.ai"}}'; else echo '{{"loggedIn": false, "authMethod": "none"}}'; fi
elif [ "$1 $2" = "auth login" ]; then
  echo "Opening https://claude.ai/oauth/authorize?code=abc in your browser"; sleep 1; touch {state}
fi'''))
    c = cx.check(LLMConfig(provider="claude-code", model="sonnet"))
    assert (c["ok"], c["logged_in"], c["can_login"]) == (False, False, True) and "Log in" in c["fix"]
    r = cx.login("claude-code", tmp_path / "logs")
    assert r["started"] and r["url"].startswith("https://claude.ai/oauth/authorize")
    for _ in range(40):
        if not cx.login_state("claude-code")["running"]:
            break
        time.sleep(0.1)
    assert cx.login_state("claude-code")["exit"] == 0
    assert cx.check(LLMConfig(provider="claude-code"))["ok"]


def test_codex_and_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_CODEX_BIN", cli(tmp_path, "codex", 'echo "Logged in using ChatGPT"'))
    assert cx.check(LLMConfig(provider="codex-cli"))["ok"]
    monkeypatch.setenv("JM_CLAUDE_BIN", "")
    monkeypatch.setattr(cx.shutil, "which", lambda _: None)
    c = cx.check(LLMConfig(provider="claude-code"))
    assert not c["installed"] and "npm install -g @anthropic-ai/claude-code" in c["fix"] and not c["can_login"]


def test_api_key_and_local_models(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = cx.check(LLMConfig(provider="anthropic", model="m", api_key_env="ANTHROPIC_API_KEY"))
    assert not c["ok"] and "ANTHROPIC_API_KEY is not set" in c["detail"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert cx.check(LLMConfig(provider="anthropic", model="m", api_key_env="ANTHROPIC_API_KEY"))["ok"]
    assert cx.check(LLMConfig(provider="openai", model="llama3", base_url="http://localhost:11434/v1",
                              api_key_env=""))["ok"]
    assert cx.login("anthropic", __import__("pathlib").Path("/tmp"))["started"] is False


def test_console_groups_models_by_who_uses_them(tmp_path):
    from james_monitoring.server import App
    app = App(tmp_path / "co", telegram=False)
    app.base.mkdir(parents=True)
    try:
        app.setup({"company": "Acme", "owner": "Maria", "timezone": "UTC", "provider": "fake",
                   "git_name": "maria", "git_email": "maria@acme.dev"})
        app.admin.add(name="Riya", role="Backend", token="")
        app.load()
        app.member_update({"id": "riya", "llm": {"provider": "fake", "model": "other"}})
        c = app.connections()
        assert [sorted(m["people"]) for m in c["models"]] == [["james"], ["riya"]] and all(m["ok"] for m in c["models"])
        assert c["git"] == {"name": "maria", "email": "maria@acme.dev", "ok": True, "fix": ""}
        app.rt.ws.update_state(lambda s: s.setdefault("heartbeat", {}).__setitem__("riya", {"error": "boom", "fails": 3}))
        assert app.connections()["models"][1]["failing"] == {"riya": "boom"}
        assert app.connection_test({"member": "riya"})["ok"]
        assert app.rt.ws.state()["heartbeat"]["riya"]["error"] == ""          # a passing test clears the alarm
    finally:
        app.submit(app._stop_services(app.sched, app.gw, app.slack), timeout=10)
        app.loop.call_soon_threadsafe(app.loop.stop)


FAKE_OPENCODE = r'''
if [ "$1 $2" = "providers list" ]; then printf '\033[0m\n●  Anthropic \033[90moauth\n●  Zhipu AI \033[90mapi\n'; exit 0; fi
if [ "$1" = "models" ]; then printf 'opencode/big-pickle\nzhipuai/glm-4.6\n'; exit 0; fi
cat > "$(dirname "$0")/stdin.txt"; env > "$(dirname "$0")/env.txt"
echo '{"type":"step_start","part":{}}'
echo '{"type":"text","part":{"type":"text","text":"{\"reply\":\"pong\",\"actions\":[]}"}}'
echo '{"type":"step_finish","part":{"tokens":{"input":120,"output":9,"reasoning":1,"cache":{"read":1000,"write":0}}}}'
'''


def test_opencode_runs_any_model_and_locks_paid_ones_down(tmp_path, monkeypatch):
    from james_monitoring.llm import LLMError, make_llm
    import pytest
    monkeypatch.setenv("JM_OPENCODE_BIN", cli(tmp_path, "opencode", FAKE_OPENCODE))
    r = make_llm(LLMConfig(provider="opencode", model="zhipuai/glm-4.6")).complete("SYSTEM", [{"role": "user", "content": "ping"}])
    assert r.text == '{"reply":"pong","actions":[]}' and (r.input_tokens, r.output_tokens, r.cached_tokens) == (1120, 10, 1000)
    assert "SYSTEM" in (tmp_path / "stdin.txt").read_text()
    assert '"bash": "deny"' in (tmp_path / "env.txt").read_text()                 # paid: tools denied
    make_llm(LLMConfig(provider="opencode", model="opencode/big-pickle")).complete("S", [{"role": "user", "content": "x"}])
    assert "OPENCODE_CONFIG_CONTENT" not in (tmp_path / "env.txt").read_text()     # free tier: stock OpenCode
    with pytest.raises(LLMError, match="provider/model"):
        make_llm(LLMConfig(provider="opencode", model="glm"))
    assert cx.opencode_credentials() == ["anthropic", "zhipuai"]
    assert cx.check(LLMConfig(provider="opencode", model="zhipuai/glm-4.6"))["ok"]
    bad = cx.check(LLMConfig(provider="opencode", model="openai/gpt-5"))
    assert not bad["ok"] and "jm connection login opencode" in bad["fix"]
    assert cx.opencode_models() == ["opencode/big-pickle", "zhipuai/glm-4.6"]


def test_opencode_error_is_explained(tmp_path, monkeypatch):
    from james_monitoring.llm import LLMError, make_llm
    import pytest
    monkeypatch.setenv("JM_OPENCODE_BIN", cli(tmp_path, "opencode",
                                              """echo '{"type":"error","error":{"name":"APIError","data":{"message":"401 invalid x-api-key"}}}'"""))
    with pytest.raises(LLMError, match="401 invalid x-api-key.*opencode providers login"):
        make_llm(LLMConfig(provider="opencode", model="anthropic/claude-sonnet-4-5")).complete("s", [{"role": "user", "content": "x"}])


def test_jm_connection_command(tmp_path, monkeypatch, capsys):
    import pytest
    import yaml
    from james_monitoring.cli import main
    from .conftest import make_raw
    monkeypatch.setenv("JM_CODEX_BIN", cli(tmp_path, "codex", 'echo "Logged in using ChatGPT"'))
    raw = make_raw(tmp_path, llm={"provider": "codex-cli"})
    raw["team"][1]["llm"] = {"provider": "fake"}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(SystemExit) as e:
        main(["-c", str(tmp_path / "config.yaml"), "connection"])
    out = capsys.readouterr().out
    assert e.value.code == 0 and "✅ ChatGPT / Codex subscription" in out and "used by James, Marcus" in out
    with pytest.raises(SystemExit):
        main(["-c", str(tmp_path / "config.yaml"), "connection", "login", "anthropic"])
