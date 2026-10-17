"""Lifecycle checks use synthetic workers only, never live providers."""
import asyncio
import threading
import sys

import pytest

from james_monitoring.server import App
from james_monitoring.scheduler import Scheduler


def test_close_cancels_and_awaits_background_work_and_is_idempotent(tmp_path):
    app = App(tmp_path, telegram=False)
    started, finished = threading.Event(), threading.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    app.submit(worker(), timeout=None)
    assert started.wait(5)
    app.close(timeout=5)
    app.close(timeout=5)
    assert finished.is_set()
    assert not app.thread.is_alive()
    assert app.loop.is_closed()
    with pytest.raises(RuntimeError, match="shutting down"):
        app.submit(asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="shutting down"):
        app.load()


async def test_scheduler_stop_waits_for_cancellation_cleanup(rt):
    scheduler = Scheduler(rt)
    started, finished = asyncio.Event(), asyncio.Event()

    async def loop():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    scheduler._loop = loop
    scheduler.start()
    await started.wait()
    await scheduler.stop()
    assert finished.is_set()
    assert scheduler._task is None


async def test_cancelling_coding_run_reaps_subprocess(tmp_path, cfg, monkeypatch):
    from james_monitoring.executor import Executor
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg.projects["site"].repo = str(repo)
    cfg.executor_command = [sys.executable, "-c", "import time; time.sleep(60)"]
    executor = Executor(cfg)
    monkeypatch.setattr(executor, "_worktree", lambda *args: repo)
    spawn = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def capture(*args, **kw):
        proc = await spawn(*args, **kw)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(executor.run(project_id="site", task_id="T-001", prompt="synthetic"))
    try:
        await asyncio.wait_for(started.wait(), 5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert processes[0].returncode is not None
    assert processes[0].returncode != 0


def test_close_reports_blocking_worker_and_can_be_joined_later(tmp_path):
    app = App(tmp_path, telegram=False)
    started, release = threading.Event(), threading.Event()

    def blocking():
        started.set()
        release.wait(10)

    app.submit(asyncio.to_thread(blocking), timeout=None)
    assert started.wait(5)
    try:
        with pytest.raises(TimeoutError, match="still waiting"):
            app.close(timeout=0.01)
        assert app.thread.is_alive()
    finally:
        release.set()
        app.close(timeout=5)
    assert app.loop.is_closed()


def test_failed_console_startup_closes_loop(tmp_path, monkeypatch):
    from james_monitoring import server
    apps = []

    def bad_config(self):
        apps.append(self)
        raise ValueError("synthetic invalid config")

    monkeypatch.setattr(App, "load", bad_config)
    with pytest.raises(ValueError, match="invalid config"):
        server.serve(tmp_path, open_browser=False, telegram=False)
    assert apps[0].loop.is_closed()
    assert not apps[0].thread.is_alive()
