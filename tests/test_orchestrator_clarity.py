"""Orchestrator clarity: project auto-pick, global queue with order, delegate to other projects, run later."""
import asyncio
import os
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import concurrency, project_pick  # noqa: E402
from orchestrator.core.orchestration import OrchestrationError, OrchestrationManager  # noqa: E402
from orchestrator.core.scheduler import plain_when, when_chips  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402
from tests.test_orchestration import FakeMgr  # noqa: E402


@pytest.fixture(autouse=True)
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    folders = {}
    for name in ("weather-app", "billing-service", "docs-site"):
        d = tmp_path / "code" / name
        d.mkdir(parents=True)
        folders[name] = db.add_project(str(d), name)
    s = db.create_session(folders["billing-service"].id, "codex", "gpt-6-luna", "low", title="Stripe invoices")
    db.update_session(s.id, summary="Invoice PDF export and Stripe webhooks", keywords="invoice, stripe, webhook")
    mgr = FakeMgr(db)
    om = OrchestrationManager(db, mgr)
    yield db, folders, mgr, om
    om.bridge.stop()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_project_auto_pick(env):
    db, f, _mgr, _om = env
    best = project_pick.confident(project_pick.rank_projects(db, "Add a 7-day forecast to the weather app"))
    assert best and best.project.name == "weather-app" and "weather" in best.reason
    best = project_pick.confident(project_pick.rank_projects(db, "Retry failed stripe webhooks"))
    assert best and best.project.name == "billing-service" and "recent chats" in best.reason
    assert project_pick.confident(project_pick.rank_projects(db, "fix the flaky test")) is None
    res = project_pick.find_projects(db, "docs site search box")
    assert res["projects"][0]["name"] == "docs-site" and res["confident"]


def test_global_queue_order_limit_and_cancel(env):
    db, f, mgr, om = env
    db.set_setting("global.max_parallel", 1)
    a = om.start(f["weather-app"].id, "A", "codex", "gpt-6-luna", "low", max_parallel=4)
    b = om.start(f["docs-site"].id, "B", "codex", "gpt-6-luna", "low", max_parallel=4)
    t1 = om.delegate(a.id, "one", engine="codex", model="gpt-6-luna")["task_id"]
    t2 = om.delegate(a.id, "two", engine="codex", model="gpt-6-luna")["task_id"]
    t3 = om.delegate(b.id, "three", engine="codex", model="gpt-6-luna")["task_id"]
    assert db.task(t1).status == "running" and db.task(t2).status == db.task(t3).status == "queued"
    assert om.queue_positions() == {t2: 1, t3: 2}
    om.move_task(t3, -1)  # the other orchestration's task goes first
    assert om.queue_positions() == {t3: 1, t2: 2}
    mgr.finish(db.task(t1).session_id, "done")
    assert db.task(t3).status == "running" and db.task(t2).status == "queued"
    asyncio.get_event_loop().run_until_complete(om.stop_task(t2))  # cancel a queued task
    assert db.task(t2).status == "stopped" and om.queue_positions() == {}
    assert concurrency.busy(db) == 1


def test_per_orchestration_limit_still_applies(env):
    db, f, mgr, om = env
    a = om.start(f["weather-app"].id, "A", "codex", "gpt-6-luna", "low", max_parallel=1)
    t1 = om.delegate(a.id, "one", engine="codex", model="gpt-6-luna")["task_id"]
    t2 = om.delegate(a.id, "two", engine="codex", model="gpt-6-luna")["task_id"]
    assert db.task(t1).status == "running" and db.task(t2).status == "queued"


def test_delegate_to_other_project(env):
    db, f, mgr, om = env
    a = om.start(f["weather-app"].id, "A", "codex", "gpt-6-luna", "low")
    r = om.delegate(a.id, "update the docs", engine="codex", model="gpt-6-luna", project="docs-site")
    t = db.task(r["task_id"])
    assert t.cwd == f["docs-site"].path and db.session(t.session_id).project_id == f["docs-site"].id
    with pytest.raises(OrchestrationError, match="find_projects"):
        om.delegate(a.id, "x", engine="codex", model="gpt-6-luna", project="nope")
    found = asyncio.get_event_loop().run_until_complete(om.handle(a.id, "find_projects", {"query": "billing"}))
    assert found["projects"][0]["name"] == "billing-service"


def test_when_words_and_chips():
    now = datetime(2026, 11, 18, 13, 30)
    labels = [c[0] for c in when_chips(now)]
    assert labels == ["In 1 hour", "In 5 hours", "Tonight 9 PM", "Tomorrow 8 AM"]
    assert plain_when(datetime(2026, 11, 18, 18, 30), now) == "Today at 6:30 PM, in 5 hours"
    assert plain_when(datetime(2026, 11, 19, 8, 0), now) == "Tomorrow at 8:00 AM, in 19 hours"
    assert plain_when(datetime(2026, 11, 18, 13, 45), now) == "Today at 1:45 PM, in 15 minutes"


def test_page_labels_empty_state_and_auto(qapp, tmp_path):
    from PySide6.QtCore import Signal

    from orchestrator.ui.orchestrate_page import OrchestratePage

    class Mgr(FakeMgr):
        approval_requested = Signal(int, object, object)
        event = Signal(int, object)

    db = Database(tmp_path / "e.db")
    mgr = Mgr(db)
    om = OrchestrationManager(db, mgr)
    page = OrchestratePage(db, mgr, om)
    assert not page.noProjects.isHidden() and page.composer.isHidden()
    assert page.addProjectBtn.text() == "Add project folder"
    for name in ("weather-app", "docs-site"):
        d = tmp_path / name
        d.mkdir()
        db.add_project(str(d), name)
    page.reload()
    assert page.noProjects.isHidden() and not page.composer.isHidden()
    assert page.project.currentData() == "auto" and page.history.itemText(0) == "New orchestration"
    assert page.newRun.text() == "New orchestration" and page.startBtn.text() == "Run now"
    page.goal.setPlainText("Add search to the docs site")
    page._update_where()
    assert page.whereLabel.text().startswith("Working in: docs-site — because")
    page.goal.setPlainText("refactor things")
    page._start()
    assert not page.askProject.isHidden() and not db.orchestrations()
    om.bridge.stop()


def test_scheduled_orchestration_and_global_wait(tmp_path):
    from types import SimpleNamespace

    from orchestrator.core.scheduler import ScheduleRunner
    from tests.test_scheduler import FakeManager, new_schedule

    db = Database(tmp_path / "s.db")
    proj = db.add_project(str(tmp_path), "proj")
    mgr = FakeManager()
    runner = ScheduleRunner(db, mgr, f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    started = []
    plan = db.create_session(proj.id, "claude", "claude-opus-5-5", "medium", title="planner")

    def start(pid, goal, engine, model, effort, max_parallel):
        started.append((pid, goal, engine, max_parallel))
        return SimpleNamespace(session_id=plan.id)

    runner.start_orchestration = start
    sid = new_schedule(db, proj, prompt="Nightly dependency bumps", target="orchestration",
                       options={"max_parallel": 3, "auto_planner": True})
    assert db.schedule(sid).target == "orchestration" and db.schedule(sid).options["max_parallel"] == 3
    asyncio.get_event_loop().run_until_complete(runner.fire(sid))
    assert started == [(proj.id, "Nightly dependency bumps", "auto", 3)]
    assert db.runs(sid)[0].status == "running" and concurrency.busy(db) == 0  # the planner holds no slot

    db.set_setting("global.max_parallel", 1)
    chat = new_schedule(db, proj, name="Digest")
    chat2 = new_schedule(db, proj, name="Digest 2")
    asyncio.get_event_loop().run_until_complete(runner.fire(chat))
    asyncio.get_event_loop().run_until_complete(runner.fire(chat2))
    assert len(mgr.sent) == 1 and runner.waiting() == [chat2]  # second run waits for a free slot


def test_number_box_shows_its_value(qapp):
    from orchestrator.ui.number_box import NumberBox

    b = NumberBox(None, 1, 8, 4)
    b.resize(90, 33)
    b.show()
    qapp.processEvents()
    assert b.lineEdit().width() > 20 and b.text() == "4"
    b.close()
