"""One global limit on how many agents run at once, shared by orchestration tasks and scheduled runs.

Each orchestration also has its own "Run up to N tasks at once"; a task starts only when both allow it.
Whoever frees a slot calls ``slot_freed()`` so the other side (queued tasks, waiting scheduled runs) can start.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from orchestrator.storage.db import Database

DEFAULT_GLOBAL_MAX = 6


class _Hub(QObject):
    freed = Signal()


hub = _Hub()


def global_limit(db: Database) -> int:
    try:
        return max(1, int(db.get_setting("global.max_parallel", DEFAULT_GLOBAL_MAX)))
    except (TypeError, ValueError):
        return DEFAULT_GLOBAL_MAX


def busy(db: Database) -> int:
    tasks, runs = db.busy_counts()
    return tasks + runs


def free_slots(db: Database) -> int:
    return max(0, global_limit(db) - busy(db))


def slot_freed() -> None:
    hub.freed.emit()
