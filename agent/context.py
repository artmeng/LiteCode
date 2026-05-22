"""agent/context.py

ContextBuilder：System Prompt 构建器。

将以下内容按顺序用 '---' 分隔拼接：
  1. Bootstrap 文件：Soul.md / TOOL.md / USER.local.md
  2. 身份模板：templates/agent/identity.md（Jinja2 渲染）
  3. 长期记忆：MEMORY.local.md
  4. 常驻技能（always=true 的 SKILL.md）
  5. 按需技能摘要
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from .skills import SkillsLoader

if TYPE_CHECKING:
    from .memory import MemoryStore


class ContextBuilder:
    """System Prompt 构建器，每轮将各层内容拼接成完整 system prompt 传给模型。"""

    # Bootstrap 文件顺序：SOUL > TOOL > USER
    _BOOTSTRAP_FILES = ["SOUL.md", "TOOL.md", "USER.md"]

    def __init__(
        self,
        docs_dir: Path,
        skills_loader: SkillsLoader,
        memory: MemoryStore | None = None,
    ):
        self.docs_dir = docs_dir
        self.skills = skills_loader
        self.memory = memory
        # Jinja2 模板引擎，加载 templates/agent/ 下的 .md 模板
        self._env = Environment(
            loader=FileSystemLoader(docs_dir / "agent"),
            autoescape=select_autoescape(enabled_extensions=("html",)),
        )

    def render_template(self, name: str, **kwargs) -> str:
        try:
            template = self._env.get_template(name)
            return template.render(**kwargs)
        except Exception:
            logger.warning(f"Template render failed: {name}")
            return ""

    def build_system_prompt(self) -> str:
        """拼接并返回完整的 system prompt 字符串。"""
        parts = []

        # 1. 拼接 Bootstrap 文件：SOUL.md / TOOL.md / USER.local.md
        bootstrap = "\n\n".join(
            self._bootstrap_path(name).read_text(encoding="utf-8").strip()
            for name in self._BOOTSTRAP_FILES
            if self._bootstrap_path(name).exists()
        )
        if bootstrap:
            parts.append(bootstrap)

        # 2. 身份模板：工作区路径、文件结构说明
        identity = self.render_template("identity.md", workspace=str(self.docs_dir.parent))
        if identity:
            parts.append(identity)

        # 3. 长期记忆
        if self.memory:
            memory = self.memory.read_memory().strip()
            if memory:
                parts.append(f"# Long-term Memory\n\n{memory}")

        # 4. 常驻技能（always=true）
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        # 5. 按需技能摘要表
        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(
                self.render_template("skills_section.md", skills_summary=skills_summary)
            )

        return "\n\n---\n\n".join(parts)

    def _bootstrap_path(self, name: str) -> Path:
        """解析 bootstrap 文件路径：USER.md 优先用本地副本。"""
        if name == "USER.md":
            # 优先级：USER.local.md > templates/init/USER.md
            local = self.docs_dir / "USER.local.md"
            if local.exists():
                return local
            init = self.docs_dir / "init" / "USER.md"
            if init.exists():
                return init
        return self.docs_dir / name
