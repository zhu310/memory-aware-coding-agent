"""工具装配与调度注册表。

这一版刻意保持“足够结构化，但不过度抽象”：
- 不再引入 ToolRegistry / HandlerRegistry
- 直接使用 `dict + list` 表示工具和 handler
- 通过几个显式装配函数，把基础工具、任务工具、协作工具、协议工具分组注册
"""

import json
import uuid
from dataclasses import dataclass
from typing import Callable

from langchain_core.tools import tool

from final_version_app.application.agent_runs import AgentRunManager
from final_version_app.application.container import AppServices
from final_version_app.application.subagent import run_subagent
from final_version_app.application.workspace_ops import (
    run_edit,
    run_glob,
    run_grep,
    run_read,
    run_read_segment,
    run_write,
)
from final_version_app.config import TOOL_CACHE_LIMIT
from final_version_app.infra.shell import run_bash

ToolHandler = Callable[..., str]


def _register_tool_pair(
    tools_by_name: dict[str, object],
    handlers: dict[str, ToolHandler],
    name: str,
    tool_obj: object,
    handler: ToolHandler,
):
    """一次性注册同名工具对象和执行函数。"""
    tools_by_name[name] = tool_obj
    handlers[name] = handler


def build_base_toolset() -> tuple[dict[str, object], dict[str, ToolHandler]]:
    """构建所有运行时都会复用的基础工具。"""

    tools_by_name: dict[str, object] = {}
    handlers: dict[str, ToolHandler] = {}

    @tool("bash")
    def bash_tool(command: str) -> str:
        """Run a shell command."""
        return run_bash(command)

    @tool("read_file")
    def read_file_tool(path: str, limit: int = None) -> str:
        """Read file contents."""
        return run_read(path, limit)

    @tool("read_file_segment")
    def read_file_segment_tool(
        path: str,
        start_line: int = None,
        end_line: int = None,
        center_line: int = None,
        before: int = 20,
        after: int = 20,
    ) -> str:
        """Read a focused file segment by explicit line range or around a center line."""
        return run_read_segment(path, start_line, end_line, center_line, before, after)

    @tool("write_file")
    def write_file_tool(path: str, content: str) -> str:
        """Write content to file."""
        return run_write(path, content)

    @tool("edit_file")
    def edit_file_tool(path: str, old_text: str, new_text: str) -> str:
        """Replace exact text in file."""
        return run_edit(path, old_text, new_text)

    @tool("glob_files")
    def glob_files_tool(pattern: str, path: str = ".", limit: int = 100) -> str:
        """Find files or directories by glob pattern under a workspace-relative directory."""
        return run_glob(pattern, path, limit)

    @tool("grep_content")
    def grep_content_tool(
        pattern: str,
        path: str = ".",
        glob: str = "*",
        output_mode: str = "content",
        head_limit: int = 20,
        offset: int = 0,
        before: int = 0,
        after: int = 0,
        ignore_case: bool = False,
        multiline: bool = False,
    ) -> str:
        """Search file contents with regex and return matches, matching files, or counts."""
        return run_grep(pattern, path, glob, output_mode, head_limit, offset, before, after, ignore_case, multiline)

    _register_tool_pair(tools_by_name, handlers, "bash", bash_tool, lambda **kw: run_bash(kw["command"]))
    _register_tool_pair(tools_by_name, handlers, "read_file", read_file_tool, lambda **kw: run_read(kw["path"], kw.get("limit")))
    _register_tool_pair(
        tools_by_name,
        handlers,
        "read_file_segment",
        read_file_segment_tool,
        lambda **kw: run_read_segment(
            kw["path"],
            kw.get("start_line"),
            kw.get("end_line"),
            kw.get("center_line"),
            kw.get("before", 20),
            kw.get("after", 20),
        ),
    )
    _register_tool_pair(tools_by_name, handlers, "write_file", write_file_tool, lambda **kw: run_write(kw["path"], kw["content"]))
    _register_tool_pair(tools_by_name, handlers, "edit_file", edit_file_tool, lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]))
    _register_tool_pair(tools_by_name, handlers, "glob_files", glob_files_tool, lambda **kw: run_glob(kw["pattern"], kw.get("path", "."), kw.get("limit", 100)))
    _register_tool_pair(
        tools_by_name,
        handlers,
        "grep_content",
        grep_content_tool,
        lambda **kw: run_grep(
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
    )
    return tools_by_name, handlers


@dataclass
class ToolRuntime:
    """工具运行时元数据。"""

    tools: list
    handlers: dict[str, ToolHandler]
    base_tools: dict[str, object]
    cacheable_tool_names: set[str]
    cache_invalidating_tool_names: set[str]
    shutdown_requests: dict
    plan_requests: dict


def _register_task_tools(
    tools_by_name: dict[str, object],
    handlers: dict[str, ToolHandler],
    services: AppServices,
    run_task_subagent: Callable[..., str],
    agent_runs: AgentRunManager,
):
    """注册任务管理相关工具。"""

    @tool("TodoWrite")
    def todo_write_tool(items: list[dict]) -> str:
        """Update task tracking list."""
        return services.todo.update(items)

    @tool("task")
    def task_tool(prompt: str, agent_type: str = "Explore") -> str:
        """Spawn a subagent for isolated exploration or work."""
        return run_task_subagent(prompt, agent_type)

    @tool("task_async")
    def task_async_tool(
        prompt: str,
        agent_type: str = "Explore",
        expected_output_schema: str = "",
        task_id: int = None,
    ) -> str:
        """Start a background subagent run and return a run_id immediately."""
        return agent_runs.start(prompt, agent_type, expected_output_schema, task_id)

    @tool("task_check")
    def task_check_tool(run_id: str = None) -> str:
        """Check one async subagent run, or list all runs when run_id is omitted."""
        return agent_runs.check(run_id)

    @tool("task_join")
    def task_join_tool(run_id: str, timeout: int = 10) -> str:
        """Wait briefly for an async subagent run; returns status on timeout."""
        return agent_runs.join(run_id, timeout)

    @tool("load_skill")
    def load_skill_tool(name: str) -> str:
        """Load specialized knowledge by name."""
        return services.skills.load(name)

    @tool("task_create")
    def task_create_tool(subject: str, description: str = "") -> str:
        """Create a persistent file task."""
        return services.task_mgr.create(subject, description)

    @tool("task_get")
    def task_get_tool(task_id: int) -> str:
        """Get task details by ID."""
        return services.task_mgr.get(task_id)

    @tool("task_update")
    def task_update_tool(task_id: int, status: str = None, add_blocked_by: list = None, add_blocks: list = None) -> str:
        """Update task status or dependencies."""
        return services.task_mgr.update(task_id, status, add_blocked_by, add_blocks)

    @tool("task_list")
    def task_list_tool() -> str:
        """List all tasks."""
        return services.task_mgr.list_all()

    @tool("claim_task")
    def claim_task_tool(task_id: int) -> str:
        """Claim a task from the board."""
        return services.task_mgr.claim(task_id, "lead")

    _register_tool_pair(tools_by_name, handlers, "TodoWrite", todo_write_tool, lambda **kw: services.todo.update(kw["items"]))
    _register_tool_pair(tools_by_name, handlers, "task", task_tool, lambda **kw: run_task_subagent(kw["prompt"], kw.get("agent_type", "Explore")))
    _register_tool_pair(
        tools_by_name,
        handlers,
        "task_async",
        task_async_tool,
        lambda **kw: agent_runs.start(
            kw["prompt"],
            kw.get("agent_type", "Explore"),
            kw.get("expected_output_schema", ""),
            kw.get("task_id"),
        ),
    )
    _register_tool_pair(tools_by_name, handlers, "task_check", task_check_tool, lambda **kw: agent_runs.check(kw.get("run_id")))
    _register_tool_pair(tools_by_name, handlers, "task_join", task_join_tool, lambda **kw: agent_runs.join(kw["run_id"], kw.get("timeout", 10)))
    _register_tool_pair(tools_by_name, handlers, "load_skill", load_skill_tool, lambda **kw: services.skills.load(kw["name"]))
    _register_tool_pair(tools_by_name, handlers, "task_create", task_create_tool, lambda **kw: services.task_mgr.create(kw["subject"], kw.get("description", "")))
    _register_tool_pair(tools_by_name, handlers, "task_get", task_get_tool, lambda **kw: services.task_mgr.get(kw["task_id"]))
    _register_tool_pair(
        tools_by_name,
        handlers,
        "task_update",
        task_update_tool,
        lambda **kw: services.task_mgr.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("add_blocks")),
    )
    _register_tool_pair(tools_by_name, handlers, "task_list", task_list_tool, lambda **kw: services.task_mgr.list_all())
    _register_tool_pair(tools_by_name, handlers, "claim_task", claim_task_tool, lambda **kw: services.task_mgr.claim(kw["task_id"], "lead"))


def _register_collaboration_tools(
    tools_by_name: dict[str, object],
    handlers: dict[str, ToolHandler],
    services: AppServices,
    team_mgr,
):
    """注册协作与后台执行相关工具。"""

    @tool("background_run")
    def background_run_tool(command: str, timeout: int = 120) -> str:
        """Run command in background thread."""
        return services.bg.run(command, timeout)

    @tool("check_background")
    def check_background_tool(task_id: str = None) -> str:
        """Check background task status."""
        return services.bg.check(task_id)

    @tool("spawn_teammate")
    def spawn_teammate_tool(name: str, role: str, prompt: str) -> str:
        """Spawn a persistent autonomous teammate."""
        return team_mgr.spawn(name, role, prompt)

    @tool("list_teammates")
    def list_teammates_tool() -> str:
        """List all teammates."""
        return team_mgr.list_all()

    @tool("send_message")
    def send_message_tool(to: str, content: str, msg_type: str = "message") -> str:
        """Send a message to a teammate."""
        return services.bus.send("lead", to, content, msg_type)

    @tool("read_inbox")
    def read_inbox_tool() -> str:
        """Read and drain the lead's inbox."""
        return json.dumps(services.bus.read_inbox("lead"), indent=2, ensure_ascii=False)

    @tool("broadcast")
    def broadcast_tool(content: str) -> str:
        """Send message to all teammates."""
        return services.bus.broadcast("lead", content, team_mgr.member_names())

    @tool("idle")
    def idle_tool() -> str:
        """Enter idle state."""
        return "Lead does not idle."

    _register_tool_pair(tools_by_name, handlers, "background_run", background_run_tool, lambda **kw: services.bg.run(kw["command"], kw.get("timeout", 120)))
    _register_tool_pair(tools_by_name, handlers, "check_background", check_background_tool, lambda **kw: services.bg.check(kw.get("task_id")))
    _register_tool_pair(tools_by_name, handlers, "spawn_teammate", spawn_teammate_tool, lambda **kw: team_mgr.spawn(kw["name"], kw["role"], kw["prompt"]))
    _register_tool_pair(tools_by_name, handlers, "list_teammates", list_teammates_tool, lambda **kw: team_mgr.list_all())
    _register_tool_pair(
        tools_by_name,
        handlers,
        "send_message",
        send_message_tool,
        lambda **kw: services.bus.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    )
    _register_tool_pair(tools_by_name, handlers, "read_inbox", read_inbox_tool, lambda **kw: json.dumps(services.bus.read_inbox("lead"), indent=2))
    _register_tool_pair(tools_by_name, handlers, "broadcast", broadcast_tool, lambda **kw: services.bus.broadcast("lead", kw["content"], team_mgr.member_names()))
    _register_tool_pair(tools_by_name, handlers, "idle", idle_tool, lambda **kw: "Lead does not idle.")


def _register_protocol_tools(
    tools_by_name: dict[str, object],
    handlers: dict[str, ToolHandler],
    services: AppServices,
    shutdown_requests: dict[str, dict],
    plan_requests: dict[str, dict],
):
    """注册协议相关工具，例如压缩、审批、关闭请求。"""

    def handle_shutdown_request(teammate: str) -> str:
        req_id = str(uuid.uuid4())[:8]
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
        services.bus.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
        return f"Shutdown request {req_id} sent to '{teammate}'"

    def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
        req = plan_requests.get(request_id)
        if not req:
            return f"Error: Unknown plan request_id '{request_id}'"
        req["status"] = "approved" if approve else "rejected"
        services.bus.send(
            "lead",
            req["from"],
            feedback,
            "plan_approval_response",
            {"request_id": request_id, "approve": approve, "feedback": feedback},
        )
        return f"Plan {req['status']} for '{req['from']}'"

    @tool("compress")
    def compress_tool() -> str:
        """Manually compress conversation context."""
        return "Compressing..."

    @tool("shutdown_request")
    def shutdown_request_tool(teammate: str) -> str:
        """Request a teammate to shut down."""
        return handle_shutdown_request(teammate)

    @tool("plan_approval")
    def plan_approval_tool(request_id: str, approve: bool, feedback: str = "") -> str:
        """Approve or reject a teammate's plan."""
        return handle_plan_review(request_id, approve, feedback)

    _register_tool_pair(tools_by_name, handlers, "compress", compress_tool, lambda **kw: "Compressing...")
    _register_tool_pair(tools_by_name, handlers, "shutdown_request", shutdown_request_tool, lambda **kw: handle_shutdown_request(kw["teammate"]))
    _register_tool_pair(
        tools_by_name,
        handlers,
        "plan_approval",
        plan_approval_tool,
        lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    )


def build_tool_runtime(services: AppServices, team_mgr) -> ToolRuntime:
    """把基础工具、业务工具和协议工具装配成完整运行时。"""

    base_tools, base_handlers = build_base_toolset()
    tools_by_name = dict(base_tools)
    handlers = dict(base_handlers)
    shutdown_requests: dict[str, dict] = {}
    plan_requests: dict[str, dict] = {}

    def run_task_subagent(prompt: str, agent_type: str = "Explore") -> str:
        """为 `task` 工具构造适合当前任务类型的子代理工具集。"""
        sub_tools = [
            base_tools["bash"],
            base_tools["glob_files"],
            base_tools["grep_content"],
            base_tools["read_file"],
            base_tools["read_file_segment"],
        ]
        if agent_type != "Explore":
            sub_tools += [base_tools["write_file"], base_tools["edit_file"]]
        return run_subagent(prompt, agent_type, sub_tools)

    agent_runs = AgentRunManager(services, run_task_subagent)

    _register_task_tools(tools_by_name, handlers, services, run_task_subagent, agent_runs)
    _register_collaboration_tools(tools_by_name, handlers, services, team_mgr)
    _register_protocol_tools(tools_by_name, handlers, services, shutdown_requests, plan_requests)

    return ToolRuntime(
        tools=list(tools_by_name.values()),
        handlers=handlers,
        base_tools=base_tools,
        cacheable_tool_names={"glob_files", "grep_content", "read_file", "read_file_segment"},
        cache_invalidating_tool_names={"write_file", "edit_file"},
        shutdown_requests=shutdown_requests,
        plan_requests=plan_requests,
    )


def make_tool_cache_key(tool_name: str, tool_input: dict) -> str:
    """把工具名和参数序列化成缓存键。"""
    payload = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
    return f"{tool_name}:{payload}"


def store_tool_cache(cache: dict[str, str], cache_order: list[str], cache_key: str, output: str):
    """按固定上限维护一个最近使用的工具结果缓存。"""
    cache[cache_key] = output
    if cache_key in cache_order:
        cache_order.remove(cache_key)
    cache_order.append(cache_key)
    while len(cache_order) > TOOL_CACHE_LIMIT:
        oldest = cache_order.pop(0)
        cache.pop(oldest, None)
