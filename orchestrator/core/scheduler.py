"""Scheduled prompts: APScheduler (AsyncIOScheduler + SQLite job store) firing
saved prompts into the SessionManager, with run history in our own DB."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QTimer, Signal

from orchestrator.core import model_registry
from orchestrator.core.session_manager import POLICIES, SessionManager
from orchestrator.storage.db import Database, Schedule, app_dir
from orchestrator.ui_text import short_title

JOB_FUNC = "orchestrator.core.scheduler:fire"
_RUNNER: "ScheduleRunner | None" = None

CRON_PRESETS: list[tuple[str, str]] = [
    ("Every minute", "* * * * *"),
    ("Every 15 minutes", "*/15 * * * *"),
    ("Every hour", "0 * * * *"),
    ("Every day at 08:00", "0 8 * * *"),
    ("Weekdays at 08:00", "0 8 * * mon-fri"),
    ("Mondays at 09:00", "0 9 * * mon"),
]
DAYS = {"mon-fri": "Weekdays", "sat,sun": "Weekends", "mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays",
        "thu": "Thursdays", "fri": "Fridays", "sat": "Saturdays", "sun": "Sundays", "*": "Every day"}


async def fire(schedule_id: int) -> None:
    """Job entry point (stored by textual reference in the job store)."""
    if _RUNNER is not None:
        await _RUNNER.fire(schedule_id)


def make_trigger(kind: str, cron: str | None, run_at: float | None):
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    if kind == "once":
        if not run_at:
            raise ValueError("Pick a date and time")
        return DateTrigger(run_date=datetime.fromtimestamp(run_at).astimezone())
    if not cron or len(cron.split()) != 5:
        raise ValueError("Cron needs 5 fields: minute hour day month weekday")
    return CronTrigger.from_crontab(cron)


def next_fire(kind: str, cron: str | None, run_at: float | None, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc).astimezone()
    trig = make_trigger(kind, cron, run_at)
    return trig.get_next_fire_time(None, now)


def describe(s: Schedule | dict) -> str:
    g = (lambda k: getattr(s, k)) if not isinstance(s, dict) else s.get
    if g("kind") == "once":
        ts = g("run_at")
        return "Once · " + (datetime.fromtimestamp(ts).strftime("%b %d, %H:%M") if ts else "no time set")
    cron = (g("cron") or "").strip()
    for label, expr in CRON_PRESETS:
        if cron == expr:
            return label
    parts = cron.split()
    if len(parts) == 5:
        mi, hr, dom, mon, dow = parts
        if mi.startswith("*/") and hr == dom == mon == dow == "*":
            return f"Every {mi[2:]} minutes"
        if mi.isdigit() and hr.isdigit() and dom == mon == "*" and dow in DAYS:
            return f"{DAYS[dow]} at {int(hr):02d}:{int(mi):02d}"
    return f"Cron  {cron}"


def quick_times(now: datetime | None = None) -> list[tuple[str, datetime]]:
    """Time chips for 'Run once': In 15 min, In 1 hour, Tonight 9 PM (if still ahead), Tomorrow 8 AM."""
    from datetime import timedelta

    now = (now or datetime.now()).replace(second=0, microsecond=0)
    out = [("In 15 min", now + timedelta(minutes=15)), ("In 1 hour", now + timedelta(hours=1))]
    tonight = now.replace(hour=21, minute=0)
    if tonight - now >= timedelta(minutes=30):
        out.append(("Tonight 9 PM", tonight))
    out.append(("Tomorrow 8 AM", (now + timedelta(days=1)).replace(hour=8, minute=0)))
    return out


def fmt_when(dt: datetime | float | None, now: float | None = None) -> str:
    if dt is None:
        return "—"
    ts = dt.timestamp() if isinstance(dt, datetime) else float(dt)
    now = now or time.time()
    d = datetime.fromtimestamp(ts)
    same_day = d.date() == datetime.fromtimestamp(now).date()
    delta = ts - now
    if 0 <= delta < 3600:
        rel = f"in {max(1, int(delta // 60))} min"
    elif -3600 < delta < 0:
        rel = f"{max(1, int(-delta // 60))} min ago"
    else:
        rel = ""
    base = d.strftime("%H:%M") if same_day else d.strftime("%a %b %d, %H:%M")
    return f"{base} ({rel})" if rel else base


class ScheduleRunner(QObject):
    changed = Signal()
    run_started = Signal(int, int)  # schedule id, session id
    run_finished = Signal(int, int, str)  # schedule id, session id, status
    run_blocked = Signal(int, str)  # schedule id, why (e.g. its model is disabled)

    def __init__(self, db: Database, mgr: SessionManager, jobs_url: str | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.mgr = mgr
        self.jobs_url = jobs_url or f"sqlite:///{(app_dir() / 'jobs.db').as_posix()}"
        self.scheduler = None
        self._active: dict[int, tuple[int, int]] = {}  # session id -> (schedule id, run id)
        mgr.status_changed.connect(self._on_status)
        self._heartbeat = QTimer(self)  # nudges APScheduler after sleep/hibernate so missed runs fire on wake
        self._heartbeat.timeout.connect(self._wake)

    # ---- lifecycle
    def start(self) -> None:
        global _RUNNER
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        _RUNNER = self
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # qasync loop set but not running yet (app startup)
            loop = asyncio.get_event_loop()
        self.scheduler = AsyncIOScheduler(
            event_loop=loop,
            jobstores={"default": SQLAlchemyJobStore(url=self.jobs_url)},
            job_defaults={"coalesce": True, "max_instances": 1},
        )
        self.scheduler.start(paused=True)
        self.sync_all()
        if not self.paused:
            self.scheduler.resume()
        self._heartbeat.start(30_000)

    def shutdown(self) -> None:
        global _RUNNER
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        _RUNNER = None

    @property
    def paused(self) -> bool:
        return bool(self.db.get_setting("schedules.paused", False))

    def set_paused(self, paused: bool) -> None:
        self.db.set_setting("schedules.paused", paused)
        if self.scheduler:
            self.scheduler.pause() if paused else self.scheduler.resume()
        self.changed.emit()

    def _wake(self) -> None:
        if self.scheduler and self.scheduler.running and not self.paused:
            self.scheduler.wakeup()

    # ---- jobs
    @staticmethod
    def job_id(sched_id: int) -> str:
        return f"schedule-{sched_id}"

    def sync_all(self) -> None:
        wanted = {self.job_id(s.id) for s in self.db.schedules() if s.enabled}
        for job in self.scheduler.get_jobs():
            if job.id not in wanted:
                job.remove()
        for s in self.db.schedules():
            self.sync(s, replace=False)

    def sync(self, s: Schedule, replace: bool = True) -> None:
        """Create/update the job for a schedule. With replace=False an identical job is
        left alone so a missed run stored in the job store still fires on startup."""
        if not self.scheduler:
            return
        jid = self.job_id(s.id)
        job = self.scheduler.get_job(jid)
        if not s.enabled:
            if job:
                job.remove()
            return
        try:
            trig = make_trigger(s.kind, s.cron, s.run_at)
        except ValueError:
            if job:
                job.remove()
            return
        grace = None if s.missed == "run_once" else 120  # None = run however late (once, coalesced)
        if job and not replace and str(job.trigger) == str(trig) and job.misfire_grace_time == grace:
            return
        if s.kind == "once" and s.run_at and s.run_at < time.time() and s.missed != "run_once":
            if job:
                job.remove()
            return
        self.scheduler.add_job(JOB_FUNC, trig, args=[s.id], id=jid, name=s.name, replace_existing=True,
                               misfire_grace_time=grace, coalesce=True)

    def remove(self, sched_id: int) -> None:
        if self.scheduler and self.scheduler.get_job(self.job_id(sched_id)):
            self.scheduler.remove_job(self.job_id(sched_id))

    def next_run(self, sched_id: int) -> datetime | None:
        job = self.scheduler.get_job(self.job_id(sched_id)) if self.scheduler else None
        return job.next_run_time if job else None

    # ---- running
    async def fire(self, sched_id: int) -> int | None:
        """Run a schedule now. Returns the run id (or None if it could not start)."""
        s = self.db.schedule(sched_id)
        if not s:
            return None
        proj = self.db.project(s.project_id)
        if s.kind == "once":
            self.db.save_schedule({"enabled": False}, s.id)
        if not proj:
            rid = self.db.add_run(s.id, None, "skipped", "project no longer exists")
            self.changed.emit()
            return rid
        sid: int | None
        target = self.db.session(s.session_id) if s.new_or_resume == "resume" and s.session_id else None
        engine, model = (target.engine, target.model or "") if target else (s.engine, s.model or "")
        if not model_registry.is_allowed(self.db, engine, model, strict=not target):
            # Never run a disabled model and never swap in another one: skip and flag it.
            why = "Model disabled: " + model_registry.disabled_message(self.db, engine, model)
            rid = self.db.add_run(s.id, None, "skipped", why)
            self.run_blocked.emit(s.id, why)
            self.changed.emit()
            return rid
        if s.new_or_resume == "resume":
            sid = s.session_id if s.session_id and self.db.session(s.session_id) else None
            if sid is None:
                rid = self.db.add_run(s.id, None, "skipped", "the chat to continue was deleted")
                self.changed.emit()
                return rid
            if self.mgr.is_running(sid):
                rid = self.db.add_run(s.id, sid, "skipped", "that chat was still busy")
                self.changed.emit()
                return rid
        else:
            sess = self.db.create_session(proj.id, s.engine, s.model or "", s.effort or "")
            sid = sess.id
        rid = self.db.add_run(s.id, sid)
        self._active[sid] = (s.id, rid)
        started = self.mgr.send(sid, s.prompt, policy=s.permission if s.permission in POLICIES else "read-only")
        if s.new_or_resume == "new":
            self.db.update_session(sid, title=short_title(f"{s.name} · {datetime.now():%b %d %H:%M}"))
            self.mgr.session_updated.emit(sid)
        if not started:
            self._active.pop(sid, None)
            self.db.finish_run(rid, "skipped", "could not start")
        else:
            self.run_started.emit(s.id, sid)
        self.changed.emit()
        return rid

    def _on_status(self, sid: int, status: str) -> None:
        if sid not in self._active or status not in ("done", "error", "idle"):
            return
        if self.mgr.is_running(sid) and status == "idle":
            return
        sched_id, rid = self._active.pop(sid)
        final = {"done": "done", "error": "error", "idle": "stopped"}[status]
        self.db.finish_run(rid, final)
        self.run_finished.emit(sched_id, sid, final)
        self.changed.emit()

    def active_count(self) -> int:
        return len(self._active)
