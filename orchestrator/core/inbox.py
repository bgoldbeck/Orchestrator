"""Waiting on you: everything across all runs that needs the user, as one list, most impact first.

Kinds (each with a recommended action shown as the main button):
- question   an orchestrator's ask_user question (its recommended option first)
- proposal   a reuse card or an automatic pick waiting for OK
- approval   a sub-agent waiting for a tool approval in its chat
- review     a worktree task waiting for Merge / Discard ("Review changes before merging" on)
- conflict   a merge that could not be done (your checkout was left as it was)
- held       a queued task whose dependency failed: run it anyway or stop it
- silent     a running task with no output for a while
- paused     an engine's usage limit is used up: queued tasks wait for the reset

Pure data plus `perform`, so it can be tested without Qt; orchestrator.ui.inbox_page draws it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from orchestrator.core import queue_rules, review
from orchestrator.storage.db import Database, Task

KIND_TITLES = {"question": "Question", "proposal": "Needs your OK", "approval": "Approve a tool",
               "review": "Review", "conflict": "Merge problem", "held": "Held", "silent": "Silent",
               "paused": "Usage limit"}
ORDER = {"conflict": 0, "question": 1, "held": 2, "review": 3, "approval": 4, "proposal": 5, "silent": 6,
         "paused": 7}


@dataclass
class Action:
    id: str
    label: str
    primary: bool = False


@dataclass
class Item:
    key: str
    kind: str
    title: str
    detail: str = ""
    orchestration_id: int | None = None
    task_id: int | None = None
    question_id: int | None = None
    session_id: int | None = None
    engine: str = ""
    actions: list[Action] = field(default_factory=list)
    free_text: bool = False  # a typed answer is allowed (questions)
    unblocks: int = 0
    created_at: float = 0.0

    @property
    def recommended(self) -> Action | None:
        return next((a for a in self.actions if a.primary), None)


def _unblocks(db: Database, tid: int) -> int:
    return len(queue_rules.dependents(db, tid))


def _goal(db: Database, oid: int) -> str:
    o = db.orchestration(oid)
    return " ".join(o.goal.split())[:70] if o else ""


def _task_item(db: Database, t: Task, kind: str, title: str, detail: str, actions: list[Action]) -> Item:
    return Item(f"{kind}:{t.id}", kind, title, detail, t.orchestration_id, t.id, session_id=t.session_id,
                engine=t.engine, actions=actions, unblocks=_unblocks(db, t.id),
                created_at=t.finished_at or t.started_at or t.created_at)


def build(db: Database, om=None, now: float | None = None) -> list[Item]:
    now = now or time.time()
    items: list[Item] = []
    for q in db.all_open_questions():
        opts = list(q.options)
        rec = q.recommended or (opts[0] if len(opts) == 1 else None)
        acts = [Action("answer:" + o, o, primary=(o == rec)) for o in opts]
        items.append(Item(f"question:{q.id}", "question", q.text, "From the run: " + _goal(db, q.orchestration_id),
                          q.orchestration_id, question_id=q.id, actions=acts, free_text=True,
                          unblocks=1,  # the planner waits for the answer
                          created_at=q.created_at))
    for t in db.active_tasks():
        if t.status == "proposed":
            prop = t.proposal or {}
            if prop.get("kind") == "reuse":
                s = db.session(int(prop.get("session_id") or 0))
                detail = f"Continue “{s.title}” instead of starting fresh?" if s else "Continue an earlier chat?"
                acts = [Action("accept", "Use it", True), Action("fresh", "Start fresh")]
            else:
                detail = f"Start on {t.model or 'Auto'} · {t.effort or 'default'}? {t.pick_reason or ''}".strip()
                acts = [Action("accept", "Start", True), Action("stop", "Cancel")]
            items.append(_task_item(db, t, "proposal", t.title, detail, acts))
        elif t.status == "waiting":
            items.append(_task_item(db, t, "approval", t.title, "A sub-agent is waiting for you to approve a tool.",
                                    [Action("open", "Open chat", True)]))
        elif t.status == "queued" and t.hold:
            items.append(_task_item(db, t, "held", t.title, (t.hold or {}).get("text", "Held") +
                                    ". Run it anyway, or stop it?",
                                    [Action("run_anyway", "Run anyway"), Action("stop", "Stop it", True)]))
        if t.merge_state == "review":
            res = t.result or {}
            bits = [review.checks_text(t.checks)] if t.checks else []
            if res.get("assumptions"):
                bits.append("Assumed: " + "; ".join(res["assumptions"][:3]))
            if res.get("summary"):
                bits.append(res["summary"][:200])
            items.append(_task_item(db, t, "review", t.title, "  ·  ".join(b for b in bits if b),
                                    [Action("merge", "Merge", True), Action("diff", "Show changes"),
                                     Action("discard", "Discard")]))
        elif t.merge_state == "conflict":
            items.append(_task_item(db, t, "conflict", t.title, t.merge_note or "",
                                    [Action("merge", "Try merge again", True), Action("diff", "Show changes"),
                                     Action("discard", "Discard")]))
        if om is not None and t.status == "running":
            mins = om.silent_minutes(t, now)
            if mins:
                items.append(_task_item(db, t, "silent", t.title, f"No output for {mins} minutes.",
                                        [Action("open", "Open chat", True), Action("keep", "Keep waiting"),
                                         Action("stop", "Stop it")]))
    if om is not None:
        waiting = [t for t in db.queued_tasks()]
        for engine, p in om.paused().items():
            n = sum(1 for t in waiting if t.engine == engine)
            if not n:
                continue
            items.append(Item(f"paused:{engine}", "paused", p.text(),
                              f"{n} queued task{'s' if n != 1 else ''} wait for the reset and then start on their own.",
                              engine=engine, actions=[Action("wait", "Wait for the reset", True),
                                                      Action("ignore_limit", "Start anyway")],
                              unblocks=n, created_at=now))
    items.sort(key=lambda i: (-i.unblocks, ORDER.get(i.kind, 9), i.created_at))
    return items


async def perform(db: Database, om, item: Item, action: str, text: str = "") -> bool:
    """Do what the user picked. Returns False when the item had nothing to do (already handled)."""
    if action.startswith("answer:") or (action == "answer" and text.strip()):
        answer = text.strip() if action == "answer" else action.split(":", 1)[1]
        om.answer(item.question_id, answer)
        return True
    tid = item.task_id
    if action in ("accept", "fresh") and tid:
        om.resolve_proposal(tid, action)
    elif action == "stop" and tid:
        await om.stop_task(tid)
    elif action == "run_anyway" and tid:
        om.run_anyway(tid)
    elif action == "merge" and tid:
        await om.merge_task(tid)
    elif action == "discard" and tid:
        await om.discard_task(tid)
    elif action == "keep" and tid:
        om.keep_waiting(tid)
    elif action == "ignore_limit" and item.engine:
        om.ignore_limit(item.engine)
    elif action in ("wait", "open", "diff"):
        return False  # handled by the page (navigation) or nothing to do
    else:
        return False
    return True
