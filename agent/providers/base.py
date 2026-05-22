"""agent/providers/base.py

所有 LLM Provider 的抽象基类和共用数据结构。

- ToolCallRequest：模型请求调用工具的结构化表示
- LLMResponse：统一模型响应格式，包含 content / tool_calls / finish_reason / usage
- GenerationSettings：生成参数快照（max_tokens / temperature / reasoning_effort）
- LLMProvider：所有 provider 实现的抽象基类，定义 chat / chat_stream 接口
- run_sync：在非异步上下文中运行协程（Compactor 等同步包装使用）
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


ContentDelta = Callable[[str], Awaitable[None]]


@dataclass
class ToolCallRequest:
    """模型请求调用工具的结构体。"""
    id: str                    # 模型生成的工具调用 ID
    name: str                  # 工具名称
    arguments: dict[str, Any]  # 解析后的参数字典

    def to_openai_tool_call(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class LLMResponse:
    """统一模型响应格式，屏蔽不同 provider 的差异。"""
    content: str | None                          # 文本回复（可为 None、空字符串）
    tool_calls: list[ToolCallRequest] = field(default_factory=list)  # 工具调用列表
    finish_reason: str = "stop"                  # 停止原因：stop / tool_calls / length 等
    usage: dict[str, int] = field(default_factory=dict)             # Token 用量（input/output/cache_*）
    reasoning_content: str | None = None         # 思考内容（DeepSeek / Qwen 等支持）
    thinking_blocks: list[dict[str, Any]] | None = None  # Anthropic Extended Thinking 块

    @property
    def should_execute_tools(self) -> bool:
        """是否需要执行工具调用。"""
        return bool(self.tool_calls) and self.finish_reason in {"tool_calls", "stop"}


TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "model_max_tokens"})


def is_truncated(finish_reason: str | None) -> bool:
    return (finish_reason or "").lower() in TRUNCATED_FINISH_REASONS


@dataclass(frozen=True)
class GenerationSettings:
    """用于快照的生成参数。"""
    max_tokens: int = 20_000               # 最大输出 token
    temperature: float = 0.1              # 采样温度
    reasoning_effort: str | None = None   # 思考力度（部分 provider 支持）


class LLMProvider(ABC):
    """LLM Provider 抽象基类。

    所有具体 provider（OpenAI兴容、Anthropic、Bedrock）均继承此类，
    实现 chat 抽象方法，可选覆写 chat_stream 以支持流式输出。
    """
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        self.api_key = api_key
        self.api_base = api_base
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}
        self.generation = GenerationSettings()

    @abstractmethod
    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        ...

    async def chat_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        on_content_delta: ContentDelta | None = None,
    ) -> LLMResponse:
        response = await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        if response.content and on_content_delta:
            await on_content_delta(response.content)
        return response

    @staticmethod
    def anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        converted = []
        for tool in tools:
            if tool.get("type") == "function":
                converted.append(tool)
                continue
            converted.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return converted

    @staticmethod
    def openai_tools_to_anthropic(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        converted = []
        for tool in tools:
            fn = tool.get("function", tool)
            converted.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or fn.get("input_schema") or {
                    "type": "object",
                    "properties": {},
                },
            })
        return converted

    @staticmethod
    def parse_json_args(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or not value.strip():
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            try:
                import json_repair

                parsed = json_repair.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                logger.debug(f"JSON repair failed for input: {value[:100]}")
                return {}


_shared_loop = None


async def _sniffio_wrap(coro):
    """显式设置 sniffio asyncio 上下文，解决 PyCharm pydevd_nest_asyncio 拦截
    run_until_complete 后 sniffio.current_async_library_cvar 未正确设置的问题。
    """
    try:
        import sniffio
        token = sniffio.current_async_library_cvar.set("asyncio")
        try:
            return await coro
        finally:
            sniffio.current_async_library_cvar.reset(token)
    except (ImportError, AttributeError):
        return await coro


def run_sync(coro):
    """在非异步上下文中运行协程。

    安全处理：
    - 已有运行中的事件循环→报错（防止死锁）
    - 来自 asyncio.to_thread 子线程→用 asyncio.run 新建独立循环。
    - PyCharm 调试器兼容：_sniffio_wrap 显式设置 asyncio 上下文。
    """
    global _shared_loop
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _shared_loop is not None and _shared_loop.is_running():
            # 来自 asyncio.to_thread 的子线程，必须新建独立循环，否则会死锁
            return asyncio.run(_sniffio_wrap(coro))
        if _shared_loop is None or _shared_loop.is_closed():
            _shared_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_shared_loop)
        return _shared_loop.run_until_complete(_sniffio_wrap(coro))
    raise RuntimeError("Cannot run sync provider call inside a running event loop")
