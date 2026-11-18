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


@dataclass
class Project:
    id: int
    name: str
    path: str


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
        if not self.conn.execute("SELECT 1 FROM models LIMIT 1").fetchone():
            self.reset_models()
        # Sessions left "running" by a crash are not running anymore.
        self.conn.execute("UPDATE sessions SET status='idle' WHERE status IN ('running','waiting')")
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
    def projects(self) -> list[Project]:
        return [Project(r["id"], r["name"], r["path"]) for r in
                self.conn.execute("SELECT * FROM projects ORDER BY name COLLATE NOCASE")]

    def add_project(self, path: str, name: str | None = None) -> Project:
        p = Path(path)
        name = name or p.name or str(p)
        self.conn.execute("INSERT OR IGNORE INTO projects(name,path,created_at) VALUES(?,?,?)", (name, str(p), time.time()))
        self.conn.commit()
        r = self.conn.execute("SELECT * FROM projects WHERE path=?", (str(p),)).fetchone()
        return Project(r["id"], r["name"], r["path"])

    def project(self, pid: int) -> Project | None:
        r = self.conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return Project(r["id"], r["name"], r["path"]) if r else None

    def remove_project(self, pid: int) -> None:
        self.conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.conn.commit()

    # -- sessions
    @staticmethod
    def _sess(r: sqlite3.Row) -> Session:
        return Session(r["id"], r["project_id"], r["engine"], r["engine_session_id"], r["title"],
                       r["model"], r["effort"], r["status"], r["last_active"])

    def sessions(self, project_id: int) -> list[Session]:
        return [self._sess(r) for r in self.conn.execute(
            "SELECT * FROM sessions WHERE project_id=? ORDER BY last_active DESC", (project_id,))]

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

    def delete_session(self, sid: int) -> None:
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
