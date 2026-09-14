"""Interactive REPL entrypoint."""

from __future__ import annotations

import json
import os
import sys
import warnings
from dataclasses import replace

from final_version_app.application.agent_loop import agent_loop
from final_version_app.application.container import build_services
from final_version_app.application.prompting import build_base_system, build_system
from final_version_app.application.team_runtime import TeammateManager
from final_version_app.application.tooling import build_tool_runtime
from final_version_app.config import RUNTIME_STATE_DIR, SKILLS_DIR
from final_version_app.application.hybrid_memory import HybridMemoryManager
from final_version_app.domain.skills import SkillLoader
from final_version_app.domain.todos import TodoManager
from final_version_app.engine.runtime import AgentRuntime
from final_version_app.infra.usage import format_usage, get_usage_snapshot, usage_delta
from final_version_app.protocol import TurnStatus
from final_version_app.storage import JsonThreadStore, JsonlEventStore


def configure_console() -> None:
    """Keep Chinese input/output readable in Windows terminals when possible."""

    os.environ.setdefault("LANGCHAIN_OPENAI_TCP_KEEPALIVE", "0")
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_runtime():
    """Build the legacy runtime tuple retained for benchmarks and teaching code."""

    services = build_services()
    team_mgr = TeammateManager(services)
    tool_runtime = build_tool_runtime(services, team_mgr)
    base_system = build_base_system(services.skills.descriptions())
    system_prompt = build_system(base_system, tool_runtime.tools)
    return services, team_mgr, tool_runtime, system_prompt


def build_agent_runtime(state_dir=RUNTIME_STATE_DIR) -> AgentRuntime:
    """Build the protocol-driven runtime around the existing agent loop."""

    services, team_mgr, tool_runtime, system_prompt = build_runtime()
    def thread_context_factory(thread_id):
        scoped = replace(services, todo=TodoManager(),
                         skills=SkillLoader(SKILLS_DIR),
                         session_memory=HybridMemoryManager(SKILLS_DIR, thread_id))
        scoped_tools = build_tool_runtime(scoped, team_mgr)
        scoped_prompt = build_system(build_base_system(scoped.skills.descriptions()), scoped_tools.tools)
        return scoped, scoped_tools, scoped_prompt

    return AgentRuntime(
        services=services,
        tool_runtime=tool_runtime,
        system_prompt=system_prompt,
        event_store=JsonlEventStore(state_dir),
        thread_store=JsonThreadStore(state_dir),
        turn_executor=agent_loop,
        team_manager=team_mgr,
        thread_context_factory=thread_context_factory,
    )


def main():
    configure_console()
    warnings.filterwarnings(
        "ignore",
        message=r"langchain-openai injected a custom httpx transport.*",
    )
    verbose = os.getenv("AGENT_REPL_VERBOSE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    last_answer = ""
    runtime = build_agent_runtime()
    thread = runtime.start_thread(title="Interactive session")
    print(f"[workspace] {os.getcwd()}")
    print("请输入任务内容，可以直接粘贴多行文本；输入完成后，单独输入一行 /send 发送。")
    print("可选粘贴块：输入 /paste 后粘贴多行文字，再用 /end 结束该粘贴块。")
    print("常用命令：/help 查看帮助，/workspace 查看工作区，exit 或 q 结束会话。")
    if verbose:
        print(f"[runtime] thread_id={thread.thread_id}")

    while True:
        query = _read_next_query()
        stripped = query.strip()
        if stripped.lower() in ("q", "exit"):
            break
        if not stripped:
            print("(empty request ignored)")
            print()
            continue
        if stripped == "/help":
            print(
                "\n".join(
                    [
                        "命令说明：",
                        "  /send       发送当前已经输入的整段请求",
                        "  /paste      可选：进入粘贴块；粘贴块用 /end 结束",
                        "  /last       重新打印上一轮完整回答",
                        "  /usage      查看 token 使用情况",
                        "  /workspace  查看当前工作区，生成文件会写入这里",
                        "  /thread     查看当前 thread 元数据",
                        "  /threads    列出 runtime threads",
                        "  /compact    手动压缩当前 thread 上下文",
                        "  /fork       fork 当前 thread",
                        "  /resume ID  恢复指定 thread",
                        "  exit 或 q    结束会话",
                        "",
                        "多行示例：",
                        "  请根据下面内容生成文档：",
                        "  粘贴多行内容...",
                        "  请保存为 docs/example.md",
                        "  /send",
                        "",
                        "使用 /paste 的示例：",
                        "  请根据下面内容生成文档：",
                        "  /paste",
                        "  粘贴多行内容...",
                        "  /end",
                        "  /send",
                    ]
                )
            )
            print()
            continue
        if stripped == "/workspace":
            print(os.getcwd())
            print()
            continue
        if stripped == "/compact":
            if runtime.history(thread.thread_id):
                print("[manual compact via /compact]")
                before, after = runtime.compact_thread(thread.thread_id)
                print(f"[runtime] context tokens {before:,} -> {after:,}")
            continue
        if stripped == "/tasks":
            print(runtime.services.task_mgr.list_all())
            continue
        if stripped == "/team":
            print(runtime.team_manager.list_all())
            continue
        if stripped == "/inbox":
            print(json.dumps(runtime.services.bus.read_inbox("lead"), indent=2, ensure_ascii=False))
            continue
        if stripped == "/usage":
            print(format_usage("process total", get_usage_snapshot()))
            continue
        if stripped == "/last":
            print(last_answer or "(no previous answer)")
            print()
            continue
        if stripped == "/thread":
            print(json.dumps(thread.to_dict(), indent=2, ensure_ascii=False))
            continue
        if stripped == "/threads":
            print(
                json.dumps(
                    [item.to_dict() for item in runtime.list_threads()],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            continue
        if stripped.startswith("/resume "):
            thread = runtime.resume_thread(stripped.split(maxsplit=1)[1])
            print(f"[runtime] resumed thread_id={thread.thread_id}")
            continue
        if stripped == "/fork":
            thread = runtime.fork_thread(thread.thread_id)
            print(f"[runtime] forked thread_id={thread.thread_id}")
            continue

        usage_before = get_usage_snapshot()
        result = runtime.run_turn(thread.thread_id, query)
        usage_after = get_usage_snapshot()
        if verbose:
            print(f"[runtime] turn_id={result.turn_id} status={result.status.value}")
            print(format_usage("this task", usage_delta(usage_before, usage_after)))
            print(format_usage("process total", usage_after))
        if result.status == TurnStatus.FAILED:
            print(_format_runtime_error(result.error))
        elif result.status == TurnStatus.INTERRUPTED:
            print(f"[runtime interrupted] {result.error}")
        elif result.assistant_text:
            last_answer = result.assistant_text
            print(result.assistant_text)
            if _last_finish_reason(runtime, thread.thread_id) == "length":
                print(
                    "\n[notice] The model stopped because it reached its output "
                    "token limit. Use /last to reprint the captured answer, or "
                    "raise AGENT_FINAL_ANSWER_TOKENS for longer final answers."
                )
        print()


def _last_finish_reason(runtime: AgentRuntime, thread_id: str) -> str:
    for event in reversed(runtime.events(thread_id)):
        if event.kind.value != "item_completed":
            continue
        item = event.payload.get("item") or {}
        if item.get("kind") != "assistant_message":
            continue
        message = (item.get("payload") or {}).get("message") or {}
        metadata = message.get("response_metadata") or {}
        return str(metadata.get("finish_reason") or "")
    return ""


def _format_runtime_error(error: str | None) -> str:
    text = str(error or "").strip()
    lower = text.lower()
    if "insufficient balance" in lower or "error code: 402" in lower:
        return (
            "[模型调用失败] 当前配置的 API key 余额不足或额度不可用。\n"
            "请检查 coding-agent 项目 .env 中的 MODEL_ID 和对应 API key，"
            "或切换到仍有额度的模型配置后重启 REPL。\n"
            f"原始错误：{text}"
        )
    if "api key" in lower and "missing" in lower:
        return (
            "[模型调用失败] 当前模型缺少对应的 API key。\n"
            "请检查 coding-agent 项目 .env，确认 MODEL_ID 与 API key 名称匹配。\n"
            f"原始错误：{text}"
        )
    return f"[runtime error] {text}"


_IMMEDIATE_COMMANDS = {
    "/help",
    "/workspace",
    "/compact",
    "/tasks",
    "/team",
    "/inbox",
    "/usage",
    "/last",
    "/thread",
    "/threads",
    "/fork",
}


def _is_immediate_command(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped in _IMMEDIATE_COMMANDS
        or stripped.startswith("/resume ")
        or stripped.lower() in {"exit", "q"}
    )


def _read_next_query() -> str:
    """Read one user request. Plain content is submitted only after /send."""

    parts: list[str] = []
    while True:
        try:
            line = input("输入内容，完成后输入 /send 发送（/help 查看帮助，exit 退出）：")
        except (EOFError, KeyboardInterrupt):
            return "exit"

        stripped = line.strip()
        if not parts and _is_immediate_command(stripped):
            return stripped
        if stripped == "/send":
            return "\n\n".join(parts).strip()
        if stripped == "/paste":
            pasted = _read_paste_block()
            if pasted.strip():
                parts.append(pasted.strip())
            continue
        if "/paste" in line:
            prefix, suffix = line.split("/paste", 1)
            if prefix.strip():
                parts.append(prefix.strip())
            pasted = _read_paste_block()
            if pasted.strip():
                parts.append(pasted.strip())
            if suffix.strip():
                parts.append(suffix.strip())
            continue
        parts.append(line)


def _read_paste_block() -> str:
    print("请粘贴多行内容。粘贴完成后，单独输入一行 /end 结束本段粘贴。")
    lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            line = "/end"
        if line.strip() == "/end":
            break
        lines.append(line)
    return "\n".join(lines)
