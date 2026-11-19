"""SQLite storage in %APPDATA%\\Orchestrator\\orchestrator.db (no secrets)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.core.catalog import DEFAULTS, SEED_MODELS


def app_dir() -> Path:
    base = os.environ.get("ORCHESTRATOR_HOME") or (Path(os.environ.get("APPDATA", Path.home())) / "Orchestrator")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  engine TEXT NOT NULL, engine_session_id TEXT, title TEXT NOT NULL DEFAULT 'New chat',
  model TEXT, effort TEXT, status TEXT NOT NULL DEFAULT 'idle', summary TEXT, summary_updated_at REAL,
  source TEXT NOT NULL DEFAULT 'app', last_active REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL, kind TEXT NOT NULL, content_json TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS models(
  engine TEXT NOT NULL, id TEXT NOT NULL, label TEXT NOT NULL, efforts_json TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(engine, id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  prompt TEXT NOT NULL, engine TEXT NOT NULL, model TEXT, effort TEXT,
  kind TEXT NOT NULL DEFAULT 'cron', cron TEXT, run_at REAL,
  new_or_resume TEXT NOT NULL DEFAULT 'new', session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
  permission TEXT NOT NULL DEFAULT 'read-only', missed TEXT NOT NULL DEFAULT 'run_once',
  enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL, status TEXT NOT NULL,
  started_at REAL NOT NULL, finished_at REAL, note TEXT);
CREATE TABLE IF NOT EXISTS orchestrations(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL, goal TEXT NOT NULL,
  engine TEXT NOT NULL, model TEXT, effort TEXT, max_parallel INTEGER NOT NULL DEFAULT 4,
  status TEXT NOT NULL DEFAULT 'running', created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, orchestration_id INTEGER NOT NULL REFERENCES orchestrations(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL, engine TEXT NOT NULL, model TEXT, effort TEXT,
  title TEXT NOT NULL, instructions TEXT NOT NULL, cwd TEXT, worktree TEXT,
  status TEXT NOT NULL DEFAULT 'queued', result_json TEXT, created_at REAL NOT NULL,
  started_at REAL, finished_at REAL);
CREATE INDEX IF NOT EXISTS ix_tasks_orch ON tasks(orchestration_id, id);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY, orchestration_id INTEGER NOT NULL REFERENCES orchestrations(id) ON DELETE CASCADE,
  text TEXT NOT NULL, options_json TEXT, answer TEXT, created_at REAL NOT NULL, answered_at REAL);
CREATE INDEX IF NOT EXISTS ix_runs_schedule ON runs(schedule_id, started_at);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_id, last_active);
"""


MIGRATIONS = [
    ("projects", "auto", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("projects", "icon", "TEXT"),
    ("projects", "color", "TEXT"),
    ("sessions", "file_path", "TEXT"),
    ("sessions", "file_mtime", "REAL"),
    ("sessions", "cwd", "TEXT"),
    ("models", "enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("sessions", "parent_id", "INTEGER"),  # sub-agent chats nest under their orchestrator chat
    ("sessions", "keywords", "TEXT"),
    ("tasks", "pick_reason", "TEXT"),
    ("tasks", "auto_picked", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "reused", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "escalated", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "proposal_json", "TEXT"),
    ("orchestrations", "pick_reason", "TEXT"),
    ("tasks", "queue_pos", "REAL"),  # global queue order for queued tasks (lower runs first)
    ("orchestrations", "project_reason", "TEXT"),  # why Auto picked this project
    ("schedules", "target", "TEXT NOT NULL DEFAULT 'chat'"),  # chat | orchestration
    ("schedules", "options_json", "TEXT"),  # orchestration: {"max_parallel": n, "auto_planner": bool}
    ("tasks", "reviewed_at", "REAL"),
    ("sessions", "context_json", "TEXT"),  # last ContextInfo: what the engine loads into every prompt  # Board: a finished task moves from Review to Done when the user marks it
    ("tasks", "depends_json", "TEXT"),  # task ids that must finish (done) before this one starts
    ("tasks", "done_when_json", "TEXT"),  # fixed "done when" criteria, set at delegate, never changed after the start
    ("tasks", "files_json", "TEXT"),  # files the task expects to change (no two running tasks share one)
    ("tasks", "checks_json", "TEXT"),  # the project's test command, run by the app on the delivered work
    ("tasks", "merge_state", "TEXT"),  # worktree tasks: review | merged | conflict | discarded | failed
    ("tasks", "merge_note", "TEXT"),
    ("tasks", "hold_json", "TEXT"),  # why a queued task is held for the user (e.g. a dependency failed)
    ("questions", "recommended", "TEXT"),
]


@dataclass
class Project:
    id: int
    name: str
    path: str
    auto: bool = False
    pinned: bool = False
    icon: str | None = None
    color: str | None = None
    created_at: float = 0.0


@dataclass
class Session:
    id: int
    project_id: int
    engine: str
    engine_session_id: str | None
    title: str
    model: str | None
    effort: str | None
    status: str
    last_active: float
    source: str = "app"
    file_path: str | None = None
    cwd: str | None = None
    parent_id: int | None = None
    summary: str | None = None
    keywords: str | None = None
    summary_updated_at: float | None = None


SCHEDULE_FIELDS = ("name", "project_id", "prompt", "engine", "model", "effort", "kind", "cron", "run_at",
                   "new_or_resume", "session_id", "permission", "missed", "enabled", "target", "options_json")


@dataclass
class Schedule:
    id: int
    name: str
    project_id: int
    prompt: str
    engine: str
    model: str | None
    effort: str | None
    kind: str  # cron | once
    cron: str | None
    run_at: float | None
    new_or_resume: str  # new | resume
    session_id: int | None
    permission: str  # read-only | allow-edits | full
    missed: str  # run_once | skip
    enabled: bool
    target: str = "chat"  # chat | orchestration (prompt = the goal, engine/model/effort = the planner)
    options: dict | None = None


@dataclass
class Run:
    id: int
    schedule_id: int
    session_id: int | None
    status: str  # running | done | error | skipped | stopped
    started_at: float
    finished_at: float | None
    note: str | None


@dataclass
class Orchestration:
    id: int
    project_id: int
    session_id: int | None
    goal: str
    engine: str
    model: str | None
    effort: str | None
    max_parallel: int
    status: str  # running | done | error | stopped
    created_at: float
    finished_at: float | None
    pick_reason: str | None = None
    project_reason: str | None = None


@dataclass
class Question:
    id: int
    orchestration_id: int
    text: str
    options: list[str]
    answer: str | None
    created_at: float
    answered_at: float | None
    recommended: str | None = None


@dataclass
class Task:
    id: int
    orchestration_id: int
    session_id: int | None
    engine: str
    model: str | None
    effort: str | None
    title: str
    instructions: str
    cwd: str | None
    worktree: str | None
    status: str  # proposed | queued | running | waiting | done | failed | stopped
    result: dict | None
    created_at: float
    started_at: float | None
    finished_at: float | None
    pick_reason: str | None = None
    auto_picked: bool = False
    reused: bool = False
    escalated: bool = False
    proposal: dict | None = None
    queue_pos: float | None = None
    reviewed_at: float | None = None
    depends_on: list[int] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    checks: dict | None = None
    merge_state: str | None = None
    merge_note: str | None = None
    hold: dict | None = None


@dataclass
class Model:
    engine: str
    id: str
    label: str
    efforts: list[str]
    enabled: bool = True


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_dir() / "orchestrator.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                if col == "reviewed_at":  # tasks finished before the Board existed count as reviewed
                    self.conn.execute("UPDATE tasks SET reviewed_at=COALESCE(finished_at, created_at) "
                                      "WHERE status IN ('done','failed','stopped')")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_engine_id ON sessions(engine, engine_session_id)"
                          " WHERE engine_session_id IS NOT NULL")
        if not self.conn.execute("SELECT 1 FROM models LIMIT 1").fetchone():
            self.reset_models()
        # Sessions left "running" by a crash are not running anymore.
        self.conn.execute("UPDATE sessions SET status='idle' WHERE status IN ('running','waiting')")
        self.conn.execute("UPDATE runs SET status='error', note='app closed during run', finished_at=started_at "
                          "WHERE status='running'")
        self.conn.execute("UPDATE tasks SET status='stopped', finished_at=COALESCE(finished_at, created_at) "
                          "WHERE status IN ('proposed','queued','running','waiting')")
        self.conn.execute("UPDATE orchestrations SET status='stopped' WHERE status='running'")
        self.conn.execute("DELETE FROM projects WHERE auto=1 AND id NOT IN (SELECT project_id FROM sessions)")
        self._clean_harness_text()
        self.conn.commit()

    def _clean_harness_text(self) -> None:
        """Titles/summaries saved before harness markup was stripped (e.g. '<wake reason=...>')."""
        from orchestrator.core.harness_text import clean
        from orchestrator.ui_text import short_title

        rows = self.conn.execute(
            "SELECT id, title, summary, keywords FROM sessions WHERE title LIKE '%<%' OR title LIKE '%&lt;%' "
            "OR summary LIKE '%<%' OR summary LIKE '%&lt;%'").fetchall()
        for r in rows:
            title = short_title(clean(r["title"] or "")) or "Untitled chat"
            summary = clean(r["summary"]) if r["summary"] else r["summary"]
            keywords = r["keywords"] if summary == r["summary"] else None  # re-derive with the summary
            self.conn.execute("UPDATE sessions SET title=?, summary=?, keywords=? WHERE id=?",
                              (title, summary or None, keywords, r["id"]))

    # -- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))
        self.conn.commit()

    # -- projects
    @staticmethod
    def _proj(r: sqlite3.Row) -> Project:
        return Project(r["id"], r["name"], r["path"], bool(r["auto"]), bool(r["pinned"]), r["icon"], r["color"],
                       r["created_at"])

    def projects(self) -> list[Project]:
        return [self._proj(r) for r in self.conn.execute(
            "SELECT * FROM projects ORDER BY auto, name COLLATE NOCASE")]

    def add_project(self, path: str, name: str | None = None, auto: bool = False) -> Project:
        p = Path(path)
        name = name or p.name or str(p)
        r = self.conn.execute("SELECT * FROM projects WHERE path=? COLLATE NOCASE", (str(p),)).fetchone()
        if r:
            if r["auto"] and not auto:  # user adopts a discovered folder
                self.conn.execute("UPDATE projects SET auto=0 WHERE id=?", (r["id"],))
                self.conn.commit()
            return self.project(r["id"])  # type: ignore[return-value]
        cur = self.conn.execute("INSERT INTO projects(name,path,created_at,auto) VALUES(?,?,?,?)",
                                (name, str(p), time.time(), int(auto)))
        self.conn.commit()
        return self.project(cur.lastrowid)  # type: ignore[return-value]

    def project(self, pid: int) -> Project | None:
        r = self.conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return self._proj(r) if r else None

    def update_project(self, pid: int, **fields: Any) -> None:
        allowed = {k: v for k, v in fields.items() if k in ("name", "pinned", "icon", "color", "auto")}
        if not allowed:
            return
        if "pinned" in allowed:
            allowed["pinned"] = int(bool(allowed["pinned"]))
        cols = ", ".join(f"{k}=?" for k in allowed)
        self.conn.execute(f"UPDATE projects SET {cols} WHERE id=?", (*allowed.values(), pid))
        self.conn.commit()

    def project_activity(self) -> dict[int, float]:
        """Last activity per project: newest chat, else when the project was added."""
        act = {r["id"]: r["created_at"] for r in self.conn.execute("SELECT id, created_at FROM projects")}
        for r in self.conn.execute("SELECT project_id, MAX(last_active) AS t FROM sessions "
                                   "WHERE status!='hidden' GROUP BY project_id"):
            if r["t"]:
                act[r["project_id"]] = r["t"]
        return act

    def remove_project(self, pid: int) -> None:
        self.conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.conn.commit()

    # -- sessions
    @staticmethod
    def _sess(r: sqlite3.Row) -> Session:
        return Session(r["id"], r["project_id"], r["engine"], r["engine_session_id"], r["title"],
                       r["model"], r["effort"], r["status"], r["last_active"], r["source"], r["file_path"], r["cwd"],
                       r["parent_id"], r["summary"], r["keywords"], r["summary_updated_at"])

    def sessions(self, project_id: int) -> list[Session]:
        return [self._sess(r) for r in self.conn.execute(
            "SELECT * FROM sessions WHERE project_id=? AND status!='hidden' ORDER BY last_active DESC", (project_id,))]

    def set_context(self, sid: int, data: dict | None) -> None:
        self.conn.execute("UPDATE sessions SET context_json=? WHERE id=?",
                          (json.dumps(data) if data is not None else None, sid))
        self.conn.commit()

    def context(self, sid: int) -> dict | None:
        r = self.conn.execute("SELECT context_json FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    def last_usage(self, sid: int) -> dict | None:
        """The newest per-turn usage record of a chat (input, output, cached, cost)."""
        r = self.conn.execute("SELECT content_json FROM messages WHERE kind='usage' AND session_id=? "
                              "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        return json.loads(r[0]) if r else None

    def child_sessions(self, sid: int) -> list[Session]:
        """Sub-agent chats started by this orchestrator chat, oldest first."""
        return [self._sess(r) for r in self.conn.execute(
            "SELECT * FROM sessions WHERE parent_id=? AND status!='hidden' ORDER BY id", (sid,))]

    def session(self, sid: int) -> Session | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return self._sess(r) if r else None

    def create_session(self, project_id: int, engine: str, model: str, effort: str,
                       parent_id: int | None = None, title: str | None = None, cwd: str | None = None) -> Session:
        cur = self.conn.execute(
            "INSERT INTO sessions(project_id,engine,model,effort,last_active,parent_id,title,cwd) "
            "VALUES(?,?,?,?,?,?,COALESCE(?, 'New chat'),?)",
            (project_id, engine, model, effort, time.time(), parent_id, title, cwd))
        self.conn.commit()
        return self.session(cur.lastrowid)  # type: ignore[return-value]

    def update_session(self, sid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))
        self.conn.commit()

    def session_by_engine_id(self, engine: str, esid: str) -> Session | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE engine=? AND engine_session_id=?", (engine, esid)).fetchone()
        return self._sess(r) if r else None

    def claim_engine_id(self, sid: int, engine: str, esid: str) -> None:
        """Point session `sid` at engine session `esid`, absorbing any row the indexer made for it first."""
        other = self.session_by_engine_id(engine, esid)
        if other and other.id != sid:
            fields = {"file_path": other.file_path, "cwd": other.cwd}
            self.conn.execute("DELETE FROM sessions WHERE id=?", (other.id,))
            self.update_session(sid, engine_session_id=esid, **{k: v for k, v in fields.items() if v})
        else:
            self.update_session(sid, engine_session_id=esid)

    def indexed_files(self) -> dict[str, float]:
        return {r["file_path"]: r["file_mtime"] or 0 for r in
                self.conn.execute("SELECT file_path, file_mtime FROM sessions WHERE file_path IS NOT NULL")}

    def upsert_imported(self, info, project_id: int) -> tuple[int, bool]:
        """Insert or refresh an imported session. Returns (session id, created)."""
        s = self.session_by_engine_id(info.engine, info.session_id)
        if s:
            fields = {"file_path": info.path, "file_mtime": info.mtime, "cwd": info.cwd}
            if s.source == "imported":
                fields.update(title=info.title, last_active=max(s.last_active, info.last_active))
                if not s.model and info.model:
                    fields.update(model=info.model, effort=s.effort or info.effort or None)
            self.update_session(s.id, **fields)
            return s.id, False
        cur = self.conn.execute(
            "INSERT INTO sessions(project_id,engine,engine_session_id,title,status,source,last_active,file_path,"
            "file_mtime,cwd,model,effort) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, info.engine, info.session_id, info.title, "idle", "imported", info.last_active,
             info.path, info.mtime, info.cwd, info.model or None, info.effort or None))
        self.conn.commit()
        return cur.lastrowid, True  # type: ignore[return-value]

    def delete_session(self, sid: int) -> None:
        s = self.session(sid)
        if s and s.file_path:  # backed by an engine log: hide, so the indexer won't bring it back
            self.conn.execute("UPDATE sessions SET status='hidden' WHERE id=?", (sid,))
        else:
            self.conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        self.conn.commit()

    # -- messages
    def add_message(self, session_id: int, role: str, kind: str, content: dict[str, Any]) -> int:
        now = time.time()
        cur = self.conn.execute("INSERT INTO messages(session_id,role,kind,content_json,ts) VALUES(?,?,?,?,?)",
                                (session_id, role, kind, json.dumps(content), now))
        self.conn.execute("UPDATE sessions SET last_active=? WHERE id=?", (now, session_id))
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def messages(self, session_id: int) -> list[dict[str, Any]]:
        return [{"role": r["role"], "kind": r["kind"], **json.loads(r["content_json"])}
                for r in self.conn.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))]

    # -- schedules
    @staticmethod
    def _sched(r: sqlite3.Row) -> Schedule:
        return Schedule(r["id"], r["name"], r["project_id"], r["prompt"], r["engine"], r["model"], r["effort"],
                        r["kind"], r["cron"], r["run_at"], r["new_or_resume"], r["session_id"], r["permission"],
                        r["missed"], bool(r["enabled"]), r["target"] or "chat",
                        json.loads(r["options_json"]) if r["options_json"] else None)

    def schedules(self) -> list[Schedule]:
        return [self._sched(r) for r in self.conn.execute("SELECT * FROM schedules ORDER BY name COLLATE NOCASE")]

    def schedule(self, sched_id: int) -> Schedule | None:
        r = self.conn.execute("SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone()
        return self._sched(r) if r else None

    def save_schedule(self, data: dict[str, Any], sched_id: int | None = None) -> int:
        if "options" in data:
            data = {**data, "options_json": json.dumps(data["options"]) if data["options"] else None}
        vals = {k: data.get(k) for k in SCHEDULE_FIELDS if k in data}
        if "enabled" in vals:
            vals["enabled"] = int(bool(vals["enabled"]))
        if sched_id:
            cols = ", ".join(f"{k}=?" for k in vals)
            self.conn.execute(f"UPDATE schedules SET {cols} WHERE id=?", (*vals.values(), sched_id))
            self.conn.commit()
            return sched_id
        vals["created_at"] = time.time()
        cols = ", ".join(vals)
        cur = self.conn.execute(f"INSERT INTO schedules({cols}) VALUES({', '.join('?' * len(vals))})", tuple(vals.values()))
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def delete_schedule(self, sched_id: int) -> None:
        self.conn.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
        self.conn.commit()

    # -- runs
    @staticmethod
    def _run(r: sqlite3.Row) -> Run:
        return Run(r["id"], r["schedule_id"], r["session_id"], r["status"], r["started_at"], r["finished_at"], r["note"])

    def add_run(self, sched_id: int, session_id: int | None, status: str = "running", note: str | None = None) -> int:
        now = time.time()
        cur = self.conn.execute("INSERT INTO runs(schedule_id,session_id,status,started_at,finished_at,note) VALUES(?,?,?,?,?,?)",
                                (sched_id, session_id, status, now, None if status == "running" else now, note))
        self.conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def finish_run(self, run_id: int, status: str, note: str | None = None) -> None:
        self.conn.execute("UPDATE runs SET status=?, finished_at=?, note=COALESCE(?, note) WHERE id=?",
                          (status, time.time(), note, run_id))
        self.conn.commit()

    def runs(self, sched_id: int, limit: int = 20) -> list[Run]:
        return [self._run(r) for r in self.conn.execute(
            "SELECT * FROM runs WHERE schedule_id=? ORDER BY started_at DESC LIMIT ?", (sched_id, limit))]

    def running_runs(self) -> list[Run]:
        return [self._run(r) for r in self.conn.execute("SELECT * FROM runs WHERE status='running'")]

    # -- orchestrations
    @staticmethod
    def _orch(r: sqlite3.Row) -> Orchestration:
        return Orchestration(r["id"], r["project_id"], r["session_id"], r["goal"], r["engine"], r["model"], r["effort"],
                             r["max_parallel"], r["status"], r["created_at"], r["finished_at"], r["pick_reason"],
                             r["project_reason"])

    def add_orchestration(self, project_id: int, goal: str, engine: str, model: str, effort: str,
                          max_parallel: int = 4) -> Orchestration:
        cur = self.conn.execute(
            "INSERT INTO orchestrations(project_id,goal,engine,model,effort,max_parallel,created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, goal, engine, model, effort, max_parallel, time.time()))
        self.conn.commit()
        return self.orchestration(cur.lastrowid)  # type: ignore[return-value]

    def orchestration(self, oid: int) -> Orchestration | None:
        r = self.conn.execute("SELECT * FROM orchestrations WHERE id=?", (oid,)).fetchone()
        return self._orch(r) if r else None

    def orchestrations(self, project_id: int | None = None) -> list[Orchestration]:
        q = "SELECT * FROM orchestrations" + (" WHERE project_id=?" if project_id else "") + " ORDER BY id DESC"
        return [self._orch(r) for r in self.conn.execute(q, (project_id,) if project_id else ())]

    def orchestration_for_session(self, sid: int) -> Orchestration | None:
        r = self.conn.execute("SELECT * FROM orchestrations WHERE session_id=?", (sid,)).fetchone()
        return self._orch(r) if r else None

    def update_orchestration(self, oid: int, **fields: Any) -> None:
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE orchestrations SET {cols} WHERE id=?", (*fields.values(), oid))
            self.conn.commit()

    @staticmethod
    def _task(r: sqlite3.Row) -> Task:
        return Task(r["id"], r["orchestration_id"], r["session_id"], r["engine"], r["model"], r["effort"], r["title"],
                    r["instructions"], r["cwd"], r["worktree"], r["status"],
                    json.loads(r["result_json"]) if r["result_json"] else None,
                    r["created_at"], r["started_at"], r["finished_at"], r["pick_reason"], bool(r["auto_picked"]),
                    bool(r["reused"]), bool(r["escalated"]),
                    json.loads(r["proposal_json"]) if r["proposal_json"] else None, r["queue_pos"],
                    r["reviewed_at"], json.loads(r["depends_json"] or "[]"), json.loads(r["done_when_json"] or "[]"),
                    json.loads(r["files_json"] or "[]"), json.loads(r["checks_json"]) if r["checks_json"] else None,
                    r["merge_state"], r["merge_note"], json.loads(r["hold_json"]) if r["hold_json"] else None)

    def add_task(self, oid: int, engine: str, model: str, effort: str, title: str, instructions: str,
                 cwd: str | None = None) -> Task:
        cur = self.conn.execute(
            "INSERT INTO tasks(orchestration_id,engine,model,effort,title,instructions,cwd,created_at,queue_pos) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (oid, engine, model, effort, title, instructions, cwd, time.time(), time.time()))
        self.conn.commit()
        return self.task(cur.lastrowid)  # type: ignore[return-value]

    def task(self, tid: int) -> Task | None:
        r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return self._task(r) if r else None

    def tasks(self, oid: int) -> list[Task]:
        return [self._task(r) for r in self.conn.execute("SELECT * FROM tasks WHERE orchestration_id=? ORDER BY id", (oid,))]

    def board_tasks(self, finished_limit: int = 40) -> list[tuple[Task, int]]:
        """(task, project id) for the Board: every unfinished task plus the most recent finished ones."""
        q = ("SELECT t.*, o.project_id AS pid FROM tasks t JOIN orchestrations o ON o.id=t.orchestration_id "
             "WHERE t.status NOT IN ('done','failed','stopped') OR t.reviewed_at IS NULL AND t.status IN ('done','failed') "
             "UNION SELECT * FROM (SELECT t.*, o.project_id AS pid FROM tasks t JOIN orchestrations o "
             "ON o.id=t.orchestration_id WHERE t.status IN ('done','failed','stopped') "
             "ORDER BY COALESCE(t.finished_at, t.created_at) DESC LIMIT ?)")
        return [(self._task(r), r["pid"]) for r in self.conn.execute(q, (finished_limit,))]

    def queued_tasks(self) -> list[Task]:
        """Every queued task of a running orchestration, in global queue order (what starts next first)."""
        return [self._task(r) for r in self.conn.execute(
            "SELECT t.* FROM tasks t JOIN orchestrations o ON o.id=t.orchestration_id "
            "WHERE t.status='queued' AND o.status='running' ORDER BY COALESCE(t.queue_pos, t.created_at), t.id")]

    def busy_counts(self) -> tuple[int, int]:
        """(sub-agent tasks running, scheduled runs running): what the global max parallel counts."""
        t = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('running','waiting')").fetchone()[0]
        r = self.conn.execute("SELECT COUNT(*) FROM runs WHERE status='running' "
                              "AND COALESCE(note,'')!='orchestration'").fetchone()[0]  # its tasks count instead
        return int(t), int(r)

    def task_for_session(self, sid: int) -> Task | None:
        r = self.conn.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        return self._task(r) if r else None

    def update_task(self, tid: int, **fields: Any) -> None:
        if "result" in fields:
            res = fields.pop("result")
            fields["result_json"] = json.dumps(res) if res is not None else None
        if "proposal" in fields:
            prop = fields.pop("proposal")
            fields["proposal_json"] = json.dumps(prop) if prop is not None else None
        for key, col in (("depends_on", "depends_json"), ("done_when", "done_when_json"), ("files", "files_json"),
                         ("checks", "checks_json"), ("hold", "hold_json")):
            if key in fields:
                val = fields.pop(key)
                fields[col] = json.dumps(val) if val is not None else None
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))
            self.conn.commit()

    # -- questions (ask_user)
    @staticmethod
    def _q(r: sqlite3.Row) -> Question:
        return Question(r["id"], r["orchestration_id"], r["text"], json.loads(r["options_json"] or "[]"), r["answer"],
                        r["created_at"], r["answered_at"], r["recommended"])

    def add_question(self, oid: int, text: str, options: list[str] | None = None,
                     recommended: str | None = None) -> Question:
        cur = self.conn.execute(
            "INSERT INTO questions(orchestration_id,text,options_json,created_at,recommended) VALUES(?,?,?,?,?)",
            (oid, text, json.dumps(options or []), time.time(), recommended or None))
        self.conn.commit()
        return self.question(cur.lastrowid)  # type: ignore[return-value]

    def question(self, qid: int) -> Question | None:
        r = self.conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        return self._q(r) if r else None

    def open_questions(self, oid: int) -> list[Question]:
        return [self._q(r) for r in self.conn.execute(
            "SELECT * FROM questions WHERE orchestration_id=? AND answer IS NULL ORDER BY id", (oid,))]

    def all_open_questions(self) -> list[Question]:
        """Unanswered questions of running orchestrations (the "Waiting on you" inbox)."""
        return [self._q(r) for r in self.conn.execute(
            "SELECT q.* FROM questions q JOIN orchestrations o ON o.id=q.orchestration_id "
            "WHERE q.answer IS NULL AND o.status='running' ORDER BY q.id")]

    def active_tasks(self) -> list[Task]:
        """Every task that is not finished, or finished but still waiting for a merge decision."""
        return [self._task(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE status IN ('proposed','queued','running','waiting','checking') "
            "OR merge_state IN ('review','conflict') ORDER BY id")]

    def tasks_running(self) -> list[Task]:
        return [self._task(r) for r in self.conn.execute(
            "SELECT * FROM tasks WHERE status IN ('running','waiting') ORDER BY id")]

    def answer_question(self, qid: int, answer: str) -> None:
        self.conn.execute("UPDATE questions SET answer=?, answered_at=? WHERE id=?", (answer, time.time(), qid))
        self.conn.commit()

    # -- usage
    def usage(self, session_ids: list[int] | int) -> dict[str, Any]:
        """Token/cost totals from the usage records of one or more chats."""
        ids = [session_ids] if isinstance(session_ids, int) else list(session_ids)
        tot = {"input": 0, "output": 0, "cached": 0, "cost": 0.0, "turns": 0}
        if not ids:
            return tot
        q = f"SELECT content_json FROM messages WHERE kind='usage' AND session_id IN ({','.join('?' * len(ids))})"
        for (cj,) in self.conn.execute(q, ids):
            u = json.loads(cj)
            tot["input"] += int(u.get("input") or 0)
            tot["output"] += int(u.get("output") or 0)
            tot["cached"] += int(u.get("cached") or 0)
            tot["cost"] += float(u.get("cost") or 0)
            tot["turns"] += 1
        return tot

    # -- search
    def search_messages(self, query: str, limit: int = 60) -> list[dict[str, Any]]:
        """Chats whose title, summary or message text contains `query` (case-insensitive)."""
        q = (query or "").strip().lower()
        if len(q) < 2:
            return []
        like = f"%{q}%"
        out: dict[int, dict[str, Any]] = {}
        for r in self.conn.execute(
                "SELECT id, title, summary FROM sessions WHERE status!='hidden' AND "
                "(lower(title) LIKE ? OR lower(COALESCE(summary,'')) LIKE ? OR lower(COALESCE(keywords,'')) LIKE ?) "
                "ORDER BY last_active DESC LIMIT ?", (like, like, like, limit)):
            text = r["title"] if q in r["title"].lower() else (r["summary"] or r["title"])
            out[r["id"]] = {"session_id": r["id"], "snippet": text, "where": "title"}
        for r in self.conn.execute(
                "SELECT m.session_id AS sid, m.content_json AS cj FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE s.status!='hidden' AND m.kind='text' AND lower(m.content_json) LIKE ? "
                "ORDER BY s.last_active DESC, m.id DESC LIMIT ?", (like, limit * 4)):
            if r["sid"] in out:
                continue
            from orchestrator.core.harness_text import clean

            text = clean(json.loads(r["cj"]).get("text", ""))
            i = text.lower().find(q)
            if i < 0:
                continue
            a, b = max(0, i - 50), min(len(text), i + len(q) + 70)
            snip = ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")
            out[r["sid"]] = {"session_id": r["sid"], "snippet": snip, "where": "message"}
            if len(out) >= limit:
                break
        return list(out.values())[:limit]

    def all_sessions(self, limit: int = 2000) -> list[Session]:
        return [self._sess(r) for r in self.conn.execute(
            "SELECT * FROM sessions WHERE status!='hidden' ORDER BY last_active DESC LIMIT ?", (limit,))]

    # -- models
    def models(self, engine: str | None = None) -> list[Model]:
        q = "SELECT * FROM models" + (" WHERE engine=?" if engine else "") + " ORDER BY engine, sort, id"
        return [Model(r["engine"], r["id"], r["label"], json.loads(r["efforts_json"]), bool(r["enabled"]))
                for r in self.conn.execute(q, (engine,) if engine else ())]

    def save_models(self, models: list[Model]) -> None:
        self.conn.execute("DELETE FROM models")
        self.conn.executemany(
            "INSERT OR REPLACE INTO models(engine,id,label,efforts_json,sort,enabled) VALUES(?,?,?,?,?,?)",
            [(m.engine, m.id, m.label, json.dumps(m.efforts), i, int(m.enabled)) for i, m in enumerate(models)])
        self.conn.commit()

    def set_model_enabled(self, engine: str, model_id: str | None, enabled: bool) -> None:
        """Turn one model (or, with model_id None, every model of `engine`) on or off."""
        if model_id is None:
            self.conn.execute("UPDATE models SET enabled=? WHERE engine=?", (int(enabled), engine))
        else:
            self.conn.execute("UPDATE models SET enabled=? WHERE engine=? AND id=?", (int(enabled), engine, model_id))
        self.conn.commit()

    def reset_models(self) -> None:
        self.save_models([Model(e, i, l, ef) for e, i, l, ef in SEED_MODELS])

    def default_model(self, engine: str) -> tuple[str, str]:
        saved = self.get_setting(f"default.{engine}")
        if saved:
            return saved[0], saved[1]
        return DEFAULTS[engine]
