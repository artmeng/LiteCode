"""agent/model_router.py

模型路由器：根据使用场景决定应走主模型还是次模型。

路由规则：
  - main_agent：主模型
  - memory_compaction：次模型（节约）
  - subagent / team：轻量子代理 → 次模型，写入型子代理 → 主模型
  - 次模型缺失或上下文过大时自动降级主模型
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .model_config import build_provider_snapshot
from .providers import ProviderSnapshot


MAIN_ROLE = "main"
SECONDARY_ROLE = "secondary"

# 轻量只读子代理：默认走次模型
# 如：小黄门、司礼监随堂、东厂探事、尚宝监典簿
LIGHTWEIGHT_AGENT_TYPES = {
    "xiaohuangmen",
    "sili_suitang",
    "dongchang_tanshi",
    "shangbao_dianbu",
}
# 写入型子代理：默认走主模型
# 如：内官监营造
WRITING_AGENT_TYPES = {"neiguan_yingzao"}


@dataclass(frozen=True)
class ModelRoute:
    """单次路由结果：包含主快照、备用快照、使用场景和路由原因。"""
    snapshot: ProviderSnapshot        # 当前选定的 provider + 模型快照
    fallback: ProviderSnapshot | None  # 备用快照（次模型失败时使用）
    use_case: str                      # 应用场景（main_agent / subagent / team 等）
    reason: str                        # 路由原因，用于日志和调试

    @property
    def model_role(self) -> str:
        return self.snapshot.model_role


class ModelRouter:
    """中心模型角色选择器。

    一个模型条目拥有一套凭证和两个模型 ID。
    路由器决定每种内部应用场景使用哪个 ID，
    并为次模型路由附加主模型作为备用。
    """

    def __init__(self, root, *, model_override: str | None = None):
        self.root = root
        self.model_override = model_override
        self.main = build_provider_snapshot(root, model_override=model_override, role=MAIN_ROLE)       # 主模型快照
        self.secondary = build_provider_snapshot(root, model_override=model_override, role=SECONDARY_ROLE)  # 次模型快照

    def route(
        self,
        use_case: str,
        *,
        agent_type: str | None = None,
        task: str | None = None,
    ) -> ModelRoute:
        """根据使用场景和 agent_type 返回对应的 ModelRoute。"""
        key = str(use_case or "main_agent")
        if key == "main_agent":
            return self._main("main_agent")                              # 主 Agent 总走主模型
        if key == "memory_compaction":
            return self._secondary("memory_compaction")                  # 压缩走次模型
        if key in {"subagent", "team"}:
            normalized_agent = str(agent_type or "").strip()
            if normalized_agent in WRITING_AGENT_TYPES:
                return self._main(f"{key}:{normalized_agent}:write_capable")          # 写入型走主模型
            if normalized_agent in LIGHTWEIGHT_AGENT_TYPES:
                return self._secondary(f"{key}:{normalized_agent}:lightweight", task=task)  # 轻量走次模型
            return self._main(f"{key}:{normalized_agent or 'unknown'}:default_main")  # 未知类型走主模型
        return self._main(f"{key}:default_main")

    def _main(self, reason: str) -> ModelRoute:
        snapshot = replace(self.main, route_reason=reason)
        return ModelRoute(snapshot=snapshot, fallback=None, use_case=reason.split(":", 1)[0], reason=reason)

    def _secondary(self, reason: str, *, task: str | None = None) -> ModelRoute:
        """尝试路由到次模型；次模型缺失或任务过大时降级到主模型。"""
        if self.secondary.model_role != SECONDARY_ROLE:
            return self._main(f"{reason}:secondary_missing")  # 无次模型配置，降级主模型
        if task and _rough_token_estimate(task) > int(self.secondary.context_window_tokens * 0.65):
            return self._main(f"{reason}:secondary_context_too_small")  # 任务过大，次模型上下文不够
        snapshot = replace(self.secondary, route_reason=reason)
        fallback = replace(self.main, route_reason=f"{reason}:fallback_main")  # 主模型作为备用
        return ModelRoute(snapshot=snapshot, fallback=fallback, use_case=reason.split(":", 1)[0], reason=reason)

    def payload(self) -> dict[str, object]:
        return {
            "secondaryEnabled": self.secondary.model_role == SECONDARY_ROLE,
            "fallbackToMain": True,
            "mainEntry": self.main.entry_name,
            "mainModel": self.main.model,
            "secondaryModel": self.secondary.model if self.secondary.model_role == SECONDARY_ROLE else None,
        }


def _rough_token_estimate(text: str) -> int:
    return max(1, len(text or "") // 3)
