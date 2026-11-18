"""Markdown -> segments (rich text HTML + fenced code blocks) for Qt widgets."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import markdown as md
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from orchestrator.ui.theme import code_pt, isDarkTheme, mono_css, palette

FENCE = re.compile(r"^(```|~~~)[ \t]*([\w+#.-]*)[^\n]*\n(.*?)(?:^\1[ \t]*$|\Z)", re.S | re.M)


@dataclass
class Segment:
    kind: str  # "text" | "code"
    body: str
    lang: str = ""
    closed: bool = True


def split_segments(text: str) -> list[Segment]:
    segs: list[Segment] = []
    pos = 0
    for m in FENCE.finditer(text):
        if m.start() > pos and text[pos:m.start()].strip():
            segs.append(Segment("text", text[pos:m.start()]))
        body = m.group(3)
        closed = text[m.end(3):m.end()].strip().startswith(m.group(1))
        segs.append(Segment("code", body.rstrip("\n"), m.group(2) or "", closed))
        pos = m.end()
    if text[pos:].strip():
        segs.append(Segment("text", text[pos:]))
    return segs


def text_html(text: str) -> str:
    p = palette()
    body = md.markdown(text, extensions=["tables", "sane_lists", "fenced_code"], output_format="html")
    body = body.replace(
        "<code>",
        f'<code style="font-family:{mono_css()}; background-color:{p["inline_code"]}; font-size:92%;">&nbsp;',
    ).replace("</code>", "&nbsp;</code>")
    body = body.replace("<table>", f'<table cellspacing="0" cellpadding="6" style="border-collapse:collapse; border:1px solid {p["border"]};">')
    body = body.replace("<th>", f'<th style="border:1px solid {p["border"]}; text-align:left;">').replace(
        "<td>", f'<td style="border:1px solid {p["border"]};">')
    body = re.sub(r"<h([1-3])>", lambda m: f'<h{m.group(1)} style="margin-top:10px; margin-bottom:4px;">', body)
    accent = p["accent"]
    body = body.replace("<a ", f'<a style="color:{accent}; text-decoration:none;" ')
    body = body.replace("<blockquote>", f'<blockquote style="color:{p["muted"]}; margin-left:8px;">')
    return f'<div style="line-height:150%;">{body}</div>'


def code_html(code: str, lang: str) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code) if len(code) > 40 else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    style = "one-dark" if isDarkTheme() else "default"
    fmt = HtmlFormatter(noclasses=True, nowrap=True, style=style)
    inner = highlight(code, lexer, fmt)
    return (f'<pre style="font-family:{mono_css()}; font-size:{code_pt()}; margin:0; line-height:135%;">'
            f"{inner}</pre>")


def escape(text: str) -> str:
    return html.escape(text)
