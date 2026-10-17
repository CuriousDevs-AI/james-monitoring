"""Synthetic scheduled effects: restart, crash recovery, locks, cancellation and owner review."""
import asyncio
import subprocess
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from james_monitoring.schedule_history import ScheduleHistory
from james_monitoring.scheduler import Scheduler
from .test_workforce import app, http

AT = datetime(2026, 10, 19, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


async def test_successful_receipt_survives_restart_and_state_loss(rt, monkeypatch):
    effect = AsyncMock()
    monkeypatch.setattr(rt, "run_daily_report", effect)
    scheduler = Scheduler(rt)
    assert await scheduler._run_job("report", AT)
    record = scheduler.history.all()[0]
    assert record["status"] == "completed" and record["started_at"] and record["finished_at"]
    rt.ws.update_state(lambda state: state.pop("sched"))
    assert not await Scheduler(rt)._run_job("report", AT)
    effect.assert_awaited_once()


async def test_partial_failure_holds_future_runs_until_evidence(rt, monkeypatch, caplog):
    effect = AsyncMock(side_effect=TimeoutError("do-not-log-provider-secret"))
    monkeypatch.setattr(rt, "run_checks", effect)
    scheduler = Scheduler(rt)
    assert not await scheduler._run_job("checks", AT)
    record = scheduler.history.all()[0]
    assert record["status"] == "failed" and record["error_type"] == "TimeoutError"
    assert "do-not-log-provider-secret" not in str(record) + caplog.text
    assert not await Scheduler(rt)._run_job("checks", AT + timedelta(hours=1))
    effect.assert_awaited_once()
    with pytest.raises(ValueError, match="record what you checked"):
        scheduler.history.acknowledge(record["id"], "owner", "", AT.isoformat())
    scheduler.history.acknowledge(record["id"], "owner", "Verified effects and repaired the destination", AT.isoformat())
    effect.side_effect = None
    assert not await scheduler._run_job("checks", AT)  # same occurrence is never replayed
    assert await scheduler._run_job("checks", AT + timedelta(hours=2))
    assert effect.await_count == 2
    assert scheduler.history.all()[-1]["status"] == "failed"  # acknowledgment is not success


async def test_live_receipt_is_not_recovered_but_orphan_is(rt):
    history = ScheduleHistory(rt.ws.root)
    with history.guard("report") as acquired:
        assert acquired
        record = history.claim("report", AT.date().isoformat(), AT.isoformat())
        history.recover(AT.isoformat())
        assert history.all()[0]["status"] == "scheduled"
    history.recover(AT.isoformat())
    assert history.all()[0]["status"] == "interrupted"
    assert not await Scheduler(rt)._run_job("report", AT + timedelta(days=1))
    assert history.all()[0]["id"] == record["id"]


async def test_independent_schedulers_do_not_overlap_work_slots(rt, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()
        return "synthetic digest"

    monkeypatch.setattr(rt, "run_work_session", work)
    first, second = Scheduler(rt), Scheduler(rt)
    task = asyncio.create_task(first._run_job("work:10:00", AT))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not await second._run_job("work:15:00", AT + timedelta(hours=5))
        second.history.recover(AT.isoformat())
        assert first.history.all()[0]["status"] == "running"
    finally:
        release.set()
        await task
    assert first.history.all()[0]["status"] == "completed"


async def test_cancellation_is_interrupted_and_not_replayed(rt, monkeypatch):
    entered = asyncio.Event()

    async def work():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(rt, "run_work_session", work)
    scheduler = Scheduler(rt)
    task = asyncio.create_task(scheduler._run_job("work:10:00", AT))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler.history.all()[0]["status"] == "interrupted"
    assert not await Scheduler(rt)._run_job("work:15:00", AT)


async def test_background_submission_is_not_reported_as_completion(rt, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()
        return "digest"

    monkeypatch.setattr(rt, "run_work_session", work)
    monkeypatch.setattr("james_monitoring.scheduler.due_jobs", lambda *args: ["work:10:00", "checks"])
    monkeypatch.setattr(rt, "run_checks", AsyncMock())
    scheduler = Scheduler(rt, on_digest=AsyncMock())
    try:
        assert await scheduler.run_once(AT) == ["work:10:00", "checks"]
        await asyncio.wait_for(entered.wait(), 5)
        records = {r["job"]: r for r in scheduler.history.all()}
        assert records["work:10:00"]["status"] == "running"
        assert records["checks"]["status"] == "completed"
    finally:
        release.set()
        await rt.drain()
    scheduler.on_digest.assert_awaited_once_with("digest")
    assert all(r["status"] == "completed" for r in scheduler.history.all())


async def test_paused_slot_is_skipped_not_completed(rt, monkeypatch):
    effect = AsyncMock()
    monkeypatch.setattr(rt, "run_work_session", effect)
    rt.set_paused("all", True)
    scheduler = Scheduler(rt)
    assert await scheduler._run_job("work:10:00", AT)
    assert scheduler.history.all()[0]["status"] == "skipped"
    assert scheduler.history.all()[0]["reason"] == "team_paused"
    rt.set_paused("all", False)
    assert not await scheduler._run_job("work:10:00", AT)
    effect.assert_not_awaited()


async def test_corrupt_receipts_stop_scheduling(rt, monkeypatch):
    scheduler = Scheduler(rt)
    scheduler.history.directory.mkdir(parents=True)
    (scheduler.history.directory / ("a" * 64 + ".json")).write_text("not json")
    effect = AsyncMock()
    monkeypatch.setattr(rt, "run_checks", effect)
    with pytest.raises(ValueError):
        await scheduler.run_once(AT)
    effect.assert_not_awaited()


def test_run_history_and_acknowledgment_are_owner_only(app, http):
    history = ScheduleHistory(app.rt.ws.root)
    with history.guard("report"):
        record = history.claim("report", AT.date().isoformat(), AT.isoformat())
    history.recover(AT.isoformat())
    admin = http("/api/users", {"action": "add", "name": "Admin", "role": "admin"})[1]["key"]
    assert http("/api/schedule/runs", key=admin)[0] == 403
    body = {"id": record["id"], "evidence": "Verified destination; future reports may proceed"}
    assert http("/api/schedule/acknowledge", body, key=admin)[0] == 403
    assert http("/api/schedule/runs")[1]["needs_review"][0]["id"] == record["id"]
    assert http("/api/schedule/acknowledge", {**body, "evidence": ""})[0] == 400
    result = http("/api/schedule/acknowledge", body)
    assert result[0] == 200 and not result[1]["retried"]
    assert http("/api/schedule/runs")[1]["needs_review"] == []
    assert history.all()[0]["status"] == "interrupted"


async def test_start_is_idempotent(rt):
    scheduler = Scheduler(rt)
    scheduler.start()
    first = scheduler._task
    scheduler.start()
    assert scheduler._task is first
    await scheduler.stop()
    assert first.done()


async def test_stale_due_snapshot_is_rechecked_inside_family_lock(rt, monkeypatch):
    effect = AsyncMock()
    monkeypatch.setattr(rt, "run_checks", effect)
    first, second = Scheduler(rt), Scheduler(rt)
    assert await first._run_job("checks", AT, require_due=True)
    assert not await second._run_job("checks", AT + timedelta(seconds=1), require_due=True)
    effect.assert_awaited_once()


async def test_crash_between_effect_and_completion_stays_held(rt, monkeypatch):
    scheduler = Scheduler(rt)
    effect = AsyncMock()
    monkeypatch.setattr(rt, "run_daily_report", effect)
    save = scheduler.history.save

    def disk_failure(record):
        if record["status"] == "completed":
            raise OSError("synthetic disk failure")
        save(record)

    monkeypatch.setattr(scheduler.history, "save", disk_failure)
    with pytest.raises(OSError):
        await scheduler._run_job("report", AT)
    restarted = Scheduler(rt)
    restarted.history.recover(AT.isoformat())
    assert restarted.history.all()[0]["status"] == "interrupted"
    assert not await restarted._run_job("report", AT + timedelta(days=1))
    effect.assert_awaited_once()


def test_family_lock_excludes_other_process(rt):
    code = """
import sys
from pathlib import Path
from james_monitoring.schedule_history import ScheduleHistory
with ScheduleHistory(Path(sys.argv[1])).guard('work:15:00') as acquired:
    sys.exit(2 if acquired else 0)
"""
    with ScheduleHistory(rt.ws.root).guard("work:10:00"):
        result = subprocess.run([sys.executable, "-c", code, str(rt.ws.root)], timeout=10)
    assert result.returncode == 0
