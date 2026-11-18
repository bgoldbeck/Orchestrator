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
    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        f, lay = card(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(f)
        hint = CaptionLabel("Editable list. Add new models here, no code change needed. "
                            "Efforts are comma separated.", f)
        hint.setStyleSheet(f"color:{palette()['muted']};")
        lay.addWidget(hint)
        self.table = TableWidget(f)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Engine", "Model ID", "Label", "Efforts"])
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMinimumHeight(320)
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
        ms = self.db.models()
        self.table.setRowCount(len(ms))
        for r, m in enumerate(ms):
            self._set_row(r, m.engine, m.id, m.label, ", ".join(m.efforts))

    def _set_row(self, r: int, engine: str, mid: str, label: str, efforts: str) -> None:
        e = QTableWidgetItem(engine)
        e.setFlags(e.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(r, 0, e)
        self.table.setItem(r, 1, QTableWidgetItem(mid))
        self.table.setItem(r, 2, QTableWidgetItem(label))
        self.table.setItem(r, 3, QTableWidgetItem(efforts))

    def _add(self, engine: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._set_row(r, engine, "new-model-id", "New model", "low, medium, high")
        self.table.editItem(self.table.item(r, 1))

    def _remove(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def _reset(self) -> None:
        self.db.reset_models()
        self.load()

    def _save(self) -> None:
        out: list[Model] = []
        for r in range(self.table.rowCount()):
            get = lambda c: (self.table.item(r, c).text().strip() if self.table.item(r, c) else "")  # noqa: E731
            if not get(1):
                continue
            efforts = [x.strip() for x in get(3).split(",") if x.strip()]
            out.append(Model(get(0), get(1), get(2) or get(1), efforts))
        self.db.save_models(out)
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
        lay.addWidget(ModelsCard(db, body))

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Engines", body))
        f, fl = card(body)
        fl.addLayout(self._combo_row(f, "Codex sandbox", "What Codex may do in the project folder.",
                                     "codex.sandbox", ["read-only", "workspace-write", "danger-full-access"], "workspace-write"))
        fl.addLayout(self._combo_row(f, "Claude permissions", "default asks inline before tools run.",
                                     "claude.permission_mode", ["default", "acceptEdits", "plan", "bypassPermissions"], "default"))
        lay.addWidget(f)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Background", body))
        fb, fbl = card(body)
        fbl.addLayout(self._switch_row(fb, "Keep running in the tray when the window is closed",
                                       "Schedules only run while Orchestrator is running.", "ui.close_to_tray", True))
        fbl.addLayout(self._switch_row(fb, "Notifications", "When a chat or schedule finishes, fails, or needs you.",
                                       "ui.notifications", True))
        fbl.addLayout(self._switch_row(fb, "Start at login", "Starts hidden in the tray when you sign in to Windows.",
                                       None, False, getter=self._autostart_get, setter=self._autostart_set))
        lay.addWidget(fb)

        lay.addSpacing(6)
        lay.addWidget(SubtitleLabel("Appearance", body))
        f2, fl2 = card(body)
        fl2.addLayout(self._combo_row(f2, "Theme", "Applies immediately.", "ui.theme", ["Dark", "Light", "Auto"], "Dark",
                                      on_change=self._apply_theme))
        fl2.addLayout(self._combo_row(f2, "Accent color", "", "ui.accent", list(ACCENTS), "Sky",
                                      on_change=lambda v: setThemeColor(ACCENTS[v])))
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

    @staticmethod
    def _apply_theme(v: str) -> None:
        setTheme({"Dark": Theme.DARK, "Light": Theme.LIGHT, "Auto": Theme.AUTO}[v])

    def _on_version(self, name: str, ver: str) -> None:
        self._vers[name] = ver
        self.versions.setText("   ·   ".join(f"{k}: {v}" for k, v in self._vers.items()))

    def refresh(self) -> None:
        self.claude.refresh()
        self.codex.refresh()
