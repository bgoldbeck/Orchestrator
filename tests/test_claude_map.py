from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from orchestrator.core.events import Done, Error, Started, Text, ToolCall, ToolResult, Usage
from orchestrator.engines.claude import NOT_LOGGED_IN, ClaudeMapState, map_claude_message

SID = "6f1c0d2e-0000-4000-8000-000000000001"


def run(msgs):
    st = ClaudeMapState()
    out = []
    for m in msgs:
        out.extend(map_claude_message(m, st))
    return out


def delta(text):
    return StreamEvent(uuid="u", session_id=SID, event={
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})


def test_streaming_text_turn():
    evs = run([
        SystemMessage(subtype="init", data={"session_id": SID, "model": "claude-haiku-5-5"}),
        StreamEvent(uuid="u0", session_id=SID, event={"type": "message_start", "message": {}}),
        delta("PI"), delta("NG"),
        AssistantMessage(content=[TextBlock("PING")], model="claude-haiku-5-5", session_id=SID),
        ResultMessage(subtype="success", duration_ms=900, duration_api_ms=800, is_error=False, num_turns=1,
                      session_id=SID, total_cost_usd=0.0004, result="PING",
                      usage={"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 5}),
    ])
    assert [e for e in evs if isinstance(e, Started)] == [Started(SID)]
    assert evs[0] == Started(SID)
    assert [e for e in evs if isinstance(e, Text)] == [Text("PI", True), Text("NG", True), Text("PING", False)]
    u = next(e for e in evs if isinstance(e, Usage))
    assert (u.input_tokens, u.output_tokens, u.cached_tokens, u.cost_usd) == (10, 3, 5, 0.0004)
    assert evs[-1] == Done(is_error=False, result="PING")


def test_tool_use_and_result():
    evs = run([
        AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls -la"})],
                         model="m", session_id=SID),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="file.txt", is_error=False)]),
    ])
    call = next(e for e in evs if isinstance(e, ToolCall))
    assert call.name == "Bash" and call.summary == "ls -la"
    res = next(e for e in evs if isinstance(e, ToolResult))
    assert res.id == "t1" and res.output == "file.txt" and res.name == "Bash"


def test_not_logged_in_result_maps_to_auth_error():
    evs = run([ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0, is_error=True, num_turns=0,
                             session_id=SID, result="Not logged in · Please run /login")])
    err = next(e for e in evs if isinstance(e, Error))
    assert err.kind == "auth" and err.message == NOT_LOGGED_IN
    assert evs[-1].is_error


def test_auth_failed_assistant_message():
    evs = run([AssistantMessage(content=[], model="m", error="authentication_failed", session_id=SID)])
    assert any(isinstance(e, Error) and e.kind == "auth" for e in evs)
