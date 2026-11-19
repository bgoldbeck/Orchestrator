"""Usage limits: Codex rollout parsing (exact), Claude rate-limit reports (exact) + log estimates, UI pills."""
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CLAUDE", str(tmp_path / "claude"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CODEX", str(tmp_path / "codex"))
    from orchestrator.storage.db import Database

    return Database(tmp_path / "t.db")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def write_rollout(tmp_path, primary=31.0, secondary=33.0, resets=None):
    d = tmp_path / "codex" / "sessions" / "2026" / "11" / "18"
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    resets = resets or now + 3600
    lines = [
        {"timestamp": iso(now - 60), "type": "session_meta", "payload": {"id": "x"}},
        {"timestamp": iso(now - 30), "type": "event_msg", "payload": {
            "type": "token_count", "info": None,
            "rate_limits": {"limit_id": "codex",
                            "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": resets},
                            "secondary": {"used_percent": secondary, "window_minutes": 10080,
                                          "resets_at": now + 5 * 86400},
                            "credits": {"has_credits": False}}}},
    ]
    (d / "rollout-2026-11-18T10-00-00-x.jsonl").write_text("\n".join(json.dumps(x) for x in lines), "utf-8")


def test_codex_usage_is_exact_from_rollout(db, tmp_path):
    from orchestrator.core.usage import CodexUsage

    write_rollout(tmp_path, primary=72.0, secondary=33.0)
    u = CodexUsage().read(db)
    five, week = u.window("five_hour"), u.window("weekly")
    assert five.used_percent == 72.0 and five.exact and five.label == "5-hour"
    assert five.remaining_pct == 28.0 and five.used_pct == 72.0  # Codex used_percent counts UP (consumed)
    assert week.used_percent == 33.0 and week.remaining_pct == 67.0 and week.exact
    assert five.resets_at > time.time()
    assert "exact" in u.source


def test_codex_window_already_reset_reads_zero(db, tmp_path):
    from orchestrator.core.usage import CodexUsage

    write_rollout(tmp_path, primary=95.0, resets=time.time() - 10)
    w = CodexUsage().read(db).window("five_hour")
    assert w.used_percent == 0.0 and w.remaining_pct == 100.0


def test_codex_no_logs(db):
    from orchestrator.core.usage import CodexUsage

    u = CodexUsage().read(db)
    assert all(w.used_percent is None for w in u.windows) and "No Codex usage" in u.note


def test_claude_rate_limit_event_maps_and_records(db, tmp_path):
    from orchestrator.core.events import RateLimit
    from orchestrator.core.usage import ClaudeUsage, record_claude_rate_limit
    from orchestrator.engines.claude import map_claude_message

    class RateLimitEvent:  # same shape as the SDK's message
        def __init__(self):
            self.rate_limit_info = SimpleNamespace(status="allowed_warning", resets_at=int(time.time()) + 7200,
                                                   rate_limit_type="five_hour", utilization=0.81)

    from orchestrator.engines.claude import ClaudeMapState

    evs = map_claude_message(RateLimitEvent(), ClaudeMapState())
    rl = next(e for e in evs if isinstance(e, RateLimit))
    assert rl.window == "five_hour" and rl.utilization == 0.81 and rl.status == "allowed_warning"
    record_claude_rate_limit(db, {"rate_limit_type": rl.window, "status": rl.status, "utilization": rl.utilization,
                                  "resets_at": rl.resets_at})
    # local logs give the estimate
    proj = tmp_path / "claude" / "demo"
    proj.mkdir(parents=True)
    now = time.time()
    rows = [{"type": "assistant", "timestamp": iso(now - 600), "message": {"id": "m1", "usage": {
        "input_tokens": 1000, "cache_creation_input_tokens": 500, "output_tokens": 300}}}]
    rows.append(dict(rows[0]))  # streamed duplicate: counted once
    (proj / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows), "utf-8")
    u = ClaudeUsage().read(db)
    five, week = u.window("five_hour"), u.window("weekly")
    assert five.exact and five.used_percent == 81.0 and five.status == "allowed_warning"
    assert five.remaining_pct == 19.0  # Claude utilization is the fraction consumed, so left = 100 - 81
    assert "1.8k" in five.estimate or "2k" in five.estimate
    assert not week.exact and week.used_percent is None and "tokens" in week.estimate


def test_claude_estimate_only(db):
    from orchestrator.core.usage import ClaudeUsage

    u = ClaudeUsage().read(db)
    assert not any(w.exact for w in u.windows) and "estimate" in u.note


def test_level_and_history(db):
    from orchestrator.core.usage import ProviderUsage, Window, history, level, record_history

    # level() takes percent LEFT: green > 30, amber 10-30, red < 10
    assert [level(x) for x in (None, 90, 30.1, 30, 10, 9.9, 0)] == \
        ["unknown", "green", "green", "amber", "amber", "red", "red"]
    u = ProviderUsage("codex", "Codex", [Window("five_hour", "5-hour", 10.0, exact=True)])
    record_history(db, [u])
    record_history(db, [u])  # unchanged: not repeated
    u.windows[0].used_percent = 20.0
    record_history(db, [u])
    assert [h["v"] for h in history(db, "codex", "five_hour")] == [90.0, 80.0]  # history is percent left


def test_both_directions_normalize_to_left():
    from orchestrator.core.usage import Window, tightest

    up = Window("five_hour", "5-hour", used_percent=31.0)  # a source that counts up (consumed)
    down = Window("weekly", "Weekly", left_percent=69.0)  # a source that counts down (remaining)
    assert up.remaining_pct == down.remaining_pct == 69.0
    assert up.used_pct == down.used_pct == 31.0
    assert Window("x", "X").remaining_pct is None
    assert Window("x", "X", used_percent=130).remaining_pct == 0.0
    assert tightest([up, Window("weekly", "Weekly", left_percent=12.0)]).remaining_pct == 12.0
    assert tightest([Window("x", "X")]) is None


def test_custom_provider_registers(db):
    from orchestrator.core import usage as U

    class Api(U.UsageProvider):
        id, name = "anthropic-api", "Anthropic API"

        def read(self, db):
            raise RuntimeError("no key")

    U.register(Api())
    try:
        items = {u.provider: u for u in U.read_all(db)}
        assert "Could not read usage" in items["anthropic-api"].note
    finally:
        U._REGISTRY[:] = [p for p in U._REGISTRY if p.id != "anthropic-api"]


def test_usage_bar_and_page(qapp, db, tmp_path):
    from orchestrator.ui.usage_view import UsageBar, UsagePage, tooltip_for

    write_rollout(tmp_path, primary=92.0, secondary=40.0)
    bar = UsageBar(db)
    bar.refresh()  # no running loop: reads synchronously
    pill = bar.pills["codex"]
    assert pill.usage.window("five_hour").remaining_pct == 8.0
    tip = tooltip_for(pill.usage)
    assert "5-hour: 92% used · 8% left" in tip and "resets" in tip and "Weekly: 40% used · 60% left" in tip
    hits = []
    bar.openUsage.connect(hits.append)
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    pill.resize(pill.sizeHint())
    pill.mouseReleaseEvent(QMouseEvent(QMouseEvent.MouseButtonRelease, QPointF(5, 5), QPointF(5, 5),
                                       Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    assert hits == ["codex"]
    pill.grab()  # paints without error
    page = UsagePage(db, bar)
    page.resize(900, 700)
    page.render()
    from qfluentwidgets import BodyLabel, StrongBodyLabel

    names = [w.text() for w in page.widget().findChildren(StrongBodyLabel)]
    assert "Codex" in names and "Claude" in names and "5-hour" in names
    lefts = [w.text() for w in page.widget().findChildren(BodyLabel) if w.objectName() == "leftLabel"]
    assert "8% left" in lefts and "60% left" in lefts and not any("used" in t for t in lefts)


def test_main_window_has_usage_bar(qapp, db, tmp_path):
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui.main_window import MainWindow

    write_rollout(tmp_path)
    win = MainWindow(db, SessionManager(db))
    assert win.usageBar is not None and win.usageBar.parent() is win.titleBar
    win.usageBar.openUsage.emit("codex")
    assert win.stackedWidget.currentWidget() is win.usage
    win.close()


def test_read_all_from_snapshot_on_worker_thread(db):
    import threading

    from orchestrator.core import usage as U

    U.record_claude_rate_limit(db, {"rate_limit_type": "seven_day", "status": "allowed", "utilization": 0.5,
                                    "resets_at": time.time() + 86400})
    snap = U.snapshot(db)
    out = []
    t = threading.Thread(target=lambda: out.extend(U.read_all(snap)))
    t.start()
    t.join()
    claude = next(u for u in out if u.provider == "claude")
    assert claude.window("weekly").remaining_pct == 50.0 and not claude.note.startswith("Could not")


def test_claude_account_usage_fetch_and_fallback(db, monkeypatch):
    import io
    import urllib.error

    from orchestrator.core import usage as U

    body = {"five_hour": {"utilization": 37.0, "resets_at": "2099-01-01T05:00:00+00:00"},
            "seven_day": {"utilization": 61.5, "resets_at": "2099-01-05T00:00:00Z"}}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    got = U.fetch_claude_oauth_usage([("tok", "app")], opener=lambda req, timeout: Resp(json.dumps(body).encode()))
    assert got["five_hour"]["used_percent"] == 37.0 and got["weekly"]["used_percent"] == 61.5
    assert got["weekly"]["resets_at"] > time.time()

    def forbidden(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))

    bad = U.fetch_claude_oauth_usage([("tok", "app")], opener=forbidden)
    assert "setup-token" in bad["error"]
    # the provider shows exact numbers from the saved answer, and the fallback note otherwise
    db.set_setting(U.CLAUDE_OAUTH_KEY, got)
    monkeypatch.setattr(U, "_oauth_cache", {})
    u = U.ClaudeUsage().read(db)
    five, week = u.window("five_hour"), u.window("weekly")
    assert five.exact and five.remaining_pct == 63.0 and week.remaining_pct == 38.5 and "account" in u.source
    db.set_setting(U.CLAUDE_OAUTH_KEY, bad)
    monkeypatch.setattr(U, "_oauth_cache", {})
    u = U.ClaudeUsage().read(db)
    assert not u.window("five_hour").exact and "/login" in u.note


def test_claude_login_helper(tmp_path, monkeypatch):
    import json

    from orchestrator.core import claude_login as L

    cred = tmp_path / ".credentials.json"
    cred.write_text(json.dumps({"mcpOAuth": {}}))
    assert not L.has_cli_login(cred)
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    assert L.has_cli_login(cred)
    calls = []
    monkeypatch.setattr(L, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    assert L.launch_login(popen=lambda cmd, **kw: calls.append((cmd, kw))) == ""
    cmd, kw = calls[0]
    assert cmd[-2:] == ["/bin/claude", "/login"] and "CLAUDE_CODE_OAUTH_TOKEN" not in kw["env"]
    monkeypatch.setattr(L, "claude_cli", lambda: None)
    assert "Couldn't find" in L.launch_login(popen=lambda *a, **k: None)


def test_force_next_fetch_and_needs_login(tmp_path, monkeypatch):
    from orchestrator.core import usage as U
    from orchestrator.storage.db import Database

    db = Database(tmp_path / "t.db")
    monkeypatch.delenv("ORCHESTRATOR_NO_USAGE_FETCH", raising=False)
    calls = []

    def fake():
        calls.append(1)
        return {"at": 1e12, "five_hour": {"used_percent": 10.0, "resets_at": None}}

    monkeypatch.setattr(U, "fetch_claude_oauth_usage", fake)
    monkeypatch.setattr(U, "_oauth_cache", {})
    U.claude_oauth_usage(db)
    U.claude_oauth_usage(db)
    assert len(calls) == 1  # cached
    U.force_next_fetch()
    U.claude_oauth_usage(db)
    assert len(calls) == 2
    u = U.ClaudeUsage(root=tmp_path).read(db)
    assert not u.needs_login and u.window("five_hour").exact
    monkeypatch.setattr(U, "_oauth_cache", {"at": 1e12, "error": "403"})
    monkeypatch.setenv("ORCHESTRATOR_NO_USAGE_FETCH", "1")
    assert U.ClaudeUsage(root=tmp_path).read(db).needs_login
