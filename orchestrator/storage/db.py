"""SQLite storage in %APPDATA%\\Orchestrator\\orchestrator.db (no secrets)."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
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
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_id, last_active);
"""


MIGRATIONS = [
    ("projects", "auto", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "file_path", "TEXT"),
    ("sessions", "file_mtime", "REAL"),
    ("sessions", "cwd", "TEXT"),
]


@dataclass
class Project:
    id: int
    name: str
    path: str
    auto: bool = False


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


@dataclass
class Model:
    engine: str
    id: str
    label: str
    efforts: list[str]


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
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_engine_id ON sessions(engine, engine_session_id)"
                          " WHERE engine_session_id IS NOT NULL")
        if not self.conn.execute("SELECT 1 FROM models LIMIT 1").fetchone():
            self.reset_models()
        # Sessions left "running" by a crash are not running anymore.
        self.conn.execute("UPDATE sessions SET status='idle' WHERE status IN ('running','waiting')")
        self.conn.execute("DELETE FROM projects WHERE auto=1 AND id NOT IN (SELECT project_id FROM sessions)")
        self.conn.commit()

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
        return Project(r["id"], r["name"], r["path"], bool(r["auto"]))

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

    def remove_project(self, pid: int) -> None:
        self.conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.conn.commit()

    # -- sessions
    @staticmethod
    def _sess(r: sqlite3.Row) -> Session:
        return Session(r["id"], r["project_id"], r["engine"], r["engine_session_id"], r["title"],
                       r["model"], r["effort"], r["status"], r["last_active"], r["source"], r["file_path"], r["cwd"])

    def sessions(self, project_id: int) -> list[Session]:
        return [self._sess(r) for r in self.conn.execute(
            "SELECT * FROM sessions WHERE project_id=? AND status!='hidden' ORDER BY last_active DESC", (project_id,))]

    def session(self, sid: int) -> Session | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return self._sess(r) if r else None

    def create_session(self, project_id: int, engine: str, model: str, effort: str) -> Session:
        cur = self.conn.execute(
            "INSERT INTO sessions(project_id,engine,model,effort,last_active) VALUES(?,?,?,?,?)",
            (project_id, engine, model, effort, time.time()))
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

    # -- models
    def models(self, engine: str | None = None) -> list[Model]:
        q = "SELECT * FROM models" + (" WHERE engine=?" if engine else "") + " ORDER BY engine, sort, id"
        return [Model(r["engine"], r["id"], r["label"], json.loads(r["efforts_json"]))
                for r in self.conn.execute(q, (engine,) if engine else ())]

    def save_models(self, models: list[Model]) -> None:
        self.conn.execute("DELETE FROM models")
        self.conn.executemany("INSERT OR REPLACE INTO models(engine,id,label,efforts_json,sort) VALUES(?,?,?,?,?)",
                              [(m.engine, m.id, m.label, json.dumps(m.efforts), i) for i, m in enumerate(models)])
        self.conn.commit()

    def reset_models(self) -> None:
        self.save_models([Model(e, i, l, ef) for e, i, l, ef in SEED_MODELS])

    def default_model(self, engine: str) -> tuple[str, str]:
        saved = self.get_setting(f"default.{engine}")
        if saved:
            return saved[0], saved[1]
        return DEFAULTS[engine]
