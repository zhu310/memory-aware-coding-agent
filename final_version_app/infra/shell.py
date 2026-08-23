"""Shell 执行工具。

这里统一封装命令执行，主要解决两件事：
1. Windows 下强制走 PowerShell 并统一 UTF-8 编码
2. 对明显危险的命令做最基础的拦截
"""

import os
import subprocess

from final_version_app.config import WORKDIR


def run_shell(command: str, timeout: int = 120) -> tuple[str, str]:
    """执行底层命令并返回标准输出与标准错误。"""
    if os.name == "nt":
        ps_command = (
            "[Console]::InputEncoding=[System.Text.UTF8Encoding]::UTF8; "
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::UTF8; "
            "$OutputEncoding=[System.Text.UTF8Encoding]::UTF8; "
            f"{command}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_command],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    else:
        result = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    return result.stdout, result.stderr


def run_bash(command: str) -> str:
    """提供给上层工具使用的 shell 包装器。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(token in command for token in dangerous):
        return "Error: Dangerous command blocked"
    try:
        stdout, stderr = run_shell(command, timeout=120)
        output = (stdout + stderr).strip()
        return output[:50000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
