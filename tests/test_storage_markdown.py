from orchestrator.storage.db import Database
from orchestrator.ui.markdown_render import split_segments


def test_split_segments_closed_and_open_fences():
    segs = split_segments("Intro\n\n```python\nprint(1)\n```\n\nAfter")
    assert [s.kind for s in segs] == ["text", "code", "text"]
    assert segs[1].lang == "python" and segs[1].body == "print(1)" and segs[1].closed
    open_ = split_segments("x\n```js\nlet a")
    assert open_[-1].kind == "code" and open_[-1].body == "let a" and not open_[-1].closed


def test_db_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path))
    s = db.create_session(p.id, "codex", "gpt-6.2-sol", "low")
    db.add_message(s.id, "user", "text", {"text": "hi"})
    db.update_session(s.id, engine_session_id="abc", title="Hi")
    assert db.session(s.id).engine_session_id == "abc"
    assert db.messages(s.id) == [{"role": "user", "kind": "text", "text": "hi"}]
    assert db.models("codex") and db.models("claude")
    db.set_setting("k", [1, 2])
    assert db.get_setting("k") == [1, 2]
