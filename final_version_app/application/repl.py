"""Interactive REPL entrypoint."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from final_version_app.application.agent_loop import agent_loop
from final_version_app.application.compression import auto_compact
from final_version_app.application.container import build_services
from final_version_app.application.prompting import build_base_system, build_system
from final_version_app.application.session_memory import ensure_message_uuid
from final_version_app.application.team_runtime import TeammateManager
from final_version_app.application.tooling import build_tool_runtime
from final_version_app.infra.llm import render_ai_text


def build_runtime():
    services = build_services()
    team_mgr = TeammateManager(services)
    tool_runtime = build_tool_runtime(services, team_mgr)
    base_system = build_base_system(services.skills.descriptions())
    system_prompt = build_system(base_system, tool_runtime.tools)
    return services, team_mgr, tool_runtime, system_prompt


def main():
    services, team_mgr, tool_runtime, system_prompt = build_runtime()
    history: list[BaseMessage] = []

    while True:
        try:
            query = input("please input your question: ")
        except (EOFError, KeyboardInterrupt):
            break

        stripped = query.strip()
        if stripped.lower() in ("q", "exit", ""):
            break
        if stripped == "/compact":
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history, session_memory=services.session_memory, trigger="manual")
            continue
        if stripped == "/tasks":
            print(services.task_mgr.list_all())
            continue
        if stripped == "/team":
            print(team_mgr.list_all())
            continue
        if stripped == "/inbox":
            print(json.dumps(services.bus.read_inbox("lead"), indent=2, ensure_ascii=False))
            continue

        message = HumanMessage(content=query)
        ensure_message_uuid(message)
        history.append(message)
        agent_loop(history, services, tool_runtime, system_prompt)
        if history and isinstance(history[-1], AIMessage):
            text = render_ai_text(history[-1]).strip()
            if text:
                print(text)
        print()
