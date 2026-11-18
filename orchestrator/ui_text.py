"""Small text helpers shared by core and UI (no Qt imports)."""
from __future__ import annotations

import time


def short_title(text: str, n: int = 48) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def relative(ts: float | None, now: float | None = None) -> str:
    """'just now', '1 minute ago', '3 hours ago', '2 days ago', then a date ('Nov 10', 'Nov 10, 2025')."""
    import datetime as _dt

    if not ts:
        return ""
    now = now or time.time()
    s = max(0, int(now - ts))

    def unit(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'} ago"

    if s < 60:
        return "just now"
    if s < 3600:
        return unit(s // 60, "minute")
    if s < 86400:
        return unit(s // 3600, "hour")
    if s < 86400 * 7:
        return unit(s // 86400, "day")
    d = _dt.datetime.fromtimestamp(ts)
    label = f"{d:%b} {d.day}"
    return label if d.year == _dt.datetime.fromtimestamp(now).year else f"{label}, {d.year}"


def age(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    s = max(0, int((now or time.time()) - ts))
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    if s < 86400 * 7:
        return f"{s // 86400}d"
    if s < 86400 * 60:
        return f"{s // (86400 * 7)}w"
    return f"{s // (86400 * 30)}mo"


def tokens_k(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def usage_text(u: dict, short: bool = False) -> str:
    """'45.2k in · 3.1k out · $0.12' (cost only when the engine reports it)."""
    if not u or not u.get("turns"):
        return ""
    parts = [f"{tokens_k(u['input'] + u['cached'])} in", f"{tokens_k(u['output'])} out"]
    if u.get("cost"):
        parts.append(f"${u['cost']:.2f}")
    if not short:
        parts.append(f"{u['turns']} turn{'s' if u['turns'] != 1 else ''}")
    return "  ·  ".join(parts)
