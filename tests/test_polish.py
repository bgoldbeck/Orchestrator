"""M6: diff viewer (real git), usage totals, search across chats, shortcuts, frozen MCP entry."""
import os
import subprocess
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import gitdiff  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402
from orchestrator.ui_text import usage_text  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(r), *cmd], check=True)
    (r / "a.py").write_text("x = 1\ny = 2\n")
    (r / "gone.txt").write_text("bye\n")
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True)
    (r / "a.py").write_text("x = 1\ny = 3\nz = 4\n")
    (r / "gone.txt").unlink()
    (r / "sub").mkdir()
    (r / "sub" / "new.md").write_text("# hi\n")
    return r


def test_gitdiff_files_diff_and_stats(repo, tmp_path):
    files = dict((p, st) for st, p in gitdiff.changed_files(str(repo / "sub")))  # works from a sub-folder
    assert files == {"a.py": "M", "gone.txt": "D", "sub/new.md": "?"}
    d = gitdiff.file_diff(str(repo), "a.py", "M")
    assert "-y = 2" in d and "+y = 3" in d and "+z = 4" in d
    assert gitdiff.file_diff(str(repo), "sub/new.md", "?").endswith("+# hi")
    n, add, rem = gitdiff.diff_stats(str(repo))
    assert n == 3 and add >= 3 and rem >= 2
    html = gitdiff.render_diff_html(d)
    assert "rgba(63,185,80" in html and "rgba(248,81,73" in html and "&lt;" not in d
    assert gitdiff.repo_root(str(tmp_path)) is None and gitdiff.changed_files(str(tmp_path)) == []


def test_gitdiff_repo_without_commits(tmp_path):
    r = tmp_path / "fresh"
    r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q"], check=True)
    (r / "f.txt").write_text("a\n")
    assert gitdiff.changed_files(str(r)) == [("?", "f.txt")]
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)
    assert "+a" in gitdiff.file_diff(str(r), "f.txt", "A")


def test_diff_dialog_lists_files(qapp, repo):
    from PySide6.QtWidgets import QWidget

    from orchestrator.ui.diff_view import DiffDialog

    host = QWidget()
    host.resize(1200, 800)
    dlg = DiffDialog(str(repo), host)
    assert dlg.list.count() == 3 and "+y = 3" in dlg.view.toPlainText()
    none = DiffDialog(str(repo.parent), host)
    assert none.list.count() == 0


def test_usage_totals_and_text(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    a = db.create_session(p.id, "claude", "m", "low")
    b = db.create_session(p.id, "codex", "m", "low")
    db.add_message(a.id, "system", "usage", {"input": 1200, "output": 300, "cached": 800, "cost": 0.01})
    db.add_message(a.id, "system", "usage", {"input": 100, "output": 50, "cached": 0, "cost": 0.02})
    db.add_message(b.id, "system", "usage", {"input": 5000, "output": 1000, "cached": 0, "cost": None})
    ua = db.usage(a.id)
    assert ua == {"input": 1300, "output": 350, "cached": 800, "cost": pytest.approx(0.03), "turns": 2}
    assert usage_text(ua) == "2.1k in  ·  350 out  ·  $0.03  ·  2 turns"
    assert usage_text(db.usage([a.id, b.id]), short=True) == "7.1k in  ·  1.4k out  ·  $0.03"
    assert usage_text(db.usage([])) == ""


def test_search_messages_titles_and_text(tmp_path):
    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    a = db.create_session(p.id, "claude", "m", "low", title="Fix login bug")
    b = db.create_session(p.id, "codex", "m", "low", title="Other")
    db.add_message(b.id, "assistant", "text", {"text": "The webhook retry logic now uses exponential backoff."})
    hidden = db.create_session(p.id, "codex", "m", "low", title="Login hidden")
    db.update_session(hidden.id, status="hidden")
    hits = {h["session_id"]: h for h in db.search_messages("login")}
    assert a.id in hits and hidden.id not in hits
    hit = db.search_messages("EXPONENTIAL")[0]
    assert hit["session_id"] == b.id and "exponential backoff" in hit["snippet"]
    assert db.search_messages("x") == []


def test_search_palette_and_shortcuts(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CLAUDE", str(tmp_path / "none"))
    monkeypatch.setenv("ORCHESTRATOR_INDEX_CODEX", str(tmp_path / "none"))
    from orchestrator.core.session_manager import SessionManager
    from orchestrator.ui.main_window import MainWindow

    db = Database(tmp_path / "t.db")
    p = db.add_project(str(tmp_path), "p")
    s = db.create_session(p.id, "codex", "gpt-6-luna", "low", title="Webhook retries")
    db.add_message(s.id, "user", "text", {"text": "add retries"})
    win = MainWindow(db, SessionManager(db))
    win.resize(1200, 800)
    keys = {sc.key().toString() for sc in win.shortcuts}
    assert {"Ctrl+K", "Ctrl+N", "Ctrl+L", "Ctrl+.", "Ctrl+D", "Ctrl+,", "F1", "Ctrl+1", "Ctrl+3"} <= keys
    pal = win.searchPalette
    pal.popup()
    pal.edit.setText("webhook")
    pal.run()
    assert pal.list.count() == 1 and "Webhook retries" in pal.list.item(0).text()
    opened = []
    pal.openSession.disconnect()
    pal.openSession.connect(opened.append)
    pal._open(pal.list.item(0))
    assert opened == [s.id] and not pal.isVisible()
    win.om.bridge.stop()


def test_frozen_mcp_command(monkeypatch):
    from orchestrator.core import orchestration

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\Orchestrator\Orchestrator.exe")
    cmd, args = orchestration.mcp_command()
    assert cmd.endswith("Orchestrator.exe") and args == ["--mcp-server"]
    monkeypatch.setattr(sys, "frozen", False)
    cmd, args = orchestration.mcp_command()
    assert args == ["-m", "orchestrator.mcp_server"]
