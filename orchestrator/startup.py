"""Startup milestones (for scripts/startup_bench.py). No-ops unless ORCHESTRATOR_STARTUP_LOG is set.

Each mark appends "<name> <unix time>" to that file. With ORCHESTRATOR_BENCH_QUIT=1 the app quits
right after the "ready" mark.
"""
from __future__ import annotations

import os
import time

_LOG = os.environ.get("ORCHESTRATOR_STARTUP_LOG")
_seen: set[str] = set()
_ready_parts: set[str] = set()
# window painted + session list refreshed from disk + every page and service set up
READY_NEEDS = {"first_paint", "sessions", "deferred"}
on_ready = None  # set by the app: called once when everything is loaded


def mark(name: str) -> None:
    if name in _seen:
        return
    _seen.add(name)
    if _LOG:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{name} {time.time():.4f}\n")


def part_done(name: str) -> None:
    """One piece of 'fully ready' finished; marks 'ready' when all of READY_NEEDS are done."""
    mark(name)
    _ready_parts.add(name)
    if READY_NEEDS <= _ready_parts and "ready" not in _seen:
        mark("ready")
        if on_ready:
            on_ready()


def bench_quit() -> bool:
    return bool(_LOG) and os.environ.get("ORCHESTRATOR_BENCH_QUIT") == "1"
