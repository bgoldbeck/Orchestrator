"""MCP servers page: read both engines' configs, add/remove commands, on/off for Orchestrator's runs."""
import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import mcp_config as M  # noqa: E402
from orchestrator.engines.base import TurnRequest  # noqa: E402
from orchestrator.engines.codex import build_codex_cmd  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({
        "mcpServers": {"gh": {"command": "npx", "args": ["-y", "gh"], "env": {"TOKEN": "x"}},
                       "web": {"type": "http", "url": "http://h/mcp"}},
        "projects": {"D:/p": {"mcpServers": {"db": {"command": "uvx", "args": ["sqlite"]}}}}}))
    cc = tmp_path / "config.toml"
    cc.write_text('[mcp_servers.docs]\nurl = "https://d/mcp"\n[mcp_servers.gh]\ncommand = "npx"\nargs = ["gh"]\n')
    monkeypatch.setenv("ORCHESTRATOR_CLAUDE_JSON", str(cj))
    monkeypatch.setenv("ORCHESTRATOR_CODEX_CONFIG", str(cc))
    return Database(tmp_path / "t.db")


def test_list_servers(cfg):
    db = cfg
    db.set_setting("context.last.claude", {"mcp": [{"server": "gh", "status": "connected"},
                                                   {"server": "canva", "status": "connected", "source": "plugin"},
                                                   {"server": "orchestrator", "status": "connected"}]})
    got = {(s.engine, s.name): s for s in M.list_servers(db)}
    assert set(got) == {("claude", "gh"), ("claude", "web"), ("claude", "db"), ("claude", "canva"),
                        ("codex", "docs"), ("codex", "gh")}
    gh = got[("claude", "gh")]
    assert gh.status == "connected" and gh.env_keys == ["TOKEN"] and gh.target() == "npx -y gh" and gh.removable
    assert got[("claude", "web")].kind == "http" and got[("claude", "web")].target() == "http://h/mcp"
    assert got[("claude", "db")].scope == "local" and got[("claude", "db")].project == "D:/p"
    assert got[("claude", "canva")].scope == "plugin" and not got[("claude", "canva")].removable
    assert got[("codex", "docs")].url == "https://d/mcp"


def test_missing_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_CLAUDE_JSON", str(tmp_path / "nope.json"))
    monkeypatch.setenv("ORCHESTRATOR_CODEX_CONFIG", str(tmp_path / "nope.toml"))
    assert M.list_servers(None) == []


def test_add_and_remove_commands():
    assert M.add_cmd("claude", "claude", "gh", "npx", ["-y", "gh"], env={"T": "1"}) == [
        "claude", "mcp", "add", "--scope", "user", "-e", "T=1", "gh", "--", "npx", "-y", "gh"]
    assert M.add_cmd("claude", "claude", "web", url="http://h") == [
        "claude", "mcp", "add", "--scope", "user", "--transport", "http", "web", "http://h"]
    assert M.add_cmd("codex", "codex", "gh", "npx", ["gh"], env={"T": "1"}) == [
        "codex", "mcp", "add", "gh", "--env", "T=1", "--", "npx", "gh"]
    assert M.add_cmd("codex", "codex", "d", url="https://d") == ["codex", "mcp", "add", "d", "--url", "https://d"]
    s = M.McpServer("claude", "db", "local", project="D:/p")
    assert M.remove_cmd(s, "claude") == ["claude", "mcp", "remove", "db", "--scope", "local"]
    assert M.parse_env("A=1\n\nB = two=2\nbad") == {"A": "1", "B": "two=2"}
    ok, msg = M.remove_server(M.McpServer("claude", "canva", "plugin"))
    assert not ok and "plugin" in msg


def test_off_for_run(cfg):
    db = cfg
    M.set_off(db, "claude:gh", True)
    M.set_off(db, "codex:docs", True, sid=7)
    M.set_off(db, "claude:orchestrator", True)  # Orchestrator's own server can't be hidden
    assert M.off_for_run(db, "claude", 7) == ["gh"]
    assert M.off_for_run(db, "codex", 7) == ["docs"]
    assert M.off_for_run(db, "codex", 8) == []
    M.set_off(db, "claude:gh", False)
    assert M.off_for_run(db, "claude", 7) == []


def test_codex_cmd_disables_servers(tmp_path):
    req = TurnRequest(prompt="x", cwd=tmp_path, model="", effort="", mcp_off=["docs"])
    cmd = build_codex_cmd("codex", req)
    assert "mcp_servers.docs.enabled=false" in cmd


def test_mcp_page_renders(cfg):
    from PySide6.QtWidgets import QApplication

    from orchestrator.ui.mcp_page import McpPage, ServerRow

    QApplication.instance() or QApplication([])
    page = McpPage(cfg)
    rows = page.findChildren(ServerRow)
    assert len(rows) == 5
    row = next(r for r in rows if r.s.key == "claude:gh")
    row.switch.setChecked(False)
    assert "claude:gh" in M.off_everywhere(cfg)
