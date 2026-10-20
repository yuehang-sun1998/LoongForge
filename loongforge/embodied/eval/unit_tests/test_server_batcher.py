# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Server-side request batching (M3 plumbing)."""

from __future__ import annotations

import asyncio

from loongforge.embodied.eval.transport.batcher import AsyncBatcher


class _Policy:
    supports_batching = False

    def __init__(self, cached: set | None = None) -> None:
        self.batches: list[int] = []
        self.single_calls = 0
        self._cached = cached or set()

    def predict_action_batch(self, payloads):
        self.batches.append(len(payloads))
        return [{"actions": [[payload["i"]]], "inference_latency_ms": 1.0} for payload in payloads]

    def predict_action(self, **payload):
        self.single_calls += 1
        return {"actions": [[payload["i"]]], "inference_latency_ms": 1.0}

    def has_cached_action(self, payload) -> bool:
        return payload["i"] in self._cached


def _run(coro):
    return asyncio.run(coro)


def test_concurrent_requests_are_batched() -> None:
    async def scenario():
        policy = _Policy()
        batcher = AsyncBatcher(policy, max_batch_size=8, max_wait_ms=20)
        await batcher.start()
        results = await asyncio.gather(*[batcher.submit({"i": i}) for i in range(4)])
        await batcher.stop()
        return policy, batcher, results

    policy, batcher, results = _run(scenario())

    assert policy.batches == [4]
    assert [result["actions"][0][0] for result in results] == [0, 1, 2, 3]
    assert batcher.stats["requests"] == 4
    assert batcher.stats["batches"] == 1
    assert batcher.stats["mean_batch_size"] == 4.0


def test_max_batch_size_splits_requests() -> None:
    async def scenario():
        policy = _Policy()
        batcher = AsyncBatcher(policy, max_batch_size=2, max_wait_ms=20)
        await batcher.start()
        await asyncio.gather(*[batcher.submit({"i": i}) for i in range(4)])
        await batcher.stop()
        return policy

    policy = _run(scenario())

    assert sorted(policy.batches) == [2, 2]


def test_cache_hits_are_filtered_before_the_queue() -> None:
    async def scenario():
        policy = _Policy(cached={0})
        batcher = AsyncBatcher(policy, max_batch_size=8, max_wait_ms=20)
        await batcher.start()
        results = await asyncio.gather(*[batcher.submit({"i": i}) for i in range(4)])
        await batcher.stop()
        return policy, results

    policy, results = _run(scenario())

    assert policy.single_calls == 1
    assert policy.batches == [3]
    assert sorted(result["actions"][0][0] for result in results) == [0, 1, 2, 3]
