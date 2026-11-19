"""Settings: accounts (keyring), model list, engine defaults, appearance."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TableWidget,
    TitleLabel,
    Theme,
    setTheme,
    setThemeColor,
)

from orchestrator.core import auth
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database, Model, app_dir
from orchestrator.ui.theme import ACCENTS, STATUS_COLORS, engine_badge, palette

CREATE_NEW_CONSOLE = 0x00000010


def card(parent: QWidget) -> tuple[QFrame, QVBoxLayout]:
    p = palette()
    f = QFrame(parent)
    f.setObjectName("setCard")
    f.setStyleSheet(f"#setCard {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:12px; }}")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    return f, lay


class StatusChip(QLabel):
    def set_status(self, ok: bool, text: str) -> None:
        col = STATUS_COLORS["done"] if ok else STATUS_COLORS["error"]
        self.setText(f'<span style="color:{col}; font-size:13pt;">●</span>&nbsp; {text}')


class VersionProbe(QThread):
    done = Signal(str, str)

    def run(self) -> None:  # noqa: D401
        for name in ("claude", "codex"):
            exe = shutil.which(name)
            ver = "not found"
            if exe:
                try:
                    r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                                       creationflags=0x08000000 if sys.platform == "win32" else 0)
                    ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "?"
                except Exception as exc:  # noqa: BLE001
                    ver = f"error: {exc}"
            self.done.emit(name, ver)


class AccountCard(QFrame):
    changed = Signal()

    def __init__(self, engine: str, secrets: list[tuple[str, str]], fix_cmd: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.secrets = secrets
        self.fix_cmd = fix_cmd
        f, lay = card(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(f)

        top = QHBoxLayout()
        badge = QLabel(f)
        badge.setPixmap(engine_badge(engine, 28))
        badge.setFixedSize(28, 28)
        name = StrongBodyLabel(ENGINES[engine], f)
        self.chip = StatusChip(f)
        self.detail = CaptionLabel("", f)
        self.detail.setStyleSheet(f"color:{palette()['muted']};")
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(name)
        col.addWidget(self.detail)
        top.addWidget(badge)
        top.addSpacing(6)
        top.addLayout(col, 1)
        top.addWidget(self.chip)
        fix = PushButton(FIF.COMMAND_PROMPT, "Open login terminal", f)
        fix.setToolTip(fix_cmd)
        fix.clicked.connect(self._run_fix)
        top.addWidget(fix)
        lay.addLayout(top)

        row = QHBoxLayout()
        self.kind = ComboBox(f)
        for key, label in secrets:
            self.kind.addItem(label, userData=key)
        self.kind.setFixedWidth(230)
        self.value = PasswordLineEdit(f)
        self.value.setPlaceholderText("Paste token or key (stored in Windows Credential Manager)")
        save = PrimaryPushButton("Save", f)
        clear = PushButton("Clear", f)
        save.clicked.connect(self._save)
        clear.clicked.connect(self._clear)
        row.addWidget(self.kind)
        row.addWidget(self.value, 1)
        row.addWidget(save)
        row.addWidget(clear)
        lay.addLayout(row)
        self.refresh()

    def refresh(self) -> None:
        st = auth.claude_status() if self.engine == "claude" else auth.codex_status()
        self.chip.set_status(st.ok, st.summary)
        self.detail.setText(st.detail)

    def _save(self) -> None:
        val = self.value.text().strip()
        if not val:
            return
        key = self.kind.currentData()
        try:
            auth.set_secret(key, val)
        except Exception as exc:  # noqa: BLE001
            InfoBar.error("Could not save", str(exc), parent=self.window(), position=InfoBarPosition.TOP_RIGHT)
            return
        self.value.clear()
        self.refresh()
        self.changed.emit()
        InfoBar.success("Saved", f"{self.kind.currentText()} stored in Credential Manager.",
                        parent=self.window(), position=InfoBarPosition.TOP_RIGHT, duration=2500)

    def _clear(self) -> None:
        auth.delete_secret(self.kind.currentData())
        self.refresh()
        self.changed.emit()

    def _run_fix(self) -> None:
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/k", self.fix_cmd], creationflags=CREATE_NEW_CONSOLE)


class ModelsCard(QFrame):
    changed = Signal()  # enabled set or list changed: pickers refresh

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self._loading = False
        f, lay = card(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(f)
        hint = CaptionLabel("Editable list. Add new models here, no code change needed. "
                            "Efforts are comma separated. Unchecked models are hidden everywhere and never used.", f)
        hint.setStyleSheet(f"color:{palette()['muted']};")
        lay.addWidget(hint)
        self.table = TableWidget(f)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Enabled", "Engine", "Model ID", "Label", "Efforts"])
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setMinimumHeight(400)
        self.table.itemChanged.connect(self._on_item)
        bulk = QHBoxLayout()
        bulk.setSpacing(4)
        self.bulk: dict[str, tuple] = {}
        for engine, name in ENGINES.items():
            lab = CaptionLabel(name, f)
            on = HyperlinkButton("", "Enable all", f)
            off = HyperlinkButton("", "Disable all", f)
            on.clicked.connect(lambda _=False, e=engine: self._set_all(e, True))
            off.clicked.connect(lambda _=False, e=engine: self._set_all(e, False))
            bulk.addWidget(lab)
            bulk.addWidget(on)
            bulk.addWidget(off)
            bulk.addSpacing(18)
            self.bulk[engine] = (on, off)
        bulk.addStretch(1)
        self.warn = CaptionLabel("", f)
        self.warn.setStyleSheet("color:#f59e0b;")
        lay.addLayout(bulk)
        lay.addWidget(self.warn)
        lay.addWidget(self.table)
        row = QHBoxLayout()
        add_c = PushButton(FIF.ADD, "Add Claude model", f)
        add_x = PushButton(FIF.ADD, "Add Codex model", f)
        rm = PushButton(FIF.DELETE, "Remove selected", f)
        reset = PushButton(FIF.SYNC, "Reset to defaults", f)
        save = PrimaryPushButton(FIF.SAVE, "Save models", f)
        add_c.clicked.connect(lambda: self._add("claude"))
        add_x.clicked.connect(lambda: self._add("codex"))
        rm.clicked.connect(self._remove)
        reset.clicked.connect(self._reset)
        save.clicked.connect(self._save)
        for b in (add_c, add_x, rm, reset):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(save)
        lay.addLayout(row)
        self.load()

    def load(self) -> None:
        self._loading = True
        ms = self.db.models()
        self.table.setRowCount(len(ms))
        for r, m in enumerate(ms):
            self._set_row(r, m.engine, m.id, m.label, ", ".join(m.efforts), m.enabled)
        self._loading = False
        self._update_warning()

    def _set_row(self, r: int, engine: str, mid: str, label: str, efforts: str, enabled: bool = True) -> None:
        was = self._loading
        self._loading = True
        chk = QTableWidgetItem("")
        chk.setFlags((chk.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        chk.setCheckState(Qt.Checked if enabled else Qt.Unchecked)
        self.table.setItem(r, 0, chk)
        e = QTableWidgetItem(engine)
        e.setFlags(e.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 1, e)
        self.table.setItem(r, 2, QTableWidgetItem(mid))
        self.table.setItem(r, 3, QTableWidgetItem(label))
        self.table.setItem(r, 4, QTableWidgetItem(efforts))
        self._loading = was

    def _row_enabled(self, r: int) -> bool:
        it = self.table.item(r, 0)
        return bool(it and it.checkState() == Qt.Checked)

    def _on_item(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 0:
            return
        r = item.row()
        engine = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
        mid = self.table.item(r, 2).text().strip() if self.table.item(r, 2) else ""
        if mid and any(m.id == mid for m in self.db.models(engine)):  # saved row: takes effect right away
            self.db.set_model_enabled(engine, mid, self._row_enabled(r))
            self.changed.emit()
        self._update_warning()

    def _set_all(self, engine: str, enabled: bool) -> None:
        self.db.set_model_enabled(engine, None, enabled)
        self._loading = True
        for r in range(self.table.rowCount()):
            if self.table.item(r, 1) and self.table.item(r, 1).text() == engine:
                self.table.item(r, 0).setCheckState(Qt.Checked if enabled else Qt.Unchecked)
        self._loading = False
        self._update_warning()
        self.changed.emit()

    def _update_warning(self) -> None:
        off = [name for engine, name in ENGINES.items()
               if not any(self.table.item(r, 1) and self.table.item(r, 1).text() == engine and self._row_enabled(r)
                          for r in range(self.table.rowCount()))]
        self.warn.setText(" ".join(f"{n} has no enabled models, so {n} can't be used." for n in off))
        self.warn.setVisible(bool(off))

    def _add(self, engine: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._set_row(r, engine, "new-model-id", "New model", "low, medium, high")
        self.table.editItem(self.table.item(r, 2))

    def _remove(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def _reset(self) -> None:
        self.db.reset_models()
        self.load()
        self.changed.emit()

    def _save(self) -> None:
        out: list[Model] = []
        for r in range(self.table.rowCount()):
            get = lambda c: (self.table.item(r, c).text().strip() if self.table.item(r, c) else "")  # noqa: E731
            if not get(2):
                continue
            efforts = [x.strip() for x in get(4).split(",") if x.strip()]
            out.append(Model(get(1), get(2), get(3) or get(2), efforts, self._row_enabled(r)))
        self.db.save_models(out)
        self._update_warning()
        self.changed.emit()
        InfoBar.success("Models saved", f"{len(out)} models", parent=self.window(),
                        position=InfoBarPosition.TOP_RIGHT, duration=2000)


class SettingsPage(SmoothScrollArea):
    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.setObjectName("settingsPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(36, 28, 36, 36)
        lay.setSpacing(14)
        lay.addWidget(TitleLabel("Settings", body))

        lay.addWidget(SubtitleLabel("Accounts", body))
        self.claude = AccountCard("claude", [(auth.CLAUDE_TOKEN, "Claude OAuth token"),
                                             (auth.ANTHROPIC_KEY, "Anthropic API key")], "claude setup-token", body)
        self.codex = AccountCard("codex", [(auth.OPENAI_KEY, "OpenAI API key (optional)")], "codex login", body)
        lay.addWidget(self.claude)
        lay.addWidget(self.codex)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Models", body))
        self.models = ModelsCard(db, body)
        lay.addWidget(self.models)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Engines", body))
        f, fl = card(body)
        fl.addLayout(self._combo_row(f, "Codex sandbox", "What Codex may do in the project folder.",
                                     "codex.sandbox", ["read-only", "workspace-write", "danger-full-access"], "workspace-write"))
        fl.addLayout(self._combo_row(f, "Claude permissions", "default asks inline before tools run.",
                                     "claude.permission_mode", ["default", "acceptEdits", "plan", "bypassPermissions"], "default"))
        fl.addLayout(self._switch_row(f, "Lean mode for Claude chats",
                                      "Skip your own plugins, skills, hooks and ~/.claude/CLAUDE.md so each prompt "
                                      "is smaller. The project's CLAUDE.md still loads. Each chat can override this.",
                                      "claude.lean_default", False))
        lay.addWidget(f)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Orchestrator", body))
        from orchestrator.core.orchestration import CONFIRM_MODES
        from orchestrator.core.routing import DEFAULT_GUIDE
        from orchestrator.core.summaries import MODES

        fo, fol = card(body)
        fol.addLayout(self._keyed_row(fo, "Reuse and model picks", "When the planner wants to continue an earlier "
                                      "chat (or, with Always ask, for every automatic model pick).",
                                      "orchestrate.confirm", CONFIRM_MODES, "countdown"))
        grow0 = QHBoxLayout()
        gcol = QVBoxLayout()
        gcol.setSpacing(0)
        gcol.addWidget(BodyLabel("Agents running at once (whole app)", fo))
        gsub = CaptionLabel("Orchestration tasks and scheduled runs share this limit; the rest wait in a queue. "
                            "Each orchestration also has its own “Run up to N tasks at once”.", fo)
        gsub.setWordWrap(True)
        gsub.setStyleSheet(f"color:{palette()['muted']};")
        gcol.addWidget(gsub)
        from orchestrator.ui.number_box import NumberBox

        from orchestrator.core import concurrency

        self.globalMax = NumberBox(fo)
        self.globalMax.setObjectName("globalMaxParallel")
        self.globalMax.setRange(1, 32)
        self.globalMax.setValue(concurrency.global_limit(db))
        self.globalMax.valueChanged.connect(lambda v: (db.set_setting("global.max_parallel", int(v)),
                                                       concurrency.slot_freed()))
        grow0.addLayout(gcol, 1)
        grow0.addWidget(self.globalMax)
        fol.addLayout(grow0)
        fol.addLayout(self._switch_row(fo, "Retry failed automatic picks once",
                                       "More effort first, then the next stronger enabled model.",
                                       "orchestrate.escalate", True))
        fol.addLayout(self._switch_row(fo, "Review changes before merging",
                                       "Tasks that ran in their own git worktree wait in Waiting on you for Merge or "
                                       "Discard. Off: they merge on their own when the project's tests pass.",
                                       "orchestrate.review_before_merge", False))
        fol.addLayout(self._switch_row(fo, "Pause new tasks when a usage limit is reached",
                                       "Queued tasks wait for the 5-hour or weekly reset instead of failing; a task "
                                       "the limit stopped goes back in the queue.", "orchestrate.pause_on_limit", True))
        srow = QHBoxLayout()
        scol = QVBoxLayout()
        scol.setSpacing(0)
        scol.addWidget(BodyLabel("Mark a task silent after (minutes)", fo))
        ssub = CaptionLabel("A running task with no output this long gets a Silent badge and shows up in "
                            "Waiting on you. 0 = off.", fo)
        ssub.setStyleSheet(f"color:{palette()['muted']};")
        scol.addWidget(ssub)
        self.silentMin = NumberBox(fo)
        self.silentMin.setObjectName("silentMinutes")
        self.silentMin.setRange(0, 600)
        self.silentMin.setValue(int(db.get_setting("orchestrate.silent_min", 20) or 0))
        self.silentMin.valueChanged.connect(lambda v: db.set_setting("orchestrate.silent_min", int(v)))
        srow.addLayout(scol, 1)
        srow.addWidget(self.silentMin)
        fol.addLayout(srow)
        fol.addWidget(BodyLabel("Test command per project", fo))
        tsub = CaptionLabel("Run by the app on each finished task's work (for example: pytest -q, npm test). "
                            "A failing run marks the task failed with the output, so the orchestrator can fix it. "
                            "Empty = no checks.", fo)
        tsub.setWordWrap(True)
        tsub.setStyleSheet(f"color:{palette()['muted']};")
        fol.addWidget(tsub)
        from qfluentwidgets import ComboBox, LineEdit

        from orchestrator.core import review

        trow = QHBoxLayout()
        self.checkProject = ComboBox(fo)
        self.checkProject.setMinimumWidth(180)
        for pr in db.projects():
            if not pr.auto:
                self.checkProject.addItem(pr.name, userData=pr.id)
        self.checkCmd = LineEdit(fo)
        self.checkCmd.setObjectName("checkCommand")
        self.checkCmd.setPlaceholderText("Test command, e.g. pytest -q")

        def load_cmd(_i: int = 0) -> None:
            pid = self.checkProject.currentData()
            self.checkCmd.setText(review.check_command(db, pid) if pid else "")

        self.checkProject.currentIndexChanged.connect(load_cmd)
        self.checkCmd.editingFinished.connect(
            lambda: self.checkProject.currentData() and review.set_check_command(
                db, self.checkProject.currentData(), self.checkCmd.text()))
        load_cmd()
        trow.addWidget(self.checkProject)
        trow.addWidget(self.checkCmd, 1)
        fol.addLayout(trow)
        fol.addLayout(self._keyed_row(fo, "Session summaries", "Written when a chat goes idle; used to find chats "
                                      "to reuse. Cheap model = the last enabled model in the list above.",
                                      "summaries.mode", MODES, "model"))
        from orchestrator.ui.rules_card import RulesCard

        fol.addWidget(BodyLabel("Model preferences", fo))
        self.rules = RulesCard(db, fo)
        self.models.changed.connect(self.rules.load)  # enabling/disabling models changes which rules apply
        fol.addWidget(self.rules)
        gl = BodyLabel("Routing guide (used when no rule matches)", fo)
        fol.addWidget(gl)
        gs = CaptionLabel("Shown to the planner when it picks engine, model and effort. Edit freely.", fo)
        gs.setStyleSheet(f"color:{palette()['muted']};")
        fol.addWidget(gs)
        from qfluentwidgets import PlainTextEdit

        self.guide = PlainTextEdit(fo)
        self.guide.setPlainText(db.get_setting("routing.guide") or DEFAULT_GUIDE)
        self.guide.setFixedHeight(120)
        fol.addWidget(self.guide)
        grow = QHBoxLayout()
        gsave = PushButton(FIF.SAVE, "Save guide", fo)
        greset = PushButton(FIF.SYNC, "Reset", fo)
        gsave.clicked.connect(lambda: db.set_setting("routing.guide", self.guide.toPlainText().strip() or None))
        greset.clicked.connect(lambda: (db.set_setting("routing.guide", None), self.guide.setPlainText(DEFAULT_GUIDE)))
        grow.addStretch(1)
        grow.addWidget(greset)
        grow.addWidget(gsave)
        fol.addLayout(grow)
        lay.addWidget(fo)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Background", body))
        fb, fbl = card(body)
        fbl.addLayout(self._switch_row(fb, "Keep running in the tray when the window is closed",
                                       "Schedules only run while Orchestrator is running.", "ui.close_to_tray", True))
        fbl.addLayout(self._switch_row(fb, "Notifications", "When a chat or schedule finishes, fails, or needs you.",
                                       "ui.notifications", True))
        fbl.addLayout(self._switch_row(fb, "Start at login", "Starts hidden in the tray when you sign in to Windows.",
                                       None, False, getter=self._autostart_get, setter=self._autostart_set))
        if sys.platform == "win32":
            fbl.addLayout(self._shortcut_row(fb))
        lay.addWidget(fb)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Appearance", body))
        f2, fl2 = card(body)
        from orchestrator.ui.appearance_card import AppearanceCard

        self.appearance = AppearanceCard(db, f2)
        fl2.addWidget(self.appearance)
        lay.addWidget(f2)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("About", body))
        f3, fl3 = card(body)
        self.versions = BodyLabel("Checking engine versions…", f3)
        fl3.addWidget(self.versions)
        dr = QHBoxLayout()
        dl = CaptionLabel(f"Data folder: {app_dir()}", f3)
        dl.setStyleSheet(f"color:{palette()['muted']};")
        ob = PushButton(FIF.FOLDER, "Open", f3)
        ob.clicked.connect(lambda: os.startfile(app_dir()))
        dr.addWidget(dl, 1)
        dr.addWidget(ob)
        fl3.addLayout(dr)
        lay.addWidget(f3)
        lay.addStretch(1)
        self.setWidget(body)

        self._vers: dict[str, str] = {}
        self._probe = VersionProbe(self)
        self._probe.done.connect(self._on_version)
        self._probe.start()

    def _shortcut_row(self, parent) -> QHBoxLayout:
        """Desktop / Start menu shortcuts with the app icon and taskbar identity, so pinning works."""
        from orchestrator.ui.app_icon import shortcut_paths

        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(BodyLabel("Shortcuts", parent))
        self.shortcutNote = CaptionLabel("", parent)
        self.shortcutNote.setWordWrap(True)
        self.shortcutNote.setStyleSheet(f"color:{palette()['muted']};")
        col.addWidget(self.shortcutNote)
        row.addLayout(col, 1)
        self.shortcutBtns = {}
        for where, text in (("desktop", "Create desktop shortcut"), ("start_menu", "Add to Start menu")):
            b = PushButton(FIF.LINK, text, parent)
            b.setObjectName(f"shortcut_{where}")
            b.setMinimumWidth(b.sizeHint().width())
            b.clicked.connect(lambda _=False, w=where: self._make_shortcut(w))
            self.shortcutBtns[where] = b
            row.addWidget(b)
        self._shortcut_paths = shortcut_paths()
        self._update_shortcut_note()
        return row

    def _update_shortcut_note(self) -> None:
        have = [n for w, n in (("desktop", "desktop"), ("start_menu", "Start menu"))
                if self._shortcut_paths[w].exists()]
        self.shortcutNote.setText(
            "Opens Orchestrator with its own icon, and pinning to the taskbar keeps working. "
            + (f"Exists on: {', '.join(have)} (click again to update)." if have else ""))

    def _make_shortcut(self, where: str) -> None:
        from orchestrator.ui.app_icon import create_shortcut

        try:
            path = create_shortcut(self._shortcut_paths[where])
        except Exception as exc:  # noqa: BLE001 - shown to the user
            InfoBar.error("Shortcut not created", str(exc), parent=self.window(), position=InfoBarPosition.TOP_RIGHT,
                          duration=5000)
            return
        self._update_shortcut_note()
        InfoBar.success("Shortcut created", str(path), parent=self.window(), position=InfoBarPosition.TOP_RIGHT,
                        duration=3000)

    def _switch_row(self, parent, title, sub, key, default, getter=None, setter=None) -> QHBoxLayout:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(BodyLabel(title, parent))
        if sub:
            s = CaptionLabel(sub, parent)
            s.setStyleSheet(f"color:{palette()['muted']};")
            col.addWidget(s)
        sw = SwitchButton(parent)
        sw.setOnText("On")
        sw.setOffText("Off")
        sw.setChecked(getter() if getter else bool(self.db.get_setting(key, default)))
        sw.checkedChanged.connect(setter if setter else (lambda v: self.db.set_setting(key, v)))
        row.addLayout(col, 1)
        row.addWidget(sw)
        return row

    @staticmethod
    def _autostart_get() -> bool:
        try:
            from orchestrator.core import autostart

            return autostart.is_enabled()
        except Exception:  # noqa: BLE001
            return False

    def _autostart_set(self, on: bool) -> None:
        from orchestrator.core import autostart

        try:
            autostart.set_enabled(on)
        except Exception as exc:  # noqa: BLE001
            InfoBar.error("Could not change start at login", str(exc), parent=self.window(),
                          position=InfoBarPosition.TOP_RIGHT)

    def _combo_row(self, parent, title, sub, key, options, default, on_change=None) -> QHBoxLayout:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(BodyLabel(title, parent))
        if sub:
            s = CaptionLabel(sub, parent)
            s.setStyleSheet(f"color:{palette()['muted']};")
            col.addWidget(s)
        cb = ComboBox(parent)
        cb.addItems(options)
        cb.setCurrentText(self.db.get_setting(key, default))
        cb.setMinimumWidth(200)

        def changed(v: str) -> None:
            self.db.set_setting(key, v)
            if on_change:
                on_change(v)

        cb.currentTextChanged.connect(changed)
        row.addLayout(col, 1)
        row.addWidget(cb)
        return row

    def _keyed_row(self, parent, title, sub, key, mapping: dict[str, str], default: str) -> QHBoxLayout:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(BodyLabel(title, parent))
        if sub:
            s = CaptionLabel(sub, parent)
            s.setWordWrap(True)
            s.setStyleSheet(f"color:{palette()['muted']};")
            col.addWidget(s)
        cb = ComboBox(parent)
        for k, label in mapping.items():
            cb.addItem(label, userData=k)
        cb.setCurrentIndex(max(0, cb.findData(self.db.get_setting(key, default))))
        cb.setMinimumWidth(240)
        cb.currentIndexChanged.connect(lambda _i: self.db.set_setting(key, cb.currentData()))
        row.addLayout(col, 1)
        row.addWidget(cb)
        return row

    @staticmethod
    def _apply_theme(v: str) -> None:
        setTheme({"Dark": Theme.DARK, "Light": Theme.LIGHT, "Auto": Theme.AUTO}[v])

    def _on_version(self, name: str, ver: str) -> None:
        self._vers[name] = ver
        self.versions.setText("   ·   ".join(f"{k}: {v}" for k, v in self._vers.items()))

    def refresh(self) -> None:
        self.claude.refresh()
        self.codex.refresh()
