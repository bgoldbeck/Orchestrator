import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QObject, Signal

from orchestrator.core import autostart
from orchestrator.core.scheduler import ScheduleRunner, describe, fmt_when, make_trigger, next_fire
from orchestrator.storage.db import Database


class FakeManager(QObject):
    status_changed = Signal(int, str)
    session_updated = Signal(int)

    def __init__(self):
        super().__init__()
        self.sent = []
        self.busy = set()

    def is_running(self, sid):
        return sid in self.busy

    def send(self, sid, text, policy=None):
        self.sent.append((sid, text, policy))
        self.busy.add(sid)
        return True

    def finish(self, sid, status="done"):
        self.busy.discard(sid)
        self.status_changed.emit(sid, status)


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    proj = db.add_project(str(tmp_path), "proj")
    mgr = FakeManager()
    return db, proj, mgr, f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}"


def new_schedule(db, proj, **kw):
    data = dict(name="Daily digest", project_id=proj.id, prompt="Summarize", engine="codex", model="gpt-6.2-sol",
                effort="low", kind="cron", cron="0 8 * * mon-fri", new_or_resume="new", permission="read-only",
                missed="run_once", enabled=True)
    data.update(kw)
    return db.save_schedule(data)


def test_describe_and_triggers():
    assert describe({"kind": "cron", "cron": "0 8 * * mon-fri"}) == "Weekdays at 08:00"
    assert describe({"kind": "cron", "cron": "30 18 * * *"}) == "Every day at 18:30"
    assert describe({"kind": "cron", "cron": "*/5 * * * *"}) == "Every 5 minutes"
    assert describe({"kind": "cron", "cron": "0 3 1 * *"}).startswith("Cron")
    assert describe({"kind": "once", "run_at": datetime(2026, 12, 1, 15, 0).timestamp()}) == "Once · Dec 01, 15:00"
    with pytest.raises(ValueError):
        make_trigger("cron", "bad", None)
    with pytest.raises(ValueError):
        make_trigger("once", None, None)
    now = datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc).astimezone()
    nxt = next_fire("cron", "* * * * *", None, now)
    assert nxt and 0 <= (nxt - now).total_seconds() <= 60
    assert fmt_when(None) == "—"
    assert "in 5 min" in fmt_when(time.time() + 330)


def test_fire_new_chat_records_run_and_policy(env):
    db, proj, mgr, url = env
    sid_sched = new_schedule(db, proj, permission="allow-edits")
    runner = ScheduleRunner(db, mgr, url)
    finished = []
    runner.run_finished.connect(lambda a, b, c: finished.append((a, b, c)))
    rid = asyncio.run(runner.fire(sid_sched))
    (sess_id, text, policy), = mgr.sent
    assert text == "Summarize" and policy == "allow-edits"
    s = db.session(sess_id)
    assert s.engine == "codex" and s.model == "gpt-6.2-sol" and s.title.startswith("Daily digest")
    assert db.runs(sid_sched)[0].status == "running"
    mgr.finish(sess_id, "done")
    assert db.runs(sid_sched)[0].status == "done" and db.runs(sid_sched)[0].id == rid
    assert finished == [(sid_sched, sess_id, "done")]


def test_fire_resume_skips_busy_or_missing_chat(env):
    db, proj, mgr, url = env
    chat = db.create_session(proj.id, "claude", "claude-haiku-5-5", "low")
    sched = new_schedule(db, proj, new_or_resume="resume", session_id=chat.id)
    runner = ScheduleRunner(db, mgr, url)
    mgr.busy.add(chat.id)
    asyncio.run(runner.fire(sched))
    assert db.runs(sched)[0].status == "skipped" and "busy" in db.runs(sched)[0].note
    mgr.busy.clear()
    asyncio.run(runner.fire(sched))
    assert mgr.sent[-1][0] == chat.id and db.runs(sched)[0].status == "running"
    mgr.finish(chat.id, "error")
    assert db.runs(sched)[0].status == "error"
    db.delete_session(chat.id)
    asyncio.run(runner.fire(sched))
    assert db.runs(sched)[0].status == "skipped" and "deleted" in db.runs(sched)[0].note


def test_once_schedule_disables_itself(env):
    db, proj, mgr, url = env
    sched = new_schedule(db, proj, kind="once", cron=None, run_at=time.time() + 3600)
    asyncio.run(ScheduleRunner(db, mgr, url).fire(sched))
    assert not db.schedule(sched).enabled


def test_job_store_sync_and_missed_run_fires_once_on_start(env):
    db, proj, mgr, url = env
    sched = new_schedule(db, proj, cron="0 3 * * *")
    off = new_schedule(db, proj, name="Off", enabled=False)

    async def first_session():
        r = ScheduleRunner(db, mgr, url)
        r.start()
        assert r.scheduler.get_job(r.job_id(sched)) and not r.scheduler.get_job(r.job_id(off))
        # Simulate the PC sleeping through the 03:00 run: push the stored next run into the past.
        r.scheduler.get_job(r.job_id(sched)).modify(next_run_time=datetime.now(timezone.utc) - timedelta(hours=2))
        r.set_paused(True)
        r.shutdown()

    async def second_session():
        db.set_setting("schedules.paused", False)
        r = ScheduleRunner(db, mgr, url)
        r.start()  # identical job is kept (replace=False), so the missed run is still due
        for _ in range(50):
            if mgr.sent:
                break
            await asyncio.sleep(0.1)
        nxt = r.next_run(sched)
        r.shutdown()
        return nxt

    asyncio.run(first_session())
    nxt = asyncio.run(second_session())
    assert len(mgr.sent) == 1, "missed run should fire exactly once"
    assert nxt and nxt > datetime.now(timezone.utc)


def test_skip_policy_drops_missed_run(env):
    db, proj, mgr, url = env
    sched = new_schedule(db, proj, cron="0 3 * * *", missed="skip")

    async def go():
        r = ScheduleRunner(db, mgr, url)
        r.start()
        r.scheduler.get_job(r.job_id(sched)).modify(next_run_time=datetime.now(timezone.utc) - timedelta(hours=2))
        await asyncio.sleep(1.5)
        r.shutdown()

    asyncio.run(go())
    assert mgr.sent == []


class FakeReg:
    def __init__(self):
        self.d = {}

    def get(self, key, name):
        return self.d.get((key, name))

    def set(self, key, name, value):
        self.d[(key, name)] = value

    def delete(self, key, name):
        self.d.pop((key, name), None)


def test_autostart_toggle():
    reg = FakeReg()
    assert not autostart.is_enabled(reg)
    autostart.set_enabled(True, reg, command='"pythonw.exe" "run.py" --tray')
    assert autostart.is_enabled(reg) and reg.get(autostart.RUN_KEY, "Orchestrator").endswith("--tray")
    autostart.set_enabled(False, reg)
    assert not autostart.is_enabled(reg)
    assert autostart.launch_command().endswith("--tray")
