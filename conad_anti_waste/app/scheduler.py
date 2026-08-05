"""Job scheduling with catch-up.

Plain APScheduler would silently skip a job whose moment passed while the process
was down. On a WSL laptop that sleeps every night, "silently skipped" is the
normal case, so the daily prompt would simply never fire and the product would
appear broken.

Every job therefore records its `last_run` in the database, and on startup any
job whose previous scheduled occurrence is newer than its last run is executed
once, immediately. Catch-up runs once, not once per missed occurrence — waking
up to four days of backlog prompts would be worse than the gap itself.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import db
from app.config import get_settings

log = logging.getLogger(__name__)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hhmm(value: str, default=(20, 30)) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return default


def _record_run(job_name: str, when: Optional[datetime] = None) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO job_runs(job_name, last_run) VALUES(?, ?) "
            "ON CONFLICT(job_name) DO UPDATE SET last_run = excluded.last_run",
            (job_name, (when or datetime.now()).isoformat(timespec="seconds")),
        )


def _last_run(job_name: str) -> Optional[datetime]:
    with db.session() as conn:
        row = conn.execute("SELECT last_run FROM job_runs WHERE job_name = ?",
                           (job_name,)).fetchone()
    if not row or not row["last_run"]:
        return None
    try:
        return datetime.fromisoformat(row["last_run"])
    except ValueError:
        return None


def _previous_daily(hour: int, minute: int, now: datetime) -> datetime:
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today <= now else today - timedelta(days=1)


def _previous_weekly(weekday: int, hour: int, minute: int, now: datetime) -> datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (now.weekday() - weekday) % 7
    candidate -= timedelta(days=delta)
    return candidate if candidate <= now else candidate - timedelta(days=7)


class Scheduler:
    """Wraps APScheduler and adds the catch-up behaviour."""

    def __init__(self):
        s = get_settings()
        self.tz = ZoneInfo(s.tz)
        self.settings = s
        self.sched = AsyncIOScheduler(timezone=self.tz)
        self._catchup: list[tuple[str, Callable[[], Awaitable], datetime]] = []

    def _wrap(self, job_name: str, fn: Callable[[], Awaitable]):
        async def runner():
            try:
                await fn()
                _record_run(job_name)
                log.info("job %s completed", job_name)
            except Exception:  # noqa: BLE001 - a failing job must never kill the loop
                log.exception("job %s failed", job_name)
        return runner

    def add_daily(self, name: str, hhmm: str, fn: Callable[[], Awaitable]) -> None:
        hour, minute = _parse_hhmm(hhmm)
        self.sched.add_job(self._wrap(name, fn), CronTrigger(hour=hour, minute=minute),
                           id=name, replace_existing=True)
        self._catchup.append((name, self._wrap(name, fn),
                              _previous_daily(hour, minute, datetime.now(self.tz).replace(tzinfo=None))))

    def add_weekly(self, name: str, day: str, hhmm: str, fn: Callable[[], Awaitable]) -> None:
        hour, minute = _parse_hhmm(hhmm, default=(18, 0))
        weekday = WEEKDAYS.get(day.lower()[:3], 6)
        self.sched.add_job(self._wrap(name, fn),
                           CronTrigger(day_of_week=day.lower()[:3], hour=hour, minute=minute),
                           id=name, replace_existing=True)
        self._catchup.append((name, self._wrap(name, fn),
                              _previous_weekly(weekday, hour, minute,
                                               datetime.now(self.tz).replace(tzinfo=None))))

    def add_interval(self, name: str, hours: int, fn: Callable[[], Awaitable]) -> None:
        self.sched.add_job(self._wrap(name, fn), IntervalTrigger(hours=hours),
                           id=name, replace_existing=True)
        self._catchup.append((name, self._wrap(name, fn),
                              datetime.now().replace(tzinfo=None) - timedelta(hours=hours)))

    async def run_catchup(self) -> list[str]:
        """Run every job whose scheduled moment passed while we were down."""
        ran = []
        for name, runner, due_at in self._catchup:
            last = _last_run(name)
            if last is not None and last >= due_at:
                continue
            if last is None:
                # First ever start: record the position, do not fire a burst of
                # prompts at someone who has just installed the thing.
                _record_run(name)
                continue
            log.info("catch-up: %s was due %s, last ran %s", name, due_at, last)
            await runner()
            ran.append(name)
        return ran

    def start(self) -> None:
        self.sched.start()
        for job in self.sched.get_jobs():
            log.info("scheduled %s -> next run %s", job.id, job.next_run_time)

    def shutdown(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)
