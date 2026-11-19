"""What goes into every prompt: the per-category breakdown Claude reports (same data as /context),
the last turn's new vs cached input, and the Lean mode switch for this chat."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBoxBase,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
)

from orchestrator.storage.db import Database
from orchestrator.ui.theme import palette

CAT_COLORS = ["#60a5fa", "#a78bfa", "#f472b6", "#fb923c", "#facc15", "#4ade80", "#2dd4bf", "#94a3b8", "#f87171"]
# Plain-language names for the categories Claude Code reports.
CAT_NAMES = {
    "system prompt": "Claude Code's own instructions",
    "system tools": "Built-in tools",
    "mcp tools": "MCP server tools",
    "custom agents": "Custom agents",
    "memory files": "CLAUDE.md files",
    "skills": "Skills list",
    "messages": "This conversation",
    "free space": "Free space",
    "autocompact buffer": "Reserved for auto-compact",
}
FIXED = ("system prompt", "system tools", "mcp tools", "custom agents", "memory files", "skills")


def fmt_tokens(n: int | float | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def plain_name(name: str) -> str:
    return CAT_NAMES.get(name.strip().lower(), name)


def setup_tokens(ctx: dict) -> int:
    """Tokens loaded before you type anything: everything except the conversation and free space."""
    return sum(c["tokens"] for c in ctx.get("categories", [])
               if c["name"].strip().lower() in FIXED and not c.get("deferred"))


def turn_text(u: dict | None) -> str:
    """Last turn: new input vs cache reads (input in usage records already includes cache writes)."""
    if not u:
        return "No turns yet."
    new, cached, out = int(u.get("input") or 0), int(u.get("cached") or 0), int(u.get("output") or 0)
    total = new + cached
    pct = (cached / total * 100) if total else 0
    return (f"Last turn read {fmt_tokens(total)} input tokens: {fmt_tokens(new)} new and {fmt_tokens(cached)} "
            f"from cache ({pct:.0f}%), and wrote {fmt_tokens(out)} output tokens.")


def tips(ctx: dict) -> list[str]:
    out = []
    cats = {c["name"].strip().lower(): c["tokens"] for c in ctx.get("categories", [])}
    if cats.get("skills", 0) > 2000:
        out.append(f"The skills list is {fmt_tokens(cats['skills'])} tokens. Turning off plugins you don't use "
                   "(claude.ai or the desktop app › Customize › Plugins) shrinks it, or turn on Lean mode below.")
    if cats.get("mcp tools", 0) > 3000:
        big = ", ".join(m["server"] for m in ctx.get("mcp", [])[:3] if m["tokens"])
        out.append(f"MCP tools take {fmt_tokens(cats['mcp tools'])} tokens" + (f" (biggest: {big})" if big else "")
                   + ". Remove servers you don't use.")
    if cats.get("memory files", 0) > 3000:
        out.append(f"CLAUDE.md files take {fmt_tokens(cats['memory files'])} tokens. Keep them to short rules.")
    if cats.get("messages", 0) > 60000:
        out.append("This conversation is long and is re-read on every step. Starting a new chat is the biggest saver.")
    return out


class StackBar(QWidget):
    def __init__(self, parts: list[tuple[str, int, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.parts = parts
        self.setFixedHeight(14)

    def paintEvent(self, _e) -> None:  # noqa: N802
        total = sum(t for _n, t, _c in self.parts) or 1
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(palette()["border"].replace("0.08", "0.2")) if "rgba" not in palette()["border"]
                   else QColor(128, 128, 128, 50))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 7, 7)
        x = 0.0
        for _n, t, col in self.parts:
            w = self.width() * t / total
            p.setBrush(QColor(col))
            p.drawRect(QRectF(x, 0, max(0.0, w - 1), self.height()))
            x += w
        p.end()


def _row(parent: QWidget, color: str | None, name: str, tokens: str, note: str = "") -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    if color:
        dot = QLabel(parent)
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background:{color}; border-radius:5px;")
        row.addWidget(dot)
    lb = BodyLabel(name, parent)
    row.addWidget(lb, 1)
    if note:
        n = CaptionLabel(note, parent)
        n.setStyleSheet(f"color:{palette()['muted']};")
        row.addWidget(n)
    t = StrongBodyLabel(tokens, parent)
    t.setMinimumWidth(56)
    t.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(t)
    return row


class ContextDialog(MessageBoxBase):
    """Opened from the pie button in a chat's header."""

    def __init__(self, db: Database, mgr, sid: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.db, self.mgr, self.sid = db, mgr, sid
        s = db.session(sid)
        ctx = db.context(sid) or {}
        p = palette()
        self.widget.setMinimumWidth(560)
        self.viewLayout.addWidget(SubtitleLabel("What goes into each prompt", self))

        scroll = SmoothScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        scroll.setMinimumHeight(440)
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 4, 8, 4)
        lay.setSpacing(6)
        scroll.setWidget(body)
        self.viewLayout.addWidget(scroll)

        def caption(text: str) -> None:
            c = CaptionLabel(text, body)
            c.setWordWrap(True)
            c.setStyleSheet(f"color:{p['muted']};")
            lay.addWidget(c)

        caption(turn_text(db.last_usage(sid)) + " Cached input costs about a tenth of new input.")
        if s and s.engine != "claude":
            caption("Codex reports only totals per turn, not a breakdown, so there is nothing more to show here.")
        elif not ctx:
            caption("No breakdown yet. Send a message in this chat and it appears after the reply.")
        else:
            cats = [c for c in ctx.get("categories", []) if c["tokens"] > 0
                    and c["name"].strip().lower() not in ("free space", "autocompact buffer")]
            parts = [(c["name"], c["tokens"], CAT_COLORS[i % len(CAT_COLORS)]) for i, c in enumerate(cats)]
            lay.addSpacing(4)
            head = StrongBodyLabel(f"{fmt_tokens(setup_tokens(ctx))} tokens load before you type anything"
                                   + (f"  ·  {fmt_tokens(ctx.get('total'))} of {fmt_tokens(ctx.get('max'))} "
                                      "context used" if ctx.get("max") else ""), body)
            head.setWordWrap(True)
            lay.addWidget(head)
            lay.addWidget(StackBar(parts, body))
            for name, tokens, col in parts:
                deferred = next((c.get("deferred") for c in cats if c["name"] == name), False)
                lay.addLayout(_row(body, col, plain_name(name), fmt_tokens(tokens),
                                   "loaded only when used" if deferred else ""))
            if ctx.get("memory_files"):
                lay.addSpacing(8)
                lay.addWidget(StrongBodyLabel("CLAUDE.md files", body))
                for f in ctx["memory_files"]:
                    lay.addLayout(_row(body, None, f["path"], fmt_tokens(f["tokens"])))
            if ctx.get("mcp"):
                lay.addSpacing(8)
                lay.addWidget(StrongBodyLabel("MCP servers", body))
                for m in ctx["mcp"]:
                    note = f"{m['tools']} tools" + (f" · {m['status']}" if m.get("status") else "")
                    lay.addLayout(_row(body, None, m["server"], fmt_tokens(m["tokens"]), note))
            extra = []
            if ctx.get("skills") is not None:
                extra.append(f"{ctx['skills']} skills")
            if ctx.get("plugins"):
                extra.append(f"{len(ctx['plugins'])} plugins: " + ", ".join(ctx["plugins"][:12])
                             + ("…" if len(ctx["plugins"]) > 12 else ""))
            if extra:
                lay.addSpacing(8)
                caption("Loaded: " + "  ·  ".join(extra))
            for t in tips(ctx):
                lay.addSpacing(4)
                tip = BodyLabel("• " + t, body)
                tip.setWordWrap(True)
                lay.addWidget(tip)
        lay.addStretch(1)

        if s and s.engine == "claude":
            row = QHBoxLayout()
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(StrongBodyLabel("Lean mode for this chat", self))
            sub = CaptionLabel("Skips your own plugins, skills, hooks and ~/.claude/CLAUDE.md from the next message "
                               "on. The project's CLAUDE.md still loads.", self)
            sub.setWordWrap(True)
            sub.setStyleSheet(f"color:{p['muted']};")
            col.addWidget(sub)
            row.addLayout(col, 1)
            self.lean = SwitchButton(self)
            self.lean.setOnText("On")
            self.lean.setOffText("Off")
            self.lean.setChecked(mgr.lean_for(sid))
            self.lean.checkedChanged.connect(lambda v: mgr.set_lean(sid, v))
            row.addWidget(self.lean)
            self.viewLayout.addLayout(row)
        self.yesButton.setText("Close")
        self.cancelButton.hide()
