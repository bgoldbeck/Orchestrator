"""Token breakdown: Claude's /context data summarized, stored per chat, shown with tips; Lean mode."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.engines.claude import summarize_context  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402
from orchestrator.ui.context_view import fmt_tokens, setup_tokens, tips, turn_text  # noqa: E402

USAGE = {
    "categories": [{"name": "System prompt", "tokens": 3000}, {"name": "System tools", "tokens": 12000},
                   {"name": "MCP tools", "tokens": 5000, "isDeferred": True}, {"name": "Skills", "tokens": 7600},
                   {"name": "Memory files", "tokens": 400}, {"name": "Messages", "tokens": 70000},
                   {"name": "Free space", "tokens": 100000}],
    "totalTokens": 98000, "maxTokens": 200000, "model": "claude-opus-5-5",
    "memoryFiles": [{"path": "~/.claude/CLAUDE.md", "tokens": 400, "type": "User"}],
    "mcpTools": [{"name": "a", "serverName": "big", "tokens": 3000}, {"name": "b", "serverName": "big", "tokens": 1000},
                 {"name": "c", "serverName": "small", "tokens": 1000}],
}
INIT = {"mcp_servers": [{"name": "big", "status": "connected"}, {"name": "dead", "status": "failed"}],
        "plugins": [{"name": "caveman", "path": "/p"}], "skills": ["a", "b"], "tools": ["Bash", "Read"]}


def test_summarize_context():
    d = summarize_context(USAGE, INIT, lean=True)
    assert d["total"] == 98000 and d["max"] == 200000 and d["lean"] is True
    assert [(m["server"], m["tools"], m["tokens"], m["status"]) for m in d["mcp"]] == [
        ("big", 2, 4000, "connected"), ("small", 1, 1000, ""), ("dead", 0, 0, "failed")]
    assert d["plugins"] == ["caveman"] and d["skills"] == 2 and d["tools"] == 2
    assert d["memory_files"][0]["tokens"] == 400
    assert summarize_context(None, None)["categories"] == []


def test_setup_tokens_and_tips():
    d = summarize_context(USAGE, INIT)
    assert setup_tokens(d) == 3000 + 12000 + 7600 + 400  # deferred MCP tools and the conversation excluded
    t = " ".join(tips(d))
    assert "skills list" in t and "MCP tools" in t and "new chat" in t
    assert fmt_tokens(1234) == "1.2k" and fmt_tokens(999) == "999" and fmt_tokens(2_500_000) == "2.5M"
    assert "96%" in turn_text({"input": 2000, "cached": 48000, "output": 10})
    assert turn_text(None) == "No turns yet."


def test_context_stored_and_lean(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "medium")
    assert db.context(s.id) is None
    db.set_context(s.id, {"total": 5})
    assert db.context(s.id) == {"total": 5}
    db.add_message(s.id, "system", "usage", {"input": 1, "output": 2, "cached": 3})
    db.add_message(s.id, "system", "usage", {"input": 4, "output": 5, "cached": 6})
    assert db.last_usage(s.id)["input"] == 4

    from orchestrator.core.session_manager import SessionManager

    mgr = SessionManager(db)
    assert mgr.lean_for(s.id) is False
    db.set_setting("claude.lean_default", True)
    assert mgr.lean_for(s.id) is True
    mgr.set_lean(s.id, False)  # the chat's own choice wins over the default
    assert mgr.lean_for(s.id) is False


def test_lean_maps_to_project_settings_only(monkeypatch):
    """Lean mode passes setting_sources=['project']; normal chats keep the SDK default (None)."""
    import inspect

    from orchestrator.engines import claude

    src = inspect.getsource(claude.ClaudeAdapter.run)
    assert 'setting_sources=["project"] if req.lean else None' in src
