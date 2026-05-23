from __future__ import annotations

# Team 子系统运行时事件构造函数
# 每个函数返回一个字典，用于通过 WebSocket 将 Team 变化推送到前端

from typing import Any

from .models import TeamMember, TeamMessage


def member_update(member: TeamMember) -> dict[str, Any]:
    """队友状态变化事件：唡建、状态转换、错误时推送。"""
    return {"event": "team_member_update", "member": member.to_dict()}


def message_event(message: TeamMessage) -> dict[str, Any]:
    """消息投递事件：lead 发任务、队友回禁、或错误通知时推送。"""
    return {"event": "team_message", "message": message.to_dict()}


def run_start(*, parent_id: str | None, member: TeamMember, purpose: str) -> dict[str, Any]:
    """队友开始执行一轮任务时推送。"""
    return {
        "event": "team_run_start",
        "parent_id": parent_id,
        "teammate": member.name,
        "role": member.role,
        "agent_type": member.agent_type,
        "purpose": purpose,
    }


def run_delta(*, parent_id: str | None, member: TeamMember, delta: str) -> dict[str, Any]:
    """队友 LLM 流式输出的增量块，前端用于实时展示流式回复。"""
    return {
        "event": "team_run_delta",
        "parent_id": parent_id,
        "teammate": member.name,
        "delta": delta,
    }


def run_tool_call(
    *,
    parent_id: str | None,
    member: TeamMember,
    id: str | None,
    name: str,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """队友发起工具调用时推送，前端展示工具执行进度。"""
    return {
        "event": "team_run_tool_call",
        "parent_id": parent_id,
        "teammate": member.name,
        "id": id,
        "name": name,
        "arguments": arguments or {},
    }


def run_tool_result(
    *,
    parent_id: str | None,
    member: TeamMember,
    id: str | None,
    name: str | None,
    summary: str,
) -> dict[str, Any]:
    """工具执行成功后推送结果摘要。"""
    return {
        "event": "team_run_tool_result",
        "parent_id": parent_id,
        "teammate": member.name,
        "id": id,
        "name": name,
        "summary": summary,
    }


def run_tool_error(
    *,
    parent_id: str | None,
    member: TeamMember,
    id: str | None,
    name: str | None,
    message: str,
) -> dict[str, Any]:
    """工具执行失败时推送错误信息。"""
    return {
        "event": "team_run_tool_error",
        "parent_id": parent_id,
        "teammate": member.name,
        "id": id,
        "name": name,
        "message": message,
    }


def run_done(*, parent_id: str | None, member: TeamMember, summary: str) -> dict[str, Any]:
    """队友完成本轮执行时推送，带最终回答摘要。"""
    return {
        "event": "team_run_done",
        "parent_id": parent_id,
        "teammate": member.name,
        "summary": summary,
    }


def run_error(*, parent_id: str | None, member: TeamMember, message: str) -> dict[str, Any]:
    """队友执行抛异常时推送错误事件。"""
    return {
        "event": "team_run_error",
        "parent_id": parent_id,
        "teammate": member.name,
        "message": message,
    }
