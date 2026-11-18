"""Small text helpers shared by core and UI (no Qt imports)."""
from __future__ import annotations

import time


def short_title(text: str, n: int = 48) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


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
