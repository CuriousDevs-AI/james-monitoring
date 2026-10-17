import json
import os
import sys

import pytest

from james_monitoring.config import ConfigError, LLMConfig, load_config, parse_config
from james_monitoring.fileio import write_config
from james_monitoring.llm import LLMError, LLMResult
from james_monitoring.secret_store import SecretStore, SecretStoreError, save_secrets
from james_monitoring.server import App
from .conftest import make_raw
from .test_workforce import app, http  # existing isolated console/HTTP fixtures


@pytest.fixture
def keychain(monkeypatch):
    class MemoryKeychain:
        def __init__(self):
            self.values = {}
            self.fail = False

        def get_password(self, service, name):
            if self.fail:
                raise RuntimeError("secret-value-in-native-error")
            return self.values.get((service, name))

        def set_password(self, service, name, value):
            if self.fail:
                raise RuntimeError(value)
            self.values[service, name] = value

    backend = MemoryKeychain()
    monkeypatch.setattr(SecretStore, "_keychain", lambda self: backend)
    return backend


def native_store(tmp_path, namespace="test-company"):
    return SecretStore(tmp_path, {"backend": "keychain", "namespace": namespace})


def test_keychain_roundtrip_rotation_namespace_and_no_plaintext(tmp_path, keychain, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-environment-value")
    store = native_store(tmp_path)
    assert store.get("OPENAI_API_KEY") == ""
    store.save({"OPENAI_API_KEY": "first-secret"})
    assert store.get("OPENAI_API_KEY") == "first-secret"
    store.save({"OPENAI_API_KEY": "rotated-secret"})
    assert store.get("OPENAI_API_KEY") == "rotated-secret"
    assert native_store(tmp_path, "another-company").get("OPENAI_API_KEY") == ""
    assert os.environ["OPENAI_API_KEY"] == "unrelated-environment-value"
    assert not (tmp_path / ".env").exists()
    assert "rotated-secret" not in repr(store)


def test_keychain_failures_do_not_leak_or_fallback(tmp_path, keychain, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-account")
    keychain.fail = True
    store = native_store(tmp_path)
    for action in (lambda: store.get("OPENAI_API_KEY"), lambda: store.save({"OPENAI_API_KEY": "sensitive-value"})):
        with pytest.raises(SecretStoreError) as error:
            action()
        assert "sensitive-value" not in str(error.value)
        assert "secret-value-in-native-error" not in str(error.value)
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("settings", [{"backend": "plaintext"}, {"backend": "keychain"},
                                      {"backend": "keychain", "namespace": "../bad"},
                                      {"backend": "keychain", "namespace": 123}, [], "bad"])
def test_bad_store_config_rejected(tmp_path, settings):
    with pytest.raises(ConfigError):
        parse_config(make_raw(tmp_path, secrets=settings), base_dir=tmp_path)


def test_non_macos_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SecretStoreError, match="macOS"):
        native_store(tmp_path).get("OPENAI_API_KEY")


@pytest.mark.parametrize("pairs", [{"BAD\nNAME": "value"}, {"OPENAI_API_KEY": "x\nINJECTED=value"},
                                  {"OPENAI_API_KEY": "x\0y"}, {"OPENAI_API_KEY": ""}])
def test_invalid_secret_input_rejected_before_write(tmp_path, pairs, keychain):
    for store in (SecretStore(tmp_path), native_store(tmp_path)):
        with pytest.raises(SecretStoreError):
            store.save(pairs)
    assert not keychain.values and not (tmp_path / ".env").exists()


def test_config_uses_native_store_for_all_connector_scopes(tmp_path, keychain, monkeypatch):
    raw = make_raw(tmp_path, secrets={"backend": "keychain", "namespace": "test-company"},
                   llm={"provider": "openai", "model": "test"},
                   projects={"site": {"llm": {"provider": "openrouter", "model": "test"},
                                      "agents": {"sofia": {"llm": {"provider": "anthropic", "model": "test"}}}}})
    raw["team"][1]["llm"] = {"provider": "openai", "model": "other", "api_key_env": "SOFIA_API_KEY"}
    write_config(tmp_path / "config.yaml", raw)
    (tmp_path / ".env").write_text("JM_SHOULD_NOT_LOAD=plaintext\n")
    monkeypatch.delenv("JM_SHOULD_NOT_LOAD", raising=False)
    pairs = {"OPENAI_API_KEY": "company-key", "SOFIA_API_KEY": "person-key", "OPENROUTER_API_KEY": "project-key",
             "ANTHROPIC_API_KEY": "project-person-key", "TG_TOKEN_JAMES": "bot-key",
             "SLACK_BOT_TOKEN": "slack-bot", "SLACK_APP_TOKEN": "slack-app"}
    save_secrets(tmp_path, pairs)
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.llm.api_key == "company-key"
    assert cfg.member("sofia").llm.api_key == "person-key"
    assert cfg.projects["site"].llm.api_key == "project-key"
    assert cfg.llm_for(cfg.member("sofia"), "site").api_key == "project-person-key"
    assert cfg.monitor.bot_token == "bot-key"
    assert cfg.slack.bot_token == "slack-bot" and cfg.slack.app_token == "slack-app"
    assert "JM_SHOULD_NOT_LOAD" not in os.environ
    assert not any(value in (tmp_path / "config.yaml").read_text() for value in pairs.values())


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_missing_native_model_credential_never_uses_sdk_environment(tmp_path, keychain, monkeypatch, provider):
    from james_monitoring.llm import make_llm
    name = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    monkeypatch.setenv(name, "wrong-account-key")
    cfg = LLMConfig(provider=provider, model="test-model", api_key_env=name, secret_store=native_store(tmp_path))
    with pytest.raises(LLMError, match="missing from Keychain"):
        make_llm(cfg)
    keychain.fail = True
    with pytest.raises(LLMError, match="Keychain read failed"):
        make_llm(cfg)


@pytest.mark.parametrize("fail", [False, True])
def test_model_probe_does_not_mutate_environment_or_echo_secret(tmp_path, monkeypatch, fail):
    monkeypatch.setenv("OPENAI_API_KEY", "original-key")
    app = App(tmp_path, telegram=False)
    seen = []

    class Probe:
        def complete(self, *args):
            if fail:
                raise LLMError("request failed: temporary-credential")
            return LLMResult("pong temporary-credential")

    def make(conf):
        seen.append(conf.api_key)
        assert "temporary-credential" not in repr(conf)
        return Probe()

    monkeypatch.setattr("james_monitoring.server.make_llm", make)
    try:
        result = app.test_model({"provider": "openai", "model": "test", "api_key": "temporary-credential"})
        assert seen == ["temporary-credential"]
        assert "temporary-credential" not in json.dumps(result)
        assert os.environ["OPENAI_API_KEY"] == "original-key"
        assert not (tmp_path / ".env").exists()
    finally:
        app.close()


@pytest.mark.parametrize("probe", [False, True])
def test_anthropic_native_or_probe_key_excludes_ambient_auth_token(tmp_path, keychain, monkeypatch, probe):
    from james_monitoring.llm.anthropic_llm import AnthropicLLM
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "wrong-account-token")
    store = native_store(tmp_path)
    store.save({"ANTHROPIC_API_KEY": "native-key"})
    cfg = LLMConfig(model="test", secret_store=None if probe else store,
                    api_key_override="probe-key" if probe else None)
    kwargs = []
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: kwargs.append(kw))
    AnthropicLLM(cfg)
    assert kwargs == [{"api_key": "probe-key" if probe else "native-key", "auth_token": ""}]


def test_secret_metadata_and_write_are_owner_only_over_http(app, http, keychain):
    raw = app.raw()
    raw["secrets"] = {"backend": "keychain", "namespace": "test-company"}
    app.save_raw(raw)
    app.load()
    admin = http("/api/users", {"action": "add", "name": "Admin", "role": "admin"})[1]["key"]
    assert http("/api/secrets", key=admin)[0] == 403
    assert http("/api/secrets", {"name": "OPENAI_API_KEY", "secret": "no"}, key=admin)[0] == 403
    assert http("/api/secrets", {"name": "PATH", "secret": "no"})[0] == 400
    assert http("/api/model_key", {"env": "TG_TOKEN_NOVA", "key": "no"}, key=admin)[0] == 400
    assert http("/api/secrets", {"name": "OPENAI_API_KEY", "secret": "non-token-shaped-sensitive-value"})[0] == 200
    status, data = http("/api/secrets")
    assert status == 200 and data["backend"] == "keychain"
    assert {"name": "OPENAI_API_KEY", "status": "configured"} in data["entries"]
    assert "non-token-shaped-sensitive-value" not in json.dumps(data)
    assert "non-token-shaped-sensitive-value" not in json.dumps(http("/api/audit")[1])
    assert not (app.base / ".env").exists()
    # Existing API-key UI honours Keychain too (no hidden legacy-file write).
    assert http("/api/model_key", {"env": "OPENROUTER_API_KEY", "key": "router-value"})[0] == 200
    assert keychain.values["james-monitoring:test-company", "OPENROUTER_API_KEY"] == "router-value"


def test_scaffold_saves_bot_credentials_to_selected_store(tmp_path, keychain):
    from james_monitoring.setup import scaffold
    raw = make_raw(tmp_path, secrets={"backend": "keychain", "namespace": "test-company"})
    cfg = scaffold(raw, tokens={"james": "synthetic-bot-token"}, base_dir=tmp_path)
    assert cfg.monitor.bot_token == "synthetic-bot-token"
    assert not (tmp_path / ".env").exists()
    assert "synthetic-bot-token" not in (tmp_path / "config.yaml").read_text()


def test_locked_store_does_not_break_console_startup_and_read_views(app, keychain):
    raw = app.raw()
    raw["secrets"] = {"backend": "keychain", "namespace": "test-company"}
    raw["llm"] = {"provider": "openai", "model": "test-model"}
    app.save_raw(raw)
    keychain.fail = True
    app.telegram_enabled = True
    app.load()
    assert app.ready and app.gw is None and app.slack is None
    state = app.state()
    assert "Keychain" in state["llm_error"]
    assert state["telegram"]["credential_status"] == "unavailable"
    assert state["slack"]["credential_status"] == "unavailable"
    assert state["telegram"]["error"] and state["slack"]["error"]
    assert all(m["telegram_credential_status"] == "unavailable" for m in state["members"])
    channels = app.connections()
    assert not channels["models"][0]["ok"]
    assert channels["telegram"]["credential_status"] == "unavailable"
    checks = {c["name"]: c for c in app.health()["checks"]}
    assert not checks["Telegram"]["ok"] and not checks["Slack"]["ok"]
    assert not checks["AI models"]["ok"]
    assert "unavailable" in checks["Telegram"]["detail"].lower()
    assert all(m["token_status"] == "unavailable" for m in app.admin.state()["members"])
    assert not app.admin.member_status(app.rt.cfg.monitor.id)["ok"]
    assert all(e["status"] == "unavailable" for e in app.secrets_status()["entries"])
    assert "secret-value-in-native-error" not in json.dumps(state)


def test_store_locked_after_startup_is_reported_without_reload(app, keychain):
    raw = app.raw()
    raw["secrets"] = {"backend": "keychain", "namespace": "test-company"}
    app.save_raw(raw)
    app.load()
    assert not app.state()["telegram"]["error"]
    keychain.fail = True
    for status in (app.state(), app.connections()):
        for name in ("telegram", "slack"):
            assert status[name]["credential_status"] == "unavailable" and status[name]["error"]
    keychain.fail = False
    assert not app.state()["telegram"]["error"]
    assert app.state()["telegram"]["credential_status"] == "missing"


def test_model_readiness_reports_locked_store_and_recovers(tmp_path, keychain):
    from james_monitoring.connections import check
    store = native_store(tmp_path)
    cfg = LLMConfig(provider="openai", model="test", api_key_env="OPENAI_API_KEY", secret_store=store)
    keychain.fail = True
    result = check(cfg)
    assert not result["ok"] and result["credential_status"] == "unavailable"
    assert "secret-value-in-native-error" not in json.dumps(result)
    keychain.fail = False
    assert check(cfg)["credential_status"] == "missing"
    store.save({"OPENAI_API_KEY": "restored-value"})
    assert check(cfg)["ok"] and check(cfg)["credential_status"] == "configured"


def test_legacy_env_mode_is_explicit_and_compatible(tmp_path, monkeypatch):
    monkeypatch.delenv("JM_TEST_CREDENTIAL", raising=False)
    store = SecretStore(tmp_path)
    store.save({"JM_TEST_CREDENTIAL": "legacy-value"})
    assert store.get("JM_TEST_CREDENTIAL") == "legacy-value"
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


async def test_coding_executor_does_not_inherit_connector_credentials(tmp_path, monkeypatch):
    from james_monitoring.executor import Executor
    from .test_executor import git
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("test\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
    monkeypatch.setenv("TG_TOKEN_JAMES", "sensitive-bot-value")
    monkeypatch.setenv("JM_CUSTOM_SECRET", "sensitive-custom-value")
    code = "import os; assert 'TG_TOKEN_JAMES' not in os.environ; assert 'JM_CUSTOM_SECRET' not in os.environ; print('safe')"
    cfg = parse_config(make_raw(tmp_path, projects={"site": {"repo": str(repo)}},
                                executor={"command": [sys.executable, "-c", code]}), base_dir=tmp_path)
    result = await Executor(cfg).run(project_id="site", task_id="T-001", prompt="test")
    assert "safe" in result.output_tail and "sensitive" not in result.output_tail
