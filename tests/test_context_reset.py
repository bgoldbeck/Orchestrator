"""Context reset points: settings, size tracking, when a reset is due, and the reset flow in SessionManager."""
import asyncio
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import context_reset as R  # noqa: E402
from orchestrator.core.events import Compacted, ContextInfo, Done, Started, Text, Usage  # noqa: E402
from orchestrator.engines.base import AgentAdapter, TurnRequest  # noqa: E402
from orchestrator.engines.claude import compact_summary  # noqa: E402
from orchestrator.engines.codex import build_codex_cmd  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402


def _chat(tmp_path, engine="claude", esid="abc"):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, engine, "m", "medium")
    if esid:
        db.update_session(s.id, engine_session_id=esid)
    return db, s.id


def test_defaults_and_override(tmp_path):
    db, sid = _chat(tmp_path)
    assert R.reset_at(db, sid) == 300_000 and R.action(db, sid) == "compact"
    db.set_setting("context.reset_at", 200_000)
    assert R.reset_at(db, sid) == 200_000
    R.set_chat_override(db, sid, at=500_000, action="fresh")
    assert R.reset_at(db, sid) == 500_000 and R.action(db, sid) == "fresh"
    R.set_chat_override(db, sid)
    assert R.chat_override(db, sid) == {} and R.reset_at(db, sid) == 200_000
    assert R.fmt_k(300_000) == "300k" and R.fmt_k(1_000_000) == "1M"


def test_size_and_due(tmp_path):
    db, sid = _chat(tmp_path)
    assert R.due(db, sid) is None
    db.add_message(sid, "system", "usage", {"input": 1000, "cached": 310_000, "output": 5})
    db.set_context(sid, {"total": 312_000, "max": 1_000_000})
    assert R.context_size(db, sid) == (312_000, 1_000_000)
    assert R.due(db, sid) == "compact"
    R.set_chat_override(db, sid, action="off")
    assert R.due(db, sid) is None
    R.set_chat_override(db, sid, at=400_000)
    assert R.due(db, sid) is None
    R.set_chat_override(db, sid)
    db.add_message(sid, "system", "reset", {"how": "compact", "before": 312_000, "after": 20_000})
    assert R.context_size(db, sid)[0] == 20_000 and R.due(db, sid) is None


def test_codex_compacts_itself(tmp_path):
    db, sid = _chat(tmp_path, engine="codex")
    db.add_message(sid, "system", "usage", {"input": 5000, "cached": 400_000})
    assert R.context_size(db, sid) == (405_000, 0)
    assert R.due(db, sid) is None and R.codex_limit(db, sid) == 300_000
    R.set_chat_override(db, sid, action="fresh")
    assert R.due(db, sid) == "fresh" and R.codex_limit(db, sid) == 0
    req = TurnRequest(prompt="x", cwd=tmp_path, model="", effort="", compact_limit=300_000)
    assert "model_auto_compact_token_limit=300000" in build_codex_cmd("codex", req)


def test_compact_summary_from_session_file(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    lines = [{"type": "user", "message": {"content": "hi"}},
             {"type": "user", "isCompactSummary": True, "message": {"content": "old"}},
             {"type": "user", "isCompactSummary": True, "message": {"content": [{"type": "text", "text": "new"}]}}]
    (d / "abc.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    assert compact_summary("abc", tmp_path) == "new"
    assert compact_summary("zzz", tmp_path) == ""


def test_claude_compact_boundary_maps():
    from claude_agent_sdk import SystemMessage

    from orchestrator.engines.claude import ClaudeMapState, map_claude_message

    msg = SystemMessage(subtype="compact_boundary",
                        data={"session_id": "s", "compact_metadata": {"trigger": "manual", "pre_tokens": 305000}})
    evs = map_claude_message(msg, ClaudeMapState())
    assert any(isinstance(e, Compacted) and e.pre_tokens == 305000 and e.trigger == "manual" for e in evs)


class Fake(AgentAdapter):
    engine = "claude"
    calls: list[TurnRequest] = []

    def __init__(self, script):
        super().__init__(None)
        self.script = script

    async def run(self, req):
        Fake.calls.append(req)
        for ev in self.script(req):
            yield ev

    async def interrupt(self):
        pass


def _mgr(db, script):
    from orchestrator.core.session_manager import SessionManager

    m = SessionManager(db)
    m._adapter = lambda sid, engine, policy=None: Fake(script)
    return m


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def test_auto_compact_before_next_message(tmp_path, monkeypatch):
    db, sid = _chat(tmp_path)
    db.add_message(sid, "system", "usage", {"input": 1000, "cached": 310_000})
    db.set_context(sid, {"total": 311_000, "max": 1_000_000})
    monkeypatch.setattr("orchestrator.engines.claude.compact_summary", lambda esid, root=None: "SUMMARY")
    Fake.calls = []

    def script(req):
        if req.prompt == "/compact":
            return [Started("abc"), Compacted(pre_tokens=311_000), ContextInfo("claude", {"total": 25_000}), Done()]
        return [Started("abc"), Text("ok"), Usage(100, 5, 25_000), Done()]

    m = _mgr(db, script)

    async def go():
        assert m.send(sid, "next step")
        await m._tasks[sid]

    _run(go)
    assert [c.prompt for c in Fake.calls] == ["/compact", "next step"]
    kinds = [x["kind"] for x in db.messages(sid)]
    assert kinds.index("reset") < max(i for i, x in enumerate(db.messages(sid)) if x["role"] == "user")
    rec = next(x for x in db.messages(sid) if x["kind"] == "reset")
    assert rec["before"] == 311_000 and rec["after"] == 25_000 and rec["summary"] == "SUMMARY"
    assert db.session(sid).engine_session_id == "abc"
    assert R.due(db, sid) is None


def test_fresh_reset_carries_summary(tmp_path):
    db, sid = _chat(tmp_path)
    db.update_session(sid, file_path=str(tmp_path / "abc.jsonl"))
    Fake.calls = []

    def script(req):
        if req.prompt == R.SUMMARY_PROMPT:
            return [Started("abc"), Text("Goal: X. Next: Y."), Done()]
        return [Started("new1"), Text("ok"), Done()]

    m = _mgr(db, script)

    async def go():
        assert m.reset_now(sid, "fresh")
        await m._tasks[sid]
        assert m.send(sid, "carry on")
        await m._tasks[sid]

    _run(go)
    assert Fake.calls[0].resume_id == "abc"
    second = Fake.calls[1]
    assert second.resume_id is None and second.prompt.startswith(R.CARRY_HEAD) and "Goal: X." in second.prompt
    assert second.prompt.endswith("carry on")
    assert db.session(sid).engine_session_id == "new1"
    assert db.session_by_engine_id("claude", "abc").status == "hidden"  # old session parked, not re-imported
    assert db.get_setting(f"chat.carry.{sid}") is None
    shown = [x["text"] for x in db.messages(sid) if x["role"] == "user"]
    assert shown == ["carry on"]


def test_failed_reset_still_sends(tmp_path, monkeypatch):
    db, sid = _chat(tmp_path)
    db.add_message(sid, "system", "usage", {"input": 1000, "cached": 310_000})
    monkeypatch.setattr("orchestrator.engines.claude.compact_summary", lambda esid, root=None: "")
    Fake.calls = []
    m = _mgr(db, lambda req: [Started("abc"), Done()])

    async def go():
        assert m.send(sid, "hello")
        await m._tasks[sid]

    _run(go)
    assert [c.prompt for c in Fake.calls] == ["/compact", "hello"]
    assert any("Context reset failed" in x.get("text", "") for x in db.messages(sid) if x["kind"] == "error")


def test_dialog_controls_save_override(tmp_path):
    from PySide6.QtWidgets import QApplication, QWidget

    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui.context_view import ContextDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    asyncio.set_event_loop(asyncio.new_event_loop())
    for engine in ("claude", "codex"):
        db, sid = _chat(tmp_path / engine if (tmp_path / engine).mkdir() is None else tmp_path, engine=engine)
        host = QWidget()
        host.resize(1000, 800)
        dlg = ContextDialog(db, SessionManager(db), sid, host)
        at = dlg.findChild(QWidget, "resetAtK")
        assert at.value() == 300 and dlg.findChild(QWidget, "compactNow").isEnabled()
        at.setValue(450)
        assert R.reset_at(db, sid) == 450_000
        dlg.findChild(QWidget, "resetAction").setCurrentIndex(list(R.ACTIONS).index("off"))
        assert R.action(db, sid) == "off"
