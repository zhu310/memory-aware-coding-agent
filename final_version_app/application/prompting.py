"""提示词组装工具。"""

from final_version_app.config import OS_NAME, SHELL_NAME, WORKDIR
from final_version_app.infra.llm import format_tool_guide


def build_base_system(skills_description: str) -> str:
    """构造基础 system prompt，不包含工具清单。"""
    return f"""你是工作在 {WORKDIR} 的编码智能体。请使用工具来完成任务。
对于多步骤工作，优先使用 task_create/task_update/task_list；对于简短检查清单，使用 TodoWrite。
当下一步依赖子任务结果时，使用 task 做短时阻塞式委派。对于可以在后台独立运行的工作，使用 task_async，并在汇合点使用 task_check 或 task_join。需要专项知识时，使用 load_skill。
可用技能：{skills_description}

运行环境：
- 操作系统：{OS_NAME}
- Shell：{SHELL_NAME}

命令使用指导：
- 如果操作系统是 Windows，优先使用 PowerShell 原生命令：
  Get-ChildItem、Select-String、Measure-Object、Get-Content。
- 在 Windows 上避免使用仅适用于 Linux 的参数或命令（例如：ls -la、wc）。"""


def build_system(base_system: str, tools: list) -> str:
    """在基础 prompt 上追加工具说明和操作规则。"""
    return (
        f"{base_system}\n\n"
        "工具使用说明：\n"
        f"{format_tool_guide(tools)}\n\n"
        "操作规则：\n"
        "- 在编辑前，先使用 read_file 或 bash 验证你的判断。\n"
        "- 在打开文件之前，先用 glob_files 缩小候选路径范围。\n"
        "- 在大范围 read_file 或 bash 扫描之前，先用 grep_content 定位相关代码或文本。\n"
        "- 当 grep_content 返回命中行后，优先使用 read_file_segment 查看局部片段。\n"
        "- 定点修改优先使用 edit_file；整文件替换或新建文件时使用 write_file。\n"
        "- 对于多步骤工作，使用 task_create/task_update/task_list/task_get 维护显式的 DAG 任务关系。\n"
        "- 当下一步依赖子任务结果时，使用 task 进行短时子代理工作。\n"
        "- 对于可独立运行的长任务，使用 task_async，保存返回的 run_id，用 task_check 查看状态，只在汇合点用较短超时的 task_join 等待。\n"
        "- background_run 只用于长时间命令，结果通过 check_background 或通知机制消费。\n"
        "- 团队协作时，使用 send_message/broadcast/read_inbox，并给出明确的行动项。\n"
        "- shutdown_request 和 plan_approval 只用于协议动作，不用于普通任务更新。"
    )
