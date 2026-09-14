"""Integration tests for the protocol-driven runtime v0.1."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
from time import sleep
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from final_version_app.engine import AgentRuntime
from final_version_app.protocol import (
    CloseThreadCommand,
    EventKind,
    ForkThreadCommand,
    RuntimeEvent,
    StartThreadCommand,
    TurnStatus,
)
from final_version_app.storage import JsonThreadStore, JsonlEventStore


def build_test_runtime(root: Path, executor) -> AgentRuntime:
    """Build a kernel with no network or real model dependencies."""

    return AgentRuntime(
        services=SimpleNamespace(),
        tool_runtime=SimpleNamespace(),
        system_prompt="test system",
        event_store=JsonlEventStore(root),
        thread_store=JsonThreadStore(root),
        turn_executor=executor,
    )


def append_assistant(messages, observer, text: str) -> None:
    message = AIMessage(content=text)
    messages.append(message)
    observer.message_appended(message)


def test_event_store_assigns_monotonic_sequences_and_round_trips(tmp_path):
    store = JsonlEventStore(tmp_path)
    thread_id = "thr_test"

    first = store.append(RuntimeEvent(thread_id=thread_id, kind=EventKind.THREAD_STARTED))
    second = store.append(RuntimeEvent(thread_id=thread_id, kind=EventKind.THREAD_RESUMED))

    assert (first.sequence, second.sequence) == (1, 2)
    assert [event.event_id for event in store.read(thread_id)] == [
        first.event_id,
        second.event_id,
    ]
    assert [event.sequence for event in store.read(thread_id, after_sequence=1)] == [2]


def test_runtime_persists_turn_and_resumes_message_history(tmp_path):
    seen_message_counts = []

    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        cancellation.raise_if_cancelled()
        seen_message_counts.append(len(messages))
        append_assistant(messages, observer, f"answer-{len(seen_message_counts)}")

    runtime = build_test_runtime(tmp_path, executor)
    thread = runtime.submit(StartThreadCommand(title="runtime test"))
    first = runtime.run_turn(thread.thread_id, "first question")

    assert first.status == TurnStatus.COMPLETED
    assert first.assistant_text == "answer-1"
    assert seen_message_counts == [1]

    restarted = build_test_runtime(tmp_path, executor)
    resumed = restarted.resume_thread(thread.thread_id)
    assert resumed.thread_id == thread.thread_id
    assert len(restarted.history(thread.thread_id)) == 2

    second = restarted.run_turn(thread.thread_id, "second question")
    assert second.status == TurnStatus.COMPLETED
    assert seen_message_counts == [1, 3]

    events = restarted.events(thread.thread_id)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(event.kind == EventKind.TURN_COMPLETED for event in events) == 2
    assert sum(event.kind == EventKind.TOKEN_USAGE_UPDATED for event in events) == 2


def test_context_compaction_is_the_projection_used_after_resume(tmp_path):
    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        cancellation.raise_if_cancelled()
        before = 100
        messages[:] = [AIMessage(content="compacted context")]
        observer.context_compacted("test", messages, before, 5)
        append_assistant(messages, observer, "final answer")

    runtime = build_test_runtime(tmp_path, executor)
    thread = runtime.start_thread()
    runtime.run_turn(thread.thread_id, "large original prompt")

    restarted = build_test_runtime(tmp_path, executor)
    restarted.resume_thread(thread.thread_id)
    restored = restarted.history(thread.thread_id)

    assert [message.content for message in restored] == ["compacted context", "final answer"]


def test_interrupt_propagates_to_running_turn_and_records_terminal_state(tmp_path):
    entered = Event()

    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        entered.set()
        while True:
            cancellation.raise_if_cancelled()
            sleep(0.005)

    runtime = build_test_runtime(tmp_path, executor)
    thread = runtime.start_thread()
    results = []
    worker = Thread(target=lambda: results.append(runtime.run_turn(thread.thread_id, "wait")))
    worker.start()

    assert entered.wait(timeout=1)
    assert runtime.interrupt_turn(thread.thread_id, "user requested stop") is True
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert results[0].status == TurnStatus.INTERRUPTED
    assert results[0].error == "user requested stop"
    kinds = [event.kind for event in runtime.events(thread.thread_id)]
    assert EventKind.CANCELLATION_REQUESTED in kinds
    assert EventKind.TURN_INTERRUPTED in kinds


def test_failed_executor_becomes_failed_turn_without_losing_thread(tmp_path):
    def executor(*_args, **_kwargs):
        raise ValueError("synthetic failure")

    runtime = build_test_runtime(tmp_path, executor)
    thread = runtime.start_thread()
    result = runtime.run_turn(thread.thread_id, "fail safely")

    assert result.status == TurnStatus.FAILED
    assert result.error == "ValueError: synthetic failure"
    assert runtime.thread_store.get(thread.thread_id).status.value == "idle"
    assert runtime.events(thread.thread_id)[-1].kind == EventKind.TURN_FAILED


def test_fork_and_close_are_available_through_protocol_commands(tmp_path):
    def executor(messages, _services, _tools, _system, *, observer, cancellation):
        cancellation.raise_if_cancelled()
        append_assistant(messages, observer, "parent answer")

    runtime = build_test_runtime(tmp_path, executor)
    parent = runtime.submit(StartThreadCommand(title="parent"))
    runtime.run_turn(parent.thread_id, "parent question")

    fork = runtime.submit(ForkThreadCommand(parent.thread_id, title="child"))
    assert fork.parent_thread_id == parent.thread_id
    assert len(runtime.history(fork.thread_id)) == 2

    closed = runtime.submit(CloseThreadCommand(fork.thread_id))
    assert closed.status.value == "closed"
    assert runtime.events(fork.thread_id)[-1].kind == EventKind.THREAD_CLOSED
