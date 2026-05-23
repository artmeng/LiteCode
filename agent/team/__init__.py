# agent.team 包的公共导出接口
# 外部模块只需 from agent.team import ... 即可使用所有 Team 能力

from .bus import MessageBus
from .manager import TeamManager, role_to_agent_type
from .models import (
    SCHEMA_VERSION,
    TeamMember,
    TeamMessage,
    TeamStatus,
    validate_member_name,
)
from .store import TeamStore
from .tools import (
    TeamBroadcastTool,
    TeamListTool,
    TeamReadInboxTool,
    TeamSendMessageTool,
    TeamShutdownTool,
    TeamSpawnTool,
)

__all__ = [
    "MessageBus",
    "TeamManager",
    "TeamStore",
    "TeamMember",
    "TeamMessage",
    "TeamStatus",
    "SCHEMA_VERSION",
    "validate_member_name",
    "role_to_agent_type",
    "TeamBroadcastTool",
    "TeamListTool",
    "TeamReadInboxTool",
    "TeamSendMessageTool",
    "TeamShutdownTool",
    "TeamSpawnTool",
]
