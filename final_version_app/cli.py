"""Productized command line entrypoint for the coding agent runtime."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _ensure_import_path() -> None:
    """Allow direct execution from a source checkout without installation."""

    package_root = Path(__file__).resolve().parent
    project_root = str(package_root.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


def _load_project_env() -> None:
    """Load the agent project's .env before switching to the target workspace."""

    project_root = Path(__file__).resolve().parent.parent
    os.environ["AGENT_PROJECT_ROOT"] = str(project_root)
    os.environ["AGENT_ENV_FILE"] = str(project_root / ".env")
    project_env = project_root / ".env"
    if project_env.exists():
        load_dotenv(project_env, override=True)


def _workspace(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"Workspace does not exist: {value}")
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"Workspace is not a directory: {value}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="Run the memory-aware coding agent against a local workspace.",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=_workspace,
        default=Path.cwd(),
        help="Directory the agent may read, edit, and store runtime state in.",
    )
    parser.add_argument(
        "--max-tool-rounds",
        type=int,
        help="Maximum model/tool rounds per user turn.",
    )
    parser.add_argument(
        "--max-turn-seconds",
        type=float,
        help="Maximum wall-clock seconds per user turn before finalization.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show tool traces, runtime ids, and token usage in the REPL.",
    )

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("doctor", help="Check local environment and model configuration.")
    subcommands.add_parser("repl", help="Start the interactive command line agent.")

    api = subcommands.add_parser("api", help="Start the FastAPI adapter.")
    api.add_argument("--host", default=None, help="API host, default 127.0.0.1.")
    api.add_argument("--port", type=int, default=None, help="API port, default 8000.")

    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_import_path()
    _load_project_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    os.chdir(workspace)
    os.environ["AGENT_WORKSPACE"] = str(workspace)
    if args.max_tool_rounds is not None:
        os.environ["AGENT_MAX_TOOL_ROUNDS"] = str(args.max_tool_rounds)
    if args.max_turn_seconds is not None:
        os.environ["AGENT_MAX_TURN_SECONDS"] = str(args.max_turn_seconds)
    if args.verbose:
        os.environ["AGENT_REPL_VERBOSE"] = "1"

    command = args.command or "repl"
    if command == "doctor":
        return _doctor(workspace)

    if command == "api":
        if args.host:
            os.environ["AGENT_API_HOST"] = args.host
        if args.port is not None:
            os.environ["AGENT_API_PORT"] = str(args.port)
        importlib.import_module("final_version_app.api").main()
        return 0

    if command == "repl":
        importlib.import_module("final_version_app.application.repl").main()
        return 0

    parser.error(f"Unsupported command: {command}")
    return 2


def _doctor(workspace: Path) -> int:
    _ensure_import_path()
    from final_version_app.config import AGENT_ENV_FILE, MODEL

    lower_model = MODEL.lower()
    if lower_model.startswith("qwen"):
        key_name = "DASHSCOPE_API_KEY"
    elif lower_model.startswith("deepseek"):
        key_name = "DEEPSEEK_API_KEY"
    else:
        key_name = "OPENAI_API_KEY"

    checks = [
        ("workspace", workspace.exists() and workspace.is_dir(), str(workspace)),
        ("env_file", AGENT_ENV_FILE.exists(), str(AGENT_ENV_FILE)),
        ("model", bool(MODEL.strip()), MODEL),
        (key_name, bool(os.getenv(key_name)), "set" if os.getenv(key_name) else "missing"),
    ]
    for name, ok, detail in checks:
        mark = "ok" if ok else "missing"
        print(f"[{mark}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
