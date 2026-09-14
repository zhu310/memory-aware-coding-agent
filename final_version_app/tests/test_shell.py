"""Regression tests for the Agent shell execution boundary."""

from __future__ import annotations

import os

import pytest

from final_version_app.infra import shell


def test_nonzero_exit_is_reported_as_a_tool_error(monkeypatch):
    monkeypatch.setattr(
        shell,
        "run_shell",
        lambda command, timeout=120: shell.ShellExecutionResult(
            stdout="",
            stderr="python：无法识别该命令",
            exit_code=7,
        ),
    )

    output = shell.run_bash("python broken.py")

    assert output.startswith("Error: Command failed with exit code 7.")
    assert "无法识别" in output


def test_windows_chinese_output_decoder_falls_back_to_gb18030(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(shell, "os", SimpleNamespace(name="nt"))
    message = "中文路径：E:\\项目\\测试.py"
    assert shell._decode_output(message.encode("gb18030")) == message


def test_powershell_and_chain_ignores_operators_inside_quotes():
    parts = shell._split_powershell_and_chain('Write-Output "a && b" && python demo.py')
    assert parts == ['Write-Output "a && b"', "python demo.py"]


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration test")
def test_windows_shell_supports_and_chain_python_alias_and_utf8():
    execution = shell.run_shell(
        'Write-Output "准备执行" && python -c "import sys; print(\'中文输出\'); sys.exit(7)"'
    )

    assert execution.exit_code == 7
    assert "准备执行" in execution.stdout
    assert "中文输出" in execution.stdout
    assert "����" not in execution.output
