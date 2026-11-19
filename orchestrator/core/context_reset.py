"""Context reset points: when a chat's context grows past a size, reset it with a carry-over summary.

Two ways to reset:
- "compact": the engine summarizes the conversation in the same session (Claude: /compact). The summary is
  written into the Claude Code session file, so the Claude desktop app sees the same reset when it opens the chat.
- "fresh": the chat writes a hand-off summary, and the app starts a new chat seeded with it. The old chat is left
  as it was.

The reset point is a token count (default 300k). Each chat can override the app default.
"""
from __future__ import annotations

from typing import Any

DEFAULT_AT = 300_000
MIN_AT = 20_000
ACTIONS = {
    "compact": "Compact with a carry-over summary",
    "fresh": "Start a fresh chat with a carry-over summary",
    "off": "Off (never reset on its own)",
}
DEFAULT_ACTION = "compact"


def default_at(db) -> int:
    return int(db.get_setting("context.reset_at", DEFAULT_AT) or DEFAULT_AT)


def default_action(db) -> str:
    a = db.get_setting("context.reset_action", DEFAULT_ACTION)
    return a if a in ACTIONS else DEFAULT_ACTION


def chat_override(db, sid: int) -> dict[str, Any]:
    """This chat's own choices ({} = app default). Keys: at, action."""
    return dict(db.get_setting(f"chat.reset.{sid}", None) or {})


def set_chat_override(db, sid: int, at: int | None = None, action: str | None = None) -> None:
    """None for a key = use the app default for it."""
    d = {k: v for k, v in (("at", at), ("action", action)) if v is not None}
    db.set_setting(f"chat.reset.{sid}", d or None)


def reset_at(db, sid: int) -> int:
    return int(chat_override(db, sid).get("at") or default_at(db))


def action(db, sid: int) -> str:
    a = chat_override(db, sid).get("action")
    return a if a in ACTIONS else default_action(db)


def fmt_k(n: int | float) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.0f}M" if v == int(v) else f"{v:.1f}M"
    return f"{round(n / 1000)}k"


SUMMARY_PROMPT = (
    "Write a carry-over summary of this conversation so a fresh session can continue the work without it. "
    "Cover: the goal, decisions made, what is done (files changed), what is left to do, and open questions. "
    "Be specific (file names, commands, numbers) and keep it under 400 words. Reply with the summary only."
)
CARRY_HEAD = "[Carry-over summary from the earlier part of this chat]"
CARRY_END = "[End of summary. The new message follows.]"


def carry_prompt(summary: str, prompt: str) -> str:
    return f"{CARRY_HEAD}\n{summary.strip()}\n{CARRY_END}\n\n{prompt}"


def _since_reset(msgs: list[dict]) -> tuple[dict | None, list[dict]]:
    """(the last reset record, messages after it)."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("kind") == "reset":
            return msgs[i], msgs[i + 1:]
    return None, msgs


def context_size(db, sid: int) -> tuple[int, int]:
    """(tokens in this chat's context now, context window or 0).

    Claude reports both after each turn; Codex: the last turn's input (new + cached). After a reset, only turns
    since the reset count, and until the next turn the size is what the reset left."""
    s = db.session(sid)
    if not s:
        return 0, 0
    last, after = _since_reset(db.messages(sid))
    ctx = db.context(sid) or {}
    cap = int(ctx.get("max") or 0) if s.engine == "claude" else 0
    usage = [m for m in after if m.get("kind") == "usage"]
    if not usage:
        return (int(last.get("after") or 0) if last else 0), cap
    if s.engine == "claude" and ctx.get("total"):
        return int(ctx["total"]), cap
    u = usage[-1]
    return int(u.get("input") or 0) + int(u.get("cached") or 0), cap


def due(db, sid: int) -> str | None:
    """The reset to run before this chat's next message ("compact" | "fresh"), or None.

    Codex compacts on its own at the reset point (model_auto_compact_token_limit), so only "fresh" is due there."""
    s = db.session(sid)
    if not s or not s.engine_session_id:
        return None
    how = action(db, sid)
    if how == "off" or (how == "compact" and s.engine == "codex"):
        return None
    used, _cap = context_size(db, sid)
    return how if used >= reset_at(db, sid) else None


def codex_limit(db, sid: int) -> int:
    """model_auto_compact_token_limit for a Codex turn (0 = leave Codex's own default)."""
    return reset_at(db, sid) if action(db, sid) == "compact" else 0
