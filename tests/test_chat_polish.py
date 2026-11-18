"""Chat polish: harness markup hidden from imported transcripts, titles, summaries and search; picker in composer."""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import indexer as ix  # noqa: E402
from orchestrator.core.harness_text import clean, strip_harness  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402

WAKE = ('<wake reason="mention" current-time="2026-11-18T20:18:35Z">\n  <project id="p" type="project">\n'
        '    <thread ts="t">\n      <message trigger="true" from="human" id="m1">fix the login bug</message>\n'
        '    </thread>\n  </project>\n</wake>')


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_strip_harness_variants():
    assert strip_harness(WAKE) == ("fix the login bug", ["wake"])
    escaped = WAKE.replace("<", "&lt;").replace(">", "&gt;")
    assert clean(escaped) == "fix the login bug"
    text, tags = strip_harness("<system-reminder>\nbe terse\n</system-reminder>\nadd a test please")
    assert text == "add a test please" and tags == ["system-reminder"]
    assert clean("<command-name>/clear</command-name><command-message>clear</command-message>") == ""
    assert clean("<system-note>x</system-note>") == ""
    assert clean("<local-command-stdout>ok</local-command-stdout>") == ""
    # ordinary text with angle brackets is left alone
    assert clean("use a <div> and List<int> here") == "use a <div> and List<int> here"
    assert clean("write a <note> element") == "write a <note> element"


def _claude_log(path):
    rows = [
        {"type": "user", "sessionId": "11111111-2222-3333-4444-555555555555", "cwd": "D:/proj",
         "timestamp": "2026-11-18T20:00:00Z", "message": {"role": "user", "content": WAKE}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-x", "content": [
            {"type": "text", "text": "On it.<system-reminder>internal</system-reminder>"}]}},
        {"type": "user", "message": {"role": "user", "content": "<system-reminder>ctx</system-reminder>"}},
        {"type": "user", "message": {"role": "user", "content": "thanks"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_imported_claude_history_and_title(tmp_path):
    f = tmp_path / "11111111-2222-3333-4444-555555555555.jsonl"
    _claude_log(f)
    info = ix.parse_claude_file(f)
    assert info.title == "fix the login bug"
    hist = ix.load_claude_history(f)
    kinds = [(m["role"], m["kind"]) for m in hist]
    assert kinds == [("system", "context"), ("user", "text"), ("assistant", "text"), ("system", "context"),
                     ("user", "text")]
    assert hist[1]["text"] == "fix the login bug" and hist[2]["text"] == "On it."
    assert not any("<" in m.get("text", "") for m in hist)


def test_old_dirty_titles_cleaned_and_search(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "claude", "m", "low", title='<wake reason="mention"> <message>x</message>')
    db.add_message(s.id, "user", "text", {"text": "<system-reminder>secret harness</system-reminder>deploy it"})
    db.update_session(s.id, summary="<system-note>n</system-note>Deployed")
    db.conn.commit()
    db2 = Database(tmp_path / "t.db")
    s2 = db2.session(s.id)
    assert s2.title == "x" and s2.summary == "Deployed"
    assert db2.search_messages("harness") == []
    assert db2.search_messages("deploy")


def test_picker_lives_in_composer(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui.chat_view import ChatView

    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "codex", "gpt-6-luna", "low", title="t")
    v = ChatView(db, SessionManager(db))
    v.show_session(s.id)
    assert v.pill.parent() is v.prompt and v.prompt.row.indexOf(v.pill) == 0
    assert "low" in v.pill.text() and not v.header.findChildren(type(v.pill))
