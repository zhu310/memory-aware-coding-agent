"""Todo 清单模型。"""


class TodoManager:
    """维护一个轻量任务清单，供主 agent 在短链路任务中自我约束。"""
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """校验并更新 todo 列表。"""
        validated, in_progress = [], 0

        # 状态别名映射表
        status_aliases = {
            "pending": "pending",
            "\u5f85\u529e": "pending",
            "\u5f85\u5904\u7406": "pending",
            "\u672a\u5f00\u59cb": "pending",
            "in_progress": "in_progress",
            "in-progress": "in_progress",
            "in progress": "in_progress",
            "\u8fdb\u884c\u4e2d": "in_progress",
            "\u5904\u7406\u4e2d": "in_progress",
            "completed": "completed",
            "complete": "completed",
            "\u5b8c\u6210": "completed",
            "\u5df2\u5b8c\u6210": "completed",
        }
        
        for index, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status_raw = item.get("status", "pending")
            status_text = str(status_raw).strip()
            status = status_aliases.get(status_text.lower()) or status_aliases.get(status_text)
            active_form_raw = item.get("activeForm", "")
            if not content:
                raise ValueError(f"Item {index}: content required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {index}: invalid status '{status_text}'")
            if not isinstance(active_form_raw, str) or not active_form_raw.strip():
                raise ValueError(f"Item {index}: activeForm must be a non-empty string")
            active_form = active_form_raw.strip()
            if status == "in_progress":
                in_progress += 1
            validated.append({"content": content, "status": status, "activeForm": active_form})
        if len(validated) > 20:
            raise ValueError("Max 20 todos")
        if in_progress > 1:
            raise ValueError("Only one in_progress allowed")
        self.items = validated
        return self.render()

    def render(self) -> str:
        """把内部 todo 状态渲染成适合终端展示的文本。"""
        if not self.items:
            return "No todos."
        lines = []
        for item in self.items:
            marker = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{marker} {item['content']}{suffix}")
        done = sum(1 for item in self.items if item["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        """判断当前是否还存在未完成事项。"""
        return any(item.get("status") != "completed" for item in self.items)
