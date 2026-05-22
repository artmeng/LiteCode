"""agent/compactor.py

历史压缩器：将旧对话归纳为当日情景记忆，并更新 MEMORY.local.md 与 USER.local.md。

压缩流程：
  1. 将 history[:-K] 拼成可读文本发给压缩模型
  2. 解析模型输出中的 <episode> / <updated_memory> / <updated_user> 标签
  3. 将情景记忆追加写入当日 .md，长期记忆和用户档案视情覆写
  4. 调用 memory.append_compact_marker 将已压缩原始行冷归档
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from .memory import MemoryStore
from .providers import LLMProvider
from .providers.base import run_sync


_UTC8 = timezone(timedelta(hours=8))

_PROMPT_FILE = Path(__file__).parent.parent / "templates" / "agent" / "compact_prompt.md"
_PROMPT_TEMPLATE = _PROMPT_FILE.read_text(encoding="utf-8")


def _extract(tag: str, text: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    """Flatten OpenAI-style history messages into a readable transcript."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "tool":
            snippet = str(content or "")[:500]
            name = msg.get("name") or msg.get("tool_call_id") or "tool"
            parts.append(f"[tool_result:{name}] {snippet}")
            continue
        if isinstance(content, str) and content:
            parts.append(f"[{role}] {content}")
        elif isinstance(content, list):
            parts.extend(_content_blocks_to_text(role, content))

        for tool_call in msg.get("tool_calls") or []:
            fn = tool_call.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments", "{}")
            parts.append(f"[assistant:tool_call] {name} {args}")

    return "\n".join(parts)


def _content_blocks_to_text(role: str, blocks: list[Any]) -> list[str]:
    parts: list[str] = []
    for block in blocks:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype == "text":
            text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
            parts.append(f"[{role}] {text}")
        elif btype in {"tool_use", "tool_call"}:
            name = getattr(block, "name", None) or block.get("name", "")
            parts.append(f"[{role}:tool_call] {name}")
        elif btype == "tool_result":
            c = getattr(block, "content", None) or block.get("content", "")
            if isinstance(c, list):
                c = " ".join(
                    (getattr(x, "text", None) or (x.get("text") if isinstance(x, dict) else str(x)) or "")
                    for x in c
                )
            parts.append(f"[{role}:tool_result] {str(c)[:500]}")
    return parts


class Compactor:
    """LLM 驱动的历史压缩器。

    K：压缩时保留最近 K 条结果，其余归纳为情景记忆。
    使用次模型（secondaryModelId）进行，次模型失败时自动改用主模型重试。
    """

    K = 10  # 压缩后保留的最近消息数

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        memory_store: MemoryStore,
        max_tokens: int = 4000,
        temperature: float = 0.1,
        reasoning_effort: str | None = None,
        provider_name: str | None = None,
        token_tracker=None,
        usage_type: str = "memory_compaction",
        model_role: str = "main",
        fallback_provider: LLMProvider | None = None,
        fallback_model: str | None = None,
        fallback_provider_name: str | None = None,
        fallback_generation: Any | None = None,
        fallback_model_role: str = "main",
    ):
        self.provider = provider
        self.model = model
        self.memory = memory_store
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.provider_name = provider_name
        self.token_tracker = token_tracker
        self.usage_type = usage_type
        self.model_role = model_role
        self.fallback_provider = fallback_provider
        self.fallback_model = fallback_model
        self.fallback_provider_name = fallback_provider_name
        self.fallback_generation = fallback_generation
        self.fallback_model_role = fallback_model_role

    def compact(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同步包装：在线程池中调用异步压缩。"""
        return run_sync(self.compact_async(history))

    async def compact_async(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compress history[:-K]; write episode + memory + user; return history[-K:]."""
        if len(history) <= self.K:
            return history

        old = history[: -self.K]
        recent = history[-self.K :]
        await self._compact_messages(old)
        self.memory.append_compact_marker(active_history=recent)
        logger.info(f"[Compacted: {len(old)} turns -> today episode + MEMORY updated]")
        return recent

    def compact_startup(self, history: list[dict[str, Any]]) -> None:
        """启动时全量压缩未归档历史，不保留最近 K 条。"""
        run_sync(self.compact_startup_async(history))

    async def compact_startup_async(self, history: list[dict[str, Any]]) -> None:
        """Archive all unarchived startup history without keeping recent turns."""
        if len(history) < 2:
            return
        await self._compact_messages(history)
        self.memory.append_compact_marker(active_history=[])
        logger.info(f"[Startup compacted: {len(history)} unarchived turns -> MEMORY updated]")

    async def _compact_messages(self, messages: list[dict[str, Any]]) -> None:
        """核心压缩逻辑：构建压缩 prompt → 调用模型 → 解析标签 → 写回记忆。"""
        # 构建压缩 prompt：将旧对话和当前记忆一起传入模型
        prompt = _PROMPT_TEMPLATE.format(
            old_conversation=_messages_to_text(messages),
            current_memory=self.memory.read_memory() or "(空)",
            current_user=self.memory.read_user() or "(空)",
            today_episode=self.memory.read_today_episode() or "(空)",
            now_hhmm=datetime.now(_UTC8).strftime("%H:%M"),
        )
        actual_model = self.model
        actual_provider_name = self.provider_name
        actual_model_role = self.model_role
        try:
            resp = await self.provider.chat(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
                messages=[{"role": "user", "content": prompt}],
                tools=None,
            )
        except Exception as exc:
            # 次模型失败 → 尝试 fallback 到主模型
            if not (self.fallback_provider and self.fallback_model):
                raise
            logger.warning(
                "memory compaction fallback: {} / {} -> {} because {}",
                self.provider_name,
                self.model,
                self.fallback_model,
                exc,
            )
            generation = self.fallback_generation
            actual_model = self.fallback_model
            actual_provider_name = self.fallback_provider_name
            actual_model_role = self.fallback_model_role
            resp = await self.fallback_provider.chat(
                model=self.fallback_model,
                max_tokens=min(self.max_tokens, int(getattr(generation, "max_tokens", self.max_tokens) or self.max_tokens)),
                temperature=getattr(generation, "temperature", self.temperature),
                reasoning_effort=getattr(generation, "reasoning_effort", self.reasoning_effort),
                messages=[{"role": "user", "content": prompt}],
                tools=None,
            )
        if self.token_tracker and resp.usage:
            self.token_tracker.record(
                actual_model,
                resp.usage,
                provider=actual_provider_name,
                usage_type=self.usage_type,
                model_role=actual_model_role,
            )
        text = resp.content or ""

        if episode := _extract("episode", text):
            self.memory.append_episode(episode)
        if new_memory := _extract("updated_memory", text):
            self.memory.write_memory(new_memory)
        if new_user := _extract("updated_user", text):
            self.memory.write_user(new_user)
