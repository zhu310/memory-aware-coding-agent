# Memory-Aware Coding Agent

这是一个面向复杂开发任务的本地 Coding Agent Runtime。用户输入自然语言任务后，系统会经过模型推理、工具调用、文件读写、命令执行、事件记录和记忆管理，完成多步骤代码任务。

本项目对标的是 Codex 这类 Coding Agent 的核心运行机制，而不是完整的 IDE 或云端沙箱产品。它重点实现了 Agent Loop、工具编排、上下文压缩、Session Memory、线程/回合持久化、文件资产管理和多 Agent 协作等 Runtime 能力。

## 核心能力

- 在指定工作区内读取、搜索、写入和编辑文件。
- 在指定工作区内执行 shell 命令。
- 生成代码、Markdown、Word、Excel 等内容或资产。
- 通过 API 上传并解析本地文档。
- 持久化 thread、turn、event、memory、task 和生成文件资产。
- 在长任务中进行上下文压缩，并尽量保持工具调用链完整。
- 支持子 Agent、异步任务、持久化队友 Agent 和消息总线。
- 提供 CLI REPL 和可选 FastAPI 接入方式。

## 架构概览

```text
用户请求
  -> AgentRuntime thread/turn
  -> agent_loop
  -> LLM response
  -> ToolOrchestrator
  -> tool handler
  -> ToolMessage
  -> LLM final answer

final_version_app/
  application/  # Agent Loop、提示词、压缩、记忆编排
  domain/       # 任务、待办、消息、后台任务、技能、Office 能力
  engine/       # Thread/Turn Runtime 内核、事件、取消机制
  protocol/     # Runtime 命令、记录、事件、条目模型
  storage/      # JSONL thread/event 存储，SQLite 认证/资产索引
  tools/        # ToolOrchestrator 和统一工具执行路径
  infra/        # LLM、shell、workspace、usage 适配层

skills/         # Agent 按需加载的技能说明
web/            # 教学可视化材料，不是当前主产品 UI
docs/           # 课程和架构说明材料
```

## 快速启动

先进入 `coding-agent` 源码项目目录，安装一次 Agent Runtime：

```powershell
Set-Location E:\大模型\第三期\coding-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
```

在 `.env` 中填写 `MODEL_ID` 对应的 API key，然后检查环境：

```powershell
coding-agent doctor
```

在当前目录启动交互式 Agent：

```powershell
coding-agent
```

如果已经激活了 `coding-agent` 的 `.venv`，可以把其他目录作为工作区启动：

```powershell
coding-agent --workspace E:\大模型\第三期\campus-agent repl
```

如果你现在已经在另一个项目目录里，例如 `E:\大模型\第三期\campus-agent`，但没有激活 `coding-agent` 的虚拟环境，直接输入 `coding-agent` 会找不到命令。此时不要在目标项目里重新 `python -m venv` 或 `pip install -e .`，目标项目只是被操作的工作区，真正的 Agent 仍然安装在 `coding-agent` 项目的虚拟环境里。可以用完整路径启动：

```powershell
& "E:\大模型\第三期\coding-agent\.venv\Scripts\coding-agent.exe" --workspace . repl
```

也可以临时把 `coding-agent` 的命令目录加入当前 PowerShell 的 `PATH`：

```powershell
$env:Path = "E:\大模型\第三期\coding-agent\.venv\Scripts;$env:Path"
coding-agent --workspace . repl
```

跨目录启动时，CLI 会读取 `coding-agent` 源码目录里的 `.env`，不会让目标工作区的 `.env` 悄悄覆盖模型配置。可以用 `coding-agent doctor` 查看实际读取的 `env_file`、`MODEL_ID` 和对应 key 是否存在。

调试时查看工具调用、runtime id 和 token usage：

```powershell
coding-agent --verbose repl
```

启动 HTTP API，用于后续接前端或外部系统：

```powershell
coding-agent --workspace E:\path\to\your-project api --host 127.0.0.1 --port 8000
```

## 快速演示

在 REPL 中可以输入：

```text
读取 README.md，总结这个项目的核心能力。
```

```text
搜索 final_version_app 中 SQLite 的使用位置，并说明分别保存了什么数据。
```

```text
创建 demo_hello.py，内容是打印 hello coding agent，然后运行它并告诉我输出。
```

```text
生成一份项目介绍 Markdown 文档，保存为 docs/generated_project_intro.md。
```

预期效果：

- Agent 会根据任务自主调用 `read_file`、`grep_content`、`write_file`、`edit_file`、`bash` 等工具。
- 生成的代码或 Markdown 会真实写入当前选定的工作区。
- 上传文件或 Office 生成资产会保存到 `.agent_runtime/assets/`，并由 SQLite 建立索引。
- REPL 默认只输出最终回答；输入 `/last` 可以重新打印上一轮完整回答，输入 `/usage` 可以查看 token 使用情况。
- REPL 默认支持多行输入。可以直接粘贴多行需求、代码或报错信息，输入完成后单独输入一行 `/send` 才会提交整次请求。
- 如果希望显式标记粘贴块，也可以输入 `/paste`，粘贴完成后单独输入一行 `/end` 结束该粘贴块，然后继续补充要求，最后输入 `/send` 提交。
- 不确定文件保存到哪里时，输入 `/workspace` 查看当前工作区。普通文件会写入该目录下的相对路径。

多行输入示例：

```text
请根据下面简历生成面试追问文档：
项目描述：
......
请保存为 docs/interview_questions.md
/send
```

## Deployment boundary

The supported runtime topology is:

```text
1 Workspace = 1 API Process = 1 AgentRuntime
```

Do not run multiple API processes against the same workspace. The local JSONL,
SQLite and file stores are scoped to one workspace-owned Runtime process, not a
cross-process coordination layer.

The optional Gateway is a static route table only:

```text
account/slot -> pre-existing Runtime API URL
```

It does not create workspaces, create accounts on disk, start Runtime processes,
allocate ports, manage Docker/containers, provision volumes, assign disk space,
or perform dynamic user provisioning. The deployer must ensure each configured
slot points to the intended independent workspace runtime. The Gateway only
validates facts visible in its static config, such as duplicate Runtime URLs.

## Benchmark

运行本地上下文压缩 benchmark：

```powershell
python benchmarks\context_compaction_benchmark.py
```

当前样例结果：

```json
{
  "rounds": 24,
  "tool_chars_per_result": 12000,
  "before_tokens_estimate": 77028,
  "after_tool_budget_tokens_estimate": 17364,
  "after_microcompact_tokens_estimate": 8954,
  "saved_tokens_estimate": 68074,
  "token_reduction_percent": 88.38
}
```

这个 benchmark 不调用 LLM，也不会消耗模型 token。它验证的是本地上下文控制层：单条工具结果裁剪和旧工具输出的 microcompact 压缩。

## 工作区边界

默认工作区是启动命令所在目录，也可以通过 `--workspace` 指定。

以下文件工具都会经过 `safe_path()` 校验，只允许访问工作区内部路径：

- `read_file`
- `read_file_segment`
- `write_file`
- `edit_file`
- `glob_files`
- `grep_content`
- `file_import`

运行时状态会保存在当前工作区内：

```text
.agent_runtime/  # thread/event 状态、上传文件、生成资产
.memory/         # session memory 和 skill memory
.tasks/          # 持久化任务状态
.team/           # 队友 Agent 状态和 inbox
.usage/          # token usage 记录
```

需要注意：shell 工具也会从工作区启动，但目前还不是完整 OS 沙箱。它现在使用命令防护和黑名单策略，而不是容器级隔离。

## 与 Codex 的关系

本项目已经实现的能力：

- 本地工作区代码读取、搜索、编辑和命令执行。
- ReAct 风格的模型/工具循环，工具结果通过 ToolMessage 回到上下文。
- thread、turn、tool call、取消、usage 等运行事件记录。
- 上下文压缩和 memory-first 的长任务处理。
- CLI 和 API 两种入口。
- 基础子 Agent 和多 Agent 协作机制。

尚未达到 Codex 产品级体验的地方：

- 没有成熟的 IDE/Web UI 来展示 diff、审批修改和管理任务。
- 没有强化的 OS/container 沙箱。
- 没有生产级权限审批策略。
- 没有云端任务环境和工业级长任务调度系统。
- 代码生成质量依赖当前配置的 OpenAI Compatible 模型。

更准确的定位是：本项目实现了 Coding Agent Runtime 的核心机制，用于学习和验证 Agent 工程化设计，而不是完整复刻 Codex 产品。

## 测试

```powershell
python -m pytest final_version_app\tests -q
```

当前本地测试结果：

```text
111 passed
```

## 安全说明

不要提交 `.env`、运行时记忆、任务状态、transcript、上传文件、生成资产或 API key。
