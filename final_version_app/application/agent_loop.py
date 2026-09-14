"""Main agent loop."""

from __future__ import annotations

import json
import os
import sys
from time import monotonic

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from final_version_app.application.compression import apply_tool_result_budget, auto_compact, context_collapse, microcompact
from final_version_app.application.session_memory import annotate_messages, ensure_message_uuid
from final_version_app.application.tooling import ToolRuntime
from final_version_app.config import (
    AGENT_FINAL_ANSWER_TOKENS,
    AGENT_MAX_TOOL_ROUNDS,
    AGENT_MAX_TURN_SECONDS,
    CONTEXT_COLLAPSE_TRIGGER_RATIO,
    TOKEN_THRESHOLD,
    TOOL_CACHE_LIMIT,
)
from final_version_app.engine.cancellation import CancellationToken
from final_version_app.engine.loop_observer import LoopObserver, NullLoopObserver
from final_version_app.infra.llm import estimate_tokens, invoke_langchain
from final_version_app.tools import ToolOrchestrator


def _console_safe(value) -> str:
    """Make arbitrary tool output printable on legacy Windows code pages."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _debug_print(value) -> None:
    verbose = os.getenv("AGENT_REPL_VERBOSE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if verbose:
        print(_console_safe(value))


def _append_message(messages: list, message, observer: LoopObserver):
    """Append one model-visible message and report the same fact to the runtime."""

    ensure_message_uuid(message)
    messages.append(message)
    observer.message_appended(message)


def _finish_without_tools(
    messages: list,
    system_prompt: str,
    observer: LoopObserver,
    reason: str,
):
    """Produce a terminal answer after runtime guardrails stop tool use."""

    _append_message(
        messages,
        HumanMessage(
            content=(
                "<runtime_guardrail>\n"
                f"{reason}\n"
                "Do not call any tools. Use only the information already gathered "
                "in the conversation and provide the best final answer now.\n"
                "</runtime_guardrail>"
            )
        ),
        observer,
    )
    from final_version_app.config import MODEL

    if hasattr(observer, "model_started"):
        observer.model_started(MODEL)
    try:
        response = invoke_langchain(
            messages=messages,
            tools=[],
            system=system_prompt,
            max_tokens=AGENT_FINAL_ANSWER_TOKENS,
        )
    finally:
        if hasattr(observer, "model_completed"):
            observer.model_completed()
    ensure_message_uuid(response)
    messages.append(response)
    observer.message_appended(response)


def agent_loop(
    messages: list,
    services,
    tool_runtime: ToolRuntime,
    system_prompt: str,
    *,
    observer: LoopObserver | None = None,
    cancellation: CancellationToken | None = None,
):
    """Run model/tool steps until the model returns a terminal response.

    ``observer`` and ``cancellation`` are optional so existing callers keep the
    original lightweight API. The new runtime supplies both to gain durable
    events and cooperative interruption without moving persistence into this
    reasoning loop.
    """

    observer = observer or NullLoopObserver()
    cancellation = cancellation or CancellationToken()
    # `messages` 是模型当前直接看到的实时工作上下文。
    # 主循环的职责就是在不打断任务的前提下，持续维护这份上下文：
    # 1. 后台调度 session memory 提取
    # 2. 先做轻量压缩
    # 3. 在逼近 token 上限前做更强压缩
    # 4. 再继续正常的模型 / 工具调用循环
    annotate_messages(messages)
    rounds_without_todo = 0
    tool_rounds = 0
    started_at = monotonic()
    tool_orchestrator = ToolOrchestrator(
        tool_runtime.handlers,
        cacheable_names=tool_runtime.cacheable_tool_names,
        cache_invalidating_names=tool_runtime.cache_invalidating_tool_names,
        cache_limit=TOOL_CACHE_LIMIT,
    )

    while True:
        cancellation.raise_if_cancelled()
        elapsed = monotonic() - started_at
        if tool_rounds >= AGENT_MAX_TOOL_ROUNDS:
            _finish_without_tools(
                messages,
                system_prompt,
                observer,
                f"Maximum tool round limit reached: {AGENT_MAX_TOOL_ROUNDS}.",
            )
            return
        if elapsed >= AGENT_MAX_TURN_SECONDS:
            _finish_without_tools(
                messages,
                system_prompt,
                observer,
                f"Maximum turn time reached: {AGENT_MAX_TURN_SECONDS:.0f} seconds.",
            )
            return

        # 每条消息都必须先补齐稳定 uuid。
        # session memory 的边界状态就是靠这些 uuid 记录的。
        annotate_messages(messages)

        # 后台提取调度：
        # 这里只是判断“现在值不值得更新长期记忆”，不会阻塞主循环。
        services.session_memory.maybe_schedule_extraction(messages)

        # 便宜的本地压缩每轮都跑。
        apply_tool_result_budget(messages)
        microcompact(messages)

        token_estimate = estimate_tokens(messages)
        if token_estimate > services.session_memory.auto_compact_threshold():
            # 首选路径：
            # 用结构化 session memory + 最近一段原始消息来替代大部分旧历史。
            _debug_print("[session-memory compact triggered]")
            before_tokens = token_estimate
            messages[:] = auto_compact(messages, session_memory=services.session_memory, trigger="auto")
            annotate_messages(messages)
            token_estimate = estimate_tokens(messages)
            observer.context_compacted("auto", messages, before_tokens, token_estimate)

        if not getattr(services.session_memory, "durable_archive", False) and token_estimate > int(TOKEN_THRESHOLD * CONTEXT_COLLAPSE_TRIGGER_RATIO):
            # 第二层缓压：
            # 如果 session memory compact 之后上下文仍偏大，
            # 就把更早历史折叠成一个结构化文本块。
            _debug_print("[context-collapse triggered]")
            before_tokens = token_estimate
            messages[:] = context_collapse(messages)
            annotate_messages(messages)
            token_estimate = estimate_tokens(messages)
            observer.context_compacted("context_collapse", messages, before_tokens, token_estimate)

        if token_estimate > TOKEN_THRESHOLD:
            # 硬 fallback：
            # 这里仍然会先尝试 session memory 压缩，
            # 但如果不行，最终一定会走 legacy summarization。
            _debug_print("[legacy compact triggered]")
            before_tokens = token_estimate
            messages[:] = auto_compact(messages, session_memory=services.session_memory, trigger="overflow")
            annotate_messages(messages)
            observer.context_compacted(
                "overflow",
                messages,
                before_tokens,
                estimate_tokens(messages),
            )

        # 后台命令结果、队友消息，也会重新写回正常消息流，
        # 这样它们后续也能被提取进 session memory。
        notifs = services.bg.drain()
        if notifs:
            text = "\n".join(f"[bg:{item['task_id']}] {item['status']}: {item['result']}" for item in notifs)
            _append_message(
                messages,
                HumanMessage(content=f"<background-results>\n{text}\n</background-results>"),
                observer,
            )
            _append_message(messages, AIMessage(content="Noted background results."), observer)

        inbox = services.bus.read_inbox("lead")
        if inbox:
            _append_message(
                messages,
                HumanMessage(content=f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>"),
                observer,
            )
            _append_message(messages, AIMessage(content="Noted inbox messages."), observer)

        cancellation.raise_if_cancelled()
        from final_version_app.config import MODEL
        if hasattr(observer, "model_started"): observer.model_started(MODEL)
        try:
            response = invoke_langchain(messages=messages, tools=tool_runtime.tools, system=system_prompt, max_tokens=8000)
        finally:
            if hasattr(observer, "model_completed"): observer.model_completed()
        ensure_message_uuid(response)
        messages.append(response)
        observer.message_appended(response)
        if not (response.tool_calls or []):
            # 如果 assistant 这一轮没有 tool_calls，
            # 就把它视为一个自然停顿点，此时适合推进记忆边界，
            # 因为尾部不存在未闭合的 tool/result 配对。
            services.session_memory.maybe_schedule_extraction(messages)
            return

        used_todo = False
        manual_compress = False
        tool_rounds += 1
        for call in response.tool_calls or []:
            cancellation.raise_if_cancelled()
            result = tool_orchestrator.execute(
                call,
                observer=observer,
                cancellation=cancellation,
            )
            if result.call.name == "compress":
                manual_compress = True
            cache_label = " [cache hit]" if result.cached else ""
            _debug_print(f"> {result.call.name}:{cache_label} {result.output[:200]}")
            tool_message = ToolMessage(content=result.output, tool_call_id=result.call.call_id,
                additional_kwargs={"memory_page": True} if result.call.name in {"memory_search", "memory_read"} else {})
            ensure_message_uuid(tool_message)
            messages.append(tool_message)
            observer.message_appended(tool_message)
            if result.call.name == "TodoWrite":
                used_todo = True

        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if services.todo.has_open_items() and rounds_without_todo >= 3:
            _append_message(
                messages,
                HumanMessage(content="<reminder>Update your todos.</reminder>"),
                observer,
            )

        services.session_memory.maybe_schedule_extraction(messages)
        if manual_compress:
            _debug_print("[manual compact]")
            before_tokens = estimate_tokens(messages)
            messages[:] = auto_compact(messages, session_memory=services.session_memory, trigger="manual")
            annotate_messages(messages)
            observer.context_compacted(
                "manual",
                messages,
                before_tokens,
                estimate_tokens(messages),
            )
