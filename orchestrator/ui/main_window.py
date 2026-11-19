"""Top-level Fluent window, plus tray / close-to-tray wiring."""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QHBoxLayout, QSplitter, QSystemTrayIcon, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, FluentIcon as FIF, FluentWindow, NavigationItemPosition

from orchestrator import startup
from orchestrator.core.session_manager import SessionManager
from orchestrator.storage.db import Database


class LazyPage(QWidget):
    """A navigation page whose real widget is built on first use, or when deferred startup reaches it.

    Attribute access the page itself does not have is forwarded to the real widget (building it if
    needed), so callers can treat ``win.chats.sidebar`` etc. the same either way.
    """

    def __init__(self, name: str, factory, on_built=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._factory = factory
        self._on_built = on_built
        self.content: QWidget | None = None
        self.setObjectName(name)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._placeholder = CaptionLabel("Loading…", self)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._lay.addWidget(self._placeholder)

    def ensure(self) -> QWidget:
        if self.content is None and self._factory is not None:
            factory, self._factory = self._factory, None
            w = factory(self)
            self._placeholder.hide()
            self._placeholder.deleteLater()
            self._lay.addWidget(w)
            self.content = w
            if self._on_built:
                self._on_built(w)
        return self.content

    @property
    def built(self) -> bool:
        return self.content is not None

    def showEvent(self, e) -> None:  # noqa: N802
        self.ensure()
        super().showEvent(e)

    def __getattr__(self, name: str):
        if name.startswith("_") or name in ("content", "built"):
            raise AttributeError(name)
        return getattr(self.ensure(), name)


class ChatsPage(QWidget):
    def __init__(self, db: Database, mgr: SessionManager, parent: QWidget | None = None) -> None:
        from orchestrator.ui.chat_view import ChatView
        from orchestrator.ui.sidebar import Sidebar

        super().__init__(parent)
        self.setObjectName("chatsPage")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.sidebar = Sidebar(db, mgr, self)
        self.chat = ChatView(db, mgr, self)
        self.db, self.mgr = db, mgr
        self.split = QSplitter(Qt.Horizontal, self)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(1)
        self.split.addWidget(self.chat)
        self.side = None  # second ChatView, built on first "side by side"
        lay.addWidget(self.sidebar)
        lay.addWidget(self.split, 1)
        self.sidebar.sessionSelected.connect(self.chat.show_session)
        self.sidebar.sideBySideRequested.connect(self.open_side)
        self.chat.newChatRequested.connect(self.sidebar.new_chat)
        self.chat.addProjectRequested.connect(self.sidebar.add_project)
        self.chat.sideBySideRequested.connect(self.open_side)

    def open_side(self, sid: int) -> None:
        """Show another chat next to the current one (e.g. the orchestrator and one of its agents)."""
        from orchestrator.ui.chat_view import ChatView

        if self.side is None:
            self.side = ChatView(self.db, self.mgr, self)
            self.side.set_side(True)
            self.side.closeRequested.connect(self.close_side)
            self.side.sideBySideRequested.connect(self.open_side)
            if getattr(self.chat, "app", None) is not None:
                self.side.set_app_control(self.chat.app)
            self.split.addWidget(self.side)
        if self.side.isHidden():  # size the pane first so messages wrap to its real width
            self.side.show()
            w = max(2, self.split.width())
            self.split.setSizes([w // 2, w - w // 2])
        self.side.show_session(sid)

    def close_side(self) -> None:
        if self.side is not None:
            self.side.hide()
            self.side.show_session(None)

    @property
    def side_sid(self) -> int | None:
        if self.side is None or self.side.isHidden() or not self.side.session:
            return None
        return self.side.session.id


class MainWindow(FluentWindow):
    def __init__(self, db: Database, mgr: SessionManager, icon: QIcon | None = None, indexer=None,
                 runner=None, orchestrations=None, lazy: bool = False) -> None:
        """``lazy``: build only the shell and Projects home now, the rest after the first paint
        (``finish_startup``). Without it the window is complete when the constructor returns."""
        super().__init__()
        self.db = db
        self.mgr = mgr
        self._quitting = False
        self.setWindowTitle("Orchestrator")
        self.app_icon = icon or QIcon()
        if icon:
            self.setWindowIcon(icon)
        self.resize(1320, 860)
        self.setMinimumSize(QSize(800, 600))
        from orchestrator.ui.projects_page import ProjectsPage

        self.runner = runner
        self.indexer = indexer
        self.om = orchestrations
        self.summarizer = None
        self.tray = None
        self.searchPalette = None
        self.shortcuts: list[QShortcut] = []
        self._startup_started = False
        self.startup_done = False
        # Shell: Projects home (real data from the local database) plus placeholder pages.
        self.projects = ProjectsPage(db, self)
        self.projects.set_loading(indexer is not None)
        self.chats = LazyPage("chatsPage", lambda p: ChatsPage(db, mgr, p), self._chats_built, self)
        self.orchestrate = LazyPage("orchestratePage", self._make_orchestrate, self._orchestrate_built, self)
        self.board = LazyPage("boardPage", self._make_board, self._board_built, self)
        self.schedules = (LazyPage("schedulesPage", self._make_schedules, self._schedules_built, self)
                          if runner is not None else None)
        self.settings = LazyPage("settingsPage", self._make_settings, self._settings_built, self)
        self.usageBar = None
        self.app_control = None
        self.usage = LazyPage("usagePage", self._make_usage, None, self)
        self.mcp = LazyPage("mcpPage", self._make_mcp, None, self)
        self.addSubInterface(self.projects, FIF.HOME, "Projects")
        self.addSubInterface(self.chats, FIF.CHAT, "Chats")
        self.addSubInterface(self.orchestrate, FIF.ROBOT, "Orchestrate")
        self.addSubInterface(self.board, FIF.TILES, "Board")
        if self.schedules is not None:
            self.addSubInterface(self.schedules, FIF.DATE_TIME, "Schedules")
        self.addSubInterface(self.mcp, FIF.IOT, "MCP servers", NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.usage, FIF.SPEED_HIGH, "Usage", NavigationItemPosition.BOTTOM)
        self.addSubInterface(self.settings, FIF.SETTING, "Settings", NavigationItemPosition.BOTTOM)
        self.navigationInterface.setExpandWidth(200)
        self.navigationInterface.setCollapsible(True)
        self.projects.openProject.connect(self.open_project)
        self.projects.projectsChanged.connect(self._reload_sidebar)
        self.stackedWidget.currentChanged.connect(self._on_page)
        if indexer is not None:
            indexer.changed.connect(self._reload_sidebar)
            indexer.changed.connect(self._refresh_projects_if_visible)
            indexer.scanned.connect(self._sessions_scanned)
        if not lazy:
            self.finish_startup(sync=True)

    # ---- deferred startup (after the first paint)
    def finish_startup(self, sync: bool = False) -> None:
        """Build the rest of the window and start background services, one step per event-loop turn
        so the window stays responsive while it fills in. Safe to call more than once."""
        if self._startup_started:
            return
        self._startup_started = True
        steps = [self._step_chats, self._step_orchestrate, self._step_settings, self._step_schedules,
                 self._step_usage, self._step_extras]
        if sync:
            for step in steps:
                step(True)
            self._mark_startup_done()
            return

        def run(i: int = 0) -> None:
            if i >= len(steps):
                self._mark_startup_done()
                return
            steps[i](False)
            QTimer.singleShot(0, lambda: run(i + 1))

        run()

    def _mark_startup_done(self) -> None:
        self.startup_done = True
        startup.part_done("deferred")

    def _step_chats(self, sync: bool) -> None:
        self.ensure_app_control()
        self.chats.ensure()
        if self.indexer is not None:
            self.indexer.start()  # scans engine history on a worker thread

    def _step_orchestrate(self, sync: bool) -> None:
        self._ensure_om()
        self.orchestrate.ensure()
        if self.summarizer is None:
            from orchestrator.core.summaries import Summarizer

            self.summarizer = Summarizer(self.db, self.mgr, self)

    def _step_settings(self, sync: bool) -> None:
        self.settings.ensure()

    def _step_schedules(self, sync: bool) -> None:
        if self.schedules is None:
            return
        if self.runner.scheduler is None:
            self.runner.start()
        self.schedules.ensure()

    def _step_usage(self, sync: bool) -> None:
        self.ensure_usage_bar().start()

    def ensure_usage_bar(self):
        """Title-bar pills (one per provider, 5-hour + weekly bars); click opens the Usage page."""
        if self.usageBar is None:
            from orchestrator.core.events import RateLimit
            from orchestrator.ui.usage_view import UsageBar

            bar = UsageBar(self.db, self.titleBar)
            lay = self.titleBar.hBoxLayout
            lay.insertWidget(lay.count() - 1, bar, 0, Qt.AlignVCenter)  # just left of min/max/close
            bar.openUsage.connect(lambda _p: self.switchTo(self.usage))
            self.mgr.status_changed.connect(
                lambda _sid, st: st in ("done", "error", "idle") and bar.refresh_soon())
            self.mgr.event.connect(lambda _sid, ev: isinstance(ev, RateLimit) and bar.refresh_soon(300))
            self.usageBar = bar
        return self.usageBar

    def _make_usage(self, parent):
        from orchestrator.ui.usage_view import UsagePage

        return UsagePage(self.db, self.ensure_usage_bar(), parent)

    def _step_extras(self, sync: bool) -> None:
        self._install_shortcuts()
        runner = self.runner
        if runner is not None and self.tray is None and QSystemTrayIcon.isSystemTrayAvailable():
            from orchestrator.ui.tray import Tray

            self.tray = Tray(self.db, self.mgr, runner, self.app_icon, self)
            self.tray.openRequested.connect(self.bring_to_front)
            self.tray.openSession.connect(self.open_session)
            self.tray.quitRequested.connect(self.quit_app)
            QApplication.instance().setQuitOnLastWindowClosed(False)
            runner.run_started.connect(lambda *_: self._reload_sidebar())
            self._ensure_om().question_asked.connect(self._question_toast)
            self.mgr.approval_requested.connect(self._subagent_approval_toast)

    def ensure_app_control(self):
        """Settings changed from any chat (MCP "app" tools on every chat + simple local commands)."""
        if self.app_control is None:
            from orchestrator.core.app_control import AppControl

            ctl = AppControl(self.db, self.runner, self)
            self.mgr.mcp_providers.append(ctl.mcp_for)
            ctl.changed.connect(self._app_changed)
            ctl.confirm_requested.connect(self._app_confirm)
            self.app_control = ctl
            if self.chats.built:
                self.chats.chat.set_app_control(ctl)
        return self.app_control

    def _app_changed(self, area: str) -> None:
        """Refresh what shows the setting a chat just changed."""
        if area == "preferences" and self.settings.built:
            self.settings.rules.load()
        elif area == "models":
            if self.settings.built:
                self.settings.models.load()
                self.settings.rules.load()
            self._models_changed()
        elif area == "schedules" and self.schedules is not None and self.schedules.built:
            self.schedules.reload()
        elif area == "settings" and self.settings.built:
            from orchestrator.core import concurrency

            self.settings.globalMax.blockSignals(True)
            self.settings.globalMax.setValue(concurrency.global_limit(self.db))
            self.settings.globalMax.blockSignals(False)
        elif area == "appearance" and self.settings.built:
            from orchestrator.ui import appearance

            card = self.settings.appearance
            card.look = appearance.load(self.db)
            card._paint_swatches()

    def _app_confirm(self, sid, text: str, fut) -> None:
        """Allow / Deny for a destructive change: in the chat that asked, else a dialog."""
        chat = self.chats.chat if self.chats.built else None
        if chat is not None and chat.session and chat.session.id == sid and chat.isVisible():
            chat.show_confirm(text, fut)
            return
        from qfluentwidgets import MessageBox

        box = MessageBox("A chat wants to change a setting", text, self)
        box.yesButton.setText("Allow")
        box.cancelButton.setText("Deny")
        box.yesSignal.connect(lambda: fut.done() or fut.set_result(True))
        box.cancelSignal.connect(lambda: fut.done() or fut.set_result(False))
        fut.add_done_callback(lambda _f: box.isVisible() and box.close())
        box.open()

    def _ensure_om(self):
        if self.om is None:
            from orchestrator.core.orchestration import OrchestrationManager

            self.om = OrchestrationManager(self.db, self.mgr, self)
            self.om.changed.connect(lambda _o: self.chats.built and self.chats.sidebar.reload_soon())
            if self.runner is not None:  # scheduled orchestrations start through the same manager
                self.runner.start_orchestration = self.om.start
        return self.om

    def _reload_sidebar(self) -> None:
        if self.chats.built:
            self.chats.sidebar.reload()

    # page factories / wiring (each runs when its page is first built)
    def _chats_built(self, page) -> None:
        if self.app_control is not None:
            page.chat.set_app_control(self.app_control)
        page.sidebar.set_loading(self.projects.loading)
        page.chat.openSettingsRequested.connect(lambda: self.switchTo(self.settings))
        if self.schedules is not None:
            page.chat.runOnceRequested.connect(self._run_once_from_chat)

    def _make_orchestrate(self, parent):
        from orchestrator.ui.orchestrate_page import OrchestratePage

        return OrchestratePage(self.db, self.mgr, self._ensure_om(), parent)

    def _orchestrate_built(self, page) -> None:
        page.openChat.connect(self.open_task_chat)
        page.runner = self.runner
        page.projectsChanged.connect(self._reload_sidebar)
        page.projectsChanged.connect(self._refresh_projects_if_visible)

    def _make_mcp(self, parent):
        from orchestrator.ui.mcp_page import McpPage

        return McpPage(self.db, parent)

    def _make_board(self, parent):
        from orchestrator.ui.board_page import BoardPage

        return BoardPage(self.db, self._ensure_om(), parent)

    def _board_built(self, page) -> None:
        page.openChat.connect(self.open_task_chat)

    def _make_schedules(self, parent):
        from orchestrator.ui.schedules_page import SchedulesPage

        if self.runner.scheduler is None:
            self.runner.start()
        return SchedulesPage(self.db, self.runner, parent)

    def _schedules_built(self, page) -> None:
        page.openChat.connect(self.open_session)

    def _make_settings(self, parent):
        from orchestrator.ui.settings_page import SettingsPage

        return SettingsPage(self.db, parent)

    def _settings_built(self, page) -> None:
        page.models.changed.connect(self._models_changed)

    def _sessions_scanned(self) -> None:
        if self.projects.loading:
            self.projects.set_loading(False)
            if self.chats.built:
                self.chats.sidebar.set_loading(False)
        startup.part_done("sessions")

    def _models_changed(self) -> None:
        if self.chats.built:
            self.chats.chat.refresh_models()
        if self.schedules is not None and self.schedules.built:
            self.schedules.reload()

    # ---- keyboard
    def _install_shortcuts(self) -> None:
        from orchestrator.ui.search_palette import SearchPalette

        if self.searchPalette is not None:
            return
        self.searchPalette = SearchPalette(self.db, self)
        self.searchPalette.openSession.connect(self.open_session)
        pages = [self.projects, self.chats, self.orchestrate] + ([self.schedules] if self.schedules else [])
        keys = {
            "Ctrl+K": self.searchPalette.popup,
            "Ctrl+N": self._shortcut_new_chat,
            "Ctrl+L": self._shortcut_focus_prompt,
            "Ctrl+.": self._shortcut_stop,
            "Ctrl+D": self._shortcut_diff,
            "Ctrl+,": lambda: self.switchTo(self.settings),
            "F1": self.show_shortcuts,
        }
        for i, page in enumerate(pages, 1):
            keys[f"Ctrl+{i}"] = (lambda pg=page: self.switchTo(pg))
        self.shortcuts = []
        for seq, fn in keys.items():
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(fn)
            self.shortcuts.append(sc)

    def _shortcut_new_chat(self) -> None:
        self.switchTo(self.chats)
        self.chats.sidebar.new_chat()

    def _shortcut_focus_prompt(self) -> None:
        self.switchTo(self.chats)
        self.chats.chat.prompt.focus()

    def _shortcut_stop(self) -> None:
        if self.stackedWidget.currentWidget() is self.chats and self.chats.built:
            self.chats.chat._stop()

    def _shortcut_diff(self) -> None:
        if self.stackedWidget.currentWidget() is self.chats and self.chats.built:
            self.chats.chat.show_diff()

    def show_shortcuts(self) -> None:
        from qfluentwidgets import MessageBox

        from orchestrator.ui.search_palette import SHORTCUTS

        text = "\n".join(f"{k}    —    {v}" for k, v in SHORTCUTS)
        box = MessageBox("Keyboard shortcuts", text, self)
        box.cancelButton.hide()
        box.yesButton.setText("Close")
        box.exec()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        if getattr(self, "searchPalette", None) is not None and self.searchPalette.isVisible():
            self.searchPalette.popup()

    def _question_toast(self, oid: int, qid: int) -> None:
        q = self.db.question(qid)
        o = self.db.orchestration(oid)
        if q and self.tray is not None:
            self.tray.notify("The planner asks", q.text[:200], o.session_id if o else None, "warning")

    def _subagent_approval_toast(self, sid: int, req, _fut) -> None:
        # The tray already notifies when the window is hidden; sub-agents also notify when it is visible.
        t = self.db.task_for_session(sid)
        if t and self.tray is not None and self.tray._window_visible():
            self.tray.notify("Sub-agent needs you", f"{t.title} wants to use {req.tool}", sid, "warning")

    def _refresh_projects_if_visible(self) -> None:
        if self.stackedWidget.currentWidget() is self.projects:
            self.projects.reload()

    def open_project(self, pid: int) -> None:
        """Projects home → that project's chats (latest chat, or a fresh one)."""
        self.switchTo(self.chats)
        sessions = self.db.sessions(pid)
        if sessions:
            self.chats.sidebar.select(sessions[0].id)
        else:
            self.chats.sidebar.new_chat(pid)

    def _on_page(self, _i: int) -> None:
        if self.stackedWidget.currentWidget() is self.projects:
            self.projects.reload()
        if self.stackedWidget.currentWidget() is self.orchestrate:
            self.orchestrate.reload()
        if self.stackedWidget.currentWidget() is self.settings:
            self.settings.refresh()
        elif self.schedules is not None and self.stackedWidget.currentWidget() is self.schedules:
            self.schedules.reload()

    def _run_once_from_chat(self, prefill: dict, anchor) -> None:
        if self.schedules is None:
            return
        self.schedules.run_once(prefill, anchor)

    # ---- tray behavior
    def bring_to_front(self) -> None:
        from orchestrator.single_instance import bring_window_to_front

        bring_window_to_front(self)

    def open_session(self, sid: int) -> None:
        self.bring_to_front()
        self.switchTo(self.chats)
        self.chats.sidebar.select(sid)

    def open_pair(self, main_sid: int, side_sid: int) -> None:
        """Orchestrator chat on the left, one agent's chat on the right."""
        self.open_session(main_sid)
        self.chats.open_side(side_sid)

    def open_task_chat(self, sid: int) -> None:
        """Open a sub-agent's chat next to its orchestrator when it has one, else on its own."""
        s = self.db.session(sid)
        if s and s.parent_id and self.db.session(s.parent_id):
            self.open_pair(s.parent_id, sid)
        else:
            self.open_session(sid)

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.tray is not None and not self._quitting and self.db.get_setting("ui.close_to_tray", True):
            e.ignore()
            self.hide()
            if not self.db.get_setting("ui.tray_hint_shown", False):
                self.db.set_setting("ui.tray_hint_shown", True)
                self.tray.notify("Orchestrator is still running",
                                 "Schedules keep running in the tray. Right-click the tray icon to quit.")
            return
        self._shutdown()
        super().closeEvent(e)
        QApplication.instance().quit()

    def running_turns(self) -> list[int]:
        """Chats / sub-agents / scheduled runs with a turn in progress."""
        return [sid for sid in list(self.mgr.live) if self.mgr.is_running(sid)]

    def quit_app(self, confirm: bool = True) -> None:
        """Quit for real (tray › Quit, `Orchestrator --quit`): asks first when agents are working."""
        n = len(self.running_turns())
        if confirm and n:
            from qfluentwidgets import MessageBox

            word = "task is" if n == 1 else "tasks are"
            box = MessageBox("Quit Orchestrator?", f"{n} {word} still running. Stop them and quit?", self)
            box.yesButton.setText("Stop and quit")
            box.cancelButton.setText("Keep running")
            if self.isHidden():
                self.bring_to_front()
            if not box.exec():
                return
        self._quitting = True
        self.close()

    def _shutdown(self) -> None:
        """Everything that could keep the process alive: running turns, child processes, servers, timers."""
        from orchestrator.core import procs

        if self.runner is not None:
            self.runner.shutdown()
        for sid in self.running_turns():  # ask engines to stop (Codex: ends its subprocess; Claude: interrupt)
            a = self.mgr._adapters.get(sid)
            if a is not None:
                try:
                    asyncio.ensure_future(a.interrupt())
                except Exception:  # noqa: BLE001 - quitting anyway
                    pass
        for task in list(getattr(self.mgr, "_tasks", {}).values()):
            if not task.done():
                task.cancel()
        try:
            procs.kill_children()  # claude.exe / codex.exe and the MCP servers they started
        except Exception:  # noqa: BLE001
            pass
        if self.om is not None:
            self.om.bridge.stop()
        if self.app_control is not None:
            self.app_control.bridge.stop()
        server = getattr(self, "_instance_server", None)
        if server is not None:
            server.close()
        if self.usageBar is not None:
            self.usageBar._timer.stop()
        if self.indexer is not None and hasattr(self.indexer, "stop"):
            self.indexer.stop()
        if self.tray is not None:
            self.tray.hide()
