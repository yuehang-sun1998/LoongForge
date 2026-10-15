# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the minimal 1+N coordinator (pure helpers)."""

from __future__ import annotations

from loongforge.embodied.eval.orchestrator.coordinator import _shard_config, split_round_robin


def test_split_round_robin_balances() -> None:
    """Run test_split_round_robin_balances."""
    assert split_round_robin([0, 1, 2, 3, 4], 2) == [[0, 2, 4], [1, 3]]


def test_split_round_robin_drops_empty() -> None:
    """Run test_split_round_robin_drops_empty."""
    assert split_round_robin([0, 1], 4) == [[0], [1]]
    assert split_round_robin([], 3) == []


def test_shard_config_strips_coordinator_and_sets_shard() -> None:
    """Run test_shard_config_strips_coordinator_and_sets_shard."""
    base = {
        "benchmark": {"suite": "libero_10"},
        "server": {"ckpt_path": "x"},
        "coordinator": {"n_workers": 2},
    }
    shard = _shard_config(base, [1, 2], "ws://h:1", "/tmp/out")
    assert "coordinator" not in shard
    assert shard["benchmark"]["task_ids"] == [1, 2]
    assert shard["server"]["external_url"] == "ws://h:1"
    assert shard["run"]["output_dir"] == "/tmp/out"
    assert shard["run"]["timestamped_output"] is False
    # the base config must not be mutated
    assert "coordinator" in base and "task_ids" not in base["benchmark"]
