import json

import pytest

from james_monitoring.config import parse_config
from james_monitoring.llm import LLMResult
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Event, Runtime
from .conftest import make_raw
from .test_executor import git


@pytest.mark.parametrize("reply", [
    "invalid response",
    json.dumps({"reply": "Read first", "actions": [{"type": "read_file", "path": "docs/info.md"}]}),
])
async def test_budget_stops_format_and_file_read_followups(rt, reply):
    rt.cfg.daily_tokens_per_agent = 1
    rt.llm.push(reply)
    rt.llm.push({"reply": "Must not run", "actions": [{"type": "create_task", "title": "Unexpected"}]})
    out = await rt.dispatch("sofia", Event("dm", "work", sender=rt.owner_id))
    assert len(rt.llm.calls) == 1
    assert "budget was reached" in out
    assert rt.tasks.all() == []
    assert rt.ws.state().get("queue", []) == []


async def test_budget_stops_repair_without_replaying_successful_actions(rt):
    rt.cfg.daily_tokens_per_agent = 1
    rt.llm.push({"reply": "Results", "actions": [
        {"type": "create_task", "title": "Created once"},
        {"type": "update_task", "id": "T-999", "status": "doing"},
    ]})
    out = await rt.dispatch("sofia", Event("dm", "work", sender=rt.owner_id))
    assert len(rt.llm.calls) == 1
    assert len(rt.tasks.all()) == 1
    assert "Repair stopped" in out and "Earlier action results still apply" in out
    assert rt.ws.state().get("queue", []) == []


@pytest.mark.parametrize("inputs,outputs,cached,expected", [
    (100, 20, 50, 75), (100, 20, 2000, 30), (100, 20, -50, 120), (-100, -20, 10, 0),
])
def test_provider_token_counts_cannot_make_usage_negative(inputs, outputs, cached, expected):
    result = LLMResult("synthetic", inputs, outputs, cached_tokens=cached)
    assert result.billable_tokens == expected
    assert result.total_tokens >= 0


@pytest.mark.parametrize("command", ["printf partial > result.txt; exit 2", "exit 2"])
async def test_failed_coding_command_is_not_promoted_to_merge_review(tmp_path, monkeypatch, command):
    repo = tmp_path / "site"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "result.txt").write_text("base")
    git(repo, "add", "result.txt")
    git(repo, "-c", "user.name=Synthetic", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    raw = make_raw(tmp_path, projects={"site": {"repo": str(repo)}},
                   executor={"command": ["sh", "-c", command]})
    rt = Runtime(parse_config(raw, base_dir=tmp_path), FakeLLM(), bus=ConsoleBus(quiet=True))
    rt.cfg.github.prs = True
    opened = []
    monkeypatch.setattr(rt.executor, "open_pr", lambda *args: opened.append(args))
    task = rt.tasks.create(title="Build", owner="sofia", created_by=rt.owner_id, project="site", done_means=["Verified"])
    await rt._run_code_inner(rt.cfg.member("sofia"), task.id, "site", "synthetic failure")
    assert not opened and not rt.asks.pending()
    assert rt.tasks.get(task.id).status == "blocked"
    assert rt.next_work(rt.cfg.member("sofia")) is None
    assert (repo / "result.txt").read_text() == "base"
    assert any("Coding command" in text and "failed" in text for _, _, text in rt.bus.sent)
