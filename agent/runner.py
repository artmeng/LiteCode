"""agent/runner.py

AgentRunner：单轮 LLM 调用与工具执行引擎。

负责一次完整的 turn 执行：
  1. 调用 _ask_model 获取模型响应（带上下文治理管道）
  2. 若模型请求工具，就并发 / 串行执行工具并将结果回填 history
  3. 若模型直接回复，处理空响应 / 截断续写 / todo 未完成 nudge
  4. 触发必要时的记忆压缩 + checkpoint 维护
工具并发执行规则：read_only && !exclusive 的工具组用 asyncio.gather 并发。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from .control import ClarificationAssessment, TurnPaused, parse_pause_result
from .providers import LLMProvider, ToolCallRequest
from .providers.base import is_truncated, run_sync
from .runner_model import ModelCaller
from .tools.registry import ToolRegistry


StreamEmitter = Callable[[dict[str, Any]], Awaitable[None]]


# —— 上下文治理 / 错误恢复参数 ——
_SHRINK_KEEP_RECENT = 10              # 最近 N 条工具消息保留原文
_SHRINK_MIN_BYTES = 1500              # 小于此字节的工具结果不动
_TOOL_RESULT_BUDGET = 8000            # 单条工具结果硬上限
_TOOL_RESULT_HEAD = _TOOL_RESULT_BUDGET - 200
_TOOL_RESULT_TAIL = 200
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_ASK_GUARD_BLOCK = (
    "Error: Ask Guard requires `ask_user` before this high-impact action. "
    "Use read-only tools if needed, then ask the user to resolve the ambiguity."
)


class AgentRunner:
    """单轮执行引擎。

    可同时服务主 Agent、子代理、Team 队友等多种角色，
    通过不同的 usage_type / system_prompt / registry 区分。
    """
    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        registry: ToolRegistry,
        system_prompt: str,
        max_tokens: int = 20000,
        temperature: float = 0.1,
        reasoning_effort: str | None = None,
        provider_name: str | None = None,
        model_role: str = "main",
        route_reason: str = "",
        fallback_provider: LLMProvider | None = None,
        fallback_model: str | None = None,
        fallback_provider_name: str | None = None,
        fallback_generation: Any | None = None,
        fallback_model_role: str = "main",
        usage_type: str = "main_agent",
        memory_store=None,
        token_tracker=None,
        compactor=None,
        todo_store=None,
        control_manager=None,
        max_context: int = 200_000,
        compact_threshold: float = 0.7,
        max_turns: int | None = None,
    ):
        """初始化执行引擎，注入所有依赖。

        provider / model：调用 LLM 的 provider 实例与模型 id。
        fallback_*：主模型失败时的降级配置。
        memory_store：写 history.jsonl / checkpoint 用。
        token_tracker：记录每次调用的 token 消耗。
        compactor：触发记忆压缩的执行器。
        todo_store：todolist 状态，runner 用它判断任务是否全部完成。
        control_manager：Ask / Plan 权限状态机。
        max_context / compact_threshold：触发压缩的 token 上限与比例阈值。
        max_turns：单次 step_async 最多允许的 LLM 调用轮次。
        """
        self.provider = provider
        self.model = model
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self.provider_name = provider_name
        self.model_role = model_role
        self.route_reason = route_reason
        self.fallback_provider = fallback_provider
        self.fallback_model = fallback_model
        self.fallback_provider_name = fallback_provider_name
        self.fallback_generation = fallback_generation
        self.fallback_model_role = fallback_model_role
        self._last_model_call = {
            "model": model,
            "provider": provider_name,
            "model_role": model_role,
            "route_reason": route_reason,
            "used_fallback": False,
        }
        self.usage_type = usage_type
        self.memory_store = memory_store
        self.token_tracker = token_tracker
        self.compactor = compactor
        self.todo_store = todo_store
        self.control_manager = control_manager
        self.max_context = max_context
        self.compact_threshold = compact_threshold
        self.max_turns = max_turns

    def step(self, history: list[dict[str, Any]]) -> str:
        """同步执行一轮完整 turn，会就地修改 history。"""
        return run_sync(self.step_async(history))

    async def step_stream(
        self,
        history: list[dict[str, Any]],
        emit: StreamEmitter,
        *,
        turn_id: str | None = None,
    ) -> str:
        """异步执行一轮完整 turn，并向前端流式推送 UI 事件。"""
        reply = await self.step_async(history, emit=emit, turn_id=turn_id)
        await emit({"event": "assistant_done", "content": reply})
        return reply

    async def step_async(
        self,
        history: list[dict[str, Any]],
        emit: StreamEmitter | None = None,
        *,
        turn_id: str | None = None,
    ) -> str:
        """核心异步执行循环：调用模型 → 执行工具 → 收敛到最终回复。

        每次迭代调用 _ask_model，根据响应类型分三条路：
        1. should_execute_tools=True：执行工具批次，写 checkpoint，继续循环。
        2. 空响应：注入 nudge，最多重试 _MAX_EMPTY_RETRIES 次。
        3. 截断响应：注入续写 prompt，最多恢复 _MAX_LENGTH_RECOVERIES 次。
        全部 tool_calls 收敛后，检查 todo 未完成 nudge，最后触发记忆压缩。
        """
        turns = 0
        final_parts: list[str] = []  # 收集截断续写时各段文本，最终 join 为完整回复
        empty_retries = 0
        length_retries = 0
        # 在 turn 开始前静态评估用户意图，判断是否需要先问再做（Ask Guard）
        clarification = self._assess_clarification(history)
        # 进入 turn 时先记一次快照，防止 LLM 还没回应就被杀
        if self.memory_store is not None:
            self.memory_store.write_checkpoint(history)
        while True:
            # —— 防止无限循环：达到 max_turns 上限则强制中止 ——
            if self.max_turns is not None and turns >= self.max_turns:
                reply = f"（达到 max_turns={self.max_turns} 上限，未办妥；history 中已有部分进展）"
                message = {"role": "assistant", "content": reply}
                if turn_id:
                    message["turn_id"] = turn_id
                history.append(message)
                if self.memory_store:
                    extra = {"turn_id": turn_id} if turn_id else None
                    self.memory_store.append_history("assistant", reply, extra=extra)
                    self.memory_store.clear_checkpoint()
                return reply
            turns += 1

            # —— 调用 LLM，内部完成上下文治理（配对/截断/摘要）和 system prompt 注入 ——
            response = await self._ask_model(history, emit, clarification=clarification)
            if response.usage:
                call_meta = self._last_model_call
                # 记录本次调用 token 消耗到账本，用于压缩阈值判断
                if self.token_tracker:
                    self.token_tracker.record(
                        str(call_meta.get("model") or self.model),
                        response.usage,
                        provider=str(call_meta.get("provider") or self.provider_name or "unknown"),
                        usage_type=self.usage_type,
                        model_role=str(call_meta.get("model_role") or self.model_role),
                    )
                # 向前端推送上下文用量，驱动 WebUI 进度环更新
                if emit:
                    await emit({
                        "event": "context_usage",
                        "used": _context_used_from_usage(response.usage),
                        "max": self.max_context,
                        "threshold": int(self.max_context * self.compact_threshold),
                        "usage_type": self.usage_type,
                        "model_role": call_meta.get("model_role"),
                        "model": call_meta.get("model"),
                        "provider": call_meta.get("provider"),
                    })
            # 把本次模型调用的元数据写入历史日志，便于后续统计和调试
            if self.memory_store:
                last_user = next((m for m in reversed(history) if m.get("role") == "user"), None)
                user_input = str(last_user.get("content", ""))[:500] if last_user else ""
                ai_output = str(response.content or "")[:500]
                cmd_event = None
                # 若用户输入是斜杠命令，提取命令名作为事件标记（如 "/mode"）
                if user_input.startswith("/"):
                    cmd_event = user_input.split()[0]
                input_tokens = int(response.usage.get("input", 0) or 0) if response.usage else 0
                output_tokens = int(response.usage.get("output", 0) or 0) if response.usage else 0
                self.memory_store.append_history(
                    "model_call",
                    f"{self.model} call: input={input_tokens} output={output_tokens}",
                    extra={
                        "type": "model_call",
                        "model": self._last_model_call.get("model") or self.model,
                        "provider": self._last_model_call.get("provider") or self.provider_name,
                        "model_role": self._last_model_call.get("model_role") or self.model_role,
                        "route_reason": self._last_model_call.get("route_reason") or self.route_reason,
                        "used_fallback": bool(self._last_model_call.get("used_fallback")),
                        "usage_type": self.usage_type,
                        "user_input": user_input,
                        "ai_output": ai_output,
                        "command_event": cmd_event,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        **({"turn_id": turn_id} if turn_id else {}),
                    },
                )

            # —— 分支 1：模型返回工具调用 → 执行工具批次后继续循环 ——
            if response.should_execute_tools:
                empty_retries = 0  # 有工具调用则重置空响应计数
                length_retries = 0
                assistant_content = response.content or ""
                if assistant_content:  # 工具调用前可能附带思考文本，先缓存
                    final_parts.append(assistant_content)
                assistant_message = {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": [call.to_openai_tool_call() for call in response.tool_calls],
                }
                if turn_id:
                    assistant_message["turn_id"] = turn_id
                if response.reasoning_content is not None:
                    assistant_message["reasoning_content"] = response.reasoning_content
                elif self._reasoning_enabled():
                    assistant_message["reasoning_content"] = ""
                if response.thinking_blocks:
                    assistant_message["thinking_blocks"] = response.thinking_blocks
                history.append(assistant_message)
                try:
                    tool_messages = await self._execute_tool_calls(response.tool_calls, emit, clarification=clarification)
                except TurnPaused as pause:
                    # ask_user / propose_plan 触发暂停：把占位 tool result 写入 history 和 checkpoint，
                    # 再向前端推送交互卡与 turn_paused 事件，然后重新抛出让上层挂起整个 turn
                    history.extend(pause.tool_messages)
                    if self.memory_store is not None:
                        self.memory_store.write_checkpoint(history)
                    if emit:
                        # 从占位 tool_messages 中找到与本次交互对应的那条结果消息
                        # （通过 parent_call_id 匹配），推送 tool_result 事件让前端
                        # 知道工具已"完成"（内容是占位文本），并终止查找。
                        for msg in pause.tool_messages:
                            if msg.get("tool_call_id") == pause.interaction.get("parent_call_id"):
                                await emit({
                                    "event": "tool_result",
                                    "id": msg.get("tool_call_id"),
                                    "name": msg.get("name"),
                                    "summary": msg.get("content"),
                                })
                                break
                        # 推送 ask_request 或 plan_draft 等交互卡事件，前端据此渲染
                        # AskCard / PlanCard，等待用户输入或审批。
                        await emit(_control_interaction_event(pause.interaction))
                        # 通知前端当前 turn 已进入挂起状态，禁用输入框并展示等待 UI。
                        await emit({"event": "turn_paused", "interaction": pause.interaction})
                    # 重新抛出 TurnPaused，让上层（step_async 调用方）感知暂停并
                    # 挂起整个 turn，等用户响应后再恢复执行。
                    raise
                history.extend(tool_messages)
                # 工具批次刚完成 → 此刻 history 处于"tool_calls 与 tool 消息严格配对"的一致点，
                # 写入 checkpoint；如果 LLM 下一次调用前进程被杀，重启可从此处续命。
                if self.memory_store is not None:
                    self.memory_store.write_checkpoint(history)
                continue

            # —— 分支 2：模型无工具调用 → 尝试收敛为最终回复 ——
            reply = response.content or ""

            # —— 空响应救援 ——
            if not reply.strip() and not response.tool_calls:
                if empty_retries < _MAX_EMPTY_RETRIES:
                    empty_retries += 1
                    history.append({
                        "role": "user",
                        "content": "（上一轮无任何输出，请继续推进或给出最终答复）",
                    })
                    if emit:
                        await emit({
                            "event": "tool_error",
                            "name": "_empty_response",
                            "message": f"empty response, retry {empty_retries}/{_MAX_EMPTY_RETRIES}",
                        })
                    continue

            # —— 截断续写（模型正在输出一段很长的内容，输出到一半被强制截停） ——
            if is_truncated(response.finish_reason) and length_retries < _MAX_LENGTH_RECOVERIES:
                length_retries += 1
                if reply:
                    final_parts.append(reply)
                    message = {"role": "assistant", "content": reply}
                    if turn_id:
                        message["turn_id"] = turn_id
                    history.append(message)
                history.append({
                    "role": "user",
                    "content": "（上一轮被 max_tokens 截断，请从中断处续写，不要重复已输出内容）",
                })
                if emit:
                    await emit({
                        "event": "tool_error",
                        "name": "_length_truncation",
                        "message": f"truncated, continuing {length_retries}/{_MAX_LENGTH_RECOVERIES}",
                    })
                continue

            # Ask Guard：模型给出实质回复但未主动调用 ask_user，此处强制暂停
            if clarification.required and reply.strip():
                await self._pause_for_clarification(history, clarification, emit, turn_id=turn_id)

            # Plan 模式：普通回复被包装为 PlanCard 并暂停，等待用户批准或评论
            if self._must_pause_for_plan(reply):
                await self._pause_for_plan(history, reply, emit, turn_id=turn_id)

            final_parts.append(reply)
            final_reply = "".join(final_parts)  # 合并所有续写片段为完整回复
            assistant_message = {"role": "assistant", "content": reply}
            if turn_id:
                assistant_message["turn_id"] = turn_id
            if response.reasoning_content is not None:
                assistant_message["reasoning_content"] = response.reasoning_content
            elif self._reasoning_enabled():
                assistant_message["reasoning_content"] = ""
            if response.thinking_blocks:
                assistant_message["thinking_blocks"] = response.thinking_blocks
            history.append(assistant_message)
            if self.memory_store:
                extra = {"turn_id": turn_id} if turn_id else None
                self.memory_store.append_history("assistant", final_reply, extra=extra)

            # —— Todo 未完成 nudge：有未完成任务则注入提示并继续循环，直到全部 completed ——
            if self.todo_store and self.todo_store.todos:
                unfinished = [t for t in self.todo_store.todos if t["status"] != "completed"]
                if unfinished:
                    nudge = (
                        "差事尚未办妥，以下任务仍未完成，请按计划继续执行，"
                        "并按规矩更新 todolist 状态：\n" + _render_todos(unfinished)
                    )
                    logger.info(f"\n[计划尚未办妥，继续执行...]\n{_render_todos(self.todo_store.todos)}\n")
                    history.append({"role": "user", "content": nudge})
                    continue
                logger.info(f"\n[最终计划状态 - 全部办妥]\n{_render_todos(self.todo_store.todos)}\n")
                self.todo_store.todos = []

            await self._maybe_compact(history)
            # turn 正常落地 → 清掉 checkpoint
            if self.memory_store is not None:
                self.memory_store.clear_checkpoint()
            return final_reply

    async def _ask_model(
        self,
        history: list[dict[str, Any]],
        emit: StreamEmitter | None,
        *,
        clarification: ClarificationAssessment | None = None,
    ):
        """对 history 执行三步治理管道后调用 LLM，返回模型响应对象。

        治理顺序：_pair_tool_calls → _shrink_old_tool_results → _cap_tool_result。
        若存在 control_manager，会在 system prompt 末尾追加权限模式说明和 Ask Guard 指令，
        并由 control_manager 决定向模型暴露哪些工具定义。
        """
        governed = self._pair_tool_calls(history) # 确保工具调用和工具结果配对
        governed = self._shrink_old_tool_results(governed) # 占位符替换近期会话之外的工具结果
        governed = self._cap_tool_result(governed) # 裁剪工具输出太长的文本，诱导工具指定读取偏移量，不要全文读取
        system_prompt = self.system_prompt
        if self.control_manager is not None:
            system_prompt = f"{system_prompt}\n\n---\n\n{self.control_manager.system_prompt()}"
            if clarification and clarification.required: # 决定是否追加 Ask Guard 指令到 system prompt
                system_prompt = f"{system_prompt}\n\n---\n\n{clarification.prompt()}"
            # 按当前模式过滤工具列表，让模型只看到被允许的工具。比如plan模式只留 ask_user + propose_plan + read_only=True 的只读工具；写工具、子代理、Team 写操作全部移除
            tool_definitions = self.control_manager.tool_definitions(self.registry)
        else:
            tool_definitions = self.registry.get_definitions()
        messages = [
            {"role": "system", "content": system_prompt},
            *governed,
        ]

        return await ModelCaller(self).ask(
            messages=messages,
            tools=tool_definitions,
            emit=emit,
        )

    @staticmethod
    def _pair_tool_calls(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """确保 assistant tool_calls 与后续 tool 消息严格一一对应。

        丢弃孤立 tool 消息，并为缺失的 tool 回复填入占位符，
        防止半完成 turn（执行中断、压缩切割不当）在下次 API 调用时
        触发"tool messages 不足"报错。
        """
        cleaned: list[dict[str, Any]] = []
        expected: list[tuple[str, str]] = []

        def flush_expected() -> None:
            for tid, tname in expected:
                cleaned.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "name": tname,
                    "content": "（工具执行被中断）",
                })
            expected.clear()

        for msg in history:
            role = msg.get("role")
            if role == "tool":
                tid = msg.get("tool_call_id")
                idx = next((i for i, (eid, _) in enumerate(expected) if eid == tid), None)
                if idx is None:
                    continue
                cleaned.append(msg)
                expected.pop(idx)
                continue
            flush_expected()
            cleaned.append(msg)
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    expected.append((tc.get("id") or "", fn.get("name", "")))
        flush_expected()
        return cleaned

    @staticmethod
    def _content_text_size(content: Any) -> int:
        """按 text 实际长度估算消息体积；list 形式只算 text block，跳过 base64 image_url。"""
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(
                len(str(b.get("text", "")))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return len(str(content or ""))

    @staticmethod
    def _cap_tool_result(
        history: list[dict[str, Any]],
        per_call_limit: int = _TOOL_RESULT_BUDGET,
    ) -> list[dict[str, Any]]:
        """单条工具结果硬截断，留头尾。仅作用于 role=tool；user 多模态原样保留。"""
        out: list[dict[str, Any]] = []
        for msg in history:
            if msg.get("role") == "tool":
                text = str(msg.get("content", ""))
                if len(text) > per_call_limit:
                    head = text[:_TOOL_RESULT_HEAD]
                    tail = text[-_TOOL_RESULT_TAIL:]
                    msg = {
                        **msg,
                        "content": (
                            f"{head}\n...[已截断，原文共 {len(text)} 字符，仅展示前后各部分]"
                            f"如需完整内容，请使用 read_file 并指定 offset/limit 参数分段读取，或用 grep 定位目标行。...\n{tail}"
                        ),
                    }
            out.append(msg)
        return out

    @staticmethod
    def _shrink_old_tool_results(
        history: list[dict[str, Any]],
        keep_recent: int = _SHRINK_KEEP_RECENT,
    ) -> list[dict[str, Any]]:
        """把 keep_recent 之外的大体积工具消息替换为一行摘要。仅 role=tool；user 多模态不动。"""
        cutoff = max(0, len(history) - keep_recent)
        out: list[dict[str, Any]] = []
        for i, msg in enumerate(history):
            if (
                msg.get("role") == "tool"
                and i < cutoff
                and AgentRunner._content_text_size(msg.get("content")) > _SHRINK_MIN_BYTES
            ):
                name = msg.get("name") or msg.get("tool_call_id") or "tool"
                size = AgentRunner._content_text_size(msg.get("content"))
                out.append({**msg, "content": f"[已摘要] {name} → 原文 {size} 字符已省略"})
            else:
                out.append(msg)
        return out

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCallRequest],
        emit: StreamEmitter | None,
        *,
        clarification: ClarificationAssessment | None = None,
    ) -> list[dict[str, Any]]:
        """批量执行本轮所有工具调用，返回对应的 tool 消息列表。

        并发策略：连续的 concurrency_safe 工具组用 asyncio.gather 并发执行；
        non-concurrency_safe 或 exclusive 工具串行执行。
        任意工具返回暂停信号时，立即抛出 TurnPaused，由调用方处理。
        """
        async def _report_tool_error(call: ToolCallRequest, err_msg: str) -> None:
            # 工具执行异常时向前端推送 tool_error 事件，供 WebUI 展示错误气泡；
            # emit 为 None（CLI 模式）时静默跳过，不影响 tool result 写回 history
            if emit:
                await emit({
                    "event": "tool_error",
                    "id": call.id,
                    "name": call.name,
                    "message": err_msg,
                })

        async def _run_and_collect(call: ToolCallRequest) -> str:
            try:
                return await self._run_tool(call, emit, clarification=clarification)
            except Exception as exc:
                err_msg = str(exc)
                logger.exception(f"工具执行失败: {call.name}")
                await _report_tool_error(call, err_msg)
                return f"Error: {err_msg}"

        async def _run_serial(call: ToolCallRequest) -> None:
            """串行执行单个工具：通知前端 → 执行 → 写结果 → 检查暂停 → 推送结果。"""
            await self._emit_tool_call(call, emit)
            content = await _run_and_collect(call)
            results_by_id[call.id] = content
            self._maybe_pause_for_control(content, tool_calls, results_by_id)
            if not content.startswith("Error:"):
                await self._emit_tool_result(call, content, emit)

        results_by_id: dict[str, str] = {}  # call.id -> 工具返回文本，最终组装为 tool 消息
        i = 0
        while i < len(tool_calls):
            call = tool_calls[i]
            tool = self.registry.get(call.name)

            # —— 并发分组：将连续的 concurrency_safe 工具攒成一组一次性并发执行 ——
            if tool is not None and tool.concurrency_safe:
                group: list[ToolCallRequest] = []
                # 向后扫描，把紧邻的并发安全工具全部纳入同一组
                while i < len(tool_calls):
                    candidate = tool_calls[i]
                    candidate_tool = self.registry.get(candidate.name)
                    if candidate_tool is None or not candidate_tool.concurrency_safe:
                        break
                    group.append(candidate)
                    i += 1

                if len(group) > 1:
                    # 多个工具并发：先批量通知前端「工具开始」，再 gather 并发执行
                    names = ", ".join(item.name for item in group)
                    logger.info(f"[并发执行 {len(group)} 个工具]: {names}")
                    for item in group:
                        await self._emit_tool_call(item, emit) # 向前端通知
                    tasks = [self._run_tool(item, emit, clarification=clarification) for item in group]
                    gathered = await asyncio.gather(*tasks, return_exceptions=True)
                    # 逐一收集结果；异常单独上报，不阻断其余工具的结果写入
                    for item, raw in zip(group, gathered):
                        if isinstance(raw, Exception):
                            err_msg = str(raw)
                            results_by_id[item.id] = f"Error: {err_msg}"
                            await _report_tool_error(item, err_msg)
                        else:
                            results_by_id[item.id] = raw
                            await self._emit_tool_result(item, raw, emit)
                else:
                    # 组内只有一个工具，退化为串行执行（仍走暂停检查）
                    await _run_serial(group[0])
                continue

            # —— 非并发工具：严格串行执行，每步均检查暂停信号 ——
            await _run_serial(call)
            i += 1

        # 按原始 tool_calls 顺序组装 tool 消息，保证与 assistant tool_calls 严格配对
        return [
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": results_by_id.get(call.id, ""),
            }
            for call in tool_calls
        ]

    async def _run_tool(
        self,
        call: ToolCallRequest,
        emit: StreamEmitter | None = None,
        *,
        clarification: ClarificationAssessment | None = None,
    ) -> str:
        """执行单个工具调用，返回工具结果字符串。

        执行前依次经过三道门禁：
        1. Plan 模式白名单检查（非只读工具直接拒绝）。
        2. Ask Guard 检查（高影响歧义未确认时阻断写工具）。
        3. PermissionManager 权限评估（ask_before_edit 模式下高风险操作触发审批）。
        通过后在 asyncio.to_thread 中同步执行，requires_runtime_context 工具额外注入 loop/emit。
        """
        if self.control_manager is not None and not self.control_manager.is_tool_allowed(call.name, self.registry):
            return (
                f"Error: 工具 '{call.name}' 在 Plan 模式下不可用。"
                "计划批准前只允许使用只读工具以及 ask_user/propose_plan。"
            )
        if clarification and clarification.required and self._ask_guard_blocks_tool(call.name):
            return _ASK_GUARD_BLOCK
        if self.control_manager is not None:
            decision = self.control_manager.assess_permission(call.name, call.arguments, self.registry)
            if decision.requires_approval:
                return self.control_manager.permission_approval_result(decision, parent_call_id=call.id)
            if not decision.allowed:
                return f"Error: 权限拒绝 {call.name}: {decision.reason}"
        tool = self.registry.get(call.name)
        if emit and tool is not None and getattr(tool, "requires_runtime_context", False):
            loop = asyncio.get_running_loop()
            return await asyncio.to_thread(
                self.registry.execute, call.name, call.arguments,
                emit=emit, loop=loop, parent_call_id=call.id,
            )
        return await asyncio.to_thread(self.registry.execute, call.name, call.arguments)

    def _assess_clarification(self, history: list[dict[str, Any]]) -> ClarificationAssessment:
        """评估当前 history 是否需要触发 Ask Guard，失败时降级为不触发。"""
        if self.control_manager is None:
            return ClarificationAssessment()
        try:
            return self.control_manager.assess_clarification(history)
        except Exception as exc:
            logger.warning(f"clarification assessment failed: {exc}")
            return ClarificationAssessment()

    def _ask_guard_blocks_tool(self, name: str) -> bool:
        """判断 Ask Guard 是否应拦截当前工具。ask_user/propose_plan 和只读工具不拦截。"""
        if name in {"ask_user", "propose_plan"}:
            return False
        tool = self.registry.get(name)
        if tool is None:
            return False
        return not bool(getattr(tool, "read_only", False))

    async def _pause_for_clarification(
        self,
        history: list[dict[str, Any]],
        clarification: ClarificationAssessment,
        emit: StreamEmitter | None,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Ask Guard 触发后：创建 ask interaction，写 checkpoint，推送事件，抛出 TurnPaused。"""
        if self.control_manager is None:
            return
        interaction = self.control_manager.create_ask(
            questions=clarification.questions,
            context=f"Ask Guard: {clarification.reason}",
        )
        message = {
            "role": "assistant",
            "content": "需要先确认关键取舍，已触发 Ask Guard。",
        }
        if turn_id:
            message["turn_id"] = turn_id
        history.append(message)
        if self.memory_store is not None:
            self.memory_store.write_checkpoint(history)
        payload = interaction.to_dict()
        if emit:
            await emit(_control_interaction_event(payload))
            await emit({"event": "turn_paused", "interaction": payload})
        raise TurnPaused(interaction=payload, tool_messages=[])

    async def _pause_for_plan(
        self,
        history: list[dict[str, Any]],
        reply: str,
        emit: StreamEmitter | None,
        *,
        turn_id: str | None = None,
    ) -> None:
        """Plan 模式下，将模型回复解析为 PlanCard，写 checkpoint，推送事件，抛出 TurnPaused。"""
        if self.control_manager is None:
            return
        interaction = self.control_manager.create_plan_from_text(reply)
        message = {"role": "assistant", "content": reply}
        if turn_id:
            message["turn_id"] = turn_id
        history.append(message)
        if self.memory_store is not None:
            self.memory_store.write_checkpoint(history)
        payload = interaction.to_dict()
        if emit:
            await emit(_control_interaction_event(payload))
            await emit({"event": "turn_paused", "interaction": payload})
        raise TurnPaused(interaction=payload, tool_messages=[])

    def _must_pause_for_plan(self, reply: str) -> bool:
        """判断当前是否处于 Plan 模式且需要将模型最终回复强制转为 PlanCard。"""
        return bool(
            self.control_manager is not None
            and self.control_manager.should_enforce_plan_final()
        )

    def _maybe_pause_for_control(
        self,
        content: str,
        tool_calls: list[ToolCallRequest],
        results_by_id: dict[str, str],
    ) -> None:
        """检测工具结果是否含暂停信号；是则构造 tool 消息列表并抛出 TurnPaused。"""
        interaction = parse_pause_result(content)
        if interaction is None:
            return
        tool_messages = self._tool_messages_for_pause(tool_calls, results_by_id, interaction)
        raise TurnPaused(interaction=interaction, tool_messages=tool_messages)

    @staticmethod
    def _tool_messages_for_pause(
        tool_calls: list[ToolCallRequest],
        results_by_id: dict[str, str],
        interaction: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """为 TurnPaused 构建完整的 tool 消息列表。

        触发暂停的那条工具结果替换为"等待用户回复"占位，
        尚未执行的工具结果替换为"跳过"占位，已完成的保留原文。
        """
        messages = []
        # parent_call_id 是触发暂停的那条 ask_user/propose_plan 调用的 id，用于精确定位
        current_id = str(interaction.get("parent_call_id") or "")
        for call in tool_calls:
            content = results_by_id.get(call.id)
            if content and parse_pause_result(content):
                # 触发暂停的那条工具结果含有 __CONTROL_PAUSE__ 信号，替换为等待占位，避免把毒令牌写入 history
                content = f"等待用户回复 ({interaction.get('kind')}:{interaction.get('id')})"
            elif content is None:
                # 同批次中还没来得及执行的工具（在暂停工具之后排队的），统一标记为跳过
                content = "因 turn 暂停等待用户输入而跳过"
            # 已完成且非暂停信号的工具结果保留原文
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": content,
            })
            if current_id and call.id == current_id:
                # 找到触发暂停的那条调用后清空 current_id，后续调用不再做特殊处理
                current_id = ""
        return messages

    @staticmethod
    async def _emit_tool_call(call: ToolCallRequest, emit: StreamEmitter | None) -> None:
        """向前端推送 tool_call 事件（工具名、参数），用于 UI 展示调用详情。"""
        if emit:
            await emit({
                "event": "tool_call",
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            })

    async def _emit_tool_result(
        self,
        call: ToolCallRequest,
        content: str,
        emit: StreamEmitter | None,
    ) -> None:
        """向前端推送 tool_result 事件；update_todos 工具额外附带最新 todo 列表。"""
        if emit:
            payload: dict[str, Any] = {
                "event": "tool_result",
                "id": call.id,
                "name": call.name,
                "summary": _summarize_tool_result(content),
            }
            if call.name == "update_todos" and self.todo_store is not None:
                payload["todos"] = [
                    {"id": t["id"], "content": t["content"], "status": t["status"]}
                    for t in self.todo_store.todos
                ]
            await emit(payload)

    async def _maybe_compact(self, history: list[dict[str, Any]]) -> None:
        """若 token 用量超过压缩阈值，就地压缩 history 并更新记忆文件。"""
        if not (self.compactor and self.token_tracker):
            return
        if not self.token_tracker.should_compact(self.max_context, self.compact_threshold):
            return
        if hasattr(self.compactor, "compact_async"):
            history[:] = await self.compactor.compact_async(history)
        else:
            history[:] = await asyncio.to_thread(self.compactor.compact, history)

    def _reasoning_enabled(self) -> bool:
        """判断当前配置是否开启了 reasoning / thinking（CoT）模式。"""
        return bool(self.reasoning_effort and self.reasoning_effort.lower() not in {"none", "minimal", "minimum"})


_TODO_ICON = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _render_todos(todos: list[dict]) -> str:
    """将 todo 列表渲染为带状态图标的多行文本，用于 nudge 提示。"""
    lines = []
    for t in todos:
        icon = _TODO_ICON.get(t.get("status", "pending"), "[?]")
        lines.append(f"  {icon} {t.get('id')}. {t.get('content', '')}")
    return "\n".join(lines)


def _summarize_tool_result(content: str, limit: int = 560) -> str:
    """将工具结果压缩为单行摘要，超过 limit 字符时截断并加省略号，用于 UI 展示。"""
    text = " ".join(str(content or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _context_used_from_usage(usage: dict[str, int]) -> int:
    """从 usage 字典中计算已使用的上下文 token 数（含缓存读写）。"""
    input_tokens = int(usage.get("input", usage.get("prompt_tokens", 0)) or 0)
    cache_read = int(usage.get("cache_read", usage.get("cache_read_input_tokens", 0)) or 0)
    cache_create = int(usage.get("cache_create", usage.get("cache_creation_input_tokens", 0)) or 0)
    return input_tokens + cache_read + cache_create


def _control_interaction_event(interaction: dict[str, Any]) -> dict[str, Any]:
    """将 interaction 对象转为对应的 WebSocket 事件字典（ask_request 或 plan_draft）。"""
    event = "ask_request" if interaction.get("kind") == "ask" else "plan_draft"
    return {"event": event, "interaction": interaction}
