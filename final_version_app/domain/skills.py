"""Skill 加载服务。"""

import re
import os
from pathlib import Path

from final_version_app.infra.workspace import read_text_auto


class SkillLoader:
    """扫描 `skills/` 目录，把 `SKILL.md` 组织成可加载知识块。"""
    def __init__(self, skills_dir: Path):
        self.skills_dir = Path(skills_dir)
        self.refresh()

    def refresh(self):
        skills_dir = self.skills_dir
        self.skills = {}
        shared = Path(os.getenv("AGENT_SHARED_SKILLS_DIR", str(Path(__file__).resolve().parents[2] / "skills")))
        roots = [shared, skills_dir] if shared.resolve() != skills_dir.resolve() else [skills_dir]
        for root in roots:
            if not root.exists(): continue
            for skill_file in sorted(root.rglob("SKILL.md")):
                if not skill_file.resolve().is_relative_to(root.resolve()): continue
                text = read_text_auto(skill_file)
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            key, value = line.split(":", 1)
                            meta[key.strip()] = value.strip()
                    body = match.group(2).strip()
                name = meta.get("name", skill_file.parent.name)
                self.skills[name] = {"meta": meta, "body": body, "directory": str(skill_file.parent.resolve())}

    def descriptions(self) -> str:
        """返回给 system prompt 使用的技能摘要列表。"""
        self.refresh()
        if not self.skills:
            return "(no skills)"
        return "\n".join(f"  - {name}: {skill['meta'].get('description', '-')}" for name, skill in self.skills.items())

    def load(self, name: str) -> str:
        """按名称加载完整 skill 正文。"""
        self.refresh()
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\nResource directory: {skill['directory']}\n{skill['body']}\n</skill>"
