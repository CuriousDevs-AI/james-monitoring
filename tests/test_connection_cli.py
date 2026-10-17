from types import SimpleNamespace

import pytest

from james_monitoring.cli import main


@pytest.mark.parametrize("status,exit_code", [("missing", 0), ("configured", 0), ("unavailable", 1)])
def test_connection_check_does_not_claim_live_connection_from_stored_credentials(monkeypatch, capsys, status, exit_code):
    cfg = SimpleNamespace(company="Synthetic", git_author=("Owner", "owner@example.test"),
                          monitor=SimpleNamespace(bot_token_status=status),
                          slack=SimpleNamespace(credential_status=status), github=SimpleNamespace(enabled=True))
    monkeypatch.setattr("james_monitoring.cli._cfg", lambda args: cfg)
    monkeypatch.setattr("james_monitoring.connections.providers_in_use", lambda cfg: [])
    with pytest.raises(SystemExit) as out:
        main(["connection"])
    assert out.value.code == exit_code
    text = capsys.readouterr().out
    assert "✅ Telegram" not in text and "✅ Slack" not in text
    assert "sync not verified" in text
    if status == "configured":
        assert text.count("live connection not verified") == 2
    if status == "unavailable":
        assert text.count("credential store unavailable") == 2
