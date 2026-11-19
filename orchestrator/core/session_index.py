"""Background indexer: polls the engines' session folders, parses changed files
in a worker thread, and upserts them into SQLite on the UI thread."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from orchestrator.core import indexer as ix
from orchestrator.storage.db import Database, app_dir

POLL_MS = 8000


def _under(child: str, parent: str) -> bool:
    c, p = ix.norm_path(child), ix.norm_path(parent)
    return bool(c and p) and (c == p or c.startswith(p.rstrip("\\/") + os.sep))


def scan_changed(known: dict[str, float], claude_root: Path | None = None,
                 codex_home: Path | None = None) -> list[ix.SessionInfo]:
    """Parse every session file whose mtime differs from `known`. Pure / thread-safe."""
    out: list[ix.SessionInfo] = []
    titles: dict[str, str] | None = None
    for path in ix.scan_claude(claude_root):
        try:
            if known.get(str(path)) == path.stat().st_mtime:
                continue
        except OSError:
            continue
        info = ix.parse_claude_file(path)
        if info:
            out.append(info)
    for path in ix.scan_codex(codex_home):
        try:
            if known.get(str(path)) == path.stat().st_mtime:
                continue
        except OSError:
            continue
        if titles is None:
            titles = ix.codex_titles(codex_home)
        info = ix.parse_codex_file(path, titles)
        if info:
            out.append(info)
    return out


class SessionIndexer(QObject):
    scanned = Signal()  # after every scan, changed or not
    changed = Signal()

    def __init__(self, db: Database, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self._busy = False
        self._known: dict[str, float] = {}
        self._skipped: dict[str, float] = {}  # files we parsed but chose not to import
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._temp = ix.norm_path(tempfile.gettempdir())

    def start(self) -> None:
        self.refresh()
        self._timer.start(POLL_MS)

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        try:
            fresh = self.db.get_setting("index.parser_version") != ix.PARSER_VERSION
            known = {} if fresh else {**self.db.indexed_files(), **self._skipped}
            infos = await asyncio.to_thread(scan_changed, known)
            if self.apply(infos):
                self.changed.emit()
            if fresh:  # every file re-parsed once with the current title rules
                self.db.set_setting("index.parser_version", ix.PARSER_VERSION)
        except Exception as exc:  # indexing must never take the app down
            print("indexer error:", exc)
        finally:
            self._busy = False
            self.scanned.emit()

    def project_for(self, cwd: str) -> int | None:
        if not cwd:
            return None
        best = None
        for p in self.db.projects():
            if _under(cwd, p.path) and (best is None or len(p.path) > len(best.path)):
                best = p
        if best:
            return best.id
        if (ix.norm_path(cwd).startswith(self._temp) or ix.norm_path(cwd).startswith(ix.norm_path(str(app_dir())))
                or not Path(cwd).is_absolute()):
            return None  # throwaway scratch folders: don't clutter the sidebar
        return self.db.add_project(cwd, auto=True).id

    def apply(self, infos: list[ix.SessionInfo]) -> bool:
        changed = False
        for info in infos:
            pid = self.project_for(info.cwd)
            if pid is None:
                self._skipped[info.path] = info.mtime
                continue
            self.db.upsert_imported(info, pid)
            changed = True
        return changed
