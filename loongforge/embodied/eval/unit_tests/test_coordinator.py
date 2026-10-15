# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the minimal 1+N coordinator (pure helpers)."""

from __future__ import annotations

from loongforge.embodied.eval.orchestrator.coordinator import _shard_config, build_shards


def test_build_shards_one_per_task_when_workers_fit() -> None:
    """Run test_build_shards_one_per_task_when_workers_fit."""
    assert build_shards([0, 1, 2, 3], 1, 4) == [(0, 0, 1), (1, 0, 1), (2, 0, 1), (3, 0, 1)]


def test_build_shards_can_exceed_task_count() -> None:
    """Run test_build_shards_can_exceed_task_count."""
    shards = build_shards([0, 1], 50, 4)
    assert len(shards) == 4  # episode-level chunking, not capped at 2 tasks
    assert sum(count for _, _, count in shards) == 100
    assert (0, 0, 25) in shards and (0, 25, 25) in shards  # contiguous, disjoint


def test_build_shards_covers_every_episode() -> None:
    """Run test_build_shards_covers_every_episode."""
    shards = build_shards([0, 1, 2], 50, 7)
    per_task: dict = {}
    for task, offset, count in shards:
        per_task.setdefault(task, []).extend(range(offset, offset + count))
    assert all(sorted(v) == list(range(50)) for v in per_task.values())


def test_shard_config_sets_offset_and_count() -> None:
    """Run test_shard_config_sets_offset_and_count."""
    base = {
        "benchmark": {"suite": "libero_10", "episodes_per_task": 50},
        "server": {"ckpt_path": "x"},
        "coordinator": {"n_workers": 4},
    }
    shard = _shard_config(base, (3, 10, 20), "ws://h:1", "/tmp/out")
    assert "coordinator" not in shard
    assert shard["benchmark"]["task_ids"] == [3]
    assert shard["benchmark"]["episode_offset"] == 10
    assert shard["benchmark"]["episodes_per_task"] == 20
    assert shard["server"]["external_url"] == "ws://h:1"
    assert shard["run"]["timestamped_output"] is False
    # base config must not be mutated
    assert "coordinator" in base and "task_ids" not in base["benchmark"]
