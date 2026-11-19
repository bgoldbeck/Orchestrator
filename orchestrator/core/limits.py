"""Pause new sub-agent starts while an engine's plan usage limit is used up.

A task started while the 5-hour or weekly limit is reached would only fail, so queued tasks for that engine wait
until the reset and then start on their own. Running tasks are left alone. A task that fails because the limit hit
mid-turn is put back in the queue (see OrchestrationManager) instead of being counted as failed.

Claude: the CLI's rate-limit reports (status "rejected" = limit reached). Codex: ``used_percent`` >= 100 in the newest
session log. The Codex log read is cached for a minute, since the queue asks often.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from orchestrator.core import usage
from orchestrator.storage.db import Database

SETTING = "orchestrate.pause_on_limit"  # on by default
UNKNOWN_RESET_WAIT = 3600  # a limit with no reset time pauses for one hour

_codex_cache: dict = {"at": 0.0, "value": None}


@dataclass
class Pause:
    engine: str
    window: str  # "5-hour" | "Weekly"
    resets_at: float

    def text(self) -> str:
        name = {"claude": "Claude", "codex": "Codex"}.get(self.engine, self.engine)
        when = time.strftime("%I:%M %p", time.localtime(self.resets_at)).lstrip("0")
        if self.resets_at - time.time() > 20 * 3600:
            when = time.strftime("%a %I:%M %p", time.localtime(self.resets_at)).replace(" 0", " ")
        return f"{name} {self.window.lower()} limit reached, starts again at {when}"


def _claude(db: Database, now: float) -> Pause | None:
    data = db.get_setting(usage.CLAUDE_KEY) or {}
    for kind, rep in data.items():
        if str(rep.get("status") or "") != "rejected":
            continue
        resets = rep.get("resets_at")
        resets = float(resets) if resets else float(rep.get("at") or now) + UNKNOWN_RESET_WAIT
        if resets > now:
            return Pause("claude", usage.CLAUDE_WINDOWS.get(kind, (kind, "Usage"))[1], resets)
    return None


def _codex(now: float, reader=None) -> Pause | None:
    if reader is None and now - _codex_cache["at"] < 60:
        got = _codex_cache["value"]
    else:
        try:
            got = (reader or usage.latest_codex_rate_limits)()
        except Exception:  # noqa: BLE001 - unreadable logs never pause anything
            got = None
        if reader is None:
            _codex_cache.update(at=now, value=got)
    if not got:
        return None
    raw, seen = got
    for key, label in (("primary", "5-hour"), ("secondary", "Weekly")):
        w = raw.get(key) or {}
        pct = w.get("used_percent")
        if pct is None or float(pct) < 100:
            continue
        resets = w.get("resets_at")
        if resets is None and w.get("resets_in_seconds") is not None:
            resets = seen + float(w["resets_in_seconds"])
        resets = float(resets) if resets else seen + UNKNOWN_RESET_WAIT
        if resets > now:
            return Pause("codex", label, resets)
    return None


def paused(db: Database, now: float | None = None, codex_reader=None) -> dict[str, Pause]:
    """engine -> Pause for every engine whose limit is used up right now (empty when the setting is off)."""
    if not db.get_setting(SETTING, True):
        return {}
    now = now or time.time()
    out = {}
    for p in (_claude(db, now), _codex(now, codex_reader)):
        if p:
            out[p.engine] = p
    return out


def forget_cache() -> None:
    _codex_cache.update(at=0.0, value=None)
