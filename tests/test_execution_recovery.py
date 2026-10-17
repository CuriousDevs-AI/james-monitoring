"""Approval attempts survive restarts; uncertainty never authorises a second attempt."""
import asyncio
import subprocess
import sys

import pytest

from james_monitoring.asks import AskError, AskStore
from james_monitoring.llm.fake import FakeLLM
from james_monitoring.runtime import ConsoleBus, Event, Runtime
from .test_workforce import app, http  # reusable synthetic HTTP fixtures


def action_ask(rt):
    return rt.asks.create(requester="sofia", summary="Publish reviewed output", kind="action",
                         payload={"action": {"type": "write_file", "path": "docs/output.md", "content": "approved"}})


async def test_successful_attempt_is_durable_and_never_replayed(rt):
    ask = action_ask(rt)
    await rt.decide_ask(ask.id, "approved", rt.owner_id)
    await rt.drain()
    saved = rt.asks.get(ask.id)
    assert saved.execution["status"] == "succeeded"
    assert len(saved.execution["request_sha256"]) == 64
    assert "Do not repeat it" in rt.llm.calls[-1][1][-1]["content"]
    restarted = Runtime(rt.cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    with pytest.raises(AskError, match="No retry"):
        await restarted.decide_ask(ask.id, "approved", rt.owner_id)
    assert (rt.ws.root / "docs/output.md").read_text() == "approved\n"


async def test_started_attempt_from_crash_requires_human_reconciliation(rt):
    ask = action_ask(rt)
    rt.asks.start_execution(ask, rt.owner_id)  # crash before status/side effect
    restarted = Runtime(rt.cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    with pytest.raises(AskError, match="No retry"):
        await restarted.decide_ask(ask.id, "approved", rt.owner_id)
    assert [a.id for a in restarted.asks.unresolved_executions()] == [ask.id]
    with pytest.raises(AskError, match="evidence"):
        await restarted.reconcile_ask(ask.id, "not_completed", rt.owner_id, "")
    await restarted.reconcile_ask(ask.id, "not_completed", rt.owner_id, "Verified no output at destination")
    assert restarted.asks.get(ask.id).status == "rejected"
    assert restarted.asks.unresolved_executions() == []
    assert not (rt.ws.root / "docs/output.md").exists()
    assert not restarted.llm.calls


async def test_error_after_side_effect_is_uncertain_not_retryable(rt, monkeypatch):
    task = rt.tasks.create(title="Send", owner="sofia", created_by=rt.owner_id)
    ask = rt.asks.create(requester="sofia", summary="Send exact draft", kind="action", task=task.id,
                         payload={"action": {"type": "post_group", "text": "reviewed draft"}})
    sends = []

    async def delivered_but_timeout(*args):
        sends.append("sent")
        raise TimeoutError("provider-secret-must-not-leak")

    monkeypatch.setattr(rt, "_do", delivered_but_timeout)
    result = await rt.decide_ask(ask.id, "approved", rt.owner_id)
    assert "uncertain" in result
    assert "provider-secret" not in result + rt.asks.get(ask.id).path.read_text()
    assert rt.asks.get(ask.id).execution["status"] == "uncertain"
    assert rt.tasks.get(task.id).status == "blocked"
    assert not rt.llm.calls
    with pytest.raises(AskError, match="No retry"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)
    await rt.reconcile_ask(ask.id, "completed", rt.owner_id, "Destination receipt 123 confirms delivery")
    assert sends == ["sent"]
    assert rt.asks.get(ask.id).execution["outcome"] == "completed"


async def test_two_runtimes_and_reconciliation_cannot_race_active_execution(rt, monkeypatch):
    ask = action_ask(rt)
    other = Runtime(rt.cfg, FakeLLM(), bus=ConsoleBus(quiet=True))
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        started.set()
        await release.wait()
        return "synthetic success"

    monkeypatch.setattr(rt, "_do", blocked)
    execution = asyncio.create_task(rt.decide_ask(ask.id, "approved", rt.owner_id))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(AskError, match="elsewhere"):
            await other.decide_ask(ask.id, "approved", rt.owner_id)
        with pytest.raises(AskError, match="elsewhere"):
            await other.reconcile_ask(ask.id, "completed", rt.owner_id, "Too early")
    finally:
        release.set()
        await execution
        await rt.drain()


async def test_cancelled_attempt_remains_uncertain(rt, monkeypatch):
    ask = action_ask(rt)
    started = asyncio.Event()

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rt, "_do", blocked)
    execution = asyncio.create_task(rt.decide_ask(ask.id, "approved", rt.owner_id))
    await asyncio.wait_for(started.wait(), 5)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert rt.asks.get(ask.id).execution["status"] == "uncertain"
    with pytest.raises(AskError, match="No retry"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)


def test_os_guard_blocks_another_process(rt):
    ask = action_ask(rt)
    code = """
import sys
from pathlib import Path
from james_monitoring.asks import AskStore, AskError
store = AskStore(Path(sys.argv[1]), 'UTC')
try:
    with store.decision_guard(sys.argv[2]):
        sys.exit(2)
except AskError:
    sys.exit(0)
"""
    with rt.asks.decision_guard(ask.id):
        result = subprocess.run([sys.executable, "-c", code, str(rt.ws.root), ask.id], timeout=10)
    assert result.returncode == 0


def test_changed_request_cannot_be_claimed(rt):
    ask = action_ask(rt)
    changed = rt.asks.get(ask.id)
    changed.doc.meta["payload"]["action"]["content"] = "changed after review"
    from james_monitoring.fileio import atomic_write
    atomic_write(changed.path, changed.doc.render())
    with pytest.raises(AskError, match="changed during review"):
        rt.asks.start_execution(ask, rt.owner_id)


async def test_interrupted_task_is_held_across_scheduler_dispatch_and_actions(rt):
    task = rt.tasks.create(title="Work", owner="sofia", created_by=rt.owner_id)
    ask = rt.asks.create(requester="sofia", summary="Write", kind="action", task=task.id,
                         payload={"action": {"type": "write_file", "path": "docs/no.md", "content": "x"}})
    rt.asks.start_execution(ask, rt.owner_id)
    assert rt.next_work(rt.cfg.member("sofia")) is None
    assert "reconcile" in await rt.dispatch("sofia", Event("system", "work", sender="system", task=task.id))
    assert not rt.llm.calls
    from james_monitoring.tasks import TaskError
    with pytest.raises(TaskError, match="reconcile"):
        await rt._do(rt.cfg.member("sofia"), {"type": "write_file", "path": "docs/no.md", "content": "x"},
                     Event("system", "retry under another request", sender="system", task=task.id))
    assert not (rt.ws.root / "docs/no.md").exists()


def test_reconciliation_is_owner_only_and_never_executes_over_http(app, http):
    ask = app.rt.asks.create(requester="riya", summary="Uncertain action", kind="action",
                            payload={"action": {"type": "post_group", "text": "Never send"}})
    app.rt.asks.start_execution(ask, "Maria")
    admin = http("/api/users", {"action": "add", "name": "Admin", "role": "admin"})[1]["key"]
    assert http("/api/executions", key=admin)[0] == 403
    body = {"id": ask.id, "outcome": "not_completed", "evidence": "Confirmed no destination receipt"}
    assert http("/api/ask/reconcile", body, key=admin)[0] == 403
    assert http("/api/executions")[1][0]["id"] == ask.id
    assert http("/api/ask/reconcile", {**body, "evidence": ""})[0] == 400
    assert http("/api/ask/reconcile", body)[0] == 200
    assert http("/api/executions")[1] == []
    assert app.rt.asks.get(ask.id).status == "rejected"
    assert not app.rt.llm.calls


async def test_action_card_contains_exact_content_and_truncated_chat_cannot_approve(rt):
    sofia = rt.cfg.member("sofia")
    sofia.permissions["post_group"] = "red"
    text = "Full reviewed message " * 100
    await rt._apply(sofia, {"type": "post_group", "text": text}, Event("dm", "draft", sender=rt.owner_id))
    ask = rt.asks.pending()[0]
    assert text in ask.doc.sections["Details"]
    with pytest.raises(AskError, match="full details"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id, via="telegram")
    assert not rt.asks.get(ask.id).execution
    await rt.decide_ask(ask.id, "approved", rt.owner_id, via="console")
    await rt.drain()
    assert rt.asks.get(ask.id).execution["status"] == "succeeded"
