# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Client-side action chunks (T-14) and server-side cache ownership."""

from __future__ import annotations

import numpy as np

from loongforge.embodied.eval.orchestrator.runners._common import ChunkConsumer
from loongforge.embodied.eval.servers.loongforge_policy import GenericPredictActionPolicy


def test_chunk_is_consumed_locally() -> None:
    """A 10-action chunk costs one request and is replayed in order."""
    consumer = ChunkConsumer()
    chunk = np.arange(10 * 7, dtype=np.float32).reshape(10, 7)

    assert consumer.needs_request() is True
    consumer.load(chunk)
    assert consumer.needs_request() is False

    actions = [consumer.next_action() for _ in range(10)]

    assert consumer.requests == 1
    assert consumer.local_steps == 10
    assert all(action.shape == (1, 7) for action in actions)
    assert np.array_equal(np.concatenate(actions, axis=0), chunk)
    assert consumer.needs_request() is True


def test_limit_truncates_a_long_chunk() -> None:
    """Only the first N actions are executed when the server returns more."""
    consumer = ChunkConsumer()
    consumer.load(np.arange(32 * 7, dtype=np.float32).reshape(32, 7), limit=10)

    for _ in range(10):
        consumer.next_action()

    assert consumer.needs_request() is True


def test_disable_action_cache_requests_every_step() -> None:
    """Closed-loop models consume one action per request."""
    consumer = ChunkConsumer()
    consumer.load(np.arange(10 * 7, dtype=np.float32).reshape(10, 7))
    first = consumer.next_action(disable_action_cache=True)

    assert first.shape == (1, 7)
    assert consumer.needs_request(disable_action_cache=True) is True
    assert consumer.needs_request(disable_action_cache=False) is True


def test_reset_drops_the_chunk_and_counters() -> None:
    """A new episode must not reuse the previous episode's chunk."""
    consumer = ChunkConsumer()
    consumer.load(np.zeros((10, 7), dtype=np.float32))
    consumer.next_action()
    consumer.reset()

    assert consumer.requests == 0
    assert consumer.local_steps == 0
    assert consumer.needs_request() is True


class _CountingModel:
    def __init__(self) -> None:
        self.calls = 0

    def predict_action(self, images, instructions, state=None, dataset_stats=None):
        self.calls += 1
        return np.arange(10 * 2, dtype=np.float32).reshape(10, 2)


def _payload(**overrides):
    payload = {
        "images": [[np.zeros((2, 2, 3), dtype=np.uint8)]],
        "instructions": ["test"],
        "episode_id": "episode-0",
        "episode_step": 0,
    }
    payload.update(overrides)
    return payload


def test_return_action_chunk_bypasses_the_server_cache() -> None:
    """Client-owned chunks must always trigger a fresh inference."""
    model = _CountingModel()
    policy = GenericPredictActionPolicy(model=model, metadata={"action_dim": 2}, action_dim=2)

    first = policy.predict_action(**_payload(return_action_chunk=True))
    second = policy.predict_action(**_payload(return_action_chunk=True, episode_step=1))

    assert model.calls == 2
    assert np.array_equal(first["actions"], second["actions"])
    assert first["inference_latency_ms"] is not None
    assert second["inference_latency_ms"] is not None


def test_legacy_requests_still_use_the_server_cache() -> None:
    """Without return_action_chunk the existing replay behaviour is unchanged."""
    model = _CountingModel()
    policy = GenericPredictActionPolicy(model=model, metadata={"action_dim": 2}, action_dim=2)

    first = policy.predict_action(**_payload())
    assert policy.has_cached_action(_payload(episode_step=1)) is True
    second = policy.predict_action(**_payload(episode_step=1))

    assert model.calls == 1
    assert first["inference_latency_ms"] is not None
    assert second["inference_latency_ms"] is None
    assert np.array_equal(first["actions"], np.array([[0.0, 1.0]], dtype=np.float32))
    assert np.array_equal(second["actions"], np.array([[2.0, 3.0]], dtype=np.float32))
