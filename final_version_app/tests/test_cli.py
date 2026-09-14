"""CLI behavior that must hold before importing workspace-bound config."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from final_version_app import cli


def test_cli_sets_workspace_before_starting_repl(tmp_path, monkeypatch):
    calls = []

    def fake_repl_main():
        calls.append(Path.cwd())

    monkeypatch.setattr(
        cli.importlib,
        "import_module",
        lambda name: SimpleNamespace(main=fake_repl_main),
    )
    monkeypatch.delenv("AGENT_WORKSPACE", raising=False)
    monkeypatch.delenv("AGENT_MAX_TOOL_ROUNDS", raising=False)
    monkeypatch.delenv("AGENT_REPL_VERBOSE", raising=False)

    result = cli.main(
        ["--workspace", str(tmp_path), "--max-tool-rounds", "3", "--verbose", "repl"]
    )

    assert result == 0
    assert calls == [tmp_path]
    assert cli.os.environ["AGENT_WORKSPACE"] == str(tmp_path)
    assert cli.os.environ["AGENT_MAX_TOOL_ROUNDS"] == "3"
    assert cli.os.environ["AGENT_REPL_VERBOSE"] == "1"


def test_cli_sets_api_host_and_port(tmp_path, monkeypatch):
    calls = []

    def fake_api_main():
        calls.append(Path.cwd())

    monkeypatch.setattr(
        cli.importlib,
        "import_module",
        lambda name: SimpleNamespace(main=fake_api_main),
    )
    monkeypatch.delenv("AGENT_API_HOST", raising=False)
    monkeypatch.delenv("AGENT_API_PORT", raising=False)

    result = cli.main(
        [
            "--workspace",
            str(tmp_path),
            "api",
            "--host",
            "127.0.0.2",
            "--port",
            "8765",
        ]
    )

    assert result == 0
    assert calls == [tmp_path]
    assert cli.os.environ["AGENT_API_HOST"] == "127.0.0.2"
    assert cli.os.environ["AGENT_API_PORT"] == "8765"


def test_cli_doctor_prints_env_file(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_ID=deepseek-chat\n", encoding="utf-8")
    monkeypatch.setitem(
        sys.modules,
        "final_version_app.config",
        SimpleNamespace(MODEL="deepseek-chat", AGENT_ENV_FILE=env_file),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    result = cli._doctor(tmp_path)

    output = capsys.readouterr().out
    assert result == 0
    assert "env_file" in output
    assert str(env_file) in output
