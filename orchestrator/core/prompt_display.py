"""Show the person's words, not the instructions Orchestrator wraps around them.

A planner chat starts with the whole orchestrator prompt (tools, models, rules) and a sub-agent chat with the
sub-agent intro and the report-block instructions. The engine needs all of that; the chat only needs the goal or
the task. `split_prompt` returns what to show and the hidden part, so the chat can collapse it into a chip.
Works on stored text, so chats started before this change are cleaned up too.
"""
from __future__ import annotations

ORCH_STARTS = ("You are the orchestrator for the project folder", "You are the orchestrator (planner) inside Orchestrator")
GOAL_HEAD = "Goal from the user:\n"
GOAL_END = "\n\nYou coordinate sub-agents"
SUB_START = "You are a sub-agent started by an orchestrator."
TASK_HEAD = "Task:\n"
NEW_TASK = "New task from the orchestrator:\n"
CLOSING_START = "\n\nWhen you are finished, end your final message with this report block"


def split_prompt(text: str) -> tuple[str, str]:
    """Return (text to show, hidden instructions). Hidden is "" when there is nothing to hide."""
    t = text or ""
    if t.startswith(ORCH_STARTS) and GOAL_HEAD in t:
        start = t.index(GOAL_HEAD) + len(GOAL_HEAD)
        end = t.find(GOAL_END, start)
        goal = t[start:end if end >= 0 else len(t)].strip()
        return (goal or t), t
    shown, cut = t, False
    if shown.startswith(SUB_START) and TASK_HEAD in shown:
        shown = shown[shown.index(TASK_HEAD) + len(TASK_HEAD):]
        cut = True
    elif shown.startswith(NEW_TASK):
        shown = shown[len(NEW_TASK):]
        cut = True
    i = shown.find(CLOSING_START)
    if i >= 0:
        shown = shown[:i]
        cut = True
    shown = shown.strip()
    return (shown, t) if cut and shown else (t, "")
