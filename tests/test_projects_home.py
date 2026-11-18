import os
import time
from datetime import datetime

import pytest

from orchestrator.ui_text import relative

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_relative_times():
    now = datetime(2026, 11, 18, 12, 0).timestamp()
    assert relative(now - 20, now) == "just now"
    assert relative(now - 60, now) == "1 minute ago"
    assert relative(now - 5 * 60, now) == "5 minutes ago"
    assert relative(now - 3600, now) == "1 hour ago"
    assert relative(now - 2 * 86400, now) == "2 days ago"
    assert relative(datetime(2026, 11, 10, 9, 0).timestamp(), now) == "Nov 10"
    assert relative(datetime(2025, 3, 4, 9, 0).timestamp(), now) == "Mar 4, 2025"
    assert relative(None) == ""


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_default_style_is_stable_and_valid(qapp):
    from orchestrator.ui.projects_page import PROJECT_COLORS, PROJECT_ICONS, default_style

    a = default_style("weather-app")
    assert a == default_style("Weather-App") and a[0] in PROJECT_ICONS and a[1] in PROJECT_COLORS
    assert len({default_style(f"p{i}") for i in range(40)}) > 5  # varied


def test_db_project_fields_and_activity(tmp_path):
    from orchestrator.storage.db import Database

    db = Database(tmp_path / "t.db")
    a = db.add_project(str(tmp_path / "a"), "alpha")
    b = db.add_project(str(tmp_path / "b"), "beta")
    db.update_project(a.id, pinned=True, icon="GAME", color="#4ade80", name="Alpha!")
    pa = db.project(a.id)
    assert (pa.pinned, pa.icon, pa.color, pa.name) == (True, "GAME", "#4ade80", "Alpha!")
    s = db.create_session(b.id, "codex", "m", "low")
    db.update_session(s.id, last_active=time.time() + 100)
    db.update_session(s.id, last_active=1000.0)  # old chat: activity follows the chat, not when it was added
    act = db.project_activity()
    assert act[b.id] == 1000.0 and act[a.id] > 1000.0


def test_sort_projects():
    from orchestrator.storage.db import Project
    from orchestrator.ui.projects_page import sort_projects

    ps = [Project(1, "b", "x", created_at=10), Project(2, "a", "y", created_at=30), Project(3, "c", "z", created_at=20)]
    act = {1: 100, 2: 50, 3: 70}
    assert [p.id for p in sort_projects(ps, act, "recent")] == [1, 3, 2]
    assert [p.id for p in sort_projects(ps, act, "name")] == [2, 1, 3]
    assert [p.id for p in sort_projects(ps, act, "created")] == [2, 3, 1]


def test_projects_page_sections_and_search(qapp, tmp_path):
    from qfluentwidgets import CaptionLabel

    from orchestrator.storage.db import Database
    from orchestrator.ui.projects_page import ProjectCard, ProjectsPage

    db = Database(tmp_path / "t.db")
    a = db.add_project(str(tmp_path / "a"), "alpha")
    db.add_project(str(tmp_path / "b"), "beta")
    db.add_project(str(tmp_path / "c"), "gamma", auto=True)
    db.update_project(a.id, pinned=True)
    page = ProjectsPage(db)
    page.resize(1200, 800)

    def state():
        from PySide6.QtCore import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()
        labels = [w.text() for w in page.column.findChildren(CaptionLabel) if w.isVisibleTo(page.column)
                  and w.text() in ("Pinned", "Other projects", "Projects", "Discovered in engine history")]
        cards = sorted(c.pid for c in page.column.findChildren(ProjectCard) if not c.isHidden() and c.parent().isVisibleTo(page.column))
        return labels, cards

    page.reload()
    labels, cards = state()
    assert labels == ["Pinned", "Other projects"] and len(cards) == 2  # discovered hidden by default
    page.search.setText("gam")
    labels, cards = state()
    assert labels == ["Discovered in engine history"] and len(cards) == 1
    opened = []
    page.openProject.connect(opened.append)
    page.column.findChildren(ProjectCard)[-1].clicked.emit(a.id)
    assert opened == [a.id]
