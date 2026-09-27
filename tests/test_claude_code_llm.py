"""claude-code provider: drives the `claude` CLI (subscription login). Tested with a fake CLI script."""
import stat

import pytest

from james_monitoring.config import LLMConfig, parse_config
from james_monitoring.llm import LLMError, make_llm

FAKE_OK = """#!/bin/sh
cat > "$(dirname "$0")/last_stdin.txt"
echo "$@" > "$(dirname "$0")/last_args.txt"
echo '{"type":"result","subtype":"success","is_error":false,"result":"{\\"reply\\":\\"hi\\",\\"actions\\":[]}","usage":{"input_tokens":3,"cache_creation_input_tokens":100,"cache_read_input_tokens":0,"output_tokens":7}}'
"""
FAKE_ERR = """#!/bin/sh
echo '{"type":"result","is_error":true,"result":"Not logged in · Please run /login"}'
exit 1
"""


def fake_cli(tmp_path, body):
    p = tmp_path / "claude"
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_calls_cli_with_system_prompt_and_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_CLAUDE_BIN", str(fake_cli(tmp_path, FAKE_OK)))
    llm = make_llm(LLMConfig(provider="claude-code", model="sonnet"))
    r = llm.complete("SYSTEM RULES", [{"role": "user", "content": "first"},
                                      {"role": "assistant", "content": "ok"},
                                      {"role": "user", "content": "second"}])
    assert r.text == '{"reply":"hi","actions":[]}' and r.input_tokens == 103 and r.output_tokens == 7
    args = (tmp_path / "last_args.txt").read_text()
    assert "-p" in args and "--output-format json" in args and "--system-prompt SYSTEM RULES" in args
    assert "--model sonnet" in args and "--no-session-persistence" in args
    stdin = (tmp_path / "last_stdin.txt").read_text()
    assert "first" in stdin and "second" in stdin and "answer this one" in stdin


def test_cli_error_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_CLAUDE_BIN", str(fake_cli(tmp_path, FAKE_ERR)))
    with pytest.raises(LLMError, match="Not logged in"):
        make_llm(LLMConfig(provider="claude-code")).complete("s", [{"role": "user", "content": "x"}])


def test_config_needs_no_api_key(tmp_path):
    from .conftest import make_raw
    cfg = parse_config(make_raw(tmp_path, llm={"provider": "claude-code"}), base_dir=tmp_path)
    assert cfg.llm.api_key_env == "" and cfg.llm.model == ""
