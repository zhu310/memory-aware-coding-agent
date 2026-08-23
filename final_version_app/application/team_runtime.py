"""持久 teammate 运行时。"""

import json
import threading
import time

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from final_version_app.application.container import AppServices
from final_version_app.application.workspace_ops import (
    run_edit,
    run_glob,
    run_grep,
    run_read,
    run_read_segment,
    run_write,
)
from final_version_app.config import IDLE_TIMEOUT, POLL_INTERVAL, TASKS_DIR, TEAM_DIR, WORKDIR
from final_version_app.infra.llm import format_tool_guide, invoke_langchain
from final_version_app.infra.shell import run_bash
from final_version_app.infra.workspace import read_text_auto


class TeammateManager:
    """管理长期存在的队友代理。

    与 subagent 的区别是：
    - teammate 有持久身份
    - teammate 有 inbox / idle / auto-claim 机制
    - teammate 适合长时间并行协作
    """
    def __init__(self, services: AppServices):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = services.bus
        self.task_mgr = services.task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads = {}

    def _load(self) -> dict:
        """读取团队配置。"""
        if self.config_path.exists():
            return json.loads(read_text_auto(self.config_path))
        return {"team_name": "default", "members": []}

    def _save(self):
        """保存团队配置。"""
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")

    def _find(self, name: str) -> dict | None:
        """按名字查找队友。"""
        for member in self.config["members"]:
            if member["name"] == name:
                return member
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """启动或唤醒一个队友代理。"""
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save()
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        """更新队友状态并落盘。"""
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _teammate_exec(self, name: str, tool_name: str, args: dict) -> str:
        """队友运行时的工具执行分发。"""
        if tool_name == "bash":
            return run_bash(args["command"])
        if tool_name == "glob_files":
            return run_glob(args["pattern"], args.get("path", "."), args.get("limit", 100))
        if tool_name == "grep_content":
            return run_grep(
                args["pattern"],
                args.get("path", "."),
                args.get("glob", "*"),
                args.get("output_mode", "content"),
                args.get("head_limit", 20),
                args.get("offset", 0),
                args.get("before", 0),
                args.get("after", 0),
                args.get("ignore_case", False),
                args.get("multiline", False),
            )
        if tool_name == "read_file":
            return run_read(args["path"], args.get("limit"))
        if tool_name == "read_file_segment":
            return run_read_segment(
                args["path"],
                args.get("start_line"),
                args.get("end_line"),
                args.get("center_line"),
                args.get("before", 20),
                args.get("after", 20),
            )
        if tool_name == "write_file":
            return run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return self.bus.send(name, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "idle":
            return "Entering idle phase."
        if tool_name == "claim_task":
            return self.task_mgr.claim(args["task_id"], name)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self, base_tools: dict[str, object], name: str) -> list:
        """为单个队友拼装其可用工具集。"""
        @tool("send_message")
        def teammate_send_message_tool(to: str, content: str, msg_type: str = "message") -> str:
            """Send a message to another teammate or lead."""
            return self._teammate_exec(name, "send_message", {"to": to, "content": content, "msg_type": msg_type})

        @tool("idle")
        def teammate_idle_tool() -> str:
            """Signal that no actionable work remains for now."""
            return self._teammate_exec(name, "idle", {})

        @tool("claim_task")
        def teammate_claim_task_tool(task_id: int) -> str:
            """Claim a task by ID from the shared task board."""
            return self._teammate_exec(name, "claim_task", {"task_id": task_id})

        return [
            base_tools["bash"],
            base_tools["glob_files"],
            base_tools["grep_content"],
            base_tools["read_file"],
            base_tools["read_file_segment"],
            base_tools["write_file"],
            base_tools["edit_file"],
            teammate_send_message_tool,
            teammate_idle_tool,
            teammate_claim_task_tool,
        ]

    def _loop(self, name: str, role: str, prompt: str):
        """队友代理的主循环。

        分两段：
        1. WORK PHASE：持续工作和调用工具
        2. IDLE PHASE：轮询消息、等待任务、自动认领
        """
        from final_version_app.application.tooling import build_base_toolset

        base_tools, _ = build_base_toolset()
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            "Use idle when done with current work. You may auto-claim tasks."
        )
        messages: list[BaseMessage] = [HumanMessage(content=prompt)]
        tools = self._teammate_tools(base_tools, name)
        sys_prompt = (
            f"{sys_prompt}\n"
            "Tool usage details:\n"
            f"{format_tool_guide(tools)}\n"
            "Rules:\n"
            "- Use glob_files to discover candidate files before opening them.\n"
            "- Use grep_content to find relevant code or text before read_file.\n"
            "- After grep_content returns line hits, prefer read_file_segment over rereading the entire file.\n"
            "- Prefer read_file before edit_file/write_file.\n"
            "- Use send_message for cross-agent coordination and include concrete next actions.\n"
            "- Call idle only when no actionable work remains."
        )
        while True:
            for _ in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append(HumanMessage(content=json.dumps(msg, ensure_ascii=False)))
                try:
                    response = invoke_langchain(messages=messages, tools=tools, system=sys_prompt, max_tokens=8000)
                except Exception:
                    self._set_status(name, "shutdown")
                    return
                messages.append(response)
                if not (response.tool_calls or []):
                    break
                idle_requested = False
                for call in response.tool_calls or []:
                    tool_name = str(call.get("name", ""))
                    tool_input = call.get("args", {})
                    if not isinstance(tool_input, dict):
                        tool_input = {}
                    tool_call_id = str(call.get("id", ""))
                    if tool_name == "idle":
                        idle_requested = True
                    output = self._teammate_exec(name, tool_name, tool_input)
                    print(f"  [{name}] {tool_name}: {str(output)[:120]}")
                    messages.append(ToolMessage(content=str(output), tool_call_id=tool_call_id))
                if idle_requested:
                    break
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append(HumanMessage(content=json.dumps(msg, ensure_ascii=False)))
                    resume = True
                    break
                unclaimed = []
                for path in sorted(TASKS_DIR.glob("task_*.json")):
                    task = json.loads(read_text_auto(path))
                    if task.get("status") == "pending" and not task.get("owner") and not task.get("blockedBy"):
                        unclaimed.append(task)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    if len(messages) <= 3:
                        messages.insert(0, HumanMessage(content=f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"))
                        messages.insert(1, AIMessage(content=f"I am {name}. Continuing."))
                    messages.append(HumanMessage(content=f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"))
                    messages.append(AIMessage(content=f"Claimed task #{task['id']}. Working on it."))
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        """列出所有队友。"""
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for member in self.config["members"]:
            lines.append(f"  {member['name']} ({member['role']}): {member['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """返回所有队友名称。"""
        return [member["name"] for member in self.config["members"]]
