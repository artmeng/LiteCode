from __future__ import annotations

import copy
import time
from typing import Any

from loguru import logger

from ...model_config import (
    build_provider_snapshot,
    load_model_config,
    mark_entry_vision,
    save_model_config,
)
from ...providers.registry import provider_options


# 2x2 JPEG probe used for vision connectivity tests.
_PROBE_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAAqADAAQAAAABAAAAAgAAAAD/7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZjwCyBOmACZjs+EJ+/8AAEQgAAgACAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAAAAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoKDhIWGh4iJipKTlJWWl5eYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/bAEMAAgICAgICAwICAwUDAwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8PDw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8A/CQeNPGIAA12/AH/AE9S/wDxVL/wmvjL/oPX/wD4FS//ABVczRX3B/RB/9k="
)


class ModelService:
    def __init__(self, state) -> None:
        self.state = state
        self.root = state.root

    def payload(self) -> dict[str, Any]:
        config = load_model_config(self.root)
        from ...model_config import _resolve_active_entry

        entry = _resolve_active_entry(config, None)
        loop = self.state.loop
        secondary_snapshot = (
            loop.model_router.secondary
            if hasattr(loop, "model_router")
            else build_provider_snapshot(self.root, role="secondary")
        )
        return {
            "current": {
                "provider": loop.provider_name,
                "providerLabel": loop.provider_label,
                "model": loop.model,
                "apiBase": loop.provider_snapshot.api_base,
                "maxTokens": loop.max_tokens,
                "temperature": loop.temperature,
                "reasoningEffort": loop.reasoning_effort,
                "contextWindowTokens": loop.max_context,
                "entryName": entry.name,
                "entryLabel": entry.label or entry.name,
                "supportsVision": bool(getattr(loop, "supports_vision", False)),
                "mainModelId": entry.main_model_id,
                "secondaryModelId": entry.secondary_model_id,
                "modelRole": getattr(loop, "model_role", "main"),
            },
            "secondary": {
                "provider": secondary_snapshot.provider_name,
                "providerLabel": secondary_snapshot.provider_label,
                "model": secondary_snapshot.model,
                "apiBase": secondary_snapshot.api_base,
                "maxTokens": secondary_snapshot.generation.max_tokens,
                "temperature": secondary_snapshot.generation.temperature,
                "reasoningEffort": secondary_snapshot.generation.reasoning_effort,
                "contextWindowTokens": secondary_snapshot.context_window_tokens,
                "entryName": entry.name,
                "entryLabel": entry.label or entry.name,
                "supportsVision": bool(secondary_snapshot.supports_vision),
                "mainModelId": entry.main_model_id,
                "secondaryModelId": entry.secondary_model_id,
                "modelRole": secondary_snapshot.model_role,
            } if entry.secondary_model_id else None,
            "routing": loop.model_router.payload() if hasattr(loop, "model_router") else {
                "secondaryEnabled": bool(entry.secondary_model_id),
                "fallbackToMain": True,
            },
            "config": self.redact_apikeys(config.raw),
            "providerOptions": provider_options(),
        }

    def save(self, config: dict[str, Any]) -> None:
        existing = load_model_config(self.root).raw
        self._restore_masked_keys(config, existing)
        save_model_config(self.root, config, validate_complete=True)
        self.state.loop.refresh_model_config()

    async def test(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        entry_name = str(body.get("entryName") or "").strip()
        kind = str(body.get("kind") or "text").lower()
        role = str(body.get("role") or "main").lower()
        if kind not in {"text", "vision"}:
            return {"ok": False, "kind": kind, "error": "kind must be 'text' or 'vision'"}, 400
        if not entry_name:
            return {"ok": False, "kind": kind, "error": "entryName required"}, 400
        if role not in {"main", "secondary"}:
            return {"ok": False, "kind": kind, "error": "role must be 'main' or 'secondary'"}, 400
        if kind == "vision":
            role = "main"

        config = load_model_config(self.root)
        entry = config.find_entry(entry_name)
        if role == "secondary" and entry is not None and not entry.secondary_model_id:
            return {
                "ok": False,
                "kind": kind,
                "error": "secondaryModelId is required before testing the secondary model",
            }, 400

        try:
            snap = build_provider_snapshot(self.root, model_override=entry_name, role=role)
        except Exception as exc:
            return {"ok": False, "kind": kind, "error": f"snapshot failed: {exc}"}, 200

        messages = _vision_probe_messages() if kind == "vision" else [
            {"role": "user", "content": "Reply with exactly one word: pong"}
        ]
        started = time.monotonic()
        try:
            resp = await snap.provider.chat(
                messages=messages,
                tools=None,
                model=snap.model,
                max_tokens=64,
                temperature=0.0,
                reasoning_effort=None,
            )
        except Exception as exc:
            return {
                "ok": False,
                "kind": kind,
                "error": str(exc),
                "latencyMs": int((time.monotonic() - started) * 1000),
                "model": snap.model,
                "provider": snap.provider_name,
                "modelRole": snap.model_role,
            }, 200

        latency = int((time.monotonic() - started) * 1000)
        sample = (resp.content or "").strip()[:200]
        ok = _vision_ok(sample) if kind == "vision" else bool(sample) and "pong" in sample.lower()
        payload: dict[str, Any] = {
            "ok": ok,
            "kind": kind,
            "latencyMs": latency,
            "model": snap.model,
            "provider": snap.provider_name,
            "modelRole": snap.model_role,
            "sample": sample,
            "finishReason": getattr(resp, "finish_reason", "stop"),
        }
        if kind == "vision" and ok:
            try:
                mark_entry_vision(self.root, entry_name, value=True)
                self.state.loop.refresh_model_config()
                payload["visionMarked"] = True
            except Exception as exc:
                logger.warning(f"failed to mark entry vision: {exc}")
                payload["visionMarked"] = False
        return payload, 200

    @staticmethod
    def redact_apikeys(raw: dict) -> dict:
        out = copy.deepcopy(raw)

        def _mask(key: str) -> str:
            return "***" + key[-4:] if len(key) > 4 else "***"

        for prov in out.get("providers", {}).values():
            if isinstance(prov, dict) and isinstance(prov.get("apiKey"), str) and prov["apiKey"]:
                prov["apiKey"] = _mask(prov["apiKey"])
        for entry in out.get("models", []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("apiKey"), str) and entry["apiKey"]:
                entry["apiKey"] = _mask(entry["apiKey"])
        return out

    @staticmethod
    def _restore_masked_keys(config: dict[str, Any], existing: dict[str, Any]) -> None:
        incoming_providers = config.get("providers") or {}
        existing_providers = existing.get("providers") or {}
        for name, prov in incoming_providers.items():
            if isinstance(prov, dict) and isinstance(prov.get("apiKey"), str) and prov["apiKey"].startswith("***"):
                prov["apiKey"] = existing_providers.get(name, {}).get("apiKey", "")

        incoming_models = config.get("models") or []
        existing_models = {
            m.get("name"): m for m in (existing.get("models") or [])
            if isinstance(m, dict) and m.get("name")
        }
        for entry in incoming_models:
            if not isinstance(entry, dict):
                continue
            api_key = entry.get("apiKey")
            if isinstance(api_key, str) and api_key.startswith("***"):
                entry["apiKey"] = existing_models.get(entry.get("name"), {}).get("apiKey", "")


def _vision_probe_messages() -> list[dict[str, Any]]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Reply with ONE English word only: name a visible color in this image."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_PROBE_JPEG_BASE64}"}},
        ],
    }]


def _vision_ok(sample: str) -> bool:
    lowered = sample.lower()
    return bool(sample) and not any(
        token in lowered for token in ("invalid", "error", "cannot", "unable", "sorry")
    )
