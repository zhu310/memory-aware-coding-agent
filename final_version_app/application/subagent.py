"""子代理执行流程。"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from final_version_app.application.workspace_ops import (
    run_edit,
    run_glob,
    run_grep,
    run_read,
    run_read_segment,
    run_write,
)
from final_version_app.infra.llm import format_tool_guide, invoke_langchain, render_ai_text
from final_version_app.infra.shell import run_bash


def run_subagent(prompt: str, agent_type: str, sub_tools: list) -> str:
    """用受限工具集启动一个短生命周期子代理。"""
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "glob_files": lambda **kw: run_glob(kw["pattern"], kw.get("path", "."), kw.get("limit", 100)),
        "grep_content": lambda **kw: run_grep(
            kw["pattern"],
            kw.get("path", "."),
            kw.get("glob", "*"),
            kw.get("output_mode", "content"),
            kw.get("head_limit", 20),
            kw.get("offset", 0),
            kw.get("before", 0),
            kw.get("after", 0),
            kw.get("ignore_case", False),
            kw.get("multiline", False),
        ),
        "read_file": lambda **kw: run_read(kw["path"]),
        "read_file_segment": lambda **kw: run_read_segment(
            kw["path"],
            kw.get("start_line"),
            kw.get("end_line"),
            kw.get("center_line"),
            kw.get("before", 20),
            kw.get("after", 20),
        ),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    # 子代理提示词强调“先搜索再读取，再谨慎编辑”。
    system = (
        "You are a coding subagent. Complete the delegated task and return a concise summary.\n"
        f"Agent type: {agent_type}\n"
        "Tool usage details:\n"
        f"{format_tool_guide(sub_tools)}\n"
        "Rules:\n"
        "- Prefer glob_files and grep_content to narrow the search space before reading files.\n"
        "- After grep_content returns line hits, prefer read_file_segment over rereading the entire file.\n"
        "- Prefer reading files before editing them.\n"
        "- Keep edits minimal and targeted.\n"
        "- If a command or edit fails, inspect output and retry with a narrower action."
    )
    messages: list[BaseMessage] = [HumanMessage(content=prompt)]
    response: AIMessage | None = None
    # 给子代理一个有限步数，防止无限调用工具。
    for _ in range(30):
        response = invoke_langchain(messages=messages, tools=sub_tools, system=system, max_tokens=8000)
        messages.append(response)
        if not (response.tool_calls or []):
            break
        for call in response.tool_calls or []:
            tool_name = str(call.get("name", ""))
            tool_input = call.get("args", {})
            if not isinstance(tool_input, dict):
                tool_input = {}
            tool_call_id = str(call.get("id", ""))
            handler = sub_handlers.get(tool_name, lambda **kw: "Unknown tool")
            output = str(handler(**tool_input))[:50000]
            messages.append(ToolMessage(content=output, tool_call_id=tool_call_id))
    if response:
        text = render_ai_text(response).strip()
        return text or "(no summary)"
    return "(subagent failed)"
