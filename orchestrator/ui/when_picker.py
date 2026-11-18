"""Pick a moment for 'run later': quick chips, or a date plus a time from a 15-minute list, read back in words."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox, FastCalendarPicker, FlowLayout, PillPushButton

from orchestrator.core.scheduler import plain_when, when_chips
from orchestrator.ui.theme import palette


def time_slots(step_min: int = 15) -> list[tuple[str, int, int]]:
    """('6:30 PM', 18, 30) for every step of a day."""
    out = []
    for m in range(0, 24 * 60, step_min):
        h, mi = divmod(m, 60)
        out.append((datetime(2000, 1, 1, h, mi).strftime("%I:%M %p").lstrip("0"), h, mi))
    return out


class WhenPicker(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None, default: str = "In 5 hours") -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        chips = QWidget(self)
        self.flow = FlowLayout(chips, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(6)
        self.flow.setVerticalSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.chips = when_chips()
        labels = [c[0] for c in self.chips] + ["Custom"]
        self.buttons: dict[str, PillPushButton] = {}
        for i, label in enumerate(labels):
            b = PillPushButton(label, chips)
            b.setCheckable(True)
            b.setMinimumWidth(b.sizeHint().width())  # never elide the label
            self.group.addButton(b, i)
            self.flow.addWidget(b)
            self.buttons[label] = b
        self.group.idClicked.connect(self._chip)
        self._last_chip = 0
        lay.addWidget(chips)

        self.customRow = QWidget(self)
        cr = QHBoxLayout(self.customRow)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(8)
        self.date = FastCalendarPicker(self.customRow)
        self.date.setDate(QDate.currentDate())
        self.date.dateChanged.connect(lambda _d: self._emit())
        self.time = ComboBox(self.customRow)
        self.slots = time_slots()
        for label, h, mi in self.slots:
            self.time.addItem(label, userData=(h, mi))
        self.time.setMaxVisibleItems(10)
        self.time.setMinimumWidth(120)
        self.time.currentIndexChanged.connect(lambda _i: self._emit())
        cr.addWidget(self.date)
        cr.addWidget(self.time)
        cr.addStretch(1)
        self.customRow.hide()
        lay.addWidget(self.customRow)

        self.words = CaptionLabel("", self)
        self.words.setObjectName("whenWords")
        self.words.setStyleSheet(f"color:{palette()['muted']};")
        lay.addWidget(self.words)
        self.select(default if default in self.buttons else labels[0])

    def select(self, label: str) -> None:
        b = self.buttons.get(label)
        if b:
            b.setChecked(True)
            self._chip(self.group.id(b))

    def set_when(self, dt: datetime) -> None:
        """Show a specific time (Custom)."""
        self.buttons["Custom"].setChecked(True)
        self.customRow.show()
        self.date.setDate(QDate(dt.year, dt.month, dt.day))
        idx = (dt.hour * 60 + dt.minute) // 15
        self.time.setCurrentIndex(max(0, min(len(self.slots) - 1, idx)))
        self._emit()

    def _chip(self, i: int) -> None:
        custom = i >= len(self.chips)
        self.customRow.setVisible(custom)
        if not custom:
            self._last_chip = i
        else:  # start from the chip picked before (In 5 hours by default), rounded up to the list
            dt = dict(when_chips()).get(self.chips[self._last_chip][0]) if self.chips else datetime.now()
            self.date.setDate(QDate(dt.year, dt.month, dt.day))
            nxt = (dt.hour * 60 + dt.minute + 14) // 15
            self.time.setCurrentIndex(min(len(self.slots) - 1, nxt))
        self._emit()

    def when(self) -> datetime:
        i = self.group.checkedId()
        if 0 <= i < len(self.chips):  # relative chips count from now, not from when the picker opened
            label = self.chips[i][0]
            return dict(when_chips()).get(label, self.chips[i][1])
        d = self.date.getDate()
        h, mi = self.time.currentData() or (9, 0)
        return datetime(d.year(), d.month(), d.day(), h, mi)

    def _emit(self) -> None:
        self.words.setText(plain_when(self.when()))
        self.changed.emit()
