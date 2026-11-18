"""A small number field whose value is always readable.

qfluentwidgets' spin boxes on this Qt version leave the inner line edit 1 px wide, so the number is invisible.
NumberBox lays the line edit out itself, left of the stacked up/down arrows.
"""
from __future__ import annotations

from qfluentwidgets import CompactSpinBox

ARROWS_W = 30


class NumberBox(CompactSpinBox):
    def __init__(self, parent=None, minimum: int = 1, maximum: int = 99, value: int = 1) -> None:
        super().__init__(parent)
        self.setRange(minimum, maximum)
        self.setValue(value)
        self.setMinimumWidth(84)

    def _place_editor(self) -> None:
        self.lineEdit().setGeometry(10, 0, max(16, self.width() - 10 - ARROWS_W), self.height())

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._place_editor()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._place_editor()
