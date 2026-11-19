"""Board: tasks from every project in In queue / Running / Review / Done, with review marking."""
import os
import sqlite3
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import board as B  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.set_setting("global.max_parallel", 5)
    return d


def _task(db, oid, title, status, **kw):
    t = db.add_task(oid, "claude", "claude-sonnet-5-5", "medium", title, title)
    db.update_task(t.id, status=status, **kw)
    return db.task(t.id)


@pytest.fixture
def filled(db):
    a = db.add_project("/tmp/a", "alpha")
    b = db.add_project("/tmp/b", "beta")
    oa = db.add_orchestration(a.id, "goal a", "claude", "claude-opus-5-5", "medium", 3)
    ob = db.add_orchestration(b.id, "goal b", "claude", "claude-opus-5-5", "medium", 3)
    now = time.time()
    ids = {
        "q1": _task(db, oa.id, "queued one", "queued", queue_pos=now + 2).id,
        "q2": _task(db, ob.id, "queued two", "queued", queue_pos=now + 1).id,
        "prop": _task(db, oa.id, "needs ok", "proposed").id,
        "run": _task(db, oa.id, "running", "running", started_at=now - 120).id,
        "wait": _task(db, ob.id, "waiting", "waiting", started_at=now - 60).id,
        "done": _task(db, oa.id, "done new", "done", finished_at=now - 10).id,
        "fail": _task(db, ob.id, "failed", "failed", finished_at=now - 20).id,
        "old": _task(db, oa.id, "done old", "done", finished_at=now - 999, reviewed_at=now - 900).id,
        "stop": _task(db, ob.id, "stopped", "stopped", finished_at=now - 30).id,
    }
    return db, a, b, ids


def _ids(board, col):
    return [c.task.id for c in board.columns[col]]


def test_columns_and_order(filled):
    db, a, b, ids = filled
    bd = B.build_board(db)
    assert _ids(bd, "queue") == [ids["q2"], ids["q1"], ids["prop"]]  # ready (queue order) before needs-OK
    assert [c.group for c in bd.columns["queue"]] == ["ready", "ready", "proposed"]
    assert bd.columns["queue"][0].queue_pos == 1
    assert _ids(bd, "running") == [ids["wait"], ids["run"]]  # waiting on you comes first
    assert _ids(bd, "review") == [ids["fail"], ids["done"]]  # failures first
    assert set(_ids(bd, "done")) == {ids["old"], ids["stop"]}
    assert (bd.agents_busy, bd.agents_limit) == (2, 5)


def test_project_filter(filled):
    db, a, b, ids = filled
    bd = B.build_board(db, a.id)
    assert ids["q2"] not in _ids(bd, "queue") and ids["q1"] in _ids(bd, "queue")
    assert _ids(bd, "review") == [ids["done"]]


def test_mark_reviewed_moves_to_done_and_back(filled):
    db, a, b, ids = filled
    B.mark_reviewed(db, ids["done"])
    bd = B.build_board(db)
    assert ids["done"] in _ids(bd, "done") and ids["done"] not in _ids(bd, "review")
    B.mark_reviewed(db, ids["done"], False)
    assert ids["done"] in _ids(B.build_board(db), "review")
    assert B.mark_all_reviewed(db) == 1  # failures stay in Review until retried or marked one by one
    assert _ids(B.build_board(db), "review") == [ids["fail"]]


def test_unreviewed_tasks_survive_finished_limit(filled):
    db, a, b, ids = filled
    bd = B.build_board(db, finished_limit=1)
    assert set(_ids(bd, "review")) == {ids["fail"], ids["done"]}


def test_migration_marks_old_finished_tasks_reviewed(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    p = db.add_project("/tmp/x", "x")
    o = db.add_orchestration(p.id, "g", "claude", "m", "low", 2)
    t = _task(db, o.id, "finished before the board", "done", finished_at=time.time())
    db.conn.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE tasks DROP COLUMN reviewed_at")
    con.commit()
    con.close()
    db2 = Database(path)
    assert db2.task(t.id).reviewed_at
    assert not B.build_board(db2).columns["review"]


def test_time_text():
    from orchestrator.storage.db import Task

    now = 10_000.0
    t = Task(1, 1, None, "claude", "m", "low", "x", "x", None, None, "running", None, now - 50, now - 3 * 60 - 5, None)
    assert B.time_text(t, now) == "3 min"
    t.started_at = now - 125 * 60
    assert B.time_text(t, now) == "2 h 5 min"


def test_board_page_renders(filled):
    from PySide6.QtWidgets import QApplication

    from orchestrator.ui.board_page import BoardCardWidget, BoardPage

    QApplication.instance() or QApplication([])
    db, a, b, ids = filled
    page = BoardPage(db, None)
    page.reload()
    cards = page.findChildren(BoardCardWidget)
    assert len(cards) == 9
    assert page.agents.text() == "Agents 2 of 5"
    assert page.cols["review"].count.text() == "2"
    page._toggle("queue:ready")
    assert len(page.findChildren(BoardCardWidget)) <= 9
    page.mark_reviewed(ids["done"])
    assert page.cols["review"].count.text() == "1"
