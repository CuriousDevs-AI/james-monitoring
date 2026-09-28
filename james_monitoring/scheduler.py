"""Time-based work that must happen whether or not Telegram is connected:
daily report, hourly checks, and work sessions. Runs inside the app's event loop."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .util import now, parse_hhmm

log = logging.getLogger("jm.scheduler")


def due_jobs(cfg, state: dict, at: datetime) -> list[str]:
    """Which jobs should run at `at` (timezone-aware, in cfg.timezone). Pure, so it's easy to test."""
    jobs = []
    today = at.date().isoformat()
    ran = state.get("sched", {})
    rep = parse_hhmm(cfg.daily_report)
    if at.time() >= rep and ran.get("report") != today:
        jobs.append("report")
    if at.isoweekday() <= 6:                                   # Mon–Sat
        for hhmm in cfg.work_sessions:
            if at.time() >= parse_hhmm(hhmm) and ran.get(f"work:{hhmm}") != today:
                jobs.append(f"work:{hhmm}")
    last = ran.get("checks")
    if not last or (at - datetime.fromisoformat(last)).total_seconds() >= cfg.check_every_minutes * 60:
        jobs.append("checks")
    if getattr(cfg, "assistants", None):
        if at.time() >= parse_hhmm(cfg.daily_brief) and ran.get("brief") != today:
            jobs.append("brief")
    if state.get("reminders"):
        from .assistant import due_in
        if due_in(state, at):                                  # only when one is actually due
            jobs.append("reminders")
    gh = getattr(cfg, "github", None)
    if gh is not None and gh.enabled:
        last = ran.get("github")
        if not last or (at - datetime.fromisoformat(last)).total_seconds() >= gh.sync_minutes * 60:
            jobs.append("github")
    return jobs


class Scheduler:
    def __init__(self, rt, on_digest=None, tick_seconds: int = 30):
        self.rt = rt
        self.on_digest = on_digest            # async fn(text) for work-session digests
        self.tick = tick_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._first = True
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _mark(self, key: str, value: str) -> None:
        self.rt.ws.update_state(lambda s: s.setdefault("sched", {}).__setitem__(key, value))

    async def run_once(self, at: datetime | None = None) -> list[str]:
        cfg = self.rt.cfg
        at = at or now(cfg.timezone)
        state = self.rt.ws.state()
        if self._first_start_catchup(state, at):
            return []
        done = []
        try:
            jobs = due_jobs(cfg, state, at)
        except (ValueError, TypeError):
            log.exception("schedule times are invalid — only the hourly checks run")
            jobs = ["checks"]
        for job in jobs:
            try:
                if job == "report":
                    self._mark("report", at.date().isoformat())
                    await self.rt.run_daily_report()
                elif job.startswith("work:"):
                    self._mark(job, at.date().isoformat())
                    if not self.rt.paused("all"):
                        # In the background: a long session never holds up checks, the report or the next tick.
                        async def session():
                            try:
                                digest = await self.rt.run_work_session()
                                if digest and self.on_digest:
                                    await self.on_digest(digest)
                            except Exception:  # noqa: BLE001
                                log.exception("work session failed")
                        self.rt._spawn(session())
                elif job == "brief":
                    self._mark("brief", at.date().isoformat())
                    await self.rt.run_brief()
                elif job == "reminders":
                    await self.rt.run_reminders(at)
                elif job == "github":
                    self._mark("github", at.isoformat(timespec="seconds"))
                    self.rt._spawn(self.rt.sync_github())
                elif job == "checks":
                    self._mark("checks", at.isoformat(timespec="seconds"))
                    await self.rt.run_checks()
                done.append(job)
            except Exception:  # noqa: BLE001 - one failing job must never stop the others
                log.exception("scheduled job %s failed", job)
        return done

    def _first_start_catchup(self, state: dict, at: datetime) -> bool:
        """On the very first start, don't fire today's already-passed report/work sessions all at once."""
        if getattr(self, "_first", False):
            self._first = False
            if not state.get("sched"):
                today = at.date().isoformat()
                self._mark("report", today if at.time() >= parse_hhmm(self.rt.cfg.daily_report) else "")
                if at.time() >= parse_hhmm(self.rt.cfg.daily_brief):
                    self._mark("brief", today)
                for hhmm in self.rt.cfg.work_sessions:
                    if at.time() >= parse_hhmm(hhmm):
                        self._mark(f"work:{hhmm}", today)
                self._mark("checks", at.isoformat(timespec="seconds"))
                return True
        return False

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.tick)
