import json
import shutil
from pathlib import Path

from orchestrator.core import indexer as ix
from orchestrator.core.session_index import scan_changed
from orchestrator.storage.db import Database

FIX = Path(__file__).parent / "fixtures"
CLAUDE_SID = "11111111-2222-4333-8444-555555555555"
CODEX_SID = "01a1ffff-0000-7000-8000-000000000001"


def test_parse_claude_file_title_cwd_turns():
    info = ix.parse_claude_file(FIX / "claude_session.jsonl")
    assert info and info.engine == "claude" and info.session_id == CLAUDE_SID
    assert info.cwd == r"D:\Demo\weather-app"
    assert info.title == "Forecast retry"  # custom-title wins over first prompt
    assert info.turns == 1  # meta + tool_result lines are not turns
    assert info.model == "claude-sonnet-5-5"
    assert info.last_active > info.started


def test_claude_title_falls_back_to_first_prompt(tmp_path):
    lines = [l for l in (FIX / "claude_session.jsonl").read_text(encoding="utf-8").splitlines() if "custom-title" not in l]
    p = tmp_path / f"{CLAUDE_SID}.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    assert ix.parse_claude_file(p).title == "Add a retry to the forecast fetcher"


def test_load_claude_history_shapes():
    h = ix.load_claude_history(FIX / "claude_session.jsonl")
    assert [(m["role"], m["kind"]) for m in h] == [
        ("user", "text"), ("assistant", "text"), ("tool", "tool"), ("assistant", "text")]
    assert h[0]["text"] == "Add a retry to the forecast fetcher"
    assert h[2]["name"] == "Read" and h[2]["summary"] == "src/fetch.py" and h[2]["output"] == "def fetch(): ..."
    assert "sidechain" not in json.dumps(h)


def test_parse_codex_items_file_with_index_title():
    info = ix.parse_codex_file(FIX / "codex_rollout_items.jsonl", {CODEX_SID: "Health check 500"})
    assert info and info.session_id == CODEX_SID and info.cwd == r"D:\Demo\api-server"
    assert info.title == "Health check 500"
    assert (info.model, info.effort) == ("gpt-6.2-sol", "medium")
    info2 = ix.parse_codex_file(FIX / "codex_rollout_items.jsonl", {})
    assert info2.title == "Why does /health return 500?"  # environment_context is skipped


def test_load_codex_history_items():
    h = ix.load_codex_history(FIX / "codex_rollout_items.jsonl")
    kinds = [(m["role"], m.get("name", m["kind"])) for m in h]
    assert kinds == [("user", "text"), ("tool", "shell"), ("tool", "edit"), ("assistant", "text")]
    assert h[1]["summary"] == "rg health src" and not h[1]["is_error"]
    assert "src/app.py" in h[2]["summary"]


def test_codex_legacy_response_items():
    info = ix.parse_codex_file(FIX / "codex_rollout_legacy.jsonl", {})
    assert info and info.title == "List the endpoints"
    h = ix.load_codex_history(FIX / "codex_rollout_legacy.jsonl")
    assert [(m["role"], m["text"]) for m in h] == [("user", "List the endpoints"), ("assistant", "GET /health, GET /items")]


def test_codex_subagent_skipped(tmp_path):
    p = tmp_path / "rollout-x.jsonl"
    p.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "x", "cwd": "D:/a", "source": {"subagent": {"thread_spawn": {}}}, "thread_source": "subagent"}}) + "\n"
        + (FIX / "codex_rollout_items.jsonl").read_text(encoding="utf-8").splitlines()[3], encoding="utf-8")
    assert ix.parse_codex_file(p) is None


def test_empty_and_garbage_files(tmp_path):
    (tmp_path / "a.jsonl").write_text("garbage\n{}\n", encoding="utf-8")
    assert ix.parse_claude_file(tmp_path / "a.jsonl") is None
    assert ix.parse_codex_file(tmp_path / "a.jsonl") is None
    assert ix.load_history("claude", str(tmp_path / "missing.jsonl")) == []


def make_homes(tmp_path):
    claude = tmp_path / "claude" / "projects" / "D--Demo-weather-app"
    claude.mkdir(parents=True)
    shutil.copy(FIX / "claude_session.jsonl", claude / f"{CLAUDE_SID}.jsonl")
    codex = tmp_path / "codex"
    day = codex / "sessions" / "2026" / "11" / "12"
    day.mkdir(parents=True)
    shutil.copy(FIX / "codex_rollout_items.jsonl", day / f"rollout-2026-11-12T14-00-00-{CODEX_SID}.jsonl")
    (codex / "session_index.jsonl").write_text(
        json.dumps({"id": CODEX_SID, "thread_name": "Health check 500", "updated_at": "x"}) + "\n", encoding="utf-8")
    return tmp_path / "claude" / "projects", codex


def test_scan_and_upsert_matches_projects_and_is_incremental(tmp_path):
    croot, chome = make_homes(tmp_path)
    infos = scan_changed({}, croot, chome)
    assert {i.engine for i in infos} == {"claude", "codex"}

    db = Database(tmp_path / "t.db")
    user_proj = db.add_project(r"D:\Demo")  # parent folder: both sessions belong here
    from orchestrator.core.session_index import SessionIndexer  # noqa: F401  (Qt-free path below)

    for info in infos:
        db.upsert_imported(info, user_proj.id)
    sessions = db.sessions(user_proj.id)
    assert {s.title for s in sessions} == {"Forecast retry", "Health check 500"}
    assert all(s.source == "imported" and s.file_path for s in sessions)
    # Second scan: nothing changed, nothing re-parsed.
    assert scan_changed(db.indexed_files(), croot, chome) == []
    # Upsert again does not duplicate.
    for info in infos:
        db.upsert_imported(info, user_proj.id)
    assert len(db.sessions(user_proj.id)) == 2


def test_app_session_claims_imported_row(tmp_path):
    croot, chome = make_homes(tmp_path)
    db = Database(tmp_path / "t.db")
    proj = db.add_project(r"D:\Demo")
    app_s = db.create_session(proj.id, "claude", "claude-haiku-5-5", "low")
    info = next(i for i in scan_changed({}, croot, chome) if i.engine == "claude")
    db.upsert_imported(info, proj.id)  # indexer saw the file first
    db.claim_engine_id(app_s.id, "claude", CLAUDE_SID)
    rows = db.sessions(proj.id)
    assert len(rows) == 1 and rows[0].id == app_s.id and rows[0].source == "app" and rows[0].file_path


def test_hidden_imported_session_stays_hidden(tmp_path):
    croot, chome = make_homes(tmp_path)
    db = Database(tmp_path / "t.db")
    proj = db.add_project(r"D:\Demo")
    for info in scan_changed({}, croot, chome):
        db.upsert_imported(info, proj.id)
    sid = db.sessions(proj.id)[0].id
    db.delete_session(sid)
    for info in scan_changed({}, croot, chome):
        db.upsert_imported(info, proj.id)
    assert sid not in {s.id for s in db.sessions(proj.id)} and len(db.sessions(proj.id)) == 1


def test_age_and_paths():
    from orchestrator.core.session_index import _under
    from orchestrator.ui_text import age

    assert age(1000, now=1030) == "now" and age(1000, now=1000 + 7200) == "2h" and age(0) == ""
    assert _under(r"D:\Demo\api-server", r"D:\Demo") and _under(r"d:\demo", "D:\\Demo\\")
    assert not _under(r"D:\Demo2", r"D:\Demo")
