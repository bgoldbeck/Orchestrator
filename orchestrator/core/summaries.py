"""Session summaries (powers reuse) and find_sessions ranking.

After a chat goes idle, a cheap enabled model writes a 3-5 line summary plus keywords.
If summaries are off or no cheap model is enabled, a heuristic summary is used instead.
The one-shot model call runs in %APPDATA%\\Orchestrator\\scratch, which the session indexer
ignores, so it never shows up as a chat.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from orchestrator.core import model_registry
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database, Session, app_dir
from orchestrator.ui_text import relative

MODES = {"model": "Cheap model", "heuristic": "Quick (no model)", "off": "Off"}
STOP = set("""a an and are as at be but by can could do does for from had has have how i if in into is it its
just let me my no not of on or our please so that the their them then there these this to too up us use was we
were what when where which who will with would you your yes ok okay make sure also should need want like get
file files code using used add new now one two all any each per via run want""".split())
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/]{2,}")
SUMMARY_PROMPT = """Summarize this chat between a user and a coding agent for a search index.
Write 3 to 5 short lines: what the user wanted, what was done, the main files or topics, and the state
(finished, partial, blocked). Then one last line: "Keywords: " followed by 5 to 10 comma-separated keywords
(file names, components, features, error names). Reply with nothing else.

Chat:
{transcript}"""


def tokens(text: str) -> list[str]:
    out = []
    for w in WORD.findall(text or ""):
        w = w.lower().strip(".-/")
        if len(w) > 2 and w not in STOP and not w.isdigit():
            out.append(w)
    return out


def transcript(db: Database, s: Session, limit: int = 12000) -> str:
    parts = []
    for m in db.messages(s.id):
        if m["kind"] == "text":
            who = "User" if m["role"] == "user" else "Agent"
            parts.append(f"{who}: {m['text'].strip()}")
        elif m["kind"] == "tool":
            parts.append(f"[tool {m.get('name', '')}: {m.get('summary', '')}]")
    text = "\n".join(parts)
    if len(text) > limit:  # keep the start (the ask) and the end (the outcome)
        text = text[: limit // 3] + "\n…\n" + text[-(limit * 2 // 3):]
    return text


def heuristic_summary(db: Database, s: Session) -> tuple[str, str]:
    msgs = db.messages(s.id)
    users = [m["text"] for m in msgs if m["role"] == "user" and m["kind"] == "text"]
    agents = [m["text"] for m in msgs if m["role"] == "assistant" and m["kind"] == "text"]
    tools = [m.get("summary", "") for m in msgs if m["kind"] == "tool"]
    first = " ".join((users[0] if users else s.title).split())[:200]
    last = " ".join((agents[-1] if agents else "").split())[:240]
    lines = [f"Asked: {first}"]
    if last:
        lines.append(f"Result: {last}")
    counts = Counter(tokens(" ".join([s.title, *users[:3], *tools[:20], *agents[-2:]])))
    kw = [w for w, _ in counts.most_common(10)]
    return "\n".join(lines), ", ".join(kw)


def parse_model_summary(text: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    kw = ""
    body = []
    for ln in lines:
        m = re.match(r"^\**\s*keywords\s*\**\s*:\s*\**\s*(.*)$", ln, re.I)
        if m:
            kw = m.group(1)
        else:
            body.append(ln)
    return "\n".join(body[:6]), ", ".join(k.strip().lower() for k in kw.split(",") if k.strip())


def cheap_model(db: Database) -> tuple[str, str, str] | None:
    """The configured summary model if enabled, else the cheapest enabled model (last in Settings order)."""
    want = db.get_setting("summaries.model")
    if want and model_registry.is_allowed(db, want[0], want[1], strict=True):
        return want[0], want[1], want[2] if len(want) > 2 else "low"
    from orchestrator.core.routing import strength_order

    for e in ("claude", "codex"):
        ms = strength_order(model_registry.allowed(db, e))
        if ms:
            m = ms[-1]
            return e, m.id, "low" if "low" in m.efforts else (m.efforts[0] if m.efforts else "")
    return None


def scratch_dir() -> Path:
    p = app_dir() / "scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


async def ask_model(db: Database, engine: str, model: str, effort: str, prompt: str) -> str:
    """One read-only turn, no tools approved, not stored as a chat. Returns the reply text."""
    from orchestrator.core import auth
    from orchestrator.core.events import Done, Error, Text
    from orchestrator.engines.base import TurnRequest
    from orchestrator.engines.claude import ClaudeAdapter
    from orchestrator.engines.codex import CodexAdapter

    async def deny(_req) -> bool:
        return False

    adapter = (ClaudeAdapter(approval_handler=deny, permission_mode="default") if engine == "claude"
               else CodexAdapter(approval_handler=deny, env=auth.codex_status().env))
    req = TurnRequest(prompt=prompt, cwd=scratch_dir(), model=model, effort=effort, sandbox="read-only")
    buf: list[str] = []
    async for ev in adapter.run(req):
        if isinstance(ev, Text):
            if ev.delta:
                buf.append(ev.text)
            else:
                buf[:] = [ev.text]
        elif isinstance(ev, Error) and ev.fatal:
            raise RuntimeError(ev.message)
        elif isinstance(ev, Done):
            break
    return "".join(buf).strip()


class Summarizer(QObject):
    """Summarizes app chats after they go idle (debounced, one at a time)."""

    updated = Signal(int)

    def __init__(self, db: Database, mgr, parent: QObject | None = None, delay_ms: int = 20_000) -> None:
        super().__init__(parent)
        self.db, self.mgr = db, mgr
        self._queue: list[int] = []
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._kick)
        self.delay_ms = delay_ms
        mgr.status_changed.connect(self._on_status)

    def mode(self) -> str:
        return self.db.get_setting("summaries.mode", "model")

    def _on_status(self, sid: int, status: str) -> None:
        if status in ("done", "error") and self.mode() != "off":
            if sid not in self._queue:
                self._queue.append(sid)
            self._timer.start(self.delay_ms)

    def _kick(self) -> None:
        if not self._busy and self._queue:
            asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        self._busy = True
        try:
            while self._queue:
                sid = self._queue.pop(0)
                if self.mgr.is_running(sid):
                    continue  # summarize after its next turn instead
                await self.summarize(sid)
        finally:
            self._busy = False

    async def summarize(self, sid: int) -> None:
        s = self.db.session(sid)
        if not s or not self.db.messages(sid):
            return
        summary, kw = heuristic_summary(self.db, s)
        cm = cheap_model(self.db) if self.mode() == "model" else None
        if cm:
            try:
                reply = await asyncio.wait_for(
                    ask_model(self.db, *cm, SUMMARY_PROMPT.format(transcript=transcript(self.db, s))), 180)
                ms, mk = parse_model_summary(reply)
                if ms:
                    summary, kw = ms, (mk or kw)
            except Exception:  # noqa: BLE001 - heuristic summary stays
                pass
        self.db.update_session(sid, summary=summary, keywords=kw, summary_updated_at=time.time())
        self.updated.emit(sid)


# ------------------------------------------------------------------ find_sessions
def _overlap(q: set[str], text: str) -> list[str]:
    words = set(tokens(text))
    hits = [w for w in q if w in words or any(x.startswith(w) or w.startswith(x) for x in words if len(x) > 3)]
    return sorted(hits)


def find_sessions(db: Database, query: str, project_id: int | None = None, exclude: set[int] | None = None,
                  limit: int = 5, now: float | None = None) -> list[dict[str, Any]]:
    """Rank chats by keyword match (title > keywords > summary) + same project + recency."""
    q = set(tokens(query))
    now = now or time.time()
    exclude = exclude or set()
    planners = {r[0] for r in db.conn.execute("SELECT session_id FROM orchestrations WHERE session_id IS NOT NULL")}
    scored = []
    for s in db.all_sessions():
        if s.id in exclude or s.id in planners:
            continue
        summary, kw = s.summary, s.keywords
        if not summary:
            summary, kw = s.title, ", ".join(tokens(s.title))
        t_hits, k_hits, s_hits = _overlap(q, s.title), _overlap(q, kw or ""), _overlap(q, summary or "")
        hits = set(t_hits) | set(k_hits) | set(s_hits)
        if q and not hits:
            continue
        score = 3 * len(t_hits) + 2 * len(k_hits) + len(s_hits)
        same = project_id is not None and s.project_id == project_id
        if same:
            score += 2
        age_days = max(0.0, (now - (s.last_active or now)) / 86400)
        score += 1.5 * math.exp(-age_days / 14)
        why = []
        if hits:
            why.append("matches " + ", ".join(f"'{h}'" for h in sorted(hits)[:4]))
        if same:
            why.append("same project")
        why.append(relative(s.last_active, now) if s.last_active else "unknown age")
        scored.append((score, s, "; ".join(why), summary))
    scored.sort(key=lambda x: (-x[0], -(x[1].last_active or 0)))
    out = []
    for score, s, why, summary in scored[:limit]:
        proj = db.project(s.project_id)
        out.append({"session_ref": f"s:{s.id}", "engine": s.engine, "engine_label": ENGINES.get(s.engine, s.engine),
                    "title": s.title, "project": proj.name if proj else "", "model": s.model,
                    "last_active": relative(s.last_active, now) if s.last_active else "",
                    "summary": (summary or "")[:400], "why": why, "score": round(score, 2)})
    return out
