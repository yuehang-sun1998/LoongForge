# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the optional, non-enforcing shadow decision module."""

from __future__ import annotations

import json
from typing import Any

from loongforge.embodied.eval.shadow import ShadowDecisionModule, ShadowConfig


class _FakeShadow(ShadowDecisionModule):
    def __init__(self, response: dict[str, Any], config: ShadowConfig) -> None:
        super().__init__(config)
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(body)
        return self.response


def test_shadow_disabled_is_side_effect_free() -> None:
    module = ShadowDecisionModule(ShadowConfig())
    result = module.observe(
        canonical_obs={"state_raw": {"eef_pos": [0.0, 0.0, 0.0]}},
        ctx={"episode_step": 0},
        primary_response={},
        primary_action=[0.0] * 7,
        env_action=[0.0] * 7,
    )

    assert result == {"enabled": False, "status": "disabled"}


def test_shadow_builds_typed_request_and_preserves_response() -> None:
    config = ShadowConfig(enabled=True, endpoint="http://shadow.test", log_actions=False)
    module = _FakeShadow({"answers": {"next_operation": {"choice": "continue"}}}, config)
    module.reset("episode-0")

    result = module.observe(
        canonical_obs={"instruction": "pick up the block", "state_raw": {"eef_pos": [1, 2, 3]}},
        ctx={"episode_id": "episode-0", "episode_step": 4, "instruction": "pick up the block"},
        primary_response={"request_id": "primary-1", "inference_latency_ms": 2.5},
        primary_action=[0.1, 0.2],
        env_action=[0.3, 0.4],
    )

    assert result["status"] == "ok"
    assert result["answers"]["next_operation"]["choice"] == "continue"
    assert "candidate_action" not in result
    assert module.requests[0]["model"] == "~typesafe/jev-latest"
    assert module.requests[0]["state"]["episode_id"] == "episode-0"
    assert set(module.requests[0]["questions"]["next_operation"]["criteria"]) == {"continue", "replan"}
    json.dumps(module.requests[0])


def test_shadow_errors_fail_open() -> None:
    config = ShadowConfig(enabled=True, endpoint="http://shadow.test")
    module = _FakeShadow({}, config)

    def fail(_: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError("upstream timeout")

    module._request = fail  # type: ignore[method-assign]
    result = module.observe(
        canonical_obs={},
        ctx={"episode_step": 0},
        primary_response={},
        primary_action=[0.0],
        env_action=[0.0],
    )

    assert result["status"] == "error"
    assert result["error_type"] == "TimeoutError"


def test_shadow_sample_offset_queries_mid_chunk() -> None:
    config = ShadowConfig(
        enabled=True,
        endpoint="http://shadow.test",
        sample_every_n_steps=10,
        sample_offset=5,
    )
    module = _FakeShadow({"answers": {"next_operation": {"choice": "continue"}}}, config)

    def observe(step: int) -> dict[str, Any]:
        return module.observe(
            canonical_obs={},
            ctx={"episode_step": step},
            primary_response={},
            primary_action=[0.0],
            env_action=[0.0],
        )

    assert observe(0)["status"] == "skipped"
    assert observe(4)["status"] == "skipped"
    assert observe(5)["status"] == "ok"
    assert observe(14)["status"] == "skipped"
    assert observe(15)["status"] == "ok"
    assert len(module.requests) == 2
