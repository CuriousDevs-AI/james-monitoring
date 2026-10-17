import pytest

from james_monitoring.asks import AskError
from james_monitoring.runtime import Event
from james_monitoring.tasks import TaskError


def task(rt, title, **fields):
    return rt.tasks.create(title=title, owner="sofia", created_by=rt.owner_id,
                           done_means=["verified output"], **fields)


def test_dependencies_validated_on_create_and_edit(rt):
    a = task(rt, "Research")
    with pytest.raises(TaskError, match="depend"):
        task(rt, "Bad reference", depends_on=["T-999"])
    b = task(rt, "Validate", depends_on=[a.id.lower(), a.id])
    assert b.doc.meta["depends_on"] == [a.id]
    c = task(rt, "Draft", depends_on=[b.id])
    for deps in ([a.id], [c.id]):
        with pytest.raises(TaskError, match="depend"):
            rt.tasks.update_fields(a.id, rt.owner_id, depends_on=deps)
    assert rt.tasks.get(a.id).doc.meta["depends_on"] == []


@pytest.mark.parametrize("status", ["todo", "review", "cut"])
@pytest.mark.parametrize("target", ["doing", "review", "done"])
def test_only_completed_dependencies_allow_progress(rt, status, target):
    a = task(rt, "Research")
    b = task(rt, "Validate", depends_on=[a.id])
    rt.tasks.set_status(a.id, status, by=rt.owner_id)
    with pytest.raises(TaskError, match="depend"):
        rt.tasks.set_status(b.id, target, by=rt.owner_id)
    assert rt.tasks.get(b.id).status == "todo"
    # The founder can explicitly remove an obsolete prerequisite, not silently bypass it.
    rt.tasks.update_fields(b.id, rt.owner_id, depends_on=[])
    rt.tasks.set_status(b.id, target, by=rt.owner_id)


def test_scheduler_waits_until_dependencies_are_accepted(rt):
    a = rt.tasks.create(title="Research", owner="marcus", created_by=rt.owner_id)
    b = task(rt, "Validate", depends_on=[a.id])
    sofia = rt.cfg.member("sofia")
    assert rt.next_work(sofia) is None
    rt.tasks.set_status(a.id, "review", by="marcus")
    assert rt.next_work(sofia) is None
    rt.tasks.set_status(a.id, "done", by=rt.owner_id)
    assert rt.next_work(sofia)[1].id == b.id
    # Reopening a prerequisite must stop already-started dependent work too.
    rt.tasks.set_status(b.id, "doing", by="sofia")
    rt.tasks.set_status(a.id, "todo", by=rt.owner_id)
    assert rt.next_work(sofia) is None


def test_missing_dependency_from_disk_fails_closed(rt):
    a = task(rt, "Research")
    b = task(rt, "Validate", depends_on=[a.id])
    a.path.unlink()
    assert rt.next_work(rt.cfg.member("sofia")) is None
    with pytest.raises(TaskError, match="depend"):
        rt.tasks.set_status(b.id, "doing", by="sofia")


def test_unblock_requires_all_explicit_and_text_dependencies(rt):
    a, b = task(rt, "Research"), task(rt, "Source access")
    c = task(rt, "Validate", depends_on=[a.id, b.id])
    d = task(rt, "Legacy task")
    rt.tasks.set_status(c.id, "blocked", by=rt.owner_id, blocked_on=a.id)
    rt.tasks.set_status(d.id, "blocked", by=rt.owner_id, blocked_on=f"{a.id} and {b.id}")
    assert rt.tasks.unblock_dependents(a.id, rt.owner_id) == []
    rt.tasks.set_status(a.id, "done", by=rt.owner_id)
    assert rt.tasks.unblock_dependents(a.id, rt.owner_id) == []
    rt.tasks.set_status(b.id, "done", by=rt.owner_id)
    assert {t.id for t in rt.tasks.unblock_dependents(b.id, rt.owner_id)} == {c.id, d.id}


@pytest.mark.parametrize("kind", ["write_file", "run_code"])
async def test_actions_recheck_dependencies_even_after_approval(rt, kind):
    a = task(rt, "Research")
    b = task(rt, "Validate", depends_on=[a.id], project="site")
    action = {"type": kind, "task": b.id, "project": "site",
              "path": "docs/validation.md", "content": "Must not write", "instructions": "Do work"}
    with pytest.raises(TaskError, match="depend"):
        await rt._do(rt.cfg.member("sofia"), action,
                     Event("system", "approved", sender="system", meta={"approved": "A-001"}))
    assert not (rt.ws.root / "docs/validation.md").exists()
    assert b.id not in rt._coding


async def test_event_task_cannot_bypass_dependency_guard(rt):
    a = task(rt, "Research")
    b = task(rt, "Validate", depends_on=[a.id])
    with pytest.raises(TaskError, match="depend"):
        await rt._do(rt.cfg.member("sofia"), {"type": "write_file", "path": "docs/no.md", "content": "no"},
                     Event("system", "work", sender="system", task=b.id))
    assert not (rt.ws.root / "docs/no.md").exists()


async def test_action_approval_retains_task_and_rechecks_without_settling_request(rt):
    a = task(rt, "Research")
    b = task(rt, "Validate", depends_on=[a.id])
    sofia = rt.cfg.member("sofia")
    sofia.permissions["write_file"] = "red"
    await rt._apply(sofia, {"type": "write_file", "path": "docs/validation.md", "content": "Validated"},
                    Event("system", "work", sender="system", task=b.id))
    ask = rt.asks.pending()[0]
    assert ask.doc.meta["task"] == b.id
    with pytest.raises(AskError, match="depend"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)
    assert rt.asks.get(ask.id).status == "pending"
    assert not (rt.ws.root / "docs/validation.md").exists()
    rt.tasks.set_status(a.id, "done", by=rt.owner_id)
    await rt.decide_ask(ask.id, "approved", rt.owner_id)
    await rt.drain()
    assert rt.asks.get(ask.id).status == "approved"
    assert (rt.ws.root / "docs/validation.md").read_text() == "Validated\n"


async def test_unrelated_one_off_work_still_runs(rt):
    a = task(rt, "Research")
    task(rt, "Waiting work", depends_on=[a.id])
    await rt._do(rt.cfg.member("sofia"), {"type": "write_file", "path": "docs/one-off.md", "content": "Ready"},
                 Event("dm", "a separate request", sender=rt.owner_id))
    assert (rt.ws.root / "docs/one-off.md").read_text() == "Ready\n"


async def test_merge_dependency_check_happens_before_external_effect(rt, monkeypatch):
    a = task(rt, "Research")
    b = task(rt, "Validate", depends_on=[a.id])
    ask = rt.asks.create(requester="sofia", summary="Merge", kind="merge", task=b.id,
                         payload={"project": "site", "branch": "jm/test"})
    calls = []
    monkeypatch.setattr(rt.executor, "merge", lambda *args: calls.append(args))
    with pytest.raises(AskError, match="depend"):
        await rt.decide_ask(ask.id, "approved", rt.owner_id)
    assert not calls
    assert rt.asks.get(ask.id).status == "pending"


async def test_completed_projects_get_no_daily_work_or_manager_round(rt):
    a = task(rt, "Old work", project="site")
    rt.cfg.projects["site"].status = "done"
    assert rt.next_work(rt.cfg.member("sofia")) is None
    rt.tasks.set_status(a.id, "blocked", by=rt.owner_id, blocked_on="Marcus — provide input")
    assert rt.next_work(rt.cfg.member("marcus")) is None
    assert await rt.run_work_session() == ""
    assert not rt.llm.calls
