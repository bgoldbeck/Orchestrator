"""Plan usage limits per provider (5-hour and weekly windows), behind a small plug-in interface.

Add a provider by subclassing ``UsageProvider`` and calling ``register()``; the title-bar pills and the Usage page
show every registered provider.

What is exact and what is estimated:
- Codex: exact. Codex writes ``rate_limits`` (primary = 5-hour window, secondary = weekly; ``used_percent``,
  ``window_minutes``, ``resets_at``) into its session logs (~/.codex/sessions/**/rollout-*.jsonl) on every turn,
  including turns this app runs with ``codex exec``. We read the newest one.
- Claude: exact only when the Claude Code CLI reports it. The Agent SDK emits a ``rate_limit_event`` (status,
  utilization 0-1, resets_at, window type: five_hour / seven_day / …) during our Claude turns; the latest per
  window is stored here. Between those, or when the CLI omits utilization, the page shows an estimate: tokens
  used in the window, summed from the local Claude Code logs (~/.claude/projects/**/*.jsonl). There is no plan
  size in those logs, so the estimate is a token count, not a percentage.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.storage.db import Database

FIVE_HOURS = 5 * 3600
WEEK = 7 * 86400


@dataclass
class Window:
    """One limit window. A provider fills in whichever its source reports: ``used_percent`` (Codex
    ``used_percent``, Claude ``utilization``) or ``left_percent`` (a source that counts down). The UI only reads
    ``remaining_pct`` / ``used_pct``, so every provider shows the same convention: percent left."""

    key: str  # five_hour | weekly
    label: str  # "5-hour" | "Weekly"
    used_percent: float | None = None  # 0-100 consumed, None when unknown
    resets_at: float | None = None
    exact: bool = False
    status: str = ""  # allowed | allowed_warning | rejected (Claude) or ""
    estimate: str = ""  # e.g. "≈ 1.2M tokens in this window (local logs)"
    updated_at: float | None = None
    left_percent: float | None = None  # 0-100 remaining, for sources that report what is left

    @property
    def remaining_pct(self) -> float | None:
        if self.left_percent is not None:
            return max(0.0, min(100.0, float(self.left_percent)))
        if self.used_percent is not None:
            return max(0.0, min(100.0, 100.0 - float(self.used_percent)))
        return None

    @property
    def used_pct(self) -> float | None:
        left = self.remaining_pct
        return None if left is None else 100.0 - left


@dataclass
class ProviderUsage:
    provider: str
    name: str
    windows: list[Window] = field(default_factory=list)
    source: str = ""
    note: str = ""
    updated_at: float | None = None
    needs_login: bool = False  # Claude: exact plan usage needs the Claude Code CLI sign-in

    def window(self, key: str) -> Window | None:
        return next((w for w in self.windows if w.key == key), None)


class UsageProvider:
    """Interface: one AI provider's plan limits.

    ``read`` may run on a worker thread: it gets a ``SettingsSnapshot`` (only ``get_setting`` for the keys listed
    in ``settings_keys``), never the live database.
    """

    id = ""
    name = ""
    settings_keys: tuple[str, ...] = ()

    def read(self, db: Database) -> ProviderUsage:  # pragma: no cover - interface
        raise NotImplementedError


_REGISTRY: list[UsageProvider] = []


def register(provider: UsageProvider) -> None:
    if all(p.id != provider.id for p in _REGISTRY):
        _REGISTRY.append(provider)


def providers() -> list[UsageProvider]:
    return list(_REGISTRY)


class SettingsSnapshot:
    """Read-only copy of the settings providers need, safe to hand to a worker thread."""

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def snapshot(db: Database) -> SettingsSnapshot:
    """Take on the GUI thread (SQLite objects stay on the thread that made them)."""
    keys = {k for p in providers() for k in p.settings_keys}
    return SettingsSnapshot({k: db.get_setting(k) for k in keys})


def read_all(db: Database | SettingsSnapshot) -> list[ProviderUsage]:
    out = []
    for p in providers():
        try:
            out.append(p.read(db))
        except Exception as exc:  # noqa: BLE001 - one broken source must not hide the others
            out.append(ProviderUsage(p.id, p.name, note=f"Could not read usage: {exc}"))
    return out


def level(left: float | None) -> str:
    """green / amber / red for a percent LEFT: green above 30% left, amber 10-30%, red under 10%."""
    if left is None:
        return "unknown"
    return "red" if left < 10 else "amber" if left <= 30 else "green"


def tightest(windows: list[Window]) -> Window | None:
    """The window with the least left (what limits you first)."""
    known = [w for w in windows if w.remaining_pct is not None]
    return min(known, key=lambda w: w.remaining_pct) if known else None


# ------------------------------------------------------------------ Codex (exact)

def codex_home() -> Path:
    from orchestrator.core.indexer import codex_home as home  # same overrides as the indexer (tests / demos)

    return home()


def _tail_lines(path: Path, max_bytes: int = 512_000) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines  # first line may be cut


def latest_codex_rate_limits(home: Path | None = None, files: int = 8) -> tuple[dict, float] | None:
    """The newest ``rate_limits`` object from Codex session logs, with the log line's time."""
    root = (home or codex_home()) / "sessions"
    if not root.is_dir():
        return None
    logs = sorted((p for p in root.rglob("rollout-*.jsonl") if p.is_file()), key=lambda p: p.stat().st_mtime,
                  reverse=True)[:files]
    for p in logs:
        for line in reversed(_tail_lines(p)):
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            payload = d.get("payload") or {}
            rl = payload.get("rate_limits") or (payload.get("info") or {}).get("rate_limits")
            if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")):
                ts = d.get("timestamp")
                when = p.stat().st_mtime
                if isinstance(ts, str):
                    try:
                        from datetime import datetime

                        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                return rl, when
    return None


def _codex_window(raw: dict | None, key: str, label: str, seen: float) -> Window:
    if not isinstance(raw, dict):
        return Window(key, label)
    resets = raw.get("resets_at")
    if resets is None and raw.get("resets_in_seconds") is not None:
        resets = seen + float(raw["resets_in_seconds"])
    pct = raw.get("used_percent")
    if resets and float(resets) < time.time():  # that window has reset since: nothing used yet in the new one
        pct = 0.0
    return Window(key, label, None if pct is None else float(pct), float(resets) if resets else None, exact=True,
                  updated_at=seen)


class CodexUsage(UsageProvider):
    id = "codex"
    name = "Codex"

    def __init__(self, home: Path | None = None) -> None:
        self.home = home

    def read(self, db: Database) -> ProviderUsage:
        got = latest_codex_rate_limits(self.home)
        if not got:
            return ProviderUsage(self.id, self.name, [Window("five_hour", "5-hour"), Window("weekly", "Weekly")],
                                 source="Codex session logs", note="No Codex usage seen yet. It appears after a "
                                 "Codex turn (here or in the Codex CLI).")
        rl, seen = got
        five = _codex_window(rl.get("primary"), "five_hour", "5-hour", seen)
        week = _codex_window(rl.get("secondary"), "weekly", "Weekly", seen)
        for w, raw in ((five, rl.get("primary")), (week, rl.get("secondary"))):
            mins = (raw or {}).get("window_minutes")
            if mins and w.key == "five_hour" and int(mins) != 300:
                w.label = f"{int(mins) // 60}-hour"
        note = ""
        credits = rl.get("credits") or {}
        if credits.get("has_credits"):
            note = f"Credits balance: {credits.get('balance')}"
        return ProviderUsage(self.id, self.name, [five, week], source="Codex session logs (exact)", note=note,
                             updated_at=seen)


# ------------------------------------------------------------------ Claude (exact when reported, else estimate)

CLAUDE_KEY = "usage.claude.rate_limits"  # {window type: {status, utilization, resets_at, at}}
CLAUDE_OAUTH_KEY = "usage.claude.oauth"  # last answer of the account usage endpoint: {at, five_hour, seven_day, error}
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_EVERY = 300  # seconds between fetches
OAUTH_WINDOWS = {"five_hour": "five_hour", "seven_day": "weekly"}
CLAUDE_WINDOWS = {"five_hour": ("five_hour", "5-hour"), "seven_day": ("weekly", "Weekly")}


def record_claude_rate_limit(db: Database, info: dict[str, Any]) -> None:
    """Store a rate-limit report from the Claude Code CLI (one per window type)."""
    kind = str(info.get("rate_limit_type") or info.get("rateLimitType") or "five_hour")
    data = dict(db.get_setting(CLAUDE_KEY) or {})
    util = info.get("utilization")
    data[kind] = {"status": info.get("status") or "", "utilization": util,
                  "resets_at": info.get("resets_at") or info.get("resetsAt"), "at": time.time()}
    db.set_setting(CLAUDE_KEY, data)


def claude_oauth_tokens() -> list[tuple[str, str]]:
    """(token, where) pairs to try: the token the app runs Claude with, then the Claude Code CLI login."""
    out: list[tuple[str, str]] = []
    try:
        from orchestrator.core import auth

        tok, src = auth.resolve(auth.CLAUDE_TOKEN)
        if tok:
            out.append((tok, src or "app"))
    except Exception:  # noqa: BLE001 - no keyring etc.
        pass
    try:
        data = json.loads((Path.home() / ".claude" / ".credentials.json").read_text(encoding="utf-8"))
        tok = (data.get("claudeAiOauth") or {}).get("accessToken")
        if tok and all(tok != t for t, _ in out):
            out.append((tok, "Claude Code login"))
    except (OSError, ValueError):
        pass
    return out


def _iso_ts(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        from datetime import datetime

        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse_oauth_usage(body: dict[str, Any], at: float) -> dict[str, Any]:
    """{window key: {used_percent, resets_at}} from the usage endpoint (utilization is 0-100)."""
    out: dict[str, Any] = {"at": at}
    for src, key in OAUTH_WINDOWS.items():
        w = body.get(src)
        if isinstance(w, dict) and w.get("utilization") is not None:
            out[key] = {"used_percent": float(w["utilization"]), "resets_at": _iso_ts(w.get("resets_at"))}
    return out


def fetch_claude_oauth_usage(tokens: list[tuple[str, str]] | None = None, opener=None) -> dict[str, Any]:
    """Ask the account usage endpoint (what Claude Code's /usage shows). Never raises; errors land in "error"."""
    import urllib.error
    import urllib.request

    now = time.time()
    toks = claude_oauth_tokens() if tokens is None else tokens
    if not toks:
        return {"at": now, "error": "No Claude sign-in found (Settings › Accounts)."}
    errors = []
    for tok, where in toks:
        req = urllib.request.Request(OAUTH_USAGE_URL, headers={
            "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json", "User-Agent": "Orchestrator"})
        try:
            with (opener or urllib.request.urlopen)(req, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8") or "{}")
            got = parse_oauth_usage(body, now)
            if len(got) > 1:
                got["source"] = where
                return got
            errors.append(f"{where}: no usage in the answer")
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                errors.append(f"{where}: this token can't read plan usage (a `claude setup-token` token only runs "
                              "chats)")
            elif exc.code == 401:
                errors.append(f"{where}: sign-in expired (open Claude Code once to refresh it)")
            else:
                errors.append(f"{where}: HTTP {exc.code}")
        except (OSError, ValueError) as exc:
            errors.append(f"{where}: {exc}")
    return {"at": now, "error": "; ".join(errors)}


_oauth_cache: dict[str, Any] = {}


_force = {"next": False}


def force_next_fetch() -> None:
    """The next read asks the account endpoint again instead of using the cached answer."""
    _force["next"] = True


def claude_oauth_usage(db, force: bool = False) -> dict[str, Any]:
    """Cached endpoint answer: memory, else the last one saved in settings; refetched every OAUTH_EVERY s."""
    global _oauth_cache
    if not _oauth_cache:
        _oauth_cache = dict(db.get_setting(CLAUDE_OAUTH_KEY) or {})
    if os.environ.get("ORCHESTRATOR_NO_USAGE_FETCH"):
        return _oauth_cache
    if force or _force["next"] or time.time() - float(_oauth_cache.get("at") or 0) > OAUTH_EVERY:
        _force["next"] = False
        got = fetch_claude_oauth_usage()
        if "error" in got and any(k in _oauth_cache for k in OAUTH_WINDOWS.values()):
            keep = {k: v for k, v in _oauth_cache.items() if k in OAUTH_WINDOWS.values() or k == "source"}
            got = {**keep, **got}  # keep the last good numbers, try again later
        _oauth_cache = got
    return _oauth_cache


def persist(db: Database) -> None:
    """Save what workers fetched (call on the GUI thread)."""
    if _oauth_cache and db.get_setting(CLAUDE_OAUTH_KEY) != _oauth_cache:
        db.set_setting(CLAUDE_OAUTH_KEY, _oauth_cache)


def claude_root() -> Path:
    from orchestrator.core.indexer import claude_root as root

    return root()


def claude_tokens_since(since: float, root: Path | None = None) -> int:
    """Input (incl. cache writes) + output tokens of Claude Code turns since `since`, from local logs."""
    from datetime import datetime

    base = root or claude_root()
    if not base.is_dir():
        return 0
    seen: set[str] = set()
    total = 0
    for p in base.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < since:
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    ts = d.get("timestamp")
                    if not isinstance(ts, str):
                        continue
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if when < since:
                        continue
                    msg = d.get("message") or {}
                    mid = str(msg.get("id") or d.get("requestId") or d.get("uuid") or "")
                    if mid in seen:  # streamed messages repeat the same usage
                        continue
                    seen.add(mid)
                    u = msg.get("usage") or {}
                    total += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) \
                        + int(u.get("output_tokens") or 0)
        except OSError:
            continue
    return total


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


class ClaudeUsage(UsageProvider):
    id = "claude"
    name = "Claude"
    settings_keys = (CLAUDE_KEY, CLAUDE_OAUTH_KEY)

    def __init__(self, root: Path | None = None, fetch: bool = True) -> None:
        self.root = root
        self.fetch = fetch

    def read(self, db: Database) -> ProviderUsage:
        data = db.get_setting(CLAUDE_KEY) or {}
        acct = claude_oauth_usage(db) if self.fetch else (db.get_setting(CLAUDE_OAUTH_KEY) or {})
        now = time.time()
        windows = []
        for kind, (key, label) in CLAUDE_WINDOWS.items():
            w = Window(key, label)
            rep = data.get(kind)
            got = acct.get(key)
            if got and not (got.get("resets_at") and got["resets_at"] < now):
                w.used_percent = round(float(got["used_percent"]), 1)
                w.resets_at = got.get("resets_at")
                w.exact = True
                w.updated_at = acct.get("at")
                if rep:
                    w.status = rep.get("status") or ""
            elif rep:
                resets = rep.get("resets_at")
                w.resets_at = float(resets) if resets else None
                w.status = rep.get("status") or ""
                w.updated_at = rep.get("at")
                util = rep.get("utilization")
                if util is not None and not (w.resets_at and w.resets_at < now):
                    w.used_percent = round(float(util) * 100, 1)
                    w.exact = True
                elif w.resets_at and w.resets_at < now:
                    w.resets_at = None  # that window is over; a new one starts with the next turn
            span = FIVE_HOURS if key == "five_hour" else WEEK
            start = (w.resets_at - span) if (w.resets_at and w.resets_at > now) else now - span
            tokens = claude_tokens_since(start, self.root)
            w.estimate = f"≈ {fmt_tokens(tokens)} tokens in this window (from local Claude Code logs)"
            windows.append(w)
        exact = any(w.exact for w in windows)
        from_acct = any(k in acct for k in OAUTH_WINDOWS.values())
        note = ""
        if not exact:
            err = acct.get("error") or ""
            note = ("Couldn't read your Claude plan usage" + (f" ({err})" if err else "") + ". Showing token "
                    "estimates from local logs, and exact numbers whenever Claude reports them during a turn. "
                    "For exact numbers all the time, sign in to Claude Code once: Usage › Sign in to Claude, or run "
                    "`claude` in a terminal and type /login. "
                    "Chats keep using your current token.")
        return ProviderUsage(
            self.id, self.name, windows,
            source=("Claude account usage (exact, every 5 min)" if from_acct else "Claude Code rate-limit reports")
            + " + local logs (estimate)",
            note=note,
            updated_at=max((w.updated_at or 0) for w in windows) or None,
            needs_login=not from_acct)


register(CodexUsage())
register(ClaudeUsage())


# ------------------------------------------------------------------ history

HISTORY_KEY = "usage.history.left"  # samples of percent LEFT


def record_history(db: Database, items: list[ProviderUsage]) -> None:
    """Keep a small history of percent left (one sample per provider/window when it changes, max 500)."""
    hist = list(db.get_setting(HISTORY_KEY) or [])
    last = {(h["p"], h["w"]): h for h in hist}
    now = time.time()
    for u in items:
        for w in u.windows:
            left = w.remaining_pct
            if left is None:
                continue
            prev = last.get((u.provider, w.key))
            if prev and prev["v"] == left and now - prev["t"] < 3600:
                continue
            h = {"p": u.provider, "w": w.key, "v": left, "t": now, "r": w.resets_at}
            hist.append(h)
            last[(u.provider, w.key)] = h
    db.set_setting(HISTORY_KEY, hist[-500:])


def history(db: Database, provider: str, window: str) -> list[dict]:
    return [h for h in db.get_setting(HISTORY_KEY) or [] if h["p"] == provider and h["w"] == window]
