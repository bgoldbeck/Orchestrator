"""Model preference rules: router and planner use them first (enabled models only), offers from chat, UI table."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from orchestrator.core import preferences as P  # noqa: E402
from orchestrator.core import routing  # noqa: E402
from orchestrator.storage.db import Database  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    d = Database(tmp_path / "t.db")
    d.set_setting(P.CAPS_KEY, {"codex": True, "checked_at": 9e12})  # no `codex features list` in tests
    return d


def test_seeded_rules_and_match(db):
    rs = P.rules(db)
    assert rs[0].name == "Complex coding" and rs[0].model == "claude-opus-5-5" and rs[0].effort == "medium"
    assert [r.priority for r in rs] == sorted(r.priority for r in rs)
    pick = routing.route(db, "Debug the flaky login test")
    assert (pick.engine, pick.model, pick.effort) == ("claude", "claude-opus-5-5", "medium")
    assert pick.reason.startswith("Preference: Complex coding → Opus 5.5 · medium") and "debug" in pick.reason
    # keyword with suffix and phrase across hyphen
    assert P.match(db, "Refactoring the storage layer")[0].name == "Large refactors"
    assert P.match(db, "Fix the race-condition in jobs")[0].name == "Complex coding"
    assert P.match(db, "Add a new button") is None
    assert routing.route(db, "Add a new button").reason.startswith("Auto:")


def test_disabled_model_rule_is_skipped(db):
    db.set_model_enabled("claude", "claude-opus-5-5", False)
    pick = routing.route(db, "Debug the flaky login test")
    assert pick.model != "claude-opus-5-5" and not pick.reason.startswith("Preference: Complex")
    assert "claude-opus-5-5" not in P.prompt_block(db)


def test_priority_order_and_forced_engine(db):
    rules = P.rules(db)
    rules.append(P.Rule(name="Docs", keywords=["debug"], engine="codex", model="gpt-6-luna", effort="low", priority=0))
    P.save_rules(db, rules)
    assert routing.route(db, "debug it").model == "gpt-6-luna"  # priority 0 beats 1
    assert routing.route(db, "debug it", engine="claude").model == "claude-opus-5-5"


def test_planner_uses_rules_and_prompt_lists_them(db):
    pick = routing.route_planner(db, "Investigate the memory leak")
    assert pick.reason.startswith("Preference (planner): Complex coding") and pick.effort == "medium"
    g = routing.planner_guide(db)
    assert g.startswith("User preferences") and "Complex coding" in g and "model=claude-opus-5-5" in g
    assert "Otherwise:" in g


def test_parse_preference_from_chat(db):
    r = P.parse_preference(db, "Please refactor X. Also, I prefer Opus 5.5 for complex coding, medium effort.")
    assert r and r.engine == "claude" and r.model == "claude-opus-5-5" and r.effort == "medium"
    assert r.name == "Complex coding" and "complex coding" in r.keywords
    r2 = P.parse_preference(db, "use gpt-6.2-sol for images")
    assert r2.engine == "codex" and r2.model == "gpt-6.2-sol" and P.is_image_rule(r2)
    assert P.parse_preference(db, "use the new API for logging") is None


def test_image_warnings(db):
    claude = P.Rule(name="Images", keywords=["logo"], engine="claude", model="claude-sonnet-5-5")
    assert "can't generate images" in P.image_warning(db, claude)
    codex = P.Rule(name="Images", keywords=["logo"], engine="codex", model="gpt-6.2-sol")
    assert P.image_warning(db, codex) == ""
    db.set_setting(P.CAPS_KEY, {"codex": False, "checked_at": 9e12})
    assert "image_generation feature is off" in P.image_warning(db, codex)
    db.set_model_enabled("codex", None, False)
    assert "No Codex model is enabled" in P.image_warning(db, codex)
    assert P.image_warning(db, P.Rule(name="Complex coding", keywords=[], engine="claude", model="x")) == ""




def test_offer_flow(tmp_path, monkeypatch, qapp):
    from PySide6.QtCore import QObject, Signal

    from orchestrator.core.orchestration import OrchestrationManager

    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    db = Database(tmp_path / "o.db")
    db.set_setting(P.CAPS_KEY, {"codex": True, "checked_at": 9e12})
    proj = db.add_project(str(tmp_path), "proj")

    class Mgr(QObject):
        status_changed = Signal(int, str)
        session_updated = Signal(int)
        approval_requested = Signal(int, object, object)

        def __init__(self):
            super().__init__()
            self.run_options, self.sent = {}, []

        def send(self, sid, text, **_k):
            self.sent.append((sid, text))

        def is_running(self, sid):
            return False

    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    mgr = Mgr()
    om = OrchestrationManager(db, mgr)
    o = om.start(proj.id, "Tidy the docs. I prefer Haiku 5.5 for docs work", "auto", "", "")
    assert "User preferences" in mgr.sent[0][1] and "suggest_preference" in mgr.sent[0][1]
    offers = om.rule_offers[o.id]
    assert len(offers) == 1 and offers[0]["text"].startswith("Docs → Claude Haiku 5.5")
    res = om.suggest_preference(o.id, {"task_type": "Images", "model": "claude-sonnet-5-5", "keywords": ["logo"]})
    assert res["offered"] and "can't generate images" in res["warning"]
    om.resolve_offer(o.id, offers[0]["id"], True)
    assert any(r.name == "Docs" and r.model == "claude-haiku-5-5" for r in P.rules(db))
    om.resolve_offer(o.id, om.rule_offers[o.id][0]["id"], False)
    assert om.rule_offers[o.id] == [] and not any(r.name == "Images" for r in P.rules(db))
    with pytest.raises(Exception):
        om.suggest_preference(o.id, {"task_type": "X", "model": "nope"})

    from orchestrator.ui.orchestrate_page import OrchestratePage

    om.suggest_preference(o.id, {"task_type": "Images", "model": "gpt-6.2-sol", "keywords": ["logo"]})
    page = OrchestratePage(db, mgr, om)
    page.current = o.id
    page.render_board()
    from qfluentwidgets import PrimaryPushButton

    btn = next(b for b in page.findChildren(PrimaryPushButton) if b.objectName() == "saveRule")
    btn.click()
    assert any(r.name == "Images" and r.engine == "codex" for r in P.rules(db))


def test_rules_card(db, qapp):
    from orchestrator.ui.rules_card import C_MODEL, C_NAME, RulesCard

    card = RulesCard(db)
    assert card.table.rowCount() == 3
    assert card.table.item(0, C_NAME).text() == "Complex coding"
    assert card.table.cellWidget(0, C_MODEL).currentData() == "claude-opus-5-5"
    card._add()
    r = card.table.rowCount() - 1
    card.table.item(r, C_NAME).setText("Images")
    card.table.item(r, 3).setText("logo, icon art")
    assert any("can't generate images" in w for w in card.warnings())
    card.save()
    assert [x.name for x in P.rules(db)][-1] == "Images"
    db.set_model_enabled("claude", "claude-opus-5-5", False)
    card.load()
    assert any("Opus 5.5 is disabled" in w for w in card.warnings())
    assert "(disabled)" in card.table.cellWidget(0, C_MODEL).currentText()
