from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.model_config import parse_model_config, validate_complete_model_entries
from agent.model_router import ModelRouter
from agent.providers.base import LLMProvider, LLMResponse
from agent.runner import AgentRunner
from agent.telemetry import TokenTracker
from agent.tools import ToolRegistry
from agent.web.services.model_service import ModelService


def write_config(root: Path, *, secondary: str = "cheap-model") -> None:
    (root / "model_config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "work",
                        "provider": "auto",
                        "maxTokens": 1000,
                        "temperature": 0.1,
                        "reasoningEffort": None,
                        "contextWindowTokens": 100000,
                    }
                },
                "models": [
                    {
                        "name": "work",
                        "provider": "deepseek",
                        "mainModelId": "smart-model",
                        "secondaryModelId": secondary,
                        "apiKey": "",
                        "apiBase": "https://api.deepseek.com",
                    }
                ],
                "providers": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_model_entry_parses_legacy_id_as_main_model() -> None:
    config = parse_model_config({
        "agents": {"defaults": {"model": "legacy", "provider": "auto"}},
        "models": [{"name": "legacy", "id": "legacy-main", "provider": "deepseek"}],
    })

    entry = config.find_entry("legacy")
    assert entry is not None
    assert entry.main_model_id == "legacy-main"
    assert entry.id == "legacy-main"
    assert entry.secondary_model_id == ""


def test_save_validation_requires_both_model_ids() -> None:
    with pytest.raises(ValueError, match="Secondary Model ID"):
        validate_complete_model_entries({
            "models": [{"name": "work", "provider": "deepseek", "mainModelId": "smart-model"}]
        })


def test_model_router_routes_simple_work_to_secondary(tmp_path: Path) -> None:
    write_config(tmp_path)

    router = ModelRouter(tmp_path)

    assert router.route("main_agent").snapshot.model == "smart-model"
    assert router.route("memory_compaction").snapshot.model == "cheap-model"
    assert router.route("subagent", agent_type="sili_suitang").snapshot.model == "cheap-model"
    assert router.route("team", agent_type="shangbao_dianbu").snapshot.model == "cheap-model"
    assert router.route("subagent", agent_type="neiguan_yingzao").snapshot.model == "smart-model"
    assert router.route("memory_compaction").fallback is not None


def test_model_router_does_not_mutate_shared_snapshot_reason(tmp_path: Path) -> None:
    write_config(tmp_path)
    router = ModelRouter(tmp_path)

    first = router.route("subagent", agent_type="sili_suitang")
    second = router.route("team", agent_type="shangbao_dianbu")

    assert first.reason == "subagent:sili_suitang:lightweight"
    assert second.reason == "team:shangbao_dianbu:lightweight"
    assert first.snapshot.route_reason == first.reason
    assert second.snapshot.route_reason == second.reason
    assert router.secondary.route_reason == "secondary_model"


def test_model_router_falls_back_to_main_when_secondary_missing(tmp_path: Path) -> None:
    write_config(tmp_path, secondary="")

    router = ModelRouter(tmp_path)
    route = router.route("memory_compaction")

    assert route.snapshot.model == "smart-model"
    assert route.snapshot.model_role == "main"
    assert route.fallback is None


def test_model_service_rejects_explicit_secondary_test_when_missing(tmp_path: Path) -> None:
    write_config(tmp_path, secondary="")
    service = ModelService(_State(tmp_path))

    payload, status = asyncio.run(service.test({
        "entryName": "work",
        "kind": "text",
        "role": "secondary",
    }))

    assert status == 400
    assert payload["ok"] is False
    assert "secondaryModelId" in str(payload["error"])


class FailingProvider(LLMProvider):
    async def chat(self, **kwargs):
        raise RuntimeError("secondary down")


class OkProvider(LLMProvider):
    async def chat(self, **kwargs):
        return LLMResponse(content="ok", usage={"input": 1, "output": 2})


def test_runner_records_fallback_model_role(tmp_path: Path) -> None:
    tracker = TokenTracker(tmp_path / "tokens.jsonl")
    runner = AgentRunner(
        provider=FailingProvider(default_model="cheap"),
        model="cheap",
        registry=ToolRegistry(),
        system_prompt="You are concise.",
        provider_name="fake",
        model_role="secondary",
        route_reason="test",
        fallback_provider=OkProvider(default_model="smart"),
        fallback_model="smart",
        fallback_provider_name="fake",
        fallback_model_role="main",
        token_tracker=tracker,
    )

    reply = asyncio.run(runner.step_async([{"role": "user", "content": "hi"}]))

    assert reply == "ok"
    rows = [json.loads(line) for line in (tmp_path / "tokens.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["model"] == "smart"
    assert rows[-1]["model_role"] == "main"


class _State:
    def __init__(self, root: Path):
        self.root = root
        self.loop = None
