# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Optional inference-time shadow decision module.

The shadow module is deliberately side-effect free: it observes the state and
the action selected by the primary policy, asks an external decision service
for an opinion, and returns a trace-friendly record.  It never changes the
action sent to the environment.

The HTTP payload is intentionally provider-agnostic for now.  The Jev/OpenRouter
adapter can be refined once the final endpoint and response schema are known.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import numpy as np


DEFAULT_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "should_continue": {
        "type": "noul",
        "instructions": "Should the robot continue executing the current action chunk?",
        "criteria": {
            "true": "The current action should continue",
            "false": "The current action should not continue",
        },
    },
    "next_operation": {
        "type": "choice",
        "instructions": "What should the controller do next?",
        "criteria": {
            "continue": "Continue executing the current action chunk",
            "replan": "Discard the current action and request a new plan",
        },
    },
    "action_risk": {
        "type": "score",
        "instructions": "How risky is it to execute the current action?",
        "criteria": ["Safe", "Moderate risk", "High risk"],
    },
}


def _jsonable(value: Any) -> Any:
    """Convert common numpy/container values into JSON-compatible values."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _extract_json_response(response: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize Jev-like and OpenAI-compatible JSON responses."""
    if isinstance(response.get("answers"), Mapping):
        return dict(response)

    # This keeps the placeholder compatible with OpenRouter chat responses.
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            try:
                decoded = json.loads(content)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                pass
        if isinstance(message, Mapping):
            return {"answers": {"raw": dict(message)}}

    return dict(response)


@dataclass
class ShadowConfig:
    """Configuration for the optional shadow decision service."""

    enabled: bool = False
    mode: str = "shadow"
    endpoint: str = "https://openrouter.ai/api/alpha/decisions"
    model: str = "~typesafe/jev-latest"
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_sec: float = 5.0
    sample_every_n_steps: int = 1
    sample_offset: int = 0
    enriched_inputs: bool = False
    fail_open: bool = True
    log_actions: bool = True
    questions: Dict[str, Dict[str, Any]] = field(default_factory=lambda: dict(DEFAULT_QUESTIONS))

    @classmethod
    def from_raw_config(cls, raw_config: Optional[Mapping[str, Any]]) -> "ShadowConfig":
        """Build config from the optional top-level ``shadow`` YAML section."""
        raw = (raw_config or {}).get("shadow") or {}
        if not isinstance(raw, Mapping):
            raise ValueError("shadow section must be a mapping")
        questions = raw.get("questions", DEFAULT_QUESTIONS)
        if not isinstance(questions, Mapping):
            raise ValueError("shadow.questions must be a mapping")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            mode=str(raw.get("mode", "shadow")),
            endpoint=str(raw.get("endpoint", "https://openrouter.ai/api/alpha/decisions")),
            model=str(raw.get("model", "~typesafe/jev-latest")),
            api_key_env=str(raw.get("api_key_env", "OPENROUTER_API_KEY")),
            timeout_sec=float(raw.get("timeout_sec", 5.0)),
            sample_every_n_steps=max(1, int(raw.get("sample_every_n_steps", 1))),
            sample_offset=int(raw.get("sample_offset", 0)),
            enriched_inputs=bool(raw.get("enriched_inputs", False)),
            fail_open=bool(raw.get("fail_open", True)),
            log_actions=bool(raw.get("log_actions", True)),
            questions={str(key): dict(value) for key, value in questions.items()},
        )


class ShadowDecisionModule:
    """Observe primary-policy decisions without affecting control flow."""

    def __init__(self, config: ShadowConfig) -> None:
        self.config = config
        self._episode_id: Optional[str] = None

    @classmethod
    def from_raw_config(cls, raw_config: Optional[Mapping[str, Any]]) -> "ShadowDecisionModule":
        return cls(ShadowConfig.from_raw_config(raw_config))

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def active(self) -> bool:
        """Whether the optional module may request a cache replan."""
        return self.enabled and self.config.mode.lower() in {"active", "replan_gate"}

    def reset(self, episode_id: str) -> None:
        """Reset shadow-local episode bookkeeping."""
        self._episode_id = episode_id

    def close(self) -> None:
        """Release provider-specific resources when the implementation needs them."""

    def observe(
        self,
        *,
        canonical_obs: Mapping[str, Any],
        ctx: Mapping[str, Any],
        primary_response: Mapping[str, Any],
        primary_action: Any,
        env_action: Any,
    ) -> Dict[str, Any]:
        """Ask the configured service for a non-enforcing decision."""
        step = int(ctx.get("episode_step", 0))
        if not self.config.enabled:
            return {"enabled": False, "status": "disabled"}
        sample_offset = self.config.sample_offset % self.config.sample_every_n_steps
        if step < sample_offset or (step - sample_offset) % self.config.sample_every_n_steps != 0:
            return {
                "enabled": True,
                "status": "skipped",
                "reason": "sample_interval",
            }
        if not self.config.endpoint:
            return self._error_record(
                ValueError("shadow.endpoint is required when shadow.enabled=true")
            )

        state = {
            "instruction": ctx.get("instruction", canonical_obs.get("instruction")),
            "episode_id": ctx.get("episode_id", self._episode_id),
            "episode_step": step,
            "state_raw": canonical_obs.get("state_raw", {}),
            "candidate_action": _jsonable(primary_action),
            "env_action": _jsonable(env_action),
            "policy": {
                "request_id": primary_response.get("request_id"),
                "inference_latency_ms": primary_response.get("inference_latency_ms"),
            },
        }
        request_body = {
            "state": _jsonable(state),
            "model": self.config.model,
            "questions": _jsonable(self.config.questions),
        }

        started = time.perf_counter()
        if self.config.enriched_inputs:
            request_body["state"]["scene"] = _jsonable(ctx.get("jev_scene"))
            request_body["state"]["chunk"] = _jsonable(primary_response.get("chunk_context"))
        try:
            response = self._request(request_body)
            normalized = _extract_json_response(response)
            result: Dict[str, Any] = {
                "enabled": True,
                "status": "ok",
                "mode": self.config.mode,
                "latency_ms": (time.perf_counter() - started) * 1000.0,
                "answers": normalized.get("answers", normalized),
            }
            for key in ("model", "provider", "usage", "id"):
                if key in normalized:
                    result[key] = normalized[key]
            if self.config.log_actions:
                result["candidate_action"] = _jsonable(primary_action)
            if self.config.enriched_inputs:
                result["request_state"] = request_body["state"]
            return result
        except Exception as exc:
            return self._error_record(exc, latency_ms=(time.perf_counter() - started) * 1000.0)

    def _request(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """Send a provider-agnostic JSON request.

        This is intentionally small and dependency-free.  The default request
        body matches OpenRouter's ``/api/alpha/decisions`` endpoint; the
        endpoint remains configurable for local mocks and future providers.
        """
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(self.config.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            self.config.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_sec) as response:
            decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, dict):
            raise TypeError("shadow service response must be a JSON object")
        return decoded

    def _error_record(self, exc: Exception, latency_ms: Optional[float] = None) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "enabled": True,
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        if latency_ms is not None:
            record["latency_ms"] = latency_ms
        if not self.config.fail_open:
            raise exc
        return record

    @staticmethod
    def requests_replan(record: Mapping[str, Any]) -> bool:
        """Read only the supported active decision: ``continue`` or ``replan``."""
        if record.get("status") != "ok":
            return False
        answers = record.get("answers")
        if not isinstance(answers, Mapping):
            return False
        operation = answers.get("next_operation")
        return isinstance(operation, Mapping) and operation.get("choice") == "replan"


def build_shadow_module(raw_config: Optional[Mapping[str, Any]]) -> ShadowDecisionModule:
    """Build the optional module without opening a network connection."""
    return ShadowDecisionModule.from_raw_config(raw_config)
