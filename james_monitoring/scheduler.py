"""Time-based work that must happen whether or not Telegram is connected:
daily report, hourly checks, and work sessions. Runs inside the app's event loop."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .schedule_history import ScheduleHistory
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
        self.history = ScheduleHistory(rt.ws.root)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._first = True
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _mark(self, key: str, value: str) -> None:
        self.rt.ws.update_state(lambda s: s.setdefault("sched", {}).__setitem__(key, value))

    async def run_once(self, at: datetime | None = None) -> list[str]:
        cfg = self.rt.cfg
        at = at or now(cfg.timezone)
        self.history.recover(at.isoformat(timespec="seconds"))
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
                if job.startswith("work:") or job == "github":
                    # Claims are acquired inside the coroutine: cancellation before its first instruction
                    # cannot leak an OS lock. A slow handler never blocks the other schedule families.
                    self.rt._spawn(self._run_job(job, at, require_due=True))
                    done.append(job)  # submitted, not a completion receipt
                elif await self._run_job(job, at, require_due=True):
                    done.append(job)
            except Exception:  # noqa: BLE001 - one failing job must never stop the others
                log.exception("scheduled job %s failed", job)
        return done

    async def _run_job(self, job: str, at: datetime, require_due: bool = False) -> bool:
        stamp = at.isoformat(timespec="seconds")
        slot = at.date().isoformat() if job in ("report", "brief") or job.startswith("work:") else stamp
        with self.history.guard(job) as acquired:
            if not acquired:
                return False
            if require_due:
                # Another process may have settled this family since run_once took its snapshot.
                try:
                    if job not in due_jobs(self.rt.cfg, self.rt.ws.state(), at):
                        return False
                except (ValueError, TypeError):
                    if job != "checks":
                        return False
            record = self.history.claim(job, slot, stamp)
            if not record:
                return False
            try:
                self._mark(job, slot)
                if job.startswith("work:") and self.rt.paused("all"):
                    record.update(status="skipped", reason="team_paused")
                else:
                    record.update(status="running", started_at=now(self.rt.cfg.timezone).isoformat(timespec="seconds"))
                    self.history.save(record)
                    if job == "report":
                        await self.rt.run_daily_report()
                    elif job.startswith("work:"):
                        digest = await self.rt.run_work_session()
                        if digest and self.on_digest:
                            await self.on_digest(digest)
                    elif job == "brief":
                        await self.rt.run_brief()
                    elif job == "reminders":
                        await self.rt.run_reminders(at)
                    elif job == "github":
                        await self.rt.sync_github()
                    elif job == "checks":
                        await self.rt.run_checks()
                    else:
                        raise ValueError("Unknown scheduled job")
                    record["status"] = "completed"
            except asyncio.CancelledError:
                record.update(status="interrupted", error_type="CancelledError")
                raise
            except Exception as exc:  # partial effects are possible: only an owner may unblock this family
                record.update(status="failed", error_type=type(exc).__name__)
                log.error("scheduled job %s failed (%s); owner acknowledgment required", job, type(exc).__name__)
            finally:
                record["finished_at"] = now(self.rt.cfg.timezone).isoformat(timespec="seconds")
                self.history.save(record)
            return record["status"] in ("completed", "skipped")

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
