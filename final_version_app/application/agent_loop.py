"""Main agent loop."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from final_version_app.application.compression import apply_tool_result_budget, auto_compact, context_collapse, microcompact
from final_version_app.application.session_memory import annotate_messages, ensure_message_uuid
from final_version_app.application.tooling import ToolRuntime, make_tool_cache_key, store_tool_cache
from final_version_app.config import CONTEXT_COLLAPSE_TRIGGER_RATIO, TOKEN_THRESHOLD
from final_version_app.infra.llm import estimate_tokens, invoke_langchain


def _append_message(messages: list, message):
    ensure_message_uuid(message)
    messages.append(message)


def agent_loop(messages: list, services, tool_runtime: ToolRuntime, system_prompt: str):
    # `messages` 是模型当前直接看到的实时工作上下文。
    # 主循环的职责就是在不打断任务的前提下，持续维护这份上下文：
    # 1. 后台调度 session memory 提取
    # 2. 先做轻量压缩
    # 3. 在逼近 token 上限前做更强压缩
    # 4. 再继续正常的模型 / 工具调用循环
    annotate_messages(messages)
    rounds_without_todo = 0
    tool_cache: dict[str, str] = {}
    tool_cache_order: list[str] = []

    while True:
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
            print("[session-memory compact triggered]")
            messages[:] = auto_compact(messages, session_memory=services.session_memory, trigger="auto")
            annotate_messages(messages)
            token_estimate = estimate_tokens(messages)

        if token_estimate > int(TOKEN_THRESHOLD * CONTEXT_COLLAPSE_TRIGGER_RATIO):
            # 第二层缓压：
            # 如果 session memory compact 之后上下文仍偏大，
            # 就把更早历史折叠成一个结构化文本块。
            print("[context-collapse triggered]")
            messages[:] = context_collapse(messages)
            annotate_messages(messages)
            token_estimate = estimate_tokens(messages)

        if token_estimate > TOKEN_THRESHOLD:
            # 硬 fallback：
            # 这里仍然会先尝试 session memory 压缩，
            # 但如果不行，最终一定会走 legacy summarization。
            print("[legacy compact triggered]")
            messages[:] = auto_compact(messages, session_memory=services.session_memory, trigger="overflow")
            annotate_messages(messages)

        # 后台命令结果、队友消息，也会重新写回正常消息流，
        # 这样它们后续也能被提取进 session memory。
        notifs = services.bg.drain()
        if notifs:
            text = "\n".join(f"[bg:{item['task_id']}] {item['status']}: {item['result']}" for item in notifs)
            _append_message(messages, HumanMessage(content=f"<background-results>\n{text}\n</background-results>"))
            _append_message(messages, AIMessage(content="Noted background results."))

        inbox = services.bus.read_inbox("lead")
        if inbox:
            _append_message(messages, HumanMessage(content=f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>"))
            _append_message(messages, AIMessage(content="Noted inbox messages."))

        response = invoke_langchain(messages=messages, tools=tool_runtime.tools, system=system_prompt, max_tokens=8000)
        ensure_message_uuid(response)
        messages.append(response)
        if not (response.tool_calls or []):
            # 如果 assistant 这一轮没有 tool_calls，
            # 就把它视为一个自然停顿点，此时适合推进记忆边界，
            # 因为尾部不存在未闭合的 tool/result 配对。
            services.session_memory.maybe_schedule_extraction(messages)
            return

        used_todo = False
        manual_compress = False
        for call in response.tool_calls or []:
            tool_name = str(call.get("name", ""))
            tool_input = call.get("args", {})
            if not isinstance(tool_input, dict):
                tool_input = {}
            tool_call_id = str(call.get("id", ""))
            if tool_name == "compress":
                manual_compress = True
            cache_key = make_tool_cache_key(tool_name, tool_input)
            if tool_name in tool_runtime.cache_invalidating_tool_names:
                # 任何写入 / 编辑操作都可能让旧的 read/grep/glob 结果失效。
                tool_cache.clear()
                tool_cache_order.clear()
            if tool_name in tool_runtime.cacheable_tool_names and cache_key in tool_cache:
                output = tool_cache[cache_key]
                print(f"> {tool_name}: [cache hit] {str(output)[:180]}")
            else:
                handler = tool_runtime.handlers.get(tool_name)
                try:
                    output = handler(**tool_input) if handler else f"Unknown tool: {tool_name}"
                except Exception as exc:
                    output = f"Error: {exc}"
                if tool_name in tool_runtime.cacheable_tool_names and not str(output).startswith("Error:"):
                    store_tool_cache(tool_cache, tool_cache_order, cache_key, str(output))
                print(f"> {tool_name}: {str(output)[:200]}")
            tool_message = ToolMessage(content=str(output), tool_call_id=tool_call_id)
            ensure_message_uuid(tool_message)
            messages.append(tool_message)
            if tool_name == "TodoWrite":
                used_todo = True

        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if services.todo.has_open_items() and rounds_without_todo >= 3:
            _append_message(messages, HumanMessage(content="<reminder>Update your todos.</reminder>"))

        services.session_memory.maybe_schedule_extraction(messages)
        if manual_compress:
            print("[manual compact]")
            messages[:] = auto_compact(messages, session_memory=services.session_memory, trigger="manual")
            annotate_messages(messages)
