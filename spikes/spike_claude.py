"""M0 spike: drive Claude Code via claude_agent_sdk with streaming + resume.

Run: .venv\\Scripts\\python spikes\\spike_claude.py
"""
import asyncio
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    query,
)

SANDBOX = Path(__file__).resolve().parent / "sandbox"
MODEL = "haiku"


def opts(resume: str | None = None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(SANDBOX),
        model=MODEL,
        resume=resume,
        max_turns=1,
        allowed_tools=[],
        setting_sources=[],  # don't load user/project settings (hooks, plugins)
        include_partial_messages=True,  # token-level StreamEvent messages
    )


async def run(prompt: str, resume: str | None = None) -> tuple[str | None, str]:
    session_id = None
    text = []
    stream_counts: dict[str, int] = {}
    async for msg in query(prompt=prompt, options=opts(resume)):
        kind = type(msg).__name__
        if isinstance(msg, StreamEvent):
            et = msg.event.get("type", "?")
            stream_counts[et] = stream_counts.get(et, 0) + 1
            continue
        if isinstance(msg, SystemMessage):
            sid = msg.data.get("session_id")
            print(f"  {kind} subtype={msg.subtype} session_id={sid}")
            session_id = session_id or sid
        elif isinstance(msg, AssistantMessage):
            blocks = [type(b).__name__ for b in msg.content]
            print(f"  {kind} model={msg.model} blocks={blocks}")
            text += [b.text for b in msg.content if isinstance(b, TextBlock)]
        elif isinstance(msg, ResultMessage):
            print(
                f"  {kind} subtype={msg.subtype} is_error={msg.is_error} "
                f"session_id={msg.session_id} turns={msg.num_turns} "
                f"cost_usd={msg.total_cost_usd} duration_ms={msg.duration_ms}"
            )
            session_id = msg.session_id
        else:
            print(f"  {kind}")
    print(f"  StreamEvent counts: {stream_counts}")
    return session_id, "".join(text).strip()


async def main() -> None:
    SANDBOX.mkdir(exist_ok=True)
    print("== turn 1")
    sid, reply = await run("Reply with the word PING and nothing else.")
    print(f"  reply={reply!r} session_id={sid}")
    print("== turn 2 (resume)")
    sid2, reply2 = await run("What word did you just say?", resume=sid)
    print(f"  reply={reply2!r} session_id={sid2} same_session={sid == sid2}")
    print("RESULT:", "OK" if "PING" in reply2.upper() else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
