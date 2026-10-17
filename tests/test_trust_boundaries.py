"""Privacy and human-review regressions; all data and GitHub traffic are synthetic."""
import json

import pytest

from james_monitoring.asks import AskError
from james_monitoring.config import Member
from james_monitoring.github import Change
from james_monitoring.monitor import build_report, checks
from james_monitoring.runtime import Event
from james_monitoring.tasks import TaskError

from .test_github import fake_gh, runtime


def private_work(rt):
    rt.cfg.team.append(Member(id="ada", name="Ada", role="Assistant", assistant=True))
    rt.ws.write("team/ada/memory.md", "PRIVATE_SENTINEL")
    task = rt.tasks.create(title="PRIVATE_SENTINEL", owner="ada", created_by=rt.owner_id, project="site")
    ask = rt.asks.create(requester="ada", summary="PRIVATE_SENTINEL", task=task.id)
    return task, ask


def test_agent_file_reads_enforce_private_ownership(rt):
    task, ask = private_work(rt)
    sofia, ada = rt.cfg.member("sofia"), rt.cfg.member("ada")
    alias = rt.ws.root / "docs" / "alias.md"
    alias.parent.mkdir(exist_ok=True)
    alias.symlink_to(rt.ws.root / "team/ada/memory.md")
    paths = ["team/ada/memory.md", task.id, str(task.path.relative_to(rt.ws.root)),
             str(ask.path.relative_to(rt.ws.root)), "docs/alias.md"]
    for path in paths:
        assert "PRIVATE_SENTINEL" not in rt._read_files([{"path": path}], sofia)
        assert "PRIVATE_SENTINEL" in rt._read_files([{"path": path}], ada)


def test_private_asks_are_absent_from_team_context_and_reports(rt):
    _, ask = private_work(rt)
    assert "PRIVATE_SENTINEL" not in rt.context_for(rt.cfg.monitor)
    assert "PRIVATE_SENTINEL" not in rt.project_channel("site")
    assert "PRIVATE_SENTINEL" not in build_report(rt.cfg, rt.ws, rt.tasks, rt.asks)
    rt.asks.decide(ask.id, "rejected", by="Pankaj")
    assert "PRIVATE_SENTINEL" not in build_report(rt.cfg, rt.ws, rt.tasks, rt.asks)


def test_private_alerts_do_not_enter_manager_or_group_context(rt):
    task, _ = private_work(rt)
    rt.tasks.update_fields(task.id, rt.owner_id, due="2000-01-01")
    rt.ws.update_state(lambda s: s.update(heartbeat={"ada": {"error": "PRIVATE_ERROR", "fails": 3}}))
    alerts = checks(rt.cfg, rt.ws, rt.tasks, rt.asks)
    sensitive = [a for a in alerts if "PRIVATE" in a.text]
    assert len(sensitive) == 2
    assert all(a.private_to == "ada" and not a.nudge for a in sensitive)


async def test_private_assistant_shared_output_needs_approval(rt):
    private_work(rt)
    await rt._apply(rt.cfg.member("ada"), {"type": "write_file", "path": "shared.md",
                                         "content": "PRIVATE_SENTINEL"}, Event("system", "write", sender="system"))
    assert not (rt.ws.root / "docs/shared.md").exists()
    assert any(a.kind == "action" and a.requester == "ada" for a in rt.asks.pending())


async def test_private_task_cannot_be_dispatched_to_another_agent(rt):
    task, _ = private_work(rt)
    await rt.dispatch("sofia", Event("system", "PRIVATE_SENTINEL", sender="system", task=task.id))
    assert not rt.llm.calls


async def test_agents_cannot_mutate_private_tasks(rt):
    task, _ = private_work(rt)
    for mid in ("james", "sofia"):
        with pytest.raises((TaskError, ValueError), match="private"):
            await rt._do(rt.cfg.member(mid), {"type": "update_task", "id": task.id, "log": "intrusion"},
                         Event("dm", "edit", sender=rt.owner_id))
    assert "intrusion" not in rt.tasks.get(task.id).path.read_text()


@pytest.mark.parametrize("entry", ["_apply", "_do"])
async def test_event_task_privacy_is_checked_even_with_another_explicit_task(rt, entry):
    private, _ = private_work(rt)
    public = rt.tasks.create(title="Public", owner="sofia", created_by=rt.owner_id)
    with pytest.raises(TaskError, match="private"):
        await getattr(rt, entry)(rt.cfg.member("sofia"),
                                {"type": "write_file", "task": public.id,
                                 "path": "docs/no.md", "content": "private context"},
                                Event("system", "work", sender="system", task=private.id))
    assert not (rt.ws.root / "docs/no.md").exists()


@pytest.mark.parametrize("invalid", ["removed_requester", "missing_action", "unknown_action", "private_task"])
async def test_invalid_action_approval_stays_pending_without_side_effects(rt, invalid):
    task = rt.tasks.create(title="Work", owner="sofia", created_by=rt.owner_id)
    payload = {"action": {"type": "write_file", "path": "docs/no.md", "content": "not authorised"}}
    if invalid == "removed_requester":
        rt.cfg.team = [m for m in rt.cfg.team if m.id != "sofia"]
    elif invalid == "missing_action":
        payload = {}
    elif invalid == "unknown_action":
        payload["action"]["type"] = "unrecognised"
    elif invalid == "private_task":
        private_work(rt)
        rt.tasks.update_fields(task.id, rt.owner_id, owner="ada")
    ask = rt.asks.create(requester="sofia", summary="Write output", kind="action", task=task.id,
                         payload=payload)
    with pytest.raises(AskError, match="still open"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)
    assert rt.asks.get(ask.id).status == "pending"
    assert not (rt.ws.root / "docs/no.md").exists()
    # A stale request must remain dismissible without executing its action.
    await rt.decide_ask(ask.id, "rejected", rt.owner_id)
    await rt.drain()
    assert rt.asks.get(ask.id).status == "rejected"


async def test_approval_preserves_origin_when_action_names_a_different_task(rt):
    origin = rt.tasks.create(title="Original context", owner="sofia", created_by=rt.owner_id)
    target = rt.tasks.create(title="Output task", owner="sofia", created_by=rt.owner_id)
    sofia = rt.cfg.member("sofia")
    sofia.permissions["write_file"] = "red"
    await rt._apply(sofia, {"type": "write_file", "task": target.id,
                            "path": "docs/no.md", "content": "original context"},
                    Event("system", "work", sender="system", task=origin.id))
    ask = rt.asks.pending()[0]
    private_work(rt)
    rt.tasks.update_fields(origin.id, rt.owner_id, owner="ada")
    with pytest.raises(AskError, match="private"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)
    assert rt.asks.get(ask.id).status == "pending"
    assert not (rt.ws.root / "docs/no.md").exists()


async def test_private_tasks_never_enter_github_mirror(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    private_work(rt)
    await rt.sync_github()
    assert gh()["issues"] == {}


async def test_board_edit_requires_human_decision_and_is_deduplicated(tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Review me", owner="sofia", created_by=rt.owner_id)
    rt.tasks.set_status(t.id, "review", by="sofia")
    change = Change(t.id, "stage", "done", source="stage")
    await rt._apply_github(change)
    await rt._apply_github(change)
    assert rt.tasks.get(t.id).status == "review"
    pending = rt.asks.pending()
    assert len(pending) == 1 and pending[0].kind == "github_change"
    await rt.decide_ask(pending[0].id, "approved", by="Pankaj", via="console")
    await rt.drain()
    assert rt.tasks.get(t.id).status == "done"


async def test_stale_board_approval_fails_closed(tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Priority", owner="sofia", created_by=rt.owner_id)
    await rt._apply_github(Change(t.id, "priority", "P2"))
    ask = rt.asks.pending()[0]
    rt.tasks.update_fields(t.id, rt.owner_id, priority="P0")
    with pytest.raises(AskError, match="changed"):
        await rt.decide_ask(ask.id, "approved", by="Pankaj")
    assert rt.tasks.get(t.id).priority == "P0"
    assert rt.asks.get(ask.id).status == "pending"


async def test_unapproved_proposal_cannot_reach_models_through_file_reads(tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Copy", owner="sofia", created_by=rt.owner_id)
    await rt._apply_github(Change(t.id, "comment", "INJECTION_SENTINEL", actor="pankajneema", event_id="16"))
    ask = rt.asks.pending()[0]
    path = str(ask.path.relative_to(rt.ws.root))
    for m in rt.cfg.team:
        assert "INJECTION_SENTINEL" not in rt._read_files([{"path": path}], m)
        assert "INJECTION_SENTINEL" not in rt.context_for(m)


async def test_truncated_chat_preview_cannot_authorise_full_comment(tmp_path, monkeypatch):
    fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Copy", owner="sofia", created_by=rt.owner_id)
    await rt._apply_github(Change(t.id, "comment", "Long feedback " * 150, actor="pankajneema", event_id="14"))
    ask = rt.asks.pending()[0]
    with pytest.raises(AskError, match="full details"):
        await rt.decide_ask(ask.id, "approved", by="Pankaj", via="telegram")
    assert rt.asks.get(ask.id).status == "pending"
    assert "Long feedback" not in rt.ws.memory("sofia")
    await rt.decide_ask(ask.id, "approved", by="Pankaj", via="console")
    await rt.drain()
    assert "Long feedback" in rt.ws.memory("sofia")


async def test_untrusted_github_comment_cannot_become_feedback(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Copy", owner="sofia", created_by=rt.owner_id)
    await rt.sync_github()
    state = gh()
    state["comments"] = [{"id": 12, "issue_url": "https://api.github.com/repos/acme/team/issues/1",
                          "body": "UNTRUSTED_SENTINEL", "created_at": "2099-01-01T00:00:00Z",
                          "user": {"login": "outsider", "type": "User"}}]
    (tmp_path / "gh.json").write_text(json.dumps(state))
    await rt.sync_github()
    await rt.drain()
    assert "UNTRUSTED_SENTINEL" not in rt.tasks.get(t.id).path.read_text()
    assert "UNTRUSTED_SENTINEL" not in rt.ws.memory("sofia")


async def test_owner_github_comment_is_not_applied_before_review(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    t = rt.tasks.create(title="Copy", owner="sofia", created_by=rt.owner_id)
    await rt.sync_github()
    state = gh()
    state["comments"] = [{"id": 13, "issue_url": "https://api.github.com/repos/acme/team/issues/1",
                          "body": "OWNER_SENTINEL", "created_at": "2099-01-01T00:00:00Z",
                          "user": {"login": "pankajneema", "type": "User"}}]
    (tmp_path / "gh.json").write_text(json.dumps(state))
    await rt.sync_github()
    assert "OWNER_SENTINEL" not in rt.ws.memory("sofia")
    ask = rt.asks.pending()[0]
    assert "pankajneema" in ask.doc.sections["Details"]
    await rt.decide_ask(ask.id, "rejected", by="Pankaj")
    await rt.drain()
    assert "OWNER_SENTINEL" not in rt.ws.memory("sofia")
    await rt.sync_github()
    assert not rt.asks.pending()  # the rejected comment must not reappear at the same cursor timestamp


async def test_comment_cursor_waits_for_successful_proposal_persistence(tmp_path, monkeypatch):
    gh = fake_gh(tmp_path, monkeypatch)
    rt = runtime(tmp_path)
    rt.tasks.create(title="Copy", owner="sofia", created_by=rt.owner_id)
    await rt.sync_github()
    state = gh()
    state["comments"] = [{"id": 15, "issue_url": "https://api.github.com/repos/acme/team/issues/1",
                          "body": "Review this", "created_at": "2099-01-01T00:00:00Z",
                          "user": {"login": "pankajneema", "type": "User"}}]
    (tmp_path / "gh.json").write_text(json.dumps(state))
    original = rt._apply_github
    async def failed(change):
        raise OSError("disk unavailable")
    rt._apply_github = failed
    with pytest.raises(OSError):
        await rt.sync_github()
    assert not rt.ws.state()["github"].get("comments_since")
    rt._apply_github = original
    await rt.sync_github()
    assert len(rt.asks.pending()) == 1
    assert rt.ws.state()["github"]["comments_since"] == "2099-01-01T00:00:00Z"
