"""Why a queued task may not start yet, in plain words (used by the queue and shown on cards).

Rules, in the order they are checked:
1. Held for you: a task it depends on failed or was stopped (answer in Waiting on you).
2. Dependencies: every task in ``depends_on`` must be done (and merged, when it ran in a worktree).
3. Usage limit: the engine's 5-hour or weekly limit is used up (see core.limits).
4. Shared files: another running task without its own worktree declared one of the same files.
5. Slots: this run's "up to N at once", then the app-wide limit.
"""
from __future__ import annotations

from pathlib import PurePath

from orchestrator.core import concurrency
from orchestrator.storage.db import Database, Task

FAILED = ("failed", "stopped")


def _norm(path: str) -> str:
    return str(PurePath(path.strip().strip("`'\"").replace("\\", "/"))).lower().lstrip("./")


def files_overlap(a: list[str], b: list[str]) -> list[str]:
    na = {_norm(x): x for x in a if x.strip()}
    nb = {_norm(x) for x in b if x.strip()}
    return [na[k] for k in na if k in nb or any(k.startswith(o + "/") or o.startswith(k + "/") for o in nb)]


def dep_state(db: Database, t: Task) -> tuple[str, Task | None]:
    """("ok" | "waiting" | "failed", the blocking task)."""
    for d in t.depends_on:
        dt = db.task(int(d))
        if not dt:
            continue
        if dt.status in FAILED:
            return "failed", dt
        if dt.status != "done" or dt.merge_state in ("review", "conflict"):
            return "waiting", dt
    return "ok", None


def start_blocker(db: Database, t: Task, paused: dict, worktrees_on: bool = False,
                  repo_of=None) -> str | None:
    """The reason `t` may not start now (ignoring slots), or None."""
    if t.hold:
        return t.hold.get("text") or "Held: waiting for your answer in Waiting on you"
    state, dt = dep_state(db, t)
    if state == "failed":
        return f"Waiting for you: its dependency T-{dt.id} {dt.status}"
    if state == "waiting":
        what = "to be merged" if dt.status == "done" else "to finish"
        return f"Waiting for T-{dt.id} {what}"
    p = paused.get(t.engine)
    if p:
        return "Paused: " + p.text()
    if t.files:
        for o in db.tasks_running():
            if o.id == t.id or not o.files or o.worktree:
                continue
            if worktrees_on and repo_of and repo_of(o.cwd or "") and repo_of(o.cwd or "") == repo_of(t.cwd or ""):
                continue  # this task will get its own worktree, so the files cannot collide
            shared = files_overlap(t.files, o.files)
            if shared:
                return f"Waiting for T-{o.id}: it is changing {', '.join(shared[:2])}"
    return None


def why_waiting(db: Database, t: Task, paused: dict | None = None) -> str:
    """One line for a queued task: the rule that holds it, else the slot that is full."""
    from orchestrator.core import limits

    if t.status == "proposed":
        return "Waiting for you to start it"
    if t.status != "queued":
        return ""
    reason = start_blocker(db, t, limits.paused(db) if paused is None else paused)
    if reason:
        return reason
    orch = db.orchestration(t.orchestration_id)
    if orch:
        running = sum(1 for x in db.tasks(orch.id) if x.status in ("running", "waiting"))
        if running >= orch.max_parallel:
            return f"This run's limit is full ({orch.max_parallel} at once)"
    if concurrency.free_slots(db) <= 0:
        return f"All {concurrency.global_limit(db)} agent slots are busy"
    return "Starting"


def dependents(db: Database, tid: int) -> list[Task]:
    """Unfinished tasks that wait on `tid`."""
    return [x for x in db.active_tasks() if tid in x.depends_on and x.status in ("queued", "proposed")]
