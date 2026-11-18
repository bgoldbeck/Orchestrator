"""Projects home: pinned + other projects as a responsive card grid."""
from __future__ import annotations

import hashlib
import os

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CheckableMenu,
    FluentIcon as FIF,
    HyperlinkButton,
    IconWidget,
    LineEdit,
    MenuIndicatorType,
    MessageBox,
    MessageBoxBase,
    PushButton,
    RoundMenu,
    SearchLineEdit,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    ToolTipFilter,
    TransparentToolButton,
    isDarkTheme,
)

from orchestrator.storage.db import Database, Project
from orchestrator.ui.theme import palette
from orchestrator.ui_text import relative

PROJECT_ICONS = ["FOLDER", "CODE", "GAME", "GLOBE", "ROBOT", "PHOTO", "MUSIC", "BOOK_SHELF", "DEVELOPER_TOOLS",
                 "CLOUD", "IOT", "PALETTE", "SHOPPING_CART", "HEART", "LEAF", "CAMERA", "CAR", "EDUCATION",
                 "CHAT", "MOVIE", "CALENDAR", "FLAG", "LIBRARY", "TILES"]
PROJECT_COLORS = ["#60a5fa", "#a78bfa", "#f472b6", "#fb923c", "#facc15", "#4ade80", "#2dd4bf", "#38bdf8",
                  "#f87171", "#c084fc", "#94a3b8", "#e879f9"]
SORTS = [("recent", "Recent activity"), ("name", "Name"), ("created", "Date created")]
TITLE_FONTS = ["Georgia", "Cambria", "Times New Roman"]


def default_style(name: str) -> tuple[str, str]:
    """Stable icon + color picked from the project name."""
    h = int(hashlib.sha1(name.lower().encode("utf-8")).hexdigest(), 16)
    return PROJECT_ICONS[h % len(PROJECT_ICONS)], PROJECT_COLORS[(h // 97) % len(PROJECT_COLORS)]


def project_style(p: Project) -> tuple[str, str]:
    icon, color = default_style(p.name)
    return (p.icon if p.icon in PROJECT_ICONS else icon), (p.color or color)


def sort_projects(projects: list[Project], activity: dict[int, float], key: str) -> list[Project]:
    if key == "name":
        return sorted(projects, key=lambda p: p.name.lower())
    if key == "created":
        return sorted(projects, key=lambda p: -(p.created_at or 0))
    return sorted(projects, key=lambda p: -activity.get(p.id, p.created_at or 0))


class IconTile(QFrame):
    def __init__(self, icon: str, color: str, size: int = 44, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        c = QColor(color)
        self.setStyleSheet(f"QFrame {{ background: rgba({c.red()},{c.green()},{c.blue()},0.16);"
                           f" border:1px solid rgba({c.red()},{c.green()},{c.blue()},0.28); border-radius:{size // 4}px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        ic = IconWidget(getattr(FIF, icon).icon(color=c), self)
        ic.setFixedSize(size // 2 - 2, size // 2 - 2)
        ic.setStyleSheet("background:transparent; border:none;")
        lay.addWidget(ic, 0, Qt.AlignCenter)


class ProjectCard(QFrame):
    clicked = Signal(int)
    menuRequested = Signal(int, object)  # project id, global pos

    def __init__(self, p: Project, last: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pid = p.id
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("projCard")
        self.setFixedHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._paint(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 8, 0)
        lay.setSpacing(14)
        icon, color = project_style(p)
        lay.addWidget(IconTile(icon, color, 44, self))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addStretch(1)
        name = StrongBodyLabel(p.name, self)
        name.setStyleSheet("background:transparent;")
        sub = CaptionLabel(relative(last) or "No activity yet", self)
        sub.setStyleSheet(f"color:{palette()['muted']}; background:transparent;")
        sub.setToolTip(p.path)
        col.addWidget(name)
        col.addWidget(sub)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.more = TransparentToolButton(FIF.MORE, self)
        self.more.setFixedSize(32, 32)
        self.more.setIconSize(QSize(14, 14))
        self.more.setToolTip("More")
        self.more.installEventFilter(ToolTipFilter(self.more))
        self.more.clicked.connect(lambda: self.menuRequested.emit(self.pid, self.more.mapToGlobal(self.more.rect().bottomLeft())))
        self.more.setVisible(False)
        lay.addWidget(self.more, 0, Qt.AlignVCenter)

    def _paint(self, hover: bool) -> None:
        dark = isDarkTheme()
        bg = ("rgba(255,255,255,0.075)" if hover else "rgba(255,255,255,0.045)") if dark else \
             ("rgba(0,0,0,0.05)" if hover else "rgba(255,255,255,0.8)")
        border = "rgba(255,255,255,0.10)" if dark else "rgba(0,0,0,0.08)"
        self.setStyleSheet(f"#projCard {{ background:{bg}; border:1px solid {border}; border-radius:14px; }}")

    def enterEvent(self, e) -> None:  # noqa: N802
        self._paint(True)
        self.more.setVisible(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._paint(False)
        self.more.setVisible(False)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.pid)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e) -> None:  # noqa: N802
        self.menuRequested.emit(self.pid, e.globalPos())


class RenameDialog(MessageBoxBase):
    def __init__(self, name: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Rename project", self))
        self.edit = LineEdit(self)
        self.edit.setText(name)
        self.edit.selectAll()
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(380)
        self.yesButton.setText("Rename")
        self.cancelButton.setText("Cancel")

    def validate(self) -> bool:
        return bool(self.edit.text().strip())


class CustomizeDialog(MessageBoxBase):
    def __init__(self, p: Project, parent: QWidget) -> None:
        super().__init__(parent)
        self.icon, self.color = project_style(p)
        self.viewLayout.addWidget(SubtitleLabel("Customize icon", self))
        self.preview_holder = QHBoxLayout()
        self.viewLayout.addLayout(self.preview_holder)
        self.preview: IconTile | None = None
        self.viewLayout.addWidget(CaptionLabel("Icon", self))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.icon_btns = {}
        for i, name in enumerate(PROJECT_ICONS):
            b = TransparentToolButton(getattr(FIF, name), self)
            b.setFixedSize(36, 36)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=name: self._set(icon=n))
            self.icon_btns[name] = b
            grid.addWidget(b, i // 8, i % 8)
        self.viewLayout.addLayout(grid)
        self.viewLayout.addWidget(CaptionLabel("Color", self))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.color_btns = {}
        for c in PROJECT_COLORS:
            b = QLabel(self)
            b.setFixedSize(26, 26)
            b.setCursor(Qt.PointingHandCursor)
            b.mousePressEvent = lambda _e, col=c: self._set(color=col)  # type: ignore[assignment]
            self.color_btns[c] = b
            row.addWidget(b)
        row.addStretch(1)
        self.viewLayout.addLayout(row)
        reset = HyperlinkButton("", "Use automatic icon", self)
        reset.clicked.connect(self._reset)
        self.viewLayout.addWidget(reset, 0, Qt.AlignLeft)
        self.auto = False
        self.name = p.name
        self.widget.setMinimumWidth(420)
        self.yesButton.setText("Save")
        self.cancelButton.setText("Cancel")
        self._set()

    def _reset(self) -> None:
        self.icon, self.color = default_style(self.name)
        self.auto = True
        self._set()

    def _set(self, icon: str | None = None, color: str | None = None) -> None:
        if icon or color:
            self.auto = False
        self.icon = icon or self.icon
        self.color = color or self.color
        for n, b in self.icon_btns.items():
            b.setChecked(n == self.icon)
        for c, b in self.color_btns.items():
            ring = "border:2px solid white;" if c == self.color else "border:2px solid transparent;"
            b.setStyleSheet(f"background:{c}; border-radius:13px; {ring}")
        if self.preview:
            self.preview.deleteLater()
        self.preview = IconTile(self.icon, self.color, 52, self)
        self.preview_holder.addWidget(self.preview)
        self.preview_holder.addStretch(1)


class ProjectsPage(SmoothScrollArea):
    openProject = Signal(int)
    projectsChanged = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.show_discovered = False
        self.setObjectName("projectsPage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        outer = QHBoxLayout(body)
        outer.setContentsMargins(40, 32, 40, 40)
        self.column = QWidget(body)
        self.column.setMaximumWidth(1100)
        outer.addStretch(1)
        outer.addWidget(self.column, 100)
        outer.addStretch(1)
        self.lay = QVBoxLayout(self.column)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("Projects", self.column)
        f = QFont()
        f.setFamilies(TITLE_FONTS)
        f.setPointSizeF(26)
        title.setFont(f)
        title.setStyleSheet(f"color:{palette()['text']}; background:transparent;")
        head.addWidget(title)
        head.addStretch(1)
        self.search = SearchLineEdit(self.column)
        self.search.setPlaceholderText("Search projects")
        self.search.setFixedWidth(0)
        self.search.setVisible(False)
        self.search.textChanged.connect(lambda _t: self.reload())
        self._searchAnim = QPropertyAnimation(self.search, b"maximumWidth", self)
        self._searchAnim.setDuration(180)
        self._searchAnim.setEasingCurve(QEasingCurve.OutCubic)
        head.addWidget(self.search)
        self.searchBtn = TransparentToolButton(FIF.SEARCH, self.column)
        self.searchBtn.setToolTip("Search")
        self.searchBtn.installEventFilter(ToolTipFilter(self.searchBtn))
        self.searchBtn.clicked.connect(self._toggle_search)
        self.sortBtn = TransparentToolButton(FIF.SCROLL, self.column)
        self.sortBtn.setToolTip("Sort")
        self.sortBtn.installEventFilter(ToolTipFilter(self.sortBtn))
        self.sortBtn.clicked.connect(self._sort_menu)
        self.newBtn = PushButton("New project", self.column)
        self.newBtn.setFixedHeight(34)
        self._style_new_btn()
        self.newBtn.clicked.connect(self.new_project)
        for w in (self.searchBtn, self.sortBtn):
            w.setFixedSize(36, 36)
            head.addWidget(w)
        head.addSpacing(6)
        head.addWidget(self.newBtn)
        self.lay.addLayout(head)
        self.lay.addSpacing(14)

        self.content = QVBoxLayout()
        self.content.setSpacing(10)
        self.lay.addLayout(self.content)
        self.lay.addStretch(1)
        self.setWidget(body)
        self._cols = 2
        self.reload()

    def _style_new_btn(self) -> None:
        if isDarkTheme():
            css = ("PushButton { background:#ececec; color:#141414; border:none; border-radius:17px; padding:0 16px;"
                   " font-weight:600; } PushButton:hover { background:#ffffff; } PushButton:pressed { background:#d6d6d6; }")
        else:
            css = ("PushButton { background:#1c1c1c; color:#ffffff; border:none; border-radius:17px; padding:0 16px;"
                   " font-weight:600; } PushButton:hover { background:#333; }")
        self.newBtn.setStyleSheet(css)

    # ---- header actions
    def _toggle_search(self) -> None:
        opening = not self.search.isVisible() or self.search.maximumWidth() == 0
        self.search.setVisible(True)
        self.search.setFixedWidth(240)
        self._searchAnim.stop()
        self._searchAnim.setStartValue(self.search.maximumWidth() if not opening else 0)
        self._searchAnim.setEndValue(240 if opening else 0)
        self._searchAnim.start()
        if opening:
            self.search.setFocus()
        else:
            self.search.clear()

    def _sort_menu(self) -> None:
        menu = CheckableMenu(parent=self, indicatorType=MenuIndicatorType.RADIO)
        group = QActionGroup(menu)
        cur = self.db.get_setting("projects.sort", "recent")
        for key, label in SORTS:
            a = QAction(label, menu, checkable=True)
            a.setChecked(key == cur)
            a.triggered.connect(lambda _=False, k=key: (self.db.set_setting("projects.sort", k), self.reload()))
            group.addAction(a)
            menu.addAction(a)
        menu.exec(self.sortBtn.mapToGlobal(self.sortBtn.rect().bottomLeft()))

    def new_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self.window(), "Choose a project folder", os.path.expanduser("~"))
        if path:
            self.db.add_project(os.path.normpath(path))
            self.reload()
            self.projectsChanged.emit()

    # ---- content
    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        cols = 1 if self.viewport().width() < 820 else 2
        if cols != self._cols:
            self._cols = cols
            self.reload()

    def _section(self, text: str) -> QLabel:
        lb = CaptionLabel(text, self.column)
        lb.setStyleSheet(f"color:{palette()['muted']}; font-weight:600; background:transparent; padding-top:8px;")
        return lb

    def _grid(self, projects: list[Project], activity: dict[int, float]) -> QWidget:
        w = QWidget(self.column)
        g = QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(14)
        for i, p in enumerate(projects):
            card = ProjectCard(p, activity.get(p.id, p.created_at), w)
            card.clicked.connect(self.openProject.emit)
            card.menuRequested.connect(self._menu)
            g.addWidget(card, i // self._cols, i % self._cols)
        for c in range(self._cols):
            g.setColumnStretch(c, 1)
        return w

    def reload(self) -> None:
        while self.content.count():
            it = self.content.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        q = self.search.text().strip().lower()
        activity = self.db.project_activity()
        projects = [p for p in self.db.projects() if not q or q in p.name.lower() or q in p.path.lower()]
        projects = sort_projects(projects, activity, self.db.get_setting("projects.sort", "recent"))
        pinned = [p for p in projects if p.pinned]
        mine = [p for p in projects if not p.pinned and not p.auto]
        discovered = [p for p in projects if not p.pinned and p.auto]
        if not projects:
            self.content.addWidget(self._empty(bool(q)))
            return
        if pinned:
            self.content.addWidget(self._section("Pinned"))
            self.content.addWidget(self._grid(pinned, activity))
        if mine:
            self.content.addWidget(self._section("Other projects" if pinned else "Projects"))
            self.content.addWidget(self._grid(mine, activity))
        if discovered:
            if self.show_discovered or q:
                self.content.addWidget(self._section("Discovered in engine history"))
                self.content.addWidget(self._grid(discovered, activity))
            if not q:
                n = len(discovered)
                link = HyperlinkButton("", ("Hide" if self.show_discovered else "Show") +
                                       f" {n} folder{'' if n == 1 else 's'} discovered in engine history", self.column)
                link.clicked.connect(self._toggle_discovered)
                self.content.addWidget(link, 0, Qt.AlignLeft)

    def _toggle_discovered(self) -> None:
        self.show_discovered = not self.show_discovered
        self.reload()

    def _empty(self, searching: bool) -> QWidget:
        w = QWidget(self.column)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 80, 0, 0)
        lay.setSpacing(8)
        ic = IconWidget(FIF.SEARCH if searching else FIF.FOLDER_ADD, w)
        ic.setFixedSize(40, 40)
        t = StrongBodyLabel("No matching projects" if searching else "No projects yet", w)
        s = BodyLabel("Try another name." if searching else "A project is a folder on disk. Add one to start chatting in it.", w)
        s.setStyleSheet(f"color:{palette()['muted']};")
        for x in (ic, t, s):
            lay.addWidget(x, 0, Qt.AlignHCenter)
        return w

    # ---- card menu
    def _menu(self, pid: int, pos) -> None:
        p = self.db.project(pid)
        if not p:
            return
        menu = RoundMenu(parent=self)
        menu.addAction(Action(FIF.UNPIN if p.pinned else FIF.PIN, "Unpin" if p.pinned else "Pin",
                              triggered=lambda: self._update(pid, pinned=not p.pinned, auto=False)))
        menu.addAction(Action(FIF.EDIT, "Rename", triggered=lambda: self._rename(p)))
        menu.addAction(Action(FIF.PALETTE, "Customize icon", triggered=lambda: self._customize(p)))
        menu.addAction(Action(FIF.FOLDER, "Open folder", triggered=lambda: os.startfile(p.path) if os.path.isdir(p.path) else None))
        menu.addSeparator()
        menu.addAction(Action(FIF.DELETE, "Remove", triggered=lambda: self._remove(p)))
        menu.exec(pos)

    def _update(self, pid: int, **fields) -> None:
        self.db.update_project(pid, **fields)
        self.reload()
        self.projectsChanged.emit()

    def _rename(self, p: Project) -> None:
        dlg = RenameDialog(p.name, self.window())
        if dlg.exec():
            self._update(p.id, name=dlg.edit.text().strip())

    def _customize(self, p: Project) -> None:
        dlg = CustomizeDialog(p, self.window())
        if dlg.exec():
            if dlg.auto:
                self.db.conn.execute("UPDATE projects SET icon=NULL, color=NULL WHERE id=?", (p.id,))
                self.db.conn.commit()
                self.reload()
            else:
                self._update(p.id, icon=dlg.icon, color=dlg.color)

    def _remove(self, p: Project) -> None:
        box = MessageBox("Remove project?", f"“{p.name}” and its chats are removed from Orchestrator. "
                         "Files on disk and the engines' own session logs are not touched.", self.window())
        if box.exec():
            self.db.remove_project(p.id)
            self.reload()
            self.projectsChanged.emit()
