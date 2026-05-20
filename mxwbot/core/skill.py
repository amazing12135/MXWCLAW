"""技能加载器 — 扫描 .md 文件，解析 YAML frontmatter。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    """单个技能实体。"""

    name: str
    description: str
    path: Path
    content: str = ""
    availability: str = "manual"  # "always" | "manual"
    dependencies: list[str] = field(default_factory=list)

    def to_xml(self) -> str:
        """生成注入 LLM 的轻量 XML 摘要。"""
        return (
            f'<skill name="{self.name}" availability="{self.availability}">\n'
            f"  {self.description}\n"
            f"</skill>"
        )


class SkillLoader:
    """扫描工作区，解析 .md 技能文件。"""

    def __init__(self, *search_dirs: Path) -> None:
        self._search_dirs = list(search_dirs)
        self._skills: dict[str, Skill] = {}

    # -- 扫描 ---------------------------------------------------------------

    def scan(self) -> list[Skill]:
        """（重新）扫描所有搜索目录中的 .md 文件。"""
        self._skills.clear()
        for d in self._search_dirs:
            if not d.exists():
                continue
            for md_path in sorted(d.rglob("*.md")):
                skill = self._parse_skill(md_path)
                if skill:
                    self._skills[skill.name] = skill
        return list(self._skills.values())

    @staticmethod
    def _parse_skill(path: Path) -> Skill | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return None

        m = _FRONTMATTER_RE.match(raw)
        if not m:
            return None

        try:
            import yaml
            meta = yaml.safe_load(m.group(1))
        except Exception:
            return None

        if not isinstance(meta, dict):
            return None

        body = raw[m.end():].strip()
        return Skill(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            path=path,
            content=body,
            availability=meta.get("availability", "manual"),
            dependencies=meta.get("dependencies", []),
        )

    # -- 查询 ---------------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def get_always_loaded(self) -> list[Skill]:
        """返回 availability="always" 的技能（始终注入 LLM 上下文）。"""
        return [s for s in self._skills.values() if s.availability == "always"]

    def list_all(self) -> list[Skill]:
        return list(self._skills.values())

    def to_context_xml(self) -> str:
        """生成所有技能的 XML 摘要（注入 LLM system prompt）。"""
        if not self._skills:
            return ""
        lines = ["<skills>"]
        for skill in sorted(self._skills.values(), key=lambda s: s.name):
            lines.append(f"  {skill.to_xml()}")
        lines.append("</skills>")
        return "\n".join(lines)
