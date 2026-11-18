"""Settings › Orchestrator › Model preferences: task type / keywords -> engine + model + effort, by priority."""
from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QHeaderView, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    PushButton,
    TableWidget,
)

from orchestrator.core import model_registry
from orchestrator.core import preferences as P
from orchestrator.core.catalog import ENGINES
from orchestrator.storage.db import Database
from orchestrator.ui.theme import palette

COLS = ["On", "Priority", "Task type", "Keywords", "Engine", "Model", "Effort", "Notes"]
C_ON, C_PRI, C_NAME, C_KW, C_ENG, C_MODEL, C_EFF, C_NOTES = range(8)


class CapsProbe(QThread):
    done = Signal(object)

    def run(self) -> None:  # noqa: D401
        self.done.emit(P.probe_codex_image_generation())


class RulesCard(QFrame):
    changed = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self._ids: list[str] = []
        pal = palette()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        hint = CaptionLabel("Checked first, in priority order (1 first), for automatic picks of the planner and its "
                            "sub-agents; the planner also sees them. A rule applies when the task type or one of its "
                            "keywords appears in the task. Rules whose model is disabled are skipped.", self)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{pal['muted']};")
        lay.addWidget(hint)
        self.table = TableWidget(self)
        self.table.setObjectName("rulesTable")
        self.table.setColumnCount(len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().hide()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        hh = self.table.horizontalHeader()
        for c in (C_ON, C_PRI, C_ENG, C_MODEL, C_EFF):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        for c in (C_NAME, C_KW, C_NOTES):
            hh.setSectionResizeMode(c, QHeaderView.Stretch)
        self.table.setMinimumHeight(190)
        self.table.itemChanged.connect(lambda *_: self._update_warning())
        lay.addWidget(self.table)
        self.warn = CaptionLabel("", self)
        self.warn.setObjectName("rulesWarning")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color:#f59e0b;")
        lay.addWidget(self.warn)
        row = QHBoxLayout()
        add = PushButton(FIF.ADD, "Add rule", self)
        rm = PushButton(FIF.DELETE, "Remove selected", self)
        reset = PushButton(FIF.SYNC, "Reset to defaults", self)
        self.saveBtn = PrimaryPushButton(FIF.SAVE, "Save rules", self)
        for b in (add, rm, reset, self.saveBtn):
            b.setMinimumWidth(b.sizeHint().width())
        add.clicked.connect(self._add)
        rm.clicked.connect(self._remove)
        reset.clicked.connect(self._reset)
        self.saveBtn.clicked.connect(self.save)
        for b in (add, rm, reset):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.saveBtn)
        lay.addLayout(row)
        self.load()
        self._probe = None
        if P.caps_stale(self.db):  # `codex features list` once a day, off the GUI thread
            self._probe = CapsProbe(self)
            self._probe.done.connect(self._caps)
            self._probe.start()

    # ---- rows
    def load(self) -> None:
        rs = P.rules(self.db)
        self.table.setRowCount(0)
        self._ids = []
        for r in rs:
            self._append(r)
        self._update_warning()

    def _append(self, r: P.Rule) -> None:
        i = self.table.rowCount()
        self.table.insertRow(i)
        self._ids.append(r.id)
        chk = QTableWidgetItem("")
        chk.setFlags((chk.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        chk.setCheckState(Qt.Checked if r.enabled else Qt.Unchecked)
        self.table.setItem(i, C_ON, chk)
        self.table.setItem(i, C_PRI, QTableWidgetItem(str(r.priority)))
        self.table.setItem(i, C_NAME, QTableWidgetItem(r.name))
        self.table.setItem(i, C_KW, QTableWidgetItem(", ".join(r.keywords)))
        self.table.setItem(i, C_NOTES, QTableWidgetItem(r.notes))
        eng = ComboBox(self.table)
        for e, name in ENGINES.items():
            eng.addItem(name, userData=e)
        eng.setCurrentIndex(max(0, list(ENGINES).index(r.engine) if r.engine in ENGINES else 0))
        model = ComboBox(self.table)
        eff = ComboBox(self.table)
        self.table.setCellWidget(i, C_ENG, eng)
        self.table.setCellWidget(i, C_MODEL, model)
        self.table.setCellWidget(i, C_EFF, eff)
        self._fill_models(eng, model, eff, r.model, r.effort)
        eng.currentIndexChanged.connect(lambda _i, e=eng, m=model, f=eff: self._fill_models(e, m, f, "", ""))
        model.currentIndexChanged.connect(lambda _i, e=eng, m=model, f=eff: (self._fill_efforts(e, m, f, ""),
                                                                              self._update_warning()))

    def _fill_models(self, eng: ComboBox, model: ComboBox, eff: ComboBox, mid: str, effort: str) -> None:
        e = eng.currentData()
        model.blockSignals(True)
        model.clear()
        for m in self.db.models(e):
            model.addItem(m.label + ("" if m.enabled else " (disabled)"), userData=m.id)
        ids = [m.id for m in self.db.models(e)]
        if mid and mid not in ids:  # unknown id (e.g. removed from the list): keep it visible
            model.addItem(f"{mid} (unknown)", userData=mid)
            ids.append(mid)
        model.setCurrentIndex(ids.index(mid) if mid in ids else 0)
        model.blockSignals(False)
        self._fill_efforts(eng, model, eff, effort)
        self._update_warning()

    def _fill_efforts(self, eng: ComboBox, model: ComboBox, eff: ComboBox, effort: str) -> None:
        m = next((x for x in self.db.models(eng.currentData()) if x.id == model.currentData()), None)
        efforts = m.efforts if m else ["low", "medium", "high"]
        cur = effort or eff.currentText()
        eff.clear()
        eff.addItems(efforts)
        eff.setCurrentIndex(efforts.index(cur) if cur in efforts else min(1, len(efforts) - 1))

    def rows(self) -> list[P.Rule]:
        out = []
        for i in range(self.table.rowCount()):
            def text(c: int) -> str:
                it = self.table.item(i, c)
                return it.text().strip() if it else ""
            name = text(C_NAME)
            if not name:
                continue
            try:
                pri = max(1, int(text(C_PRI) or "1"))
            except ValueError:
                pri = 1
            eng, model, eff = (self.table.cellWidget(i, c) for c in (C_ENG, C_MODEL, C_EFF))
            if eng is None or model is None or eff is None or self.table.item(i, C_ON) is None:
                continue  # row still being built
            out.append(P.Rule(name=name, keywords=P.split_keywords(text(C_KW)), engine=eng.currentData(),
                              model=model.currentData() or "", effort=eff.currentText(), priority=pri,
                              enabled=self.table.item(i, C_ON).checkState() == Qt.Checked, notes=text(C_NOTES),
                              id=self._ids[i] if i < len(self._ids) else P.Rule.from_dict({}).id))
        return out

    # ---- warnings
    def _caps(self, on) -> None:
        P.store_codex_caps(self.db, on)
        self._update_warning()

    def warnings(self) -> list[str]:
        msgs = []
        for r in self.rows():
            if r.enabled and not model_registry.is_allowed(self.db, r.engine, r.model, strict=True):
                msgs.append(f"“{r.name}”: {model_registry.label(self.db, r.engine, r.model)} is disabled in "
                            "Settings › Models, so this rule is skipped.")
            w = P.image_warning(self.db, r)
            if w:
                msgs.append(f"“{r.name}”: {w}")
        return msgs

    def _update_warning(self) -> None:
        if not hasattr(self, "warn"):
            return
        msgs = self.warnings()
        self.warn.setText("\n".join("⚠ " + m for m in msgs))
        self.warn.setVisible(bool(msgs))

    # ---- buttons
    def _add(self) -> None:
        self._append(P.Rule(name="New task type", keywords=[], engine="claude",
                            model=(model_registry.default(self.db, "claude") or ("", ""))[0],
                            priority=len(self._ids) + 1))
        self.table.editItem(self.table.item(self.table.rowCount() - 1, C_NAME))

    def _remove(self) -> None:
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)
            del self._ids[r]
        self._update_warning()

    def _reset(self) -> None:
        P.reset_rules(self.db)
        self.load()
        self.changed.emit()

    def save(self) -> None:
        items = self.rows()
        P.save_rules(self.db, items)
        self.load()
        self.changed.emit()
        InfoBar.success("Rules saved", f"{len(items)} rules", parent=self.window(),
                        position=InfoBarPosition.TOP_RIGHT, duration=2000)
