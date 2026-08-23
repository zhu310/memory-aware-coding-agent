"""Constrained agent that edits the session memory markdown in place."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from final_version_app.config import WORKDIR
from final_version_app.infra.llm import format_tool_guide, invoke_langchain, render_ai_text
from final_version_app.infra.shell import run_bash
from final_version_app.infra.workspace import read_text_auto, relative_display


ToolHandler = Callable[..., str]


@dataclass
class MemoryEditorResult:
    summary: str
    changed: bool


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _display(path: Path) -> str:
    try:
        return relative_display(path)
    except Exception:
        return str(path).replace("\\", "/")


def _numbered(lines: list[str], start_line: int = 1) -> str:
    return "\n".join(f"{line_no}: {line}" for line_no, line in enumerate(lines, start_line))


class MemoryEditorToolbox:
    """File tools restricted to one memory file."""

    def __init__(self, memory_path: Path):
        self.memory_path = memory_path.resolve()

    def _resolve_target(self, path_text: str) -> Path:
        candidates = []
        raw = Path(path_text)
        if raw.is_absolute():
            candidates.append(raw.resolve())
        else:
            candidates.extend(
                [
                    (WORKDIR / path_text).resolve(),
                    (self.memory_path.parent / path_text).resolve(),
                ]
            )
        for candidate in candidates:
            if _same_path(candidate, self.memory_path):
                return candidate
        raise ValueError("Memory editor may only access the session memory file.")

    def read_file(self, path: str, limit: int | None = None) -> str:
        try:
            fp = self._resolve_target(path)
            lines = read_text_auto(fp).splitlines()
            total = len(lines)
            shown = lines
            if limit and limit < total:
                shown = lines[:limit] + [f"... ({total - limit} more)"]
            return f"[file {_display(fp)}; {total} lines; line-numbered output]\n{_numbered(shown)}"
        except Exception as exc:
            return f"Error: {exc}"

    def read_file_segment(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        center_line: int | None = None,
        before: int = 20,
        after: int = 20,
    ) -> str:
        try:
            fp = self._resolve_target(path)
            lines = read_text_auto(fp).splitlines()
            if not lines:
                return "(empty file)"
            start_idx, end_idx = _line_window(len(lines), start_line, end_line, center_line, before, after)
            return f"[segment {start_idx + 1}-{end_idx} of {len(lines)} lines]\n{_numbered(lines[start_idx:end_idx], start_idx + 1)}"
        except Exception as exc:
            return f"Error: {exc}"

    def write_file(self, path: str, content: str) -> str:
        try:
            fp = self._resolve_target(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {_display(fp)}"
        except Exception as exc:
            return f"Error: {exc}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        try:
            fp = self._resolve_target(path)
            content = read_text_auto(fp)
            if not old_text:
                return "Error: old_text is empty"
            edited = _replace_once(content, old_text, new_text)
            if edited is None:
                return "Error: Text not found. Re-read the file and use a smaller exact old_text anchor."
            fp.write_text(edited, encoding="utf-8")
            return f"Edited {_display(fp)}"
        except Exception as exc:
            return f"Error: {exc}"

    def glob_files(self, pattern: str, path: str = ".", limit: int = 100) -> str:
        if not pattern.strip():
            return "Error: pattern is required"
        if limit <= 0:
            return "Error: limit must be positive"
        rel = _display(self.memory_path)
        if fnmatch(self.memory_path.name, pattern.strip()) or fnmatch(rel, pattern.strip()):
            return json.dumps([{"path": rel, "type": "file"}][: min(limit, 500)], indent=2, ensure_ascii=False)
        return "[]"

    def grep_content(
        self,
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
        try:
            if output_mode not in {"content", "files_with_matches", "count"}:
                return "Error: output_mode must be one of: content, files_with_matches, count"
            if head_limit <= 0:
                return "Error: head_limit must be positive"
            matches = _grep_memory(read_text_auto(self.memory_path), pattern, before, after, ignore_case, multiline)
            if output_mode == "count":
                return json.dumps({"pattern": pattern, "count": len(matches), "files": 1 if matches else 0}, indent=2)
            if output_mode == "files_with_matches":
                files = [_display(self.memory_path)] if matches else []
                return json.dumps({"total": len(files), "files": files}, indent=2)
            window = matches[offset:offset + head_limit]
            return json.dumps(
                {
                    "total": len(matches),
                    "offset": offset,
                    "returned": len(window),
                    "matches": window,
                },
                indent=2,
                ensure_ascii=False,
            )
        except re.error as exc:
            return f"Error: Invalid regex: {exc}"
        except Exception as exc:
            return f"Error: {exc}"


def _line_window(
    total: int,
    start_line: int | None,
    end_line: int | None,
    center_line: int | None,
    before: int,
    after: int,
) -> tuple[int, int]:
    if center_line is not None:
        if center_line <= 0:
            raise ValueError("center_line must be >= 1")
        return max(0, center_line - 1 - max(before, 0)), min(total, center_line + max(after, 0))
    start = 1 if start_line is None else start_line
    end = min(total, start + max(after, 0)) if end_line is None else end_line
    if start <= 0 or end <= 0:
        raise ValueError("start_line and end_line must be >= 1")
    if end < start:
        raise ValueError("end_line must be >= start_line")
    return max(0, start - 1), min(total, end)


def _replace_once(content: str, old_text: str, new_text: str) -> str | None:
    if old_text in content:
        return content.replace(old_text, new_text, 1)
    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    normalized_old = old_text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized_old in normalized_content:
        normalized_new = new_text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized_content.replace(normalized_old, normalized_new, 1)
    return None


def _grep_memory(
    text: str,
    pattern: str,
    before: int,
    after: int,
    ignore_case: bool,
    multiline: bool,
) -> list[dict]:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0) | (re.DOTALL if multiline else 0)
    regex = re.compile(pattern, flags)
    rel = "session_memory.md"
    if multiline:
        return [
            {
                "path": rel,
                "match": match.group(0)[:500],
                "snippet": text[max(0, match.start() - 120): min(len(text), match.end() + 120)][:1000],
            }
            for match in regex.finditer(text)
        ]
    lines = text.splitlines()
    hits = []
    for idx, line in enumerate(lines):
        if regex.search(line):
            start = max(0, idx - before)
            end = min(len(lines), idx + after + 1)
            hits.append({"path": rel, "line": idx + 1, "match": line[:500], "context": lines[start:end]})
    return hits


def _restricted_bash(command: str) -> str:
    lowered = command.lower()
    blocked = [
        ">",
        ">>",
        "set-content",
        "add-content",
        "out-file",
        "remove-item",
        "rm ",
        "del ",
        "move-item",
        "copy-item",
        "mv ",
        "cp ",
        "curl ",
        "wget ",
        "npm install",
        "pip install",
    ]
    if any(token in lowered for token in blocked):
        return "Error: memory editor bash is read-only; use edit_file/write_file for the target memory file."
    return run_bash(command)


def build_memory_editor_toolset(toolbox: MemoryEditorToolbox) -> tuple[list, dict[str, ToolHandler]]:
    @tool("bash")
    def bash_tool(command: str) -> str:
        """Run a read-only shell inspection command."""
        return _restricted_bash(command)

    @tool("read_file")
    def read_file_tool(path: str, limit: int = None) -> str:
        """Read the session memory file."""
        return toolbox.read_file(path, limit)

    @tool("read_file_segment")
    def read_file_segment_tool(
        path: str,
        start_line: int = None,
        end_line: int = None,
        center_line: int = None,
        before: int = 20,
        after: int = 20,
    ) -> str:
        """Read a focused segment of the session memory file."""
        return toolbox.read_file_segment(path, start_line, end_line, center_line, before, after)

    @tool("write_file")
    def write_file_tool(path: str, content: str) -> str:
        """Write the complete session memory file."""
        return toolbox.write_file(path, content)

    @tool("edit_file")
    def edit_file_tool(path: str, old_text: str, new_text: str) -> str:
        """Replace exact text in the session memory file."""
        return toolbox.edit_file(path, old_text, new_text)

    @tool("glob_files")
    def glob_files_tool(pattern: str, path: str = ".", limit: int = 100) -> str:
        """Find the session memory file by glob pattern."""
        return toolbox.glob_files(pattern, path, limit)

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
        """Search the session memory file with regex."""
        return toolbox.grep_content(pattern, path, glob, output_mode, head_limit, offset, before, after, ignore_case, multiline)

    tools = [
        bash_tool,
        read_file_tool,
        read_file_segment_tool,
        write_file_tool,
        edit_file_tool,
        glob_files_tool,
        grep_content_tool,
    ]
    handlers: dict[str, ToolHandler] = {
        "bash": lambda **kw: _restricted_bash(kw["command"]),
        "read_file": lambda **kw: toolbox.read_file(kw["path"], kw.get("limit")),
        "read_file_segment": lambda **kw: toolbox.read_file_segment(
            kw["path"],
            kw.get("start_line"),
            kw.get("end_line"),
            kw.get("center_line"),
            kw.get("before", 20),
            kw.get("after", 20),
        ),
        "write_file": lambda **kw: toolbox.write_file(kw["path"], kw["content"]),
        "edit_file": lambda **kw: toolbox.edit_file(kw["path"], kw["old_text"], kw["new_text"]),
        "glob_files": lambda **kw: toolbox.glob_files(kw["pattern"], kw.get("path", "."), kw.get("limit", 100)),
        "grep_content": lambda **kw: toolbox.grep_content(
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
    }
    return tools, handlers


def run_memory_editor(memory_path: Path, transcript: str, max_rounds: int = 12) -> MemoryEditorResult:
    """Run the constrained editor agent and report whether the file changed."""
    memory_path = memory_path.resolve()
    before = read_text_auto(memory_path) if memory_path.exists() else ""
    toolbox = MemoryEditorToolbox(memory_path)
    tools, handlers = build_memory_editor_toolset(toolbox)
    messages: list[BaseMessage] = [HumanMessage(content=_editor_user_prompt(transcript))]
    response: AIMessage | None = None

    for _ in range(max_rounds):
        response = invoke_langchain(
            messages=messages,
            tools=tools,
            system=_editor_system_prompt(memory_path, tools),
            max_tokens=4000,
        )
        messages.append(response)
        if not (response.tool_calls or []):
            break
        for call in response.tool_calls or []:
            messages.append(_execute_tool_call(call, handlers))

    after = read_text_auto(memory_path) if memory_path.exists() else ""
    summary = render_ai_text(response).strip() if response else ""
    return MemoryEditorResult(summary=summary or "(memory editor completed)", changed=after != before)


def _editor_system_prompt(memory_path: Path, tools: list) -> str:
    return (
        "You are a dedicated session memory editor.\n"
        f"Target file: {memory_path}\n"
        "Your only job is to update that file using the new transcript slice.\n"
        "Tool usage details:\n"
        f"{format_tool_guide(tools)}\n"
        "Rules:\n"
        "- Only edit the target session memory file.\n"
        "- Preserve the exact section headings and their order.\n"
        "- Prefer read_file first, then edit_file with the smallest exact replacement that preserves unrelated sections.\n"
        "- Use write_file only when the file is missing, malformed, or exact local edits repeatedly fail.\n"
        "- Current State must describe the latest working state.\n"
        "- Files and Functions must list key files and responsibilities.\n"
        "- Errors & Corrections must capture failed approaches and fixes.\n"
        "- Learnings must preserve durable insights.\n"
        "- Key results must capture user-visible outcomes and pending deliverables.\n"
        "- Worklog should stay concise and recent.\n"
        "- After editing, read_file again and verify all required sections still exist.\n"
        "- Return a concise summary of changed sections when finished."
    )


def _editor_user_prompt(transcript: str) -> str:
    return (
        "Update the session memory file with this new transcript slice.\n"
        "Do not edit any other file.\n\n"
        f"```json\n{transcript}\n```"
    )


def _execute_tool_call(call: dict, handlers: dict[str, ToolHandler]) -> ToolMessage:
    tool_name = str(call.get("name", ""))
    tool_input = call.get("args", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    handler = handlers.get(tool_name)
    try:
        output = handler(**tool_input) if handler else f"Unknown tool: {tool_name}"
    except Exception as exc:
        output = f"Error: {exc}"
    return ToolMessage(content=str(output)[:50000], tool_call_id=str(call.get("id", "")))
