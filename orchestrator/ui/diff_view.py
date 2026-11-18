"""Diff viewer: changed files in a chat's (or task's) folder, with a colored unified diff."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QListWidgetItem, QTextBrowser, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ListWidget,
    MessageBoxBase,
    SubtitleLabel,
    isDarkTheme,
)

from orchestrator.core import gitdiff
from orchestrator.ui.theme import palette

STATUS_NAMES = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "?": "new", "C": "copied", "U": "conflict"}
STATUS_COLORS = {"M": "#d29922", "A": "#3fb950", "?": "#3fb950", "D": "#f85149", "R": "#79c0ff", "U": "#f85149"}


class DiffDialog(MessageBoxBase):
    def __init__(self, folder: str, parent: QWidget, title: str = "Changes") -> None:
        super().__init__(parent)
        self.folder = folder
        p = palette()
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        root = gitdiff.repo_root(folder)
        self.files = gitdiff.changed_files(folder) if root else []
        if root:
            n, add, rem = gitdiff.diff_stats(folder)
            sub = f"{root}   ·   {n} file{'s' if n != 1 else ''} changed  ·  +{add}  −{rem}   ·   since the last commit"
        else:
            sub = f"{folder or '(no folder)'} is not a git repository, so changes can't be shown."
        info = CaptionLabel(sub, self)
        info.setStyleSheet(f"color:{p['muted']};")
        info.setWordWrap(True)
        self.viewLayout.addWidget(info)

        body = QHBoxLayout()
        self.list = ListWidget(self)
        self.list.setFixedWidth(300)
        for st, path in self.files:
            it = QListWidgetItem(f"{st}   {path}")
            it.setToolTip(f"{path} ({STATUS_NAMES.get(st, st)})")
            it.setData(Qt.UserRole, (st, path))
            it.setForeground(Qt.GlobalColor.white if isDarkTheme() else Qt.GlobalColor.black)
            self.list.addItem(it)
        self.view = QTextBrowser(self)
        self.view.setOpenLinks(False)
        self.view.setStyleSheet(f"QTextBrowser {{ background:{p['code_bg']}; color:{p['text']}; border:1px solid "
                                f"{p['border']}; border-radius:8px; padding:6px; }}")
        self.view.setLineWrapMode(QTextBrowser.NoWrap)
        body.addWidget(self.list)
        body.addWidget(self.view, 1)
        holder = QWidget(self)
        holder.setLayout(body)
        holder.setMinimumSize(960, 520)
        self.viewLayout.addWidget(holder)
        self.empty = BodyLabel("No changes since the last commit." if root else "", self)
        self.viewLayout.addWidget(self.empty)
        self.empty.setVisible(bool(root) and not self.files)
        holder.setVisible(bool(self.files))
        self.list.currentItemChanged.connect(self._show)
        if self.files:
            self.list.setCurrentRow(0)
        self.yesButton.setText("Close")
        self.cancelButton.hide()

    def _show(self, item: QListWidgetItem | None, _prev=None) -> None:
        if not item:
            return
        st, path = item.data(Qt.UserRole)
        diff = gitdiff.file_diff(self.folder, path, st)
        self.view.setHtml(gitdiff.render_diff_html(diff, isDarkTheme()))
