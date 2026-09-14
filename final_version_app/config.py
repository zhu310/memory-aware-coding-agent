"""Runtime configuration for the final_version_app package."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(os.getenv("AGENT_PROJECT_ROOT", Path(__file__).resolve().parent.parent))
AGENT_ENV_FILE = Path(os.getenv("AGENT_ENV_FILE", PROJECT_ROOT / ".env"))
if AGENT_ENV_FILE.exists():
    load_dotenv(AGENT_ENV_FILE, override=True)

WORKDIR = Path.cwd()
MODEL = os.getenv("MODEL_ID", "qwen3.6-plus")
OS_NAME = platform.system()
SHELL_NAME = "PowerShell" if os.name == "nt" else os.environ.get("SHELL", "sh")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 运行时目录：
# - .team / .tasks 存协作和任务状态
# - .transcripts 存 legacy 压缩前的完整历史归档
# - .memory 存结构化 session memory 及其状态文件
# - .agent_runtime 存 Thread 元数据和 append-only 事件日志
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
AGENT_RUNS_DIR = TEAM_DIR / "runs"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
MEMORY_DIR = WORKDIR / ".memory"
RUNTIME_STATE_DIR = WORKDIR / ".agent_runtime"
SESSION_MEMORY_PATH = MEMORY_DIR / "session_memory.md"
SESSION_MEMORY_STATE_PATH = MEMORY_DIR / "session_memory_state.json"
SESSION_MEMORY_EVENTS_PATH = MEMORY_DIR / "session_memory_events.jsonl"

# 主对话上下文的硬 token 上限。
# 主循环会在逼近这个值之前先尝试较轻的压缩，超过后再走更强的 fallback。
TOKEN_THRESHOLD = 100000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

# 工具消息压缩参数：
# 1. 先裁剪单条超长工具输出
# 2. 再把较早的工具输出改写成摘要
# 目的是让最近几次工具调用仍然保留高保真信息。
TOOL_RESULT_CHAR_BUDGET = 2400
TOOL_RESULT_HEAD_CHARS = 1600
TOOL_RESULT_TAIL_CHARS = 600
MICROCOMPACT_KEEP_RECENT = 8
MICROCOMPACT_SUMMARY_HEAD = 240
MICROCOMPACT_SUMMARY_TAIL = 120
CONTEXT_COLLAPSE_TRIGGER_RATIO = 0.8
CONTEXT_COLLAPSE_KEEP_RECENT = 18
CONTEXT_COLLAPSE_MAX_TOOL_TRACES = 12
TOOL_CACHE_LIMIT = 24


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# Runtime guardrails for real interactive use. They keep an autonomous agent from
# spending an unbounded number of model/tool rounds on a single user turn.
AGENT_MAX_TOOL_ROUNDS = max(1, _int_env("AGENT_MAX_TOOL_ROUNDS", 8))
AGENT_MAX_TURN_SECONDS = max(10.0, _float_env("AGENT_MAX_TURN_SECONDS", 120.0))
AGENT_FINAL_ANSWER_TOKENS = max(256, _int_env("AGENT_FINAL_ANSWER_TOKENS", 1600))
AGENT_REPL_VERBOSE = os.getenv("AGENT_REPL_VERBOSE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Session memory 提取阈值：
# - INIT_TOKENS：第一次开始建立长期记忆所需的最小 token
# - UPDATE_TOKENS：距离上次提取后，至少新增这么多 token 才允许再次提取
# - TOOL_THRESHOLD：如果还没到自然停顿点，则至少要发生这么多工具调用，才值得更新长期记忆
SESSION_MEMORY_INIT_TOKENS = 10000
SESSION_MEMORY_UPDATE_TOKENS = 5000
SESSION_MEMORY_TOOL_THRESHOLD = 3

# Session memory 压缩阈值：
# 主循环不会等到 TOKEN_THRESHOLD 才压缩，而是提前预留：
# - reserved budget：留给 compact summary
# - buffer：留给本轮剩余推理和回复
# 这样可以避免在同一轮里直接撞上硬上限。
SESSION_MEMORY_RESERVED_BUDGET = 20000
SESSION_MEMORY_COMPACT_BUFFER = 13000

# 压缩后 recent raw messages 的保留规则：
# - 至少保留一定 token 的近期原始消息
# - 至少保留一定数量的文本消息
# - 但也不能保留过多，否则 compact 后的上下文仍然会过大
SESSION_MEMORY_MIN_RECENT_TOKENS = 10000
SESSION_MEMORY_MIN_TEXT_MESSAGES = 5
SESSION_MEMORY_MAX_RECENT_TOKENS = 40000

# 后台提取与压缩的同步规则：
# - 如果压缩发生时后台提取仍在运行，会短暂等待
# - 目的是尽量使用最新的 memory 文件
# - 但如果提取卡太久，就不再继续等，避免主流程被拖死
SESSION_MEMORY_COMPACT_WAIT_SECONDS = 15
SESSION_MEMORY_EXTRACTION_STALE_SECONDS = 60

# 如果 session memory 文件自身过长，注入上下文前会按 section 截断，
# 防止 compact_summary 自己又把上下文撑爆。
SESSION_MEMORY_SECTION_CHAR_LIMIT = 2000

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
    "agent_run_completed",
    "agent_run_failed",
}
