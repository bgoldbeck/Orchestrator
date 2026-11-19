"""MCP servers page: every server Claude Code and Codex know, with on/off for Orchestrator, add and remove."""
from __future__ import annotations

import shlex

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBox,
    MessageBoxBase,
    PlainTextEdit,
    PrimaryPushButton,
    SegmentedWidget,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
    TitleLabel,
    ToolButton,
)

from orchestrator.core import mcp_config as M
from orchestrator.storage.db import Database
from orchestrator.ui.theme import ENGINE_COLORS, palette, ui_pt

STATUS_COLORS = {"connected": "#3fb950", "failed": "#f85149", "needs-auth": "#f5b544", "pending": "#7d8590"}
SCOPE_TEXT = {"user": "your config", "local": "one project", "project": "project .mcp.json", "plugin": "plugin"}


def _chip(text: str, color: str, parent: QWidget) -> QLabel:
    lb = QLabel(text, parent)
    lb.setObjectName("chip")
    lb.setStyleSheet(f"QLabel#chip {{ color:{color}; border:1px solid {color}; border-radius:9px; padding:0 8px;"
                     f" font-size:{ui_pt(8.5)}; background:transparent; }}")
    return lb


class ServerRow(QFrame):
    def __init__(self, page: "McpPage", s: M.McpServer) -> None:
        super().__init__(page)
        self.page, self.s = page, s
        p = palette()
        self.setObjectName("mcpRow")
        self.setStyleSheet(f"#mcpRow {{ background:{p['card']}; border:1px solid {p['border']}; border-radius:10px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(StrongBodyLabel(s.name, self))
        top.addWidget(_chip({"claude": "Claude", "codex": "Codex"}[s.engine], ENGINE_COLORS.get(s.engine, "#888"), self))
        top.addWidget(_chip(SCOPE_TEXT.get(s.scope, s.scope), p["muted"], self))
        if s.status:
            top.addWidget(_chip(s.status, STATUS_COLORS.get(s.status, p["muted"]), self))
        top.addStretch(1)
        col.addLayout(top)
        detail = s.target() or ("added by a plugin from your claude.ai account" if s.scope == "plugin" else "")
        if s.project:
            detail += f"   ·   {s.project}"
        if s.env_keys:
            detail += f"   ·   env: {', '.join(s.env_keys)}"
        if detail:
            d = CaptionLabel(detail, self)
            d.setStyleSheet(f"color:{p['muted']};")
            d.setWordWrap(True)
            col.addWidget(d)
        lay.addLayout(col, 1)
        self.switch = SwitchButton(self)
        self.switch.setOnText("On")
        self.switch.setOffText("Off")
        self.switch.setToolTip("Off hides this server from chats started in Orchestrator. Its config is not changed.")
        self.switch.setChecked(s.key not in M.off_everywhere(page.db))
        self.switch.checkedChanged.connect(lambda on: M.set_off(page.db, s.key, not on))
        lay.addWidget(self.switch)
        rm = ToolButton(FIF.DELETE, self)
        rm.setToolTip("Remove from " + ("Claude Code" if s.engine == "claude" else "Codex") if s.removable
                      else "Comes from a plugin: turn it off here, or turn the plugin off in Claude")
        rm.setEnabled(s.removable)
        rm.clicked.connect(lambda: page.remove(s))
        lay.addWidget(rm)


class AddServerDialog(MessageBoxBase):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("Add MCP server", self))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        def label(text: str) -> BodyLabel:
            lb = BodyLabel(text, self)
            lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return lb

        self.engine = ComboBox(self)
        for text, data in (("Claude and Codex", "both"), ("Claude only", "claude"), ("Codex only", "codex")):
            self.engine.addItem(text, userData=data)
        grid.addWidget(label("Add to"), 0, 0)
        grid.addWidget(self.engine, 0, 1)
        self.name = LineEdit(self)
        self.name.setPlaceholderText("github")
        grid.addWidget(label("Name"), 1, 0)
        grid.addWidget(self.name, 1, 1)
        self.kind = SegmentedWidget(self)
        self.kind.addItem("cmd", "Command")
        self.kind.addItem("url", "URL")
        self.kind.setCurrentItem("cmd")
        self.kind.currentItemChanged.connect(self._kind)
        grid.addWidget(label("Runs as"), 2, 0)
        grid.addWidget(self.kind, 2, 1)
        self.target = LineEdit(self)
        grid.addWidget(label("Command"), 3, 0)
        self.targetLabel = grid.itemAtPosition(3, 0).widget()
        grid.addWidget(self.target, 3, 1)
        self.env = PlainTextEdit(self)
        self.env.setPlaceholderText("KEY=value, one per line (optional)")
        self.env.setFixedHeight(70)
        grid.addWidget(label("Environment"), 4, 0)
        grid.addWidget(self.env, 4, 1)
        self.viewLayout.addLayout(grid)
        note = CaptionLabel("Added to your user config, so it works in the CLIs too.", self)
        note.setStyleSheet(f"color:{palette()['muted']};")
        self.viewLayout.addWidget(note)
        self.widget.setMinimumWidth(520)
        self.yesButton.setText("Add")
        self._kind("cmd")

    def _kind(self, k: str) -> None:
        url = k == "url"
        self.targetLabel.setText("URL" if url else "Command")
        self.target.setPlaceholderText("https://example.com/mcp" if url else "npx -y @modelcontextprotocol/server-github")

    def validate(self) -> bool:  # noqa: D401 - MessageBoxBase hook
        return bool(self.name.text().strip() and self.target.text().strip())

    def values(self) -> dict:
        url = self.kind.currentRouteKey() == "url"
        text = self.target.text().strip()
        parts = [] if url else shlex.split(text, posix=False)
        return {"engines": ["claude", "codex"] if self.engine.currentData() == "both" else [self.engine.currentData()],
                "name": self.name.text().strip(), "url": text if url else "",
                "command": parts[0] if parts else "", "args": parts[1:], "env": M.parse_env(self.env.toPlainText())}


class McpPage(QWidget):
    changed = Signal()

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.setObjectName("mcpPage")
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 16)
        root.setSpacing(10)
        bar = QHBoxLayout()
        bar.addWidget(TitleLabel("MCP servers", self))
        bar.addStretch(1)
        add = PrimaryPushButton(FIF.ADD, "Add server", self)
        add.clicked.connect(self.add)
        bar.addWidget(add)
        root.addLayout(bar)
        sub = CaptionLabel("Servers Claude Code and Codex load into chats. Off hides a server from chats started here "
                           "(its tools stop costing tokens) without changing your config. Each chat can also turn "
                           "servers off from its pie button. Status is from the last Claude chat.", self)
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{palette()['muted']};")
        root.addWidget(sub)
        self.scroll = SmoothScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self.list = QVBoxLayout(self.body)
        self.list.setContentsMargins(0, 6, 8, 6)
        self.list.setSpacing(8)
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)
        self.reload()

    def showEvent(self, e) -> None:  # noqa: N802
        self.reload()
        super().showEvent(e)

    def reload(self) -> None:
        while self.list.count():
            w = self.list.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        servers = M.list_servers(self.db)
        for engine, title in (("claude", "Claude Code"), ("codex", "Codex")):
            mine = [s for s in servers if s.engine == engine]
            self.list.addWidget(SubtitleLabel(f"{title}  ({len(mine)})", self.body))
            if not mine:
                e = CaptionLabel("No servers configured.", self.body)
                e.setStyleSheet(f"color:{palette()['faint']};")
                self.list.addWidget(e)
            for s in mine:
                self.list.addWidget(ServerRow(self, s))
        self.list.addStretch(1)

    def _toast(self, ok: bool, title: str, text: str) -> None:
        (InfoBar.success if ok else InfoBar.error)(title, text[-300:], parent=self.window(),
                                                    position=InfoBarPosition.TOP_RIGHT, duration=4000 if ok else 8000)

    def add(self) -> None:
        dlg = AddServerDialog(self.window())
        if not dlg.exec():
            return
        v = dlg.values()
        for engine in v["engines"]:
            ok, out = M.add_server(engine, v["name"], v["command"], v["args"], v["url"], v["env"])
            self._toast(ok, f"Added to {'Claude Code' if engine == 'claude' else 'Codex'}" if ok else "Could not add",
                        out or v["name"])
        self.reload()
        self.changed.emit()

    def remove(self, s: M.McpServer) -> None:
        box = MessageBox(f"Remove {s.name}?", f"Removes it from {'Claude Code' if s.engine == 'claude' else 'Codex'}'s "
                         "config, so the CLI stops loading it too.", self.window())
        if not box.exec():
            return
        ok, out = M.remove_server(s)
        self._toast(ok, "Removed" if ok else "Could not remove", out or s.name)
        self.reload()
        self.changed.emit()
