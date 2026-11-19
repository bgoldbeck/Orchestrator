"""Attachments: stored per chat, images inline for Claude and --image for Codex, every file listed by path."""
import base64
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import attachments as A  # noqa: E402
from orchestrator.engines.base import TurnRequest  # noqa: E402
from orchestrator.engines.codex import build_codex_cmd, codex_prompt  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def test_store_and_content(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)
    doc = tmp_path / "notes.txt"
    doc.write_text("hello")
    a, b = A.store(7, img), A.store(7, img)
    assert a != b and a.parent == b.parent and a.parent.name == "7"  # unique names, chat folder
    blocks = A.claude_content("look", [str(a), str(doc)])
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[-1]["type"] == "text" and str(doc) in blocks[-1]["text"] and blocks[-1]["text"].startswith("look")
    assert A.check(tmp_path / "nope.bin")
    assert A.label(str(img)) == "shot.png (image)"


def test_codex_images_and_paths(tmp_path):
    req = TurnRequest(prompt="fix it", cwd=tmp_path, model="", effort="",
                      attachments=[str(tmp_path / "a.png"), str(tmp_path / "b.log")])
    cmd = build_codex_cmd("codex", req)
    assert f"--image={tmp_path / 'a.png'}" in cmd and not any("b.log" in c for c in cmd)
    assert cmd[-1] == "-"
    assert str(tmp_path / "b.log") in codex_prompt(req) and str(tmp_path / "a.png") in codex_prompt(req)
    req.resume_id = "thread-1"  # resume: no --image flag, paths only
    assert not any(c.startswith("--image") for c in build_codex_cmd("codex", req))


def test_send_stores_attachments(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.storage.db import Database

    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "claude", "claude-opus-5-5", "medium")
    img = tmp_path / "x.png"
    img.write_bytes(PNG)
    mgr = SessionManager(db)
    seen = {}

    async def fake_run(sid, prompt, policy=None, files=None):
        seen["files"] = files

    mgr._run = fake_run
    assert mgr.send(s.id, "", attachments=[str(img), str(tmp_path / "missing.txt")])
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0))
    msg = db.messages(s.id)[0]
    assert msg["text"] == "See the attached files." and len(msg["attachments"]) == 1
    assert seen["files"] == msg["attachments"] and "home" in msg["attachments"][0]


def test_prompt_box_chips(tmp_path):
    from PySide6.QtWidgets import QApplication

    from orchestrator.ui.chat_view import PromptBox

    QApplication.instance() or QApplication([])
    box = PromptBox()
    f = tmp_path / "a.txt"
    f.write_text("x")
    got = []
    box.submitted.connect(got.append)
    assert box.add_attachments([str(f), str(tmp_path / "missing")]) and box.attachments == [str(f)]
    box._submit()  # empty text with an attachment still sends
    assert got == [""]
    assert box.take_attachments() == [str(f)] and box.attachments == []
