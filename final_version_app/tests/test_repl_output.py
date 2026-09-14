"""REPL output should be readable for normal users."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from final_version_app.application import repl
from final_version_app.protocol import TurnStatus


@dataclass
class FakeResult:
    turn_id: str = "turn_test"
    status: TurnStatus = TurnStatus.COMPLETED
    assistant_text: str = "完整回答"
    error: str | None = None


class FakeRuntime:
    services = SimpleNamespace(
        task_mgr=SimpleNamespace(list_all=lambda: "[]"),
        bus=SimpleNamespace(read_inbox=lambda _name: []),
    )
    team_manager = SimpleNamespace(list_all=lambda: "[]")

    def __init__(self):
        self.answer = "完整回答"
        self.prompts = []

    def start_thread(self, title=""):
        return SimpleNamespace(thread_id="thr_test", to_dict=lambda: {})

    def run_turn(self, thread_id, query):
        self.prompts.append(query)
        return FakeResult(assistant_text=self.answer)

    def events(self, thread_id):
        return []

    def history(self, thread_id):
        return []

    def list_threads(self):
        return []


def test_repl_default_prints_answer_without_runtime_noise(monkeypatch, capsys):
    monkeypatch.setattr(repl, "build_agent_runtime", lambda: FakeRuntime())
    prompts = iter(["hello", "/send", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))
    monkeypatch.delenv("AGENT_REPL_VERBOSE", raising=False)

    repl.main()

    output = capsys.readouterr().out
    assert "完整回答" in output
    assert "/send" in output
    assert "结束会话" in output
    assert "[runtime] thread_id" not in output
    assert "[usage]" not in output


def test_repl_last_reprints_previous_answer(monkeypatch, capsys):
    monkeypatch.setattr(repl, "build_agent_runtime", lambda: FakeRuntime())
    prompts = iter(["hello", "/send", "/last", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(prompts))
    monkeypatch.delenv("AGENT_REPL_VERBOSE", raising=False)

    repl.main()

    output = capsys.readouterr().out
    assert output.count("完整回答") == 2


def test_repl_default_collects_multiline_until_send(monkeypatch, capsys):
    runtime = FakeRuntime()
    monkeypatch.setattr(repl, "build_agent_runtime", lambda: runtime)
    prompts = iter(["第一行需求", "第二行需求", "/send", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(prompts))
    monkeypatch.delenv("AGENT_REPL_VERBOSE", raising=False)

    repl.main()

    assert runtime.prompts == ["第一行需求\n\n第二行需求"]
    assert "完整回答" in capsys.readouterr().out


def test_repl_paste_collects_multiline_prompt(monkeypatch, capsys):
    runtime = FakeRuntime()
    monkeypatch.setattr(repl, "build_agent_runtime", lambda: runtime)
    prompts = iter(["/paste", "项目描述：", "第一行", "第二行", "/end", "/send", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(prompts))
    monkeypatch.delenv("AGENT_REPL_VERBOSE", raising=False)

    repl.main()

    assert runtime.prompts == ["项目描述：\n第一行\n第二行"]
    assert "请粘贴多行内容" in capsys.readouterr().out


def test_repl_inline_paste_keeps_prefix_prompt(monkeypatch, capsys):
    runtime = FakeRuntime()
    monkeypatch.setattr(repl, "build_agent_runtime", lambda: runtime)
    prompts = iter(
        [
            "请生成文档，简历内容如下：/paste",
            "项目描述：",
            "第一行",
            "/end",
            "请保存为 docs/interview_questions.md",
            "/send",
            "exit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(prompts))
    monkeypatch.delenv("AGENT_REPL_VERBOSE", raising=False)

    repl.main()

    assert runtime.prompts == [
        "请生成文档，简历内容如下：\n\n项目描述：\n第一行\n\n请保存为 docs/interview_questions.md"
    ]
    assert "请粘贴多行内容" in capsys.readouterr().out


def test_repl_explains_insufficient_balance_error():
    message = repl._format_runtime_error(
        "APIStatusError: Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"
    )

    assert "API key 余额不足" in message
    assert "MODEL_ID" in message
