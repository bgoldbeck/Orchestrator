from pathlib import Path

from orchestrator.core.events import Done, Error, Started, Text, ToolCall, ToolResult, Usage
from orchestrator.engines.base import TurnRequest
from orchestrator.engines.codex import build_codex_cmd, parse_codex_line

FIX = Path(__file__).parent / "fixtures"


def parse_file(name):
    out = []
    for line in (FIX / name).read_text(encoding="utf-8").splitlines():
        out.extend(parse_codex_line(line))
    return out


def test_ping_turn():
    evs = parse_file("codex_ping.jsonl")
    assert isinstance(evs[0], Started) and evs[0].session_id == "01a1ec67-1e73-7a92-80e9-034a4fd55be7"
    warn = [e for e in evs if isinstance(e, Error)]
    assert warn and warn[0].fatal is False and warn[0].kind == "warning"
    assert [e for e in evs if isinstance(e, Text)] == [Text("PING", delta=False)]
    usage = next(e for e in evs if isinstance(e, Usage))
    assert usage.input_tokens == 1200 and usage.cached_tokens == 800 and usage.output_tokens == 8
    assert isinstance(evs[-1], Done) and not evs[-1].is_error


def test_recorded_tool_turn():
    evs = parse_file("codex_tool_turn.jsonl")
    calls = [e for e in evs if isinstance(e, ToolCall)]
    results = [e for e in evs if isinstance(e, ToolResult)]
    assert calls and calls[0].name == "shell" and calls[0].summary == "type note.txt"  # wrapper unwrapped
    assert results and results[0].id == calls[0].id and not results[0].is_error
    assert "hello" in results[0].output
    assert isinstance(evs[-1], Done)


def test_failed_and_error_lines():
    evs = parse_codex_line('{"type":"turn.failed","error":{"message":"rate limited"}}')
    assert isinstance(evs[0], Error) and evs[0].message == "rate limited" and evs[0].fatal
    assert isinstance(evs[1], Done) and evs[1].is_error
    assert parse_codex_line('{"type":"error","message":"boom"}')[0].message == "boom"
    assert parse_codex_line("not json") == []
    assert parse_codex_line("") == []


def test_file_change_only_completed_emits_call_and_result():
    line = ('{"type":"item.completed","item":{"id":"i9","type":"file_change","status":"completed",'
            '"changes":[{"path":"a.py","kind":"update"}]}}')
    call, res = parse_codex_line(line)
    assert isinstance(call, ToolCall) and call.name == "edit" and "a.py" in call.summary
    assert isinstance(res, ToolResult) and res.id == "i9" and not res.is_error


def test_failed_command_is_error():
    line = ('{"type":"item.completed","item":{"id":"c1","type":"command_execution","command":"false",'
            '"aggregated_output":"","exit_code":1,"status":"failed"}}')
    _call, res = parse_codex_line(line)
    assert res.is_error


def test_build_cmd_new_and_resume():
    req = TurnRequest(prompt="hi", cwd=Path("C:/p"), model="gpt-6.2-sol", effort="low")
    cmd = build_codex_cmd("codex", req)
    assert cmd[:2] == ["codex", "exec"] and "--json" in cmd and cmd[-1] == "-"
    assert cmd[cmd.index("-m") + 1] == "gpt-6.2-sol"
    assert 'model_reasoning_effort="low"' in cmd and "-C" in cmd
    req.resume_id = "tid"
    cmd = build_codex_cmd("codex", req)
    assert cmd[1:3] == ["exec", "resume"] and cmd[-2:] == ["tid", "-"] and "-C" not in cmd
