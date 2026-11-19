"""Board: every orchestration task in four columns, grouped the way the user reads them.

    In queue  (Ready now = queued, Needs your OK = proposed)
    Running   (running, waiting on you)
    Review    (finished, not yet looked at; failures first)
    Done      (reviewed, plus stopped)

Pure data so it can be tested without Qt; orchestrator.ui.board_page draws it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from orchestrator.core import concurrency
from orchestrator.storage.db import Database, Task

COLUMNS = ("queue", "running", "review", "done")
COLUMN_TITLES = {"queue": "In queue", "running": "Running", "review": "Review", "done": "Done"}
GROUPS = {"queue": [("ready", "Ready now"), ("proposed", "Needs your OK")]}


@dataclass
class BoardCard:
    task: Task
    project_id: int
    column: str
    group: str = ""
    queue_pos: int | None = None


@dataclass
class Board:
    columns: dict[str, list[BoardCard]] = field(default_factory=lambda: {c: [] for c in COLUMNS})
    agents_busy: int = 0
    agents_limit: int = 0

    def count(self, column: str) -> int:
        return len(self.columns[column])

    def group(self, column: str, group: str) -> list[BoardCard]:
        return [c for c in self.columns[column] if c.group == group]


def column_of(t: Task) -> tuple[str, str]:
    """(column, group) for one task."""
    if t.status == "queued":
        return "queue", "ready"
    if t.status == "proposed":
        return "queue", "proposed"
    if t.status in ("running", "waiting"):
        return "running", t.status
    if t.status in ("done", "failed") and not t.reviewed_at:
        return "review", t.status
    return "done", t.status


def build_board(db: Database, project_id: int | None = None, finished_limit: int = 40) -> Board:
    b = Board(agents_busy=concurrency.busy(db), agents_limit=concurrency.global_limit(db))
    qpos = {t.id: i for i, t in enumerate(db.queued_tasks(), 1)}
    for t, pid in db.board_tasks(finished_limit):
        if project_id and pid != project_id:
            continue
        col, grp = column_of(t)
        b.columns[col].append(BoardCard(t, pid, col, grp, qpos.get(t.id)))
    big = 1e18
    b.columns["queue"].sort(key=lambda c: (c.group != "ready", c.queue_pos or big, c.task.created_at))
    b.columns["running"].sort(key=lambda c: (c.group != "waiting", -(c.task.started_at or 0)))
    b.columns["review"].sort(key=lambda c: (c.group != "failed", -(c.task.finished_at or 0)))
    b.columns["done"].sort(key=lambda c: -(c.task.reviewed_at or c.task.finished_at or c.task.created_at))
    return b


def mark_reviewed(db: Database, tid: int, reviewed: bool = True) -> None:
    db.update_task(tid, reviewed_at=time.time() if reviewed else None)


def mark_all_reviewed(db: Database, project_id: int | None = None) -> int:
    n = 0
    for c in build_board(db, project_id).columns["review"]:
        if c.task.status == "done":
            mark_reviewed(db, c.task.id)
            n += 1
    return n


def time_text(t: Task, now: float | None = None) -> str:
    """Short time pill: elapsed while running, total when finished, age when queued."""
    now = now or time.time()
    if t.started_at:
        sec = (t.finished_at or now) - t.started_at
    else:
        sec = now - t.created_at
    m = int(max(0, sec) // 60)
    if m < 1:
        return "<1 min"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"
