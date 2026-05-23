from __future__ import annotations

"""Agent Team 协调器实现：队友生命周期、消息流与唤醒执行。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Any

from loguru import logger

from ..providers.base import run_sync
from ..subagents import SubagentSpec
from ..tools.registry import ToolRegistry
from . import events
from .bus import MessageBus
from .models import LEAD_ACTOR, TeamMember, TeamMessage, TeamStatus, new_id, now_ts, validate_member_name
from .store import TeamStore


StreamEmitter = Callable[[dict[str, Any]], Awaitable[None]]


_ROLE_AGENT_TYPES = {
    "coder": "neiguan_yingzao",
    "reviewer": "shangbao_dianbu",
    "researcher": "dongchang_tanshi",
    "reader": "sili_suitang",
    "runner": "xiaohuangmen",
}


def role_to_agent_type(role: str) -> str:
    """将角色名映射到默认子代理类型。"""
    return _ROLE_AGENT_TYPES.get(str(role or "").strip().lower(), "sili_suitang")


class TeamManager:
    """Agent Team 协调器：管理队友、消息总线与执行唤醒流程。"""

    def __init__(
        self,
        *,
        root,
        parent_registry: ToolRegistry,
        subagent_registry,
        runner_factory,
    ):
        self.store = TeamStore(root)
        self.bus = MessageBus(self.store)
        self.parent_registry = parent_registry
        self.subagent_registry = subagent_registry
        self.runner_factory = runner_factory
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()

    def payload(self) -> dict[str, Any]:
        """构建 Team 面板总览数据（成员、未读、最近消息、线程摘要计数）。"""
        config = self.store.load_config()
        members = []
        for member in self.store.list_members():
            item = member.to_dict()
            item["unread"] = self.bus.unread_count(member.name)
            item["recent_messages"] = [msg.to_dict() for msg in self.bus.recent(member.name, limit=5)]
            item["thread_count"] = len(self.store.read_thread(member.name))
            item["tools"] = self._tool_names_for_member(member)
            members.append(item)
        return {
            "config": config,
            "members": members,
            "leadUnread": self.bus.unread_count(LEAD_ACTOR),
            "leadInbox": [msg.to_dict() for msg in self.bus.recent(LEAD_ACTOR, limit=50)],
        }

    def member_payload(self, name: str) -> dict[str, Any]:
        """构建单个队友详情页数据。"""
        member = self._require_member(name)
        return {
            "member": {
                **member.to_dict(),
                "unread": self.bus.unread_count(member.name),
                "tools": self._tool_names_for_member(member),
            },
            "inbox": [msg.to_dict() for msg in self.bus.recent(member.name, limit=100)],
            "leadInbox": [msg.to_dict() for msg in self.bus.recent(LEAD_ACTOR, limit=100)],
            "thread": self._thread_summary(member.name),
        }

    def spawn_teammate(
        self,
        *,
        name: str,
        role: str,
        task: str | None = None,
        agent_type: str | None = None,
        sender: str = LEAD_ACTOR,
        emit: StreamEmitter | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        parent_call_id: str | None = None,
    ) -> str:
        """创建/唤回队友，并可选投递首个任务后立即唤醒执行。"""
        # 校验名称合法性（字母数字开头，不含保留词）
        safe_name = validate_member_name(name)

        # agent_type 未显式指定时，按 role 推导默认子代理类型
        resolved_agent_type = agent_type or role_to_agent_type(role)
        spec = self.subagent_registry.get(resolved_agent_type)
        if spec is None:
            # 未知 agent_type，直接返回错误，不写入任何状态
            return (
                f"Error: unknown agent_type '{resolved_agent_type}'. "
                f"Available: {self.subagent_registry.names(include_aliases=True)}"
            )

        # 查询是否已有同名队友
        existing = self.store.get_member(safe_name)
        if existing and existing.status == TeamStatus.SHUTDOWN.value:
            # shutdown 队友可被唤回：先在内存中恢复为 idle，再写入
            existing = existing.touch(status=TeamStatus.IDLE.value, last_error=None)

        # 构造队友对象：沿用旧记录的 created_at / status，避免重复创建时重置历史
        member = TeamMember(
            name=safe_name,
            role=role,
            agent_type=self.subagent_registry.resolve_name(resolved_agent_type),
            status=(existing.status if existing else TeamStatus.IDLE.value),
            created_at=(existing.created_at if existing else now_ts()),
            last_error=(existing.last_error if existing else None),
        )
        # 写入 config.json 并推送前端状态更新
        self.store.upsert_member(member)
        self._emit(events.member_update(member), emit, loop)

        # 无任务时仅创建队友，不触发唤醒
        if not task:
            return json.dumps({"created": member.to_dict()}, ensure_ascii=False)

        # 将任务写入队友 inbox
        task_id = new_id("task")
        msg = self.bus.send(
            from_actor=sender,
            to=member.name,
            content=task,
            type="task",
            task_id=task_id,
        )
        self._emit(events.message_event(msg), emit, loop)

        # 立即唤醒队友执行，purpose 用于前端展示当前任务摘要
        result = self.wake_teammate(
            member.name,
            emit=emit,
            loop=loop,
            parent_call_id=parent_call_id,
            purpose=task[:120],
        )
        return json.dumps(
            {"created": member.to_dict(), "message": msg.to_dict(), "result": result},
            ensure_ascii=False,
        )

    def list_teammates(self) -> str:
        """以 JSON 字符串返回 Team 总览。"""
        return json.dumps(self.payload(), ensure_ascii=False, indent=2)

    def read_inbox(self, *, actor: str = LEAD_ACTOR, limit: int = 20, mark_read: bool = True) -> str:
        """读取指定 actor 的 inbox 未读消息。"""
        messages = self.bus.read(actor, limit=limit, mark_read=mark_read)
        return json.dumps([message.to_dict() for message in messages], ensure_ascii=False, indent=2)

    def send_message(
        self,
        *,
        to: str,
        content: str,
        sender: str = LEAD_ACTOR,
        wake: bool = True,
        type: str = "message",
        emit: StreamEmitter | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        parent_call_id: str | None = None,
    ) -> str:
        """发送单条消息；可选在发送后唤醒目标队友执行。"""
        if to != LEAD_ACTOR:
            self._require_member(to)
        if sender != LEAD_ACTOR:
            self._require_member(sender)
        msg = self.bus.send(from_actor=sender, to=to, content=content, type=type)
        self._emit(events.message_event(msg), emit, loop)
        result = None
        if wake and to != LEAD_ACTOR:
            result = self.wake_teammate(
                to,
                emit=emit,
                loop=loop,
                parent_call_id=parent_call_id,
                purpose=content[:120],
            )
        return json.dumps({"message": msg.to_dict(), "result": result}, ensure_ascii=False)

    def broadcast(
        self,
        *,
        content: str,
        recipients: list[str] | None = None,
        wake: bool = True,
        emit: StreamEmitter | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        parent_call_id: str | None = None,
    ) -> str:
        """向多个队友广播同一消息，并可按成员逐个唤醒。"""
        members = [
            member
            for member in self.store.list_members()
            if member.status != TeamStatus.SHUTDOWN.value
        ]
        if recipients:
            wanted = {validate_member_name(name) for name in recipients}
            members = [member for member in members if member.name in wanted]
        sent = []
        results = []
        for member in members:
            msg = self.bus.send(from_actor=LEAD_ACTOR, to=member.name, content=content, type="message")
            sent.append(msg.to_dict())
            self._emit(events.message_event(msg), emit, loop)
            if wake:
                results.append({
                    "name": member.name,
                    "result": self.wake_teammate(
                        member.name,
                        emit=emit,
                        loop=loop,
                        parent_call_id=parent_call_id,
                        purpose=content[:120],
                    ),
                })
        return json.dumps({"sent": sent, "results": results}, ensure_ascii=False, indent=2)

    def shutdown_teammate(self, *, name: str, emit: StreamEmitter | None = None,
                          loop: asyncio.AbstractEventLoop | None = None) -> str:
        """将队友标记为 shutdown，停止接收后续任务。"""
        member = self.store.update_member(
            name,
            status=TeamStatus.SHUTDOWN.value,
            last_error=None,
        )
        self._emit(events.member_update(member), emit, loop)
        return json.dumps({"shutdown": member.to_dict()}, ensure_ascii=False)

    def wake_teammate(
        self,
        name: str,
        *,
        emit: StreamEmitter | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        parent_call_id: str | None = None,
        purpose: str = "",
    ) -> str:
        """唤醒队友执行一次工作循环；同一时刻同一队友仅允许一个执行实例。"""
        member = self._require_member(name)
        if member.status == TeamStatus.SHUTDOWN.value:
            return f"Error: teammate '{member.name}' is shutdown"
        lock = self._lock_for(member.name)
        if not lock.acquire(blocking=False):
            return f"Error: teammate '{member.name}' is already working"
        try:
            return self._wake_locked(
                member,
                emit=emit,
                loop=loop,
                parent_call_id=parent_call_id,
                purpose=purpose,
            )
        finally:
            lock.release()

    def _wake_locked(
        self,
        member: TeamMember,
        *,
        emit: StreamEmitter | None,
        loop: asyncio.AbstractEventLoop | None,
        parent_call_id: str | None,
        purpose: str,
    ) -> str:
        """在已获取成员锁后执行完整唤醒流程（恢复、运行、落盘、回禀）。"""
        working = self.store.update_member(
            member.name,
            status=TeamStatus.WORKING.value,
            last_error=None,
        )
        self._emit(events.member_update(working), emit, loop)
        self._emit(events.run_start(parent_id=parent_call_id, member=working, purpose=purpose), emit, loop)

        # 优先从 checkpoint 恢复（崩溃后重启场景）；否则从 thread + cursor 正常装配
        checkpoint = self.store.read_checkpoint_payload(working.name)
        pending_cursor_start: int | None = None
        pending_cursor_end: int | None = None
        pending_message_ids: list[str] = []
        if checkpoint:
            # 崩溃恢复：history 和待处理消息 ID 从 checkpoint 读取
            history = checkpoint["messages"]
            pending_cursor_start = checkpoint.get("pending_cursor_start")
            pending_cursor_end = checkpoint.get("pending_cursor_end")
            pending_message_ids = list(checkpoint.get("pending_message_ids") or [])
            inbox_by_id = {msg.id: msg for msg in self.bus.all_messages(working.name)}
            # 按保存的 ID 顺序重建 unread 列表，保证消息顺序一致
            unread = [inbox_by_id[msg_id] for msg_id in pending_message_ids if msg_id in inbox_by_id]
        else:
            # 正常唤醒：从游标往后最多取 50 条未读
            inbox = self.bus.all_messages(working.name)
            pending_cursor_start = min(self.store.read_cursor(working.name), len(inbox))
            unread = inbox[pending_cursor_start:pending_cursor_start + 50]
            pending_cursor_end = pending_cursor_start + len(unread)
            pending_message_ids = [msg.id for msg in unread]
            history = self.store.read_thread(working.name)  # 读取全量历史作底座

        if not checkpoint and not unread:
            idle = self.store.update_member(working.name, status=TeamStatus.IDLE.value, last_error=None)
            self._emit(events.member_update(idle), emit, loop)
            self._emit(events.run_done(parent_id=parent_call_id, member=idle, summary="没有未读消息。"), emit, loop)
            return "没有未读消息。"

        if not checkpoint:
            # 把未读 inbox 渲染为 user 消息追加到 history，随后喂给 runner
            history.append({"role": "user", "content": self._render_inbox_for_runner(working, unread)})
        # 写入崩溃保险：若 runner 途中崩溃，下次唤醒可从此处恢复
        self.store.write_checkpoint(
            working.name,
            history,
            pending_cursor_start=pending_cursor_start,
            pending_cursor_end=pending_cursor_end,
            pending_message_ids=pending_message_ids,
        )

        spec = self._require_spec(working.agent_type)
        sub_registry = self._registry_for_member(working, spec)  # 构建队友工具白名单
        runner = self.runner_factory(member=working, spec=spec, sub_registry=sub_registry)
        # 快照 lead inbox 当前消息 ID，用于后续判断队友是否已主动回禀
        lead_before_ids = {msg.id for msg in self.bus.all_messages(LEAD_ACTOR)}

        async def team_emit(evt: dict[str, Any]) -> None:
            evt_type = str(evt.get("event") or "")
            if evt_type.startswith("team_"):
                if emit is None:
                    return
                if loop is not None:
                    self._emit(evt, emit, loop)
                    return
                await emit(evt)
                return
            mapped = self._map_runner_event(evt, working, parent_call_id)
            if mapped:
                if emit is None:
                    return
                if loop is not None:
                    self._emit(mapped, emit, loop)
                    return
                await emit(mapped)

        try:
            # runner 会直接 append 到 history，无需额外合并
            if emit is not None:
                final = run_sync(runner.step_stream(history, team_emit))
            else:
                final = runner.step(history)
            self.store.write_thread(working.name, history)    # 落盘完整历史
            self.store.clear_checkpoint(working.name)          # 清除崩溃保险
            if pending_cursor_end is not None:
                self.store.write_cursor(working.name, pending_cursor_end)  # 推进已读游标
            idle = self.store.update_member(working.name, status=TeamStatus.IDLE.value, last_error=None)
            self._emit(events.member_update(idle), emit, loop)
            # 若队友未在执行过程中主动 send_message(to=lead)，则自动补一条 result 消息
            explicit_reply = any(
                msg.id not in lead_before_ids and msg.from_actor == working.name
                for msg in self.bus.all_messages(LEAD_ACTOR)
            )
            if not explicit_reply:
                result_msg = self.bus.send(
                    from_actor=working.name,
                    to=LEAD_ACTOR,
                    content=final,
                    type="result",
                    in_reply_to=(pending_message_ids[-1] if pending_message_ids else None),
                    meta={"role": working.role, "agent_type": working.agent_type},
                )
                self._emit(events.message_event(result_msg), emit, loop)
            logger.info(f"[队友回禁 · {working.name}]: {final[:500]}")
            return final
        except Exception as exc:
            err = str(exc)
            logger.exception(f"team wake failed: {working.name}")
            # 执行失败：保留 checkpoint 供下次恢复，并通知 lead 错误
            self.store.write_checkpoint(
                working.name,
                history,
                pending_cursor_start=pending_cursor_start,
                pending_cursor_end=pending_cursor_end,
                pending_message_ids=pending_message_ids,
            )
            error_member = self.store.update_member(
                working.name,
                status=TeamStatus.ERROR.value,
                last_error=err,
            )
            self._emit(events.member_update(error_member), emit, loop)
            self._emit(events.run_error(parent_id=parent_call_id, member=error_member, message=err), emit, loop)
            error_msg = self.bus.send(
                from_actor=working.name,
                to=LEAD_ACTOR,
                content=err,
                type="error",
                meta={"role": working.role, "agent_type": working.agent_type},
            )
            self._emit(events.message_event(error_msg), emit, loop)
            return f"Error: teammate '{working.name}' raised: {err}"

    def _registry_for_member(self, member: TeamMember, spec: SubagentSpec) -> ToolRegistry:
        """为队友构建工具白名单，并注入 team 内通信工具。"""
        from .tools import TeamReadInboxTool, TeamSendMessageTool

        registry = ToolRegistry()
        for tool_name in spec.tool_names:
            tool = self.parent_registry.get(tool_name)
            if tool is not None:
                registry.register(tool)
        registry.register(TeamSendMessageTool(self, sender=member.name, allow_wake=False))
        registry.register(TeamReadInboxTool(self, actor=member.name))
        return registry

    def _tool_names_for_member(self, member: TeamMember) -> list[str]:
        """返回队友在前端展示的可用工具名列表。"""
        spec = self.subagent_registry.get(member.agent_type)
        if spec is None:
            return []
        return [*spec.tool_names, "send_message", "read_inbox"]

    def _thread_summary(self, name: str) -> list[dict[str, Any]]:
        """提取 thread 最近片段用于 UI 展示，做长度与文本归一化。"""
        out = []
        for item in self.store.read_thread(name)[-20:]:
            content = item.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            out.append({
                "role": item.get("role"),
                "content": str(content)[:2000],
            })
        return out

    def _require_member(self, name: str) -> TeamMember:
        """获取队友对象，不存在时抛错。"""
        member = self.store.get_member(name)
        if member is None:
            raise ValueError(f"unknown teammate: {name}")
        return member

    def _require_spec(self, agent_type: str) -> SubagentSpec:
        """获取子代理规格，不存在时抛错。"""
        spec = self.subagent_registry.get(agent_type)
        if spec is None:
            raise ValueError(f"unknown agent_type: {agent_type}")
        return spec

    def _lock_for(self, name: str) -> Lock:
        """按队友名获取互斥锁，避免重复唤醒并发执行。"""
        safe = validate_member_name(name)
        with self._locks_guard:
            if safe not in self._locks:
                self._locks[safe] = Lock()
            return self._locks[safe]

    @staticmethod
    def _render_inbox_for_runner(member: TeamMember, messages: list[TeamMessage]) -> str:
        """把未读 inbox 渲染成 runner 可直接消费的一段用户提示。"""
        lines = [
            f"你是 Agent Team 队友 {member.name}，role={member.role}，agent_type={member.agent_type}。",
            "下面是你的未读 inbox。请处理这些消息，必要时调用工具，最后用 send_message(to=\"lead\", content=\"...\") 回禀，随后给出简短总结。",
            "",
            "## Inbox",
        ]
        for msg in messages:
            lines.append(
                f"- id={msg.id} type={msg.type} from={msg.from_actor} "
                f"task_id={msg.task_id or ''}: {msg.content}"
            )
        return "\n".join(lines)

    @staticmethod
    def _map_runner_event(evt: dict[str, Any], member: TeamMember,
                          parent_call_id: str | None) -> dict[str, Any] | None:
        """将通用 runner 事件映射为 Team 专用事件结构。"""
        evt_type = evt.get("event")
        if evt_type == "message_delta":
            return events.run_delta(parent_id=parent_call_id, member=member, delta=str(evt.get("delta") or ""))
        if evt_type == "tool_call":
            return events.run_tool_call(
                parent_id=parent_call_id,
                member=member,
                id=evt.get("id"),
                name=str(evt.get("name") or ""),
                arguments=evt.get("arguments") if isinstance(evt.get("arguments"), dict) else {},
            )
        if evt_type == "tool_result":
            return events.run_tool_result(
                parent_id=parent_call_id,
                member=member,
                id=evt.get("id"),
                name=evt.get("name"),
                summary=str(evt.get("summary") or ""),
            )
        if evt_type == "tool_error":
            return events.run_tool_error(
                parent_id=parent_call_id,
                member=member,
                id=evt.get("id"),
                name=evt.get("name"),
                message=str(evt.get("message") or ""),
            )
        if evt_type == "assistant_done":
            return events.run_done(parent_id=parent_call_id, member=member, summary=str(evt.get("content") or ""))
        return None

    @staticmethod
    def _emit(
        event: dict[str, Any],
        emit: StreamEmitter | None,
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """安全发送事件：有 loop 则跨线程投递，无 loop 则同步兜底。"""
        if emit is None:
            return
        if loop is not None:
            asyncio.run_coroutine_threadsafe(emit(event), loop)
            return
        try:
            run_sync(emit(event))
        except RuntimeError:
            logger.debug(f"team event dropped outside loop: {event.get('event')}")
