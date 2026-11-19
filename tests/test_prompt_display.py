from orchestrator.core import orchestration as O
from orchestrator.core.prompt_display import split_prompt


def test_orchestrator_prompt_shows_goal():
    full = O.ORCH_PROMPT.format(path="C:/x", goal="Initial test. reply when done", max_parallel=4, models="-", guide="")
    shown, hidden = split_prompt(full)
    assert shown == "Initial test. reply when done"
    assert hidden == full


def test_subagent_prompt_shows_task_and_criteria():
    full = O.SUBAGENT_INTRO + "Fix the login bug" + O.criteria_block(["tests pass"]) + O.CLOSING
    shown, hidden = split_prompt(full)
    assert shown.startswith("Fix the login bug")
    assert "1. tests pass" in shown and "report block" not in shown
    assert hidden == full


def test_follow_up_and_plain_text():
    shown, hidden = split_prompt("Also add a test" + O.CLOSING)
    assert shown == "Also add a test" and hidden
    assert split_prompt("hello") == ("hello", "")


def test_user_message_toggle(qtbot=None):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from orchestrator.ui.chat_widgets import UserMessage

    m = UserMessage("goal", hidden="FULL PROMPT")
    assert m.label.text() == "goal" and m.toggle is not None
    m.show_full(True)
    assert m.label.text() == "FULL PROMPT"
    m.show_full(False)
    assert m.label.text() == "goal"
