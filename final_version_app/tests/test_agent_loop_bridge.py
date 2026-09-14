"""Verify that the real loop emits lifecycle callbacks through the new bridge."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from final_version_app.application.agent_loop import agent_loop
from final_version_app.engine import NullLoopObserver


class RecordingObserver(NullLoopObserver):
    def __init__(self):
        self.messages = []
        self.started = []
        self.completed = []

    def message_appended(self, message):
        self.messages.append(message)

    def tool_started(self, tool_name, tool_call_id, arguments):
        self.started.append((tool_name, tool_call_id, arguments))

    def tool_completed(self, tool_name, tool_call_id, output, *, cached, failed):
        self.completed.append((tool_name, tool_call_id, output, cached, failed))


def test_agent_loop_reports_messages_and_tool_lifecycle():
    responses = [
        AIMessage(
            content="",
            tool_calls=[{"name": "read", "id": "call_read", "args": {"path": "a.py"}}],
        ),
        AIMessage(content="done"),
    ]
    services = SimpleNamespace(
        session_memory=SimpleNamespace(
            maybe_schedule_extraction=lambda _messages: None,
            auto_compact_threshold=lambda: 1_000_000,
        ),
        bg=SimpleNamespace(drain=lambda: []),
        bus=SimpleNamespace(read_inbox=lambda _name: []),
        todo=SimpleNamespace(has_open_items=lambda: False),
    )
    tools = SimpleNamespace(
        tools=[object()],
        handlers={"read": lambda path: f"contents:{path}"},
        cacheable_tool_names={"read"},
        cache_invalidating_tool_names=set(),
    )
    observer = RecordingObserver()
    messages = [HumanMessage(content="read it")]

    with patch(
        "final_version_app.application.agent_loop.invoke_langchain",
        side_effect=responses,
    ):
        agent_loop(messages, services, tools, "system", observer=observer)

    assert observer.started == [("read", "call_read", {"path": "a.py"})]
    assert observer.completed == [
        ("read", "call_read", "contents:a.py", False, False)
    ]
    assert len(observer.messages) == 3
    assert messages[-1].content == "done"


def test_agent_loop_finalizes_without_tools_after_round_limit(monkeypatch):
    first = AIMessage(
        content="",
        tool_calls=[{"name": "read", "id": "call_read", "args": {"path": "a.py"}}],
    )
    final = AIMessage(content="final answer from gathered tool output")
    services = SimpleNamespace(
        session_memory=SimpleNamespace(
            maybe_schedule_extraction=lambda _messages: None,
            auto_compact_threshold=lambda: 1_000_000,
        ),
        bg=SimpleNamespace(drain=lambda: []),
        bus=SimpleNamespace(read_inbox=lambda _name: []),
        todo=SimpleNamespace(has_open_items=lambda: False),
    )
    tools = SimpleNamespace(
        tools=[object()],
        handlers={"read": lambda path: f"contents:{path}"},
        cacheable_tool_names=set(),
        cache_invalidating_tool_names=set(),
    )
    messages = [HumanMessage(content="read it")]

    monkeypatch.setattr(
        "final_version_app.application.agent_loop.AGENT_MAX_TOOL_ROUNDS",
        1,
    )
    with patch(
        "final_version_app.application.agent_loop.invoke_langchain",
        side_effect=[first, final],
    ) as invoke:
        agent_loop(messages, services, tools, "system", observer=RecordingObserver())

    assert messages[-1].content == "final answer from gathered tool output"
    assert "runtime_guardrail" in messages[-2].content
    assert invoke.call_args_list[-1].kwargs["tools"] == []
