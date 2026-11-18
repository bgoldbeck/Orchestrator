"""System tray icon: running-job badge, menu, and Windows toast notifications."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from orchestrator.core.catalog import ENGINES
from orchestrator.core.scheduler import ScheduleRunner
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database


def badge_icon(base: QIcon, count: int) -> QIcon:
    pm = base.pixmap(64, 64)
    if count <= 0:
        return QIcon(pm)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#f85149"))
    r = QRect(30, 30, 34, 34)
    p.drawEllipse(r)
    p.setPen(QColor("white"))
    f = QFont("Segoe UI")
    f.setBold(True)
    f.setPixelSize(24 if count < 10 else 18)
    p.setFont(f)
    p.drawText(r, Qt.AlignCenter, str(count) if count < 100 else "99")
    p.end()
    return QIcon(pm)


class Tray(QObject):
    openRequested = Signal()
    openSession = Signal(int)
    quitRequested = Signal()

    def __init__(self, db: Database, mgr: SessionManager, runner: ScheduleRunner, icon: QIcon, window) -> None:
        super().__init__(window)
        self.db = db
        self.mgr = mgr
        self.runner = runner
        self.base_icon = icon
        self.window = window
        self._last_sid: int | None = None
        self.notified: list[tuple[str, str]] = []  # (title, text) log, used by tests

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Orchestrator")
        menu = QMenu()
        self.menu = menu
        self.openAct = QAction("Open Orchestrator", menu, triggered=self.openRequested.emit)
        self.pauseAct = QAction("Pause schedules", menu, checkable=True)
        self.pauseAct.setChecked(runner.paused)
        self.pauseAct.toggled.connect(runner.set_paused)
        self.quitAct = QAction("Quit", menu, triggered=self.quitRequested.emit)
        menu.addAction(self.openAct)
        menu.addSeparator()
        menu.addAction(self.pauseAct)
        menu.addSeparator()
        menu.addAction(self.quitAct)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self._message_clicked)
        self.tray.show()

        mgr.status_changed.connect(self._on_status)
        mgr.approval_requested.connect(self._on_approval)
        runner.run_finished.connect(self._on_run_finished)
        runner.run_blocked.connect(self._on_run_blocked)
        runner.changed.connect(self._sync_pause)
        self.update_badge()

    # ---- state
    def running_count(self) -> int:
        return sum(1 for sid in list(self.mgr._tasks) if self.mgr.is_running(sid))

    def update_badge(self) -> None:
        n = self.running_count()
        self.tray.setIcon(badge_icon(self.base_icon, n))
        paused = " · schedules paused" if self.runner.paused else ""
        self.tray.setToolTip(f"Orchestrator — {n} running{paused}" if n else f"Orchestrator{paused}")

    def _sync_pause(self) -> None:
        self.pauseAct.blockSignals(True)
        self.pauseAct.setChecked(self.runner.paused)
        self.pauseAct.blockSignals(False)
        self.update_badge()

    def _window_visible(self) -> bool:
        w = self.window
        return w.isVisible() and not w.isMinimized() and QApplication.activeWindow() is not None

    def notify(self, title: str, text: str, sid: int | None = None, kind: str = "info") -> None:
        if not self.db.get_setting("ui.notifications", True):
            return
        self._last_sid = sid
        self.notified.append((title, text))
        if kind in ("error", "warning"):
            icon = QSystemTrayIcon.Critical if kind == "error" else QSystemTrayIcon.Warning
            self.tray.showMessage(title, text, icon, 6000)
        else:  # our own icon on ordinary toasts
            self.tray.showMessage(title, text, self.base_icon, 6000)

    # ---- events
    def _on_status(self, sid: int, status: str) -> None:
        self.update_badge()
        if sid in self.runner._active:
            return  # schedule runs report via run_finished
        if status in ("done", "error") and not self._window_visible():
            s = self.db.session(sid)
            if s:
                verb = "finished" if status == "done" else "failed"
                self.notify(f"{ENGINES.get(s.engine, s.engine)} {verb}", s.title, sid, "error" if status == "error" else "info")

    def _on_approval(self, sid: int, req, _fut) -> None:
        if not self._window_visible():
            s = self.db.session(sid)
            self.notify("Needs your input", f"{s.title if s else 'A chat'} wants to use {req.tool}", sid, "warning")

    def _on_run_finished(self, sched_id: int, sid: int, status: str) -> None:
        s = self.db.schedule(sched_id)
        name = s.name if s else "Schedule"
        text = {"done": "finished", "error": "failed", "stopped": "was stopped"}.get(status, status)
        self.notify(f"{name} {text}", "Click to open the chat.", sid, "error" if status == "error" else "info")
        self.update_badge()

    def _on_run_blocked(self, sched_id: int, why: str) -> None:
        s = self.db.schedule(sched_id)
        self.notify(f"{s.name if s else 'Schedule'} skipped", why, None, "warning")

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.openRequested.emit()

    def _message_clicked(self) -> None:
        if self._last_sid:
            self.openSession.emit(self._last_sid)
        else:
            self.openRequested.emit()

    def hide(self) -> None:
        self.tray.hide()
