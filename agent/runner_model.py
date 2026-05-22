"""agent/runner_model.py

ModelCaller：对模型进行实际 API 调用并处理一次性 fallback。

负责：
  - 将 runner.provider.chat / chat_stream 封装为统一入口
  - 主模型失败时自动切换到 fallback 模型重试一次
  - 是否流式输出由 emit 是否为 None 决定
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from .providers import LLMProvider
from .runtime import events as runtime_events


StreamEmitter = Callable[[dict[str, Any]], Awaitable[None]]


class ModelCaller:
    """模型调用封装层，支持一次性主/次模型 fallback。"""

    def __init__(self, runner) -> None:
        self.runner = runner  # 引用属主 AgentRunner，从中取模型配置和 fallback 配置

    async def ask(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        emit: StreamEmitter | None,
    ):
        """发起一次模型调用；失败时如果配置了 fallback 则切换重试一次。"""
        # on_delta：流式输出回调，将 token delta 实时推送到前端
        async def on_delta(delta: str) -> None:
            if emit:
                await emit({"event": "message_delta", "delta": delta})

        runner = self.runner
        try:
            # 记录本次调用使用的模型信息（用于 token 计账）
            runner._last_model_call = {
                "model": runner.model,
                "provider": runner.provider_name,
                "model_role": runner.model_role,
                "route_reason": runner.route_reason,
                "used_fallback": False,
            }
            return await self._call_provider(
                provider=runner.provider,
                model=runner.model,
                max_tokens=runner.max_tokens,
                temperature=runner.temperature,
                reasoning_effort=runner.reasoning_effort,
                messages=messages,
                tools=tools,
                emit=emit,
                on_delta=on_delta,
            )
        except Exception as exc:
            if not (runner.fallback_provider and runner.fallback_model):
                raise
            logger.warning(
                "model route fallback: {} / {} -> {} because {}",
                runner.provider_name,
                runner.model,
                runner.fallback_model,
                exc,
            )
            if emit:
                await emit(runtime_events.model_route_fallback(
                    from_model=runner.model,
                    to_model=runner.fallback_model,
                    reason=str(exc),
                    usage_type=runner.usage_type,
                ))
            generation = runner.fallback_generation
            runner._last_model_call = {
                "model": runner.fallback_model,
                "provider": runner.fallback_provider_name,
                "model_role": runner.fallback_model_role,
                "route_reason": f"{runner.route_reason}:fallback",
                "used_fallback": True,
            }
            return await self._call_provider(
                provider=runner.fallback_provider,
                model=runner.fallback_model,
                max_tokens=min(runner.max_tokens, int(getattr(generation, "max_tokens", runner.max_tokens) or runner.max_tokens)),
                temperature=getattr(generation, "temperature", runner.temperature),
                reasoning_effort=getattr(generation, "reasoning_effort", runner.reasoning_effort),
                messages=messages,
                tools=tools,
                emit=emit,
                on_delta=on_delta,
            )

    @staticmethod
    async def _call_provider(
        *,
        provider: LLMProvider,
        model: str,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        emit: StreamEmitter | None,
        on_delta,
    ):
        if emit:
            return await provider.chat_stream(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                on_content_delta=on_delta,
            )
        return await provider.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
