"""Protocol-driven runtime kernel around the existing coding-agent loop."""

from __future__ import annotations

from dataclasses import replace
from threading import Lock, RLock
from time import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from final_version_app.application.session_memory import ensure_message_uuid
from final_version_app.application.compression import auto_compact
from final_version_app.application.session_memory import annotate_messages
from final_version_app.engine.cancellation import CancellationToken, TurnCancelledError
from final_version_app.engine.event_observer import RuntimeLoopObserver
from final_version_app.engine.message_codec import decode_message, decode_messages, encode_messages
from final_version_app.infra.llm import estimate_tokens, render_ai_text
from final_version_app.infra.usage import get_run_usage, get_usage_snapshot, usage_delta, usage_scope
from final_version_app.protocol import (
    AgentItem,
    CloseThreadCommand,
    EventKind,
    ForkThreadCommand,
    InterruptTurnCommand,
    ItemKind,
    ResumeThreadCommand,
    RuntimeEvent,
    StartThreadCommand,
    StartTurnCommand,
    ThreadRecord,
    ThreadStatus,
    TurnResult,
    TurnStatus,
    new_id,
)
from final_version_app.storage import EventStore, ThreadStore


TurnExecutor = Callable[..., None]


class RuntimeThreadBusyError(RuntimeError):
    """Raised when a second turn is submitted to a busy thread."""


class AgentRuntime:
    """Small orchestration kernel for durable, observable agent turns.

    The kernel owns lifecycle and persistence. It deliberately delegates model
    reasoning and tool behavior to the existing loop, keeping this layer small
    enough to remain stable as specialist units are added later.
    """

    def __init__(
        self,
        *,
        services: Any,
        tool_runtime: Any,
        system_prompt: str,
        event_store: EventStore,
        thread_store: ThreadStore,
        turn_executor: TurnExecutor,
        team_manager: Any = None,
        thread_context_factory: Any = None,
    ) -> None:
        self.services = services
        self.tool_runtime = tool_runtime
        self.system_prompt = system_prompt
        self.event_store = event_store
        self.thread_store = thread_store
        self.turn_executor = turn_executor
        self.team_manager = team_manager
        self.thread_context_factory = thread_context_factory
        self._thread_contexts = {}
        self._state_lock = RLock()
        self._histories: Dict[str, List[BaseMessage]] = {}
        self._thread_locks: Dict[str, Lock] = {}
        self._active_turns: Dict[str, Tuple[str, CancellationToken]] = {}

    def thread_context(self, thread_id: str):
        """Bind memory/tool handlers to the owning thread, including after resume."""
        if self.thread_context_factory is None:
            return self.services, self.tool_runtime, self.system_prompt
        with self._state_lock:
            if thread_id not in self._thread_contexts:
                context = self.thread_context_factory(thread_id)
                memory = getattr(context[0], "session_memory", None)
                if getattr(memory, "durable_archive", False):
                    # Bootstrap old threads from original message events, not only the
                    # latest compacted projection. Re-observation is UUID-idempotent.
                    original = []
                    for event in self.event_store.read(thread_id):
                        if event.kind != EventKind.ITEM_COMPLETED:
                            continue
                        item = event.payload.get("item") or {}
                        encoded = (item.get("payload") or {}).get("message")
                        if not isinstance(encoded, dict):
                            continue
                        message = decode_message(encoded)
                        if isinstance(message, HumanMessage) and event.turn_id:
                            message.additional_kwargs.setdefault("runtime_turn_id", event.turn_id)
                        original.append(message)
                    memory.observe(original)
                self._thread_contexts[thread_id] = context
            return self._thread_contexts[thread_id]

    def submit(self, command: Any) -> Any:
        """Dispatch a protocol command for future transport adapters."""

        if isinstance(command, StartThreadCommand):
            return self.start_thread(command.title, command.metadata)
        if isinstance(command, ResumeThreadCommand):
            return self.resume_thread(command.thread_id)
        if isinstance(command, ForkThreadCommand):
            return self.fork_thread(command.thread_id, command.title)
        if isinstance(command, CloseThreadCommand):
            return self.close_thread(command.thread_id)
        if isinstance(command, StartTurnCommand):
            return self.run_turn(command.thread_id, command.prompt)
        if isinstance(command, InterruptTurnCommand):
            return self.interrupt_turn(command.thread_id, command.reason)
        raise TypeError(f"Unsupported runtime command: {type(command).__name__}")

    def start_thread(
        self,
        title: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ThreadRecord:
        now = time()
        record = ThreadRecord(
            thread_id=new_id("thr"),
            status=ThreadStatus.IDLE,
            created_at=now,
            updated_at=now,
            title=title.strip(),
            metadata=dict(metadata or {}),
        )
        self.thread_store.save(record)
        with self._state_lock:
            self._histories[record.thread_id] = []
            self._thread_locks[record.thread_id] = Lock()
        self._append(
            RuntimeEvent(
                thread_id=record.thread_id,
                kind=EventKind.THREAD_STARTED,
                payload={"thread": record.to_dict()},
            )
        )
        return record

    def resume_thread(self, thread_id: str) -> ThreadRecord:
        record = self.thread_store.get(thread_id)
        history = self._rebuild_history(thread_id)
        # A process crash can leave metadata marked running. No task from that
        # process is alive here, so resuming safely returns the thread to idle.
        if record.status == ThreadStatus.RUNNING:
            metadata = dict(record.metadata)
            metadata["recovered_stale_turn"] = record.latest_turn_id
            record = replace(record, status=ThreadStatus.IDLE, updated_at=time(), metadata=metadata)
            self.thread_store.save(record)
        with self._state_lock:
            self._histories[thread_id] = history
            self._thread_locks.setdefault(thread_id, Lock())
        self._append(
            RuntimeEvent(
                thread_id=thread_id,
                kind=EventKind.THREAD_RESUMED,
                payload={"message_count": len(history)},
            )
        )
        return record

    def fork_thread(self, thread_id: str, title: str = "") -> ThreadRecord:
        parent = self.thread_store.get(thread_id)
        history = self.history(thread_id)
        now = time()
        record = ThreadRecord(
            thread_id=new_id("thr"),
            status=ThreadStatus.IDLE,
            created_at=now,
            updated_at=now,
            title=title.strip() or f"Fork of {parent.title or parent.thread_id}",
            parent_thread_id=thread_id,
            metadata={"forked_from_turn": parent.latest_turn_id},
        )
        self.thread_store.save(record)
        if self.thread_context_factory is not None:
            parent_memory = self.thread_context(thread_id)[0].session_memory
            child_memory = self.thread_context(record.thread_id)[0].session_memory
            if hasattr(child_memory, "fork_from"):
                child_memory.fork_from(parent_memory)
        with self._state_lock:
            self._histories[record.thread_id] = list(history)
            self._thread_locks[record.thread_id] = Lock()
        self._append(
            RuntimeEvent(
                thread_id=record.thread_id,
                kind=EventKind.THREAD_FORKED,
                payload={"parent_thread_id": thread_id, "messages": encode_messages(history)},
            )
        )
        return record

    def close_thread(self, thread_id: str) -> ThreadRecord:
        """Close an idle thread without deleting its audit history."""

        with self._state_lock:
            if thread_id in self._active_turns:
                raise RuntimeThreadBusyError(f"Cannot close running thread {thread_id}.")
        record = self.thread_store.get(thread_id)
        if record.status == ThreadStatus.CLOSED:
            return record
        closed = replace(record, status=ThreadStatus.CLOSED, updated_at=time())
        self.thread_store.save(closed)
        self._append(RuntimeEvent(thread_id=thread_id, kind=EventKind.THREAD_CLOSED))
        return closed

    def run_turn(self, thread_id: str, prompt: str) -> TurnResult:
        if not prompt.strip():
            raise ValueError("Turn prompt must not be empty.")
        record = self.thread_store.get(thread_id)
        if record.status == ThreadStatus.CLOSED:
            raise RuntimeError(f"Thread {thread_id} is closed.")

        lock = self._get_thread_lock(thread_id)
        if not lock.acquire(blocking=False):
            raise RuntimeThreadBusyError(f"Thread {thread_id} already has a running turn.")

        turn_id = new_id("turn")
        token = CancellationToken()
        observer = RuntimeLoopObserver(self._append, thread_id, turn_id)
        usage_before = get_usage_snapshot()
        status = TurnStatus.RUNNING
        error: Optional[str] = None
        assistant_text = ""
        usage = usage_delta(usage_before, usage_before)

        try:
            with self._state_lock:
                history = self._histories.get(thread_id)
                if history is None:
                    history = self._rebuild_history(thread_id)
                    self._histories[thread_id] = history
                self._active_turns[thread_id] = (turn_id, token)

            running_record = replace(
                record,
                status=ThreadStatus.RUNNING,
                latest_turn_id=turn_id,
                updated_at=time(),
            )
            self.thread_store.save(running_record)
            self._append(
                RuntimeEvent(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    kind=EventKind.TURN_STARTED,
                    # The full input is persisted once as a user-message item.
                    # Keeping it out of lifecycle metadata avoids duplication.
                    payload={"input_item_pending": True},
                )
            )

            user_message = HumanMessage(content=prompt, additional_kwargs={"runtime_turn_id": turn_id})
            ensure_message_uuid(user_message)
            history.append(user_message)
            observer.message_appended(user_message)

            scoped_services, scoped_tools, scoped_prompt = self.thread_context(thread_id)
            with usage_scope(turn_id):
                self.turn_executor(
                    history,
                    scoped_services,
                    scoped_tools,
                    scoped_prompt,
                    observer=observer,
                    cancellation=token,
                )
            memory = getattr(scoped_services, "session_memory", None)
            if getattr(memory, "durable_archive", False):
                memory.observe(history)
            token.raise_if_cancelled()
            status = TurnStatus.COMPLETED
            if history and isinstance(history[-1], AIMessage):
                assistant_text = render_ai_text(history[-1]).strip()
        except (TurnCancelledError, KeyboardInterrupt) as exc:
            token.cancel(str(exc) or "Turn interrupted.")
            status = TurnStatus.INTERRUPTED
            error = token.reason
        except Exception as exc:  # The protocol reports failure without killing the REPL.
            status = TurnStatus.FAILED
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                # Process-wide snapshot deltas double-count overlapping turns.
                # The ledger tags every model call with this turn id, so the
                # terminal event can expose an exact per-thread total.
                usage = get_run_usage(turn_id, after_sequence=usage_before.sequence)
                self._append(
                    RuntimeEvent(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        kind=EventKind.TOKEN_USAGE_UPDATED,
                        payload=usage.as_dict(),
                    )
                )
                terminal_kind = {
                    TurnStatus.COMPLETED: EventKind.TURN_COMPLETED,
                    TurnStatus.FAILED: EventKind.TURN_FAILED,
                    TurnStatus.INTERRUPTED: EventKind.TURN_INTERRUPTED,
                }.get(status, EventKind.TURN_FAILED)
                self._append(
                    RuntimeEvent(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        kind=terminal_kind,
                        payload={"error": error, "assistant_text": assistant_text},
                    )
                )
                latest = self.thread_store.get(thread_id)
                self.thread_store.save(replace(latest, status=ThreadStatus.IDLE, updated_at=time()))
            finally:
                # Persistence failures must not leak a busy lock forever.
                with self._state_lock:
                    self._active_turns.pop(thread_id, None)
                lock.release()

        return TurnResult(
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
            assistant_text=assistant_text,
            error=error,
            usage=usage.as_dict(),
        )

    def interrupt_turn(self, thread_id: str, reason: Optional[str] = None) -> bool:
        with self._state_lock:
            active = self._active_turns.get(thread_id)
        if active is None:
            return False
        turn_id, token = active
        changed = token.cancel(reason)
        if changed:
            self._append(
                RuntimeEvent(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    kind=EventKind.CANCELLATION_REQUESTED,
                    payload={"reason": token.reason},
                )
            )
        return changed

    def record_queued_interruption(
        self,
        thread_id: str,
        reason: Optional[str] = None,
    ) -> str:
        """Persist a terminal event for work cancelled before a worker starts.

        HTTP scheduling lives outside the kernel, but the canonical event log
        still needs a terminal fact or browser clients would poll forever.
        """

        lock = self._get_thread_lock(thread_id)
        if not lock.acquire(blocking=False):
            raise RuntimeThreadBusyError(f"Thread {thread_id} already has a running turn.")
        try:
            with self._state_lock:
                if thread_id in self._active_turns:
                    raise RuntimeThreadBusyError(
                        f"Thread {thread_id} already has a running turn."
                    )
            record = self.thread_store.get(thread_id)
            turn_id = new_id("turn")
            resolved_reason = reason or "Turn cancelled before execution."
            self.thread_store.save(
                replace(record, latest_turn_id=turn_id, updated_at=time())
            )
            self._append(
                RuntimeEvent(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    kind=EventKind.CANCELLATION_REQUESTED,
                    payload={"reason": resolved_reason, "phase": "queued"},
                )
            )
            current_usage = get_usage_snapshot()
            zero_usage = usage_delta(current_usage, current_usage)
            self._append(
                RuntimeEvent(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    kind=EventKind.TOKEN_USAGE_UPDATED,
                    payload=zero_usage.as_dict(),
                )
            )
            self._append(
                RuntimeEvent(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    kind=EventKind.TURN_INTERRUPTED,
                    payload={"error": resolved_reason, "assistant_text": "", "phase": "queued"},
                )
            )
            return turn_id
        finally:
            lock.release()

    def history(self, thread_id: str) -> List[BaseMessage]:
        self.thread_store.get(thread_id)
        with self._state_lock:
            history = self._histories.get(thread_id)
        if history is None:
            history = self._rebuild_history(thread_id)
            with self._state_lock:
                self._histories[thread_id] = history
        return list(history)

    def list_threads(self) -> List[ThreadRecord]:
        return self.thread_store.list()

    def compact_thread(self, thread_id: str) -> Tuple[int, int]:
        """Manually compact the active context and persist its new projection."""

        lock = self._get_thread_lock(thread_id)
        if not lock.acquire(blocking=False):
            raise RuntimeThreadBusyError(f"Thread {thread_id} already has a running turn.")
        try:
            with self._state_lock:
                history = self._histories.get(thread_id)
                if history is None:
                    history = self._rebuild_history(thread_id)
                    self._histories[thread_id] = history
            before_tokens = estimate_tokens(history)
            history[:] = auto_compact(
                history,
                session_memory=self.thread_context(thread_id)[0].session_memory,
                trigger="manual_command",
            )
            annotate_messages(history)
            after_tokens = estimate_tokens(history)
            self._append(
                RuntimeEvent(
                    thread_id=thread_id,
                    kind=EventKind.CONTEXT_COMPACTED,
                    payload={
                        "trigger": "manual_command",
                        "before_tokens": before_tokens,
                        "after_tokens": after_tokens,
                        "messages": encode_messages(history),
                    },
                )
            )
            return before_tokens, after_tokens
        finally:
            lock.release()

    def events(self, thread_id: str, after_sequence: int = 0) -> List[RuntimeEvent]:
        self.thread_store.get(thread_id)
        return self.event_store.read(thread_id, after_sequence)

    def _append(self, event: RuntimeEvent) -> RuntimeEvent:
        return self.event_store.append(event)

    def _get_thread_lock(self, thread_id: str) -> Lock:
        with self._state_lock:
            return self._thread_locks.setdefault(thread_id, Lock())

    def _rebuild_history(self, thread_id: str) -> List[BaseMessage]:
        messages: List[BaseMessage] = []
        for event in self.event_store.read(thread_id):
            if event.kind == EventKind.THREAD_FORKED:
                messages = decode_messages(event.payload.get("messages") or [])
                continue
            if event.kind == EventKind.CONTEXT_COMPACTED:
                messages = decode_messages(event.payload.get("messages") or [])
                continue
            if event.kind != EventKind.ITEM_COMPLETED:
                continue
            item_value = event.payload.get("item")
            if not isinstance(item_value, dict):
                continue
            item = AgentItem.from_dict(item_value)
            message_value = item.payload.get("message")
            if item.kind in {
                ItemKind.USER_MESSAGE,
                ItemKind.ASSISTANT_MESSAGE,
                ItemKind.TOOL_MESSAGE,
            } and isinstance(message_value, dict):
                messages.append(decode_message(message_value))
        return messages
