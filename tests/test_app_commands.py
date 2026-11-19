"""App commands from chat: model-name resolution, natural times, each tool, undo, confirmations, fast path, wiring."""
import asyncio
import os
import time
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import app_control as AC  # noqa: E402
from orchestrator.core import preferences as P  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    from orchestrator.storage.db import Database
    from orchestrator.ui import appearance as A

    db = Database(tmp_path / "t.db")
    db.set_setting(P.CAPS_KEY, {"codex": True, "checked_at": 9e12})
    proj = db.add_project(str(tmp_path), "weather-app")
    sid = db.create_session(proj.id, "claude", "claude-sonnet-5-5", "medium", title="chat").id

    class Runner:
        def __init__(self):
            self.synced, self.removed = [], []

        def sync(self, s):
            self.synced.append(s.id)

        def remove(self, i):
            self.removed.append(i)

        def next_run(self, i):
            return None

    runner = Runner()
    ctl = AC.AppControl(db, runner)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield db, ctl, sid, runner, loop
    A.apply(A.Look(), emit=False)
    loop.close()


def run(loop, coro):
    return loop.run_until_complete(coro)


def flush():
    """Process pending deleteLater() calls (re-rendered chats delete their old widgets)."""
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


# ---- names and times
def test_resolve_model_fuzzy(env):
    db = env[0]
    assert AC.resolve_model(db, "opus 5.5") == ("claude", "claude-opus-5-5")
    assert AC.resolve_model(db, "Opus") == ("claude", "claude-opus-5-5")
    assert AC.resolve_model(db, "gpt 6.2 sol") == ("codex", "gpt-6.2-sol")
    assert AC.resolve_model(db, "GPT-6.2 Astra") == ("codex", "gpt-6.2-astra")
    assert AC.resolve_model(db, "claude-haiku-5-5") == ("claude", "claude-haiku-5-5")
    with pytest.raises(AC.AppCommandError, match="could mean"):
        AC.resolve_model(db, "gpt 6.2")  # Sol or Astra
    db.set_model_enabled("codex", "gpt-6.2-astra", False)
    assert AC.resolve_model(db, "gpt 6.2") == ("codex", "gpt-6.2-sol")  # only one enabled
    with pytest.raises(AC.AppCommandError, match="disabled"):
        AC.resolve_model(db, "astra")
    with pytest.raises(AC.AppCommandError, match="No model matches"):
        AC.resolve_model(db, "llama 9")


def test_parse_when_and_repeat():
    now = datetime(2026, 11, 18, 14, 0)  # Wednesday
    assert AC.parse_when("in 5 hours", now) == datetime(2026, 11, 18, 19, 0)
    assert AC.parse_when("in 30 minutes", now) == datetime(2026, 11, 18, 14, 30)
    assert AC.parse_when("tomorrow 8am", now) == datetime(2026, 11, 19, 8, 0)
    assert AC.parse_when("tomorrow", now) == datetime(2026, 11, 19, 8, 0)
    assert AC.parse_when("tonight", now) == datetime(2026, 11, 18, 21, 0)
    assert AC.parse_when("tonight at 10", now) == datetime(2026, 11, 18, 22, 0)
    assert AC.parse_when("at 6:30pm", now) == datetime(2026, 11, 18, 18, 30)
    assert AC.parse_when("at 9am", now) == datetime(2026, 11, 19, 9, 0)  # already past today
    assert AC.parse_when("friday 9:30am", now) == datetime(2026, 11, 20, 9, 30)
    assert AC.parse_when("next wednesday", now) == datetime(2026, 11, 25, 9, 0)
    with pytest.raises(AC.AppCommandError):
        AC.parse_when("whenever", now)
    assert AC.parse_repeat("every day at 8am") == "0 8 * * *"
    assert AC.parse_repeat("weekdays 9:30") == "30 9 * * mon-fri"
    assert AC.parse_repeat("every monday and friday at 7pm") == "0 19 * * mon,fri"
    assert AC.parse_repeat("every 2 hours") == "0 */2 * * *"
    assert AC.parse_repeat("0 3 * * sun") == "0 3 * * sun"
    with pytest.raises(AC.AppCommandError):
        AC.parse_repeat("sometimes")


# ---- tools + undo
def test_set_model_preference_add_update_undo(env):
    db, ctl, sid, runner, loop = env
    seen = []
    ctl.applied.connect(seen.append)
    res = run(loop, ctl.handle(sid, "set_model_preference",
                               {"task_type": "design and planning", "model": "opus 5.5", "effort": "medium"}))
    assert res["applied"] == "Added preference: Design and planning → Claude Opus 5.5 · medium"
    r = next(x for x in P.rules(db) if x.name == "Design and planning")
    assert r.model == "claude-opus-5-5" and "design" in r.keywords and "planning" in r.keywords
    assert seen and seen[0].sid == sid
    assert any(m["kind"] == "app_change" for m in db.messages(sid))  # part of the chat history
    res2 = run(loop, ctl.handle(sid, "set_model_preference",
                                {"task_type": "Complex coding", "model": "gpt 6.2 sol", "effort": "medium"}))
    assert res2["applied"].startswith("Updated preference: Complex coding → Codex GPT-6.2 Sol")
    ctl.undo(res2["change_id"])
    assert next(x for x in P.rules(db) if x.name == "Complex coding").model == "claude-opus-5-5"
    with pytest.raises(AC.AppCommandError):
        ctl.undo(res2["change_id"])  # only once
    with pytest.raises(AC.AppCommandError, match="no effort"):
        run(loop, ctl.handle(sid, "set_model_preference", {"task_type": "x", "model": "haiku", "effort": "max"}))
    lst = run(loop, ctl.handle(sid, "list_preferences", {}))
    assert any(x["task_type"] == "Design and planning" and x["active"] for x in lst["rules"])


def test_set_appearance_relative_and_undo(env):
    db, ctl, sid, runner, loop = env
    from orchestrator.ui import appearance as A

    res = run(loop, ctl.handle(sid, "set_appearance", {"font_size": "bigger", "chat_font_size": "bigger"}))
    look = A.load(db)
    assert look.ui_size == 10.5 and look.chat_size == 11.0
    assert "Font size 10 → 10.5 pt" in res["applied"]
    run(loop, ctl.handle(sid, "set_appearance", {"theme": "light", "density": "compact", "accent": "violet"}))
    assert A.load(db).theme == "Light" and A.load(db).accent == "Violet"
    ctl.undo(res["change_id"])
    assert A.load(db).ui_size == 10.0  # undo restores the snapshot taken before that change
    with pytest.raises(AC.AppCommandError):
        run(loop, ctl.handle(sid, "set_appearance", {"theme": "purple"}))
    run(loop, ctl.handle(sid, "set_appearance", {"chat_font_size": "+2"}))
    assert A.load(db).chat_size == 12.5  # 10.5 (restored by the undo) + 2


def test_create_list_cancel_schedule_with_confirm(env):
    db, ctl, sid, runner, loop = env
    res = run(loop, ctl.handle(sid, "create_schedule", {"prompt": "Run x_y_z", "when": "in 5 hours"}))
    s = db.schedule(res["schedule_id"])
    assert s.kind == "once" and s.model == "claude-sonnet-5-5" and s.project_id == db.session(sid).project_id
    assert runner.synced == [s.id] and res["applied"].startswith("Run once chat “Run x_y_z”")
    res2 = run(loop, ctl.handle(sid, "create_schedule", {"prompt": "Digest", "repeat": "weekdays 9am",
                                                        "model": "gpt 6.2 sol", "target": "orchestration"}))
    s2 = db.schedule(res2["schedule_id"])
    assert s2.kind == "cron" and s2.cron == "0 9 * * mon-fri" and s2.engine == "codex" and s2.target == "orchestration"
    assert len(run(loop, ctl.handle(sid, "list_schedules", {}))["schedules"]) == 2
    ctl.undo(res2["change_id"])  # undo a create = delete it
    assert db.schedule(s2.id) is None
    # delete asks first: deny, then allow
    answers = iter([False, True])
    ctl.confirm_requested.connect(lambda _sid, _t, fut: fut.set_result(next(answers)))
    denied = run(loop, ctl.handle(sid, "cancel_schedule", {"name": "x_y_z"}))
    assert denied["denied"] and db.schedule(s.id)
    done = run(loop, ctl.handle(sid, "cancel_schedule", {"schedule_id": s.id}))
    assert db.schedule(s.id) is None and done["applied"] == "Deleted schedule “Run x_y_z”"
    ctl.undo(done["change_id"])  # restored
    assert any(x.name == "Run x_y_z" for x in db.schedules())


def test_confirm_without_ui_refuses(env):
    db, ctl, sid, runner, loop = env
    res = run(loop, ctl.handle(sid, "set_setting", {"name": "model_enabled", "value": "false", "model": "haiku"}))
    assert res["denied"] and next(m for m in db.models("claude") if m.id == "claude-haiku-5-5").enabled


def test_set_setting_allow_list_and_models(env):
    db, ctl, sid, runner, loop = env
    res = run(loop, ctl.handle(sid, "set_setting", {"name": "global_max_parallel", "value": "3"}))
    assert db.get_setting("global.max_parallel") == 3 and "→ 3" in res["applied"]
    ctl.undo(res["change_id"])
    assert db.get_setting("global.max_parallel") is None
    with pytest.raises(AC.AppCommandError, match="between"):
        run(loop, ctl.handle(sid, "set_setting", {"name": "global_max_parallel", "value": "99"}))
    with pytest.raises(AC.AppCommandError, match="can't be changed"):
        run(loop, ctl.handle(sid, "set_setting", {"name": "codex.sandbox", "value": "danger-full-access"}))
    ctl.confirm_requested.connect(lambda _sid, _t, fut: fut.set_result(True))
    res = run(loop, ctl.handle(sid, "set_setting", {"name": "model_enabled", "value": "off", "model": "haiku 5.5"}))
    assert res["applied"] == "Disabled Haiku 5.5"
    assert not next(m for m in db.models("claude") if m.id == "claude-haiku-5-5").enabled
    ctl.undo(res["change_id"])
    assert next(m for m in db.models("claude") if m.id == "claude-haiku-5-5").enabled
    # enabling needs no confirmation
    db.set_model_enabled("codex", "gpt-6-luna", False)
    ctl.confirm_requested.disconnect()
    res = run(loop, ctl.handle(sid, "set_setting", {"name": "model_enabled", "value": "on", "model": "luna"}))
    assert res["applied"] == "Enabled GPT-6 Luna"
    assert "model_enabled" in run(loop, ctl.handle(sid, "list_settings", {}))


# ---- fast path
def test_detect_simple_commands():
    assert AC.detect("increase the font size") == ("set_appearance", {"font_size": "bigger", "chat_font_size": "bigger"})
    assert AC.detect("Increase the font size in this app.") == ("set_appearance",
                                                               {"font_size": "bigger", "chat_font_size": "bigger"})
    assert AC.detect("make the chat font smaller") == ("set_appearance", {"chat_font_size": "smaller"})
    assert AC.detect("set code font size to 11") == ("set_appearance", {"code_font_size": "11"})
    assert AC.detect("dark mode") == ("set_appearance", {"theme": "dark"})
    assert AC.detect("switch to light theme") == ("set_appearance", {"theme": "light"})
    assert AC.detect("use compact") == ("set_appearance", {"density": "compact"})
    assert AC.detect("increase the font size in the README and fix the header") is None
    assert AC.detect("why is the font size small in my CSS?") is None


def test_chat_fast_path_card_and_undo(env):
    db, ctl, sid, runner, loop = env
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui import appearance as A
    from orchestrator.ui.chat_view import ChatView
    from orchestrator.ui.chat_widgets import AppliedCard, ConfirmCard

    mgr = SessionManager(db)
    sent = []
    mgr.send = lambda s, t, *a, **k: sent.append(t)
    view = ChatView(db, mgr)
    view.set_app_control(ctl)
    view.show_session(sid)
    view._send("increase the font size")
    assert sent == []  # no model call
    assert A.load(db).ui_size == 10.5
    cards = view.column.findChildren(AppliedCard)
    assert cards and cards[-1].label.text().startswith("Applied: Font size 10 → 10.5 pt")
    cards[-1].undoBtn.click()
    flush()  # undoing the font size re-renders the chat
    live = view.column.findChildren(AppliedCard)
    assert A.load(db).ui_size == 10.0 and len(live) == 1 and live[0].label.text().startswith("Undone:")
    view._send("refactor the parser")
    assert sent == ["refactor the parser"]
    # history shows the change (undone) after a re-render
    view.show_session(sid)
    flush()
    hist = view.column.findChildren(AppliedCard)
    assert len(hist) == 1 and hist[0].label.text().startswith("Undone:")
    # a destructive change asked from this chat shows Allow / Deny here
    fut = loop.create_future()
    view.show_confirm("Delete the schedule “x”?", fut)
    card = view.column.findChildren(ConfirmCard)[-1]
    card.allowBtn.click()
    assert fut.result() is True


def test_every_chat_gets_app_server(env):
    db, ctl, sid, runner, loop = env
    from orchestrator.core.session_manager import SessionManager

    mgr = SessionManager(db)
    mgr.mcp_providers.append(ctl.mcp_for)
    mcp = mgr.mcp_for(sid)
    assert "app" in mcp and mcp["app"]["env"]["ORCH_SERVER"] == "app"
    assert mcp["app"]["env"]["ORCH_ORCHESTRATION_ID"] == str(sid)
    mgr.mcp_provider = lambda s: {"orchestrator": {"command": "x", "args": [], "env": {}}}
    assert set(mgr.mcp_for(sid)) == {"orchestrator", "app"}
    db.set_setting("chat.app_tools", False)
    assert set(mgr.mcp_for(sid)) == {"orchestrator"}
    from orchestrator.engines.claude import claude_mcp_options
    from orchestrator.engines.codex import codex_mcp_overrides

    db.set_setting("chat.app_tools", True)
    servers, _env, allowed = claude_mcp_options(ctl.mcp_for(sid))
    assert "app" in servers and allowed == ["mcp__app"]
    assert any("mcp_servers.app.default_tools_approval_mode='approve'" in x for x in codex_mcp_overrides(ctl.mcp_for(sid)))


def test_app_mcp_server_tools(env):
    from orchestrator.mcp_server import build_app_server

    server = build_app_server()

    async def names():
        return sorted(t.name for t in await server.list_tools())

    assert run(env[4], names()) == ["about_app", "cancel_schedule", "create_schedule", "get_usage", "list_models",
                                    "list_preferences", "list_schedules", "list_settings", "set_appearance",
                                    "set_default_model", "set_model_preference", "set_setting"]


def test_bridge_round_trip(env):
    """The MCP server's bridge_call reaches AppControl.handle over the local socket."""
    db, ctl, sid, runner, loop = env
    import json

    from orchestrator import mcp_server

    ctl.bridge.start()
    cfg = ctl.mcp_for(sid)["app"]["env"]
    old = {k: os.environ.get(k) for k in cfg}
    os.environ.update(cfg)
    try:
        async def call():
            return await mcp_server.bridge_call("set_appearance", {"density": "compact"}, timeout=10)

        res = loop.run_until_complete(call())
        assert res["applied"] == "Density → Compact"
        assert json.loads(json.dumps(res))
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        ctl.bridge.stop()


def test_about_usage_models_and_default(env, monkeypatch):
    from orchestrator.core import usage as U

    db, ctl, sid, runner, loop = env
    about = run(loop, ctl.handle(sid, "about_app", {}))
    assert about["app"] == "Orchestrator" and "Orchestrate" in about["about"]
    assert about["this_chat"]["project"] == "weather-app" and about["this_chat"]["engine"] == "claude"
    assert about["source_folder"].endswith(("orchestrator", "Orchestrator")) or "source" in about["source_folder"]
    db.add_message(sid, "system", "usage", {"input": 1000, "output": 200, "cached": 5000, "cost": 0.05})
    monkeypatch.setattr(U, "read_all", lambda _db: [U.ProviderUsage("claude", "Claude", [
        U.Window("five_hour", "5-hour", used_percent=30, resets_at=time.time() + 7200)])])
    use = run(loop, ctl.handle(sid, "get_usage", {"days": 7}))
    w = use["plan_limits"][0]["windows"][0]
    assert w["percent_left"] == 70 and w["resets"].startswith("in 1 h") or w["resets"].startswith("in 2 h")
    spend = use["spend_last_7_days"][0]
    assert spend["model"] == "claude-sonnet-5-5" and spend["input"] == 1000 and spend["cost_usd"] == 0.05
    models = run(loop, ctl.handle(sid, "list_models", {}))
    assert "claude" in models["engines"] and models["engines"]["claude"]["models"]
    res = run(loop, ctl.handle(sid, "set_default_model", {"model": "haiku", "effort": "low"}))
    assert db.default_model("claude") == ("claude-haiku-5-5", "low") and db.get_setting("last")[1] == "claude-haiku-5-5"
    ctl.undo(res["change_id"])
    assert db.default_model("claude")[0] != "claude-haiku-5-5"


def test_planner_prompt_knows_the_app():
    from orchestrator.core import orchestration as O
    from orchestrator.core.prompt_display import split_prompt

    full = O.ORCH_PROMPT.format(path="C:/x", goal="Ship it", max_parallel=4, models="-", guide="")
    assert "inside Orchestrator" in full and "get_usage" in full and "set_model_preference" in full
    assert split_prompt(full)[0] == "Ship it"
