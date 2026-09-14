"""Cross-platform shell execution used by agent tools.

The public tool keeps the historical name ``bash`` for model/provider
compatibility. On Windows the command is executed by Windows PowerShell, with
an explicit UTF-8 boundary and a stable ``python`` command that points to the
same interpreter as the Agent API.
"""

from __future__ import annotations

from dataclasses import dataclass
import locale
import os
import subprocess
import sys
from typing import Iterator

from final_version_app.config import WORKDIR


@dataclass(frozen=True)
class ShellExecutionResult:
    """Structured subprocess result with backward-compatible tuple unpacking."""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        parts = [part.strip() for part in (self.stdout, self.stderr) if part.strip()]
        return "\n".join(parts)

    def __iter__(self) -> Iterator[str]:
        # Older callers used ``stdout, stderr = run_shell(...)``.
        yield self.stdout
        yield self.stderr


def _decode_output(value: bytes | None) -> str:
    """Decode PowerShell/native output without turning Chinese text into mojibake."""

    if not value:
        return ""

    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",)
    else:
        preferred = locale.getpreferredencoding(False)
        candidates = ("utf-8-sig", "gb18030", preferred, "mbcs") if os.name == "nt" else ("utf-8-sig", preferred)

    for encoding in dict.fromkeys(candidates):
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def _split_powershell_and_chain(command: str) -> list[str]:
    """Split Bash-style ``&&`` only when it occurs outside quoted strings."""

    parts: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "`":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "&" and index + 1 < len(command) and command[index + 1] == "&":
            parts.append(command[start:index].strip())
            start = index + 2
            index += 1
        index += 1
    parts.append(command[start:].strip())
    return [part for part in parts if part]


def _powershell_body(command: str) -> str:
    """Translate the common Bash ``cmd1 && cmd2`` form for PowerShell 5.1."""

    parts = _split_powershell_and_chain(command)
    if len(parts) <= 1:
        return command

    failure_guard = (
        "if (-not $?) { "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } else { exit 1 } "
        "}"
    )
    return f"; {failure_guard}; ".join(parts)


def _powershell_command(command: str) -> str:
    """Build a PowerShell 5.1-compatible, UTF-8 command envelope."""

    python_executable = str(sys.executable).replace("'", "''")
    body = _powershell_body(command)
    return (
        "[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false); "
        "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
        "$OutputEncoding=New-Object System.Text.UTF8Encoding($false); "
        "$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'; "
        f"function global:python {{ & '{python_executable}' @args }}; "
        f"function global:python3 {{ & '{python_executable}' @args }}; "
        "$global:LASTEXITCODE=0; "
        f"& {{ {body}; "
        "if (-not $?) { if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } else { exit 1 } }; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } "
        "}"
    )


def run_shell(command: str, timeout: int = 120) -> ShellExecutionResult:
    """Execute a command and preserve stdout, stderr and the real exit code."""

    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")

    if os.name == "nt":
        process = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _powershell_command(command)],
            cwd=WORKDIR,
            capture_output=True,
            text=False,
            env=environment,
            timeout=timeout,
        )
    else:
        process = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=False,
            env=environment,
            timeout=timeout,
        )

    return ShellExecutionResult(
        stdout=_decode_output(process.stdout),
        stderr=_decode_output(process.stderr),
        exit_code=process.returncode,
    )


def run_bash(command: str) -> str:
    """Execute the provider-compatible ``bash`` tool and normalize failures."""

    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(token in command.lower() for token in dangerous):
        return "Error: Dangerous command blocked"

    try:
        result = run_shell(command, timeout=120)
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 120 seconds"

    output = result.output
    if not result.succeeded:
        detail = output or "The command produced no diagnostic output."
        return f"Error: Command failed with exit code {result.exit_code}.\n{detail}"[:50000]
    return output[:50000] if output else "(no output)"
