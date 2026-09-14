"""Behavioral tests for the centralized tool execution path."""

from __future__ import annotations

from final_version_app.engine import CancellationToken, NullLoopObserver, TurnCancelledError
from final_version_app.tools import ToolOrchestrator


def model_call(name: str, call_id: str = "call_1", **arguments):
    return {"name": name, "id": call_id, "args": arguments}


def test_cacheable_tool_is_executed_once_for_identical_arguments():
    calls = []
    orchestrator = ToolOrchestrator(
        {"read": lambda path: calls.append(path) or f"content:{path}"},
        cacheable_names={"read"},
    )

    first = orchestrator.execute(model_call("read", path="a.py"))
    second = orchestrator.execute(model_call("read", call_id="call_2", path="a.py"))

    assert first.cached is False
    assert second.cached is True
    assert second.output == "content:a.py"
    assert calls == ["a.py"]


def test_mutating_tool_invalidates_read_cache():
    reads = []
    orchestrator = ToolOrchestrator(
        {
            "read": lambda path: reads.append(path) or f"version:{len(reads)}",
            "write": lambda path, content: "ok",
        },
        cacheable_names={"read"},
        cache_invalidating_names={"write"},
    )

    assert orchestrator.execute(model_call("read", path="a.py")).output == "version:1"
    orchestrator.execute(model_call("write", path="a.py", content="changed"))
    assert orchestrator.execute(model_call("read", path="a.py")).output == "version:2"


def test_unknown_tool_and_handler_exception_are_normalized():
    def broken():
        raise ValueError("broken")

    orchestrator = ToolOrchestrator({"broken": broken})

    unknown = orchestrator.execute(model_call("missing"))
    failure = orchestrator.execute(model_call("broken"))

    assert unknown.failed is True
    assert unknown.error_type == "UnknownToolError"
    assert failure.failed is True
    assert failure.error_type == "ValueError"
    assert failure.output == "Error: broken"


def test_cancelled_turn_does_not_start_tool_execution():
    token = CancellationToken()
    token.cancel("stop")
    orchestrator = ToolOrchestrator({"read": lambda: "should not run"})

    try:
        orchestrator.execute(
            model_call("read"),
            observer=NullLoopObserver(),
            cancellation=token,
        )
    except TurnCancelledError as exc:
        assert str(exc) == "stop"
    else:
        raise AssertionError("Expected cancellation to stop tool execution.")
