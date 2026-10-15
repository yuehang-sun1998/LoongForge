# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the coordinator (pure helpers)."""

from __future__ import annotations

from typing import Any

from loongforge.embodied.eval.orchestrator.coordinator import (
    _shard_config,
    _shards,
    build_shards,
    chunk_size,
)


def test_chunk_size_leaves_slack_per_slot() -> None:
    """Run test_chunk_size_leaves_slack_per_slot."""
    assert chunk_size(100, 4) == 7  # ceil(100 / (4 slots x 4 units))
    assert chunk_size(10, 4) == 1
    assert chunk_size(0, 4) == 1


def test_build_shards_contiguous_and_disjoint() -> None:
    """Run test_build_shards_contiguous_and_disjoint."""
    assert build_shards([0, 1], 50, 25) == [(0, 0, 25), (0, 25, 25), (1, 0, 25), (1, 25, 25)]


def test_shards_cover_every_episode_exactly_once() -> None:
    """Run test_shards_cover_every_episode_exactly_once."""
    config = {"benchmark": {"suites": ["a", "b"], "task_ids": [0, 1], "episodes_per_task": 4}}
    shards = _shards(config, 4)
    covered = {(suite, task, ep) for suite, task, off, count in shards for ep in range(off, off + count)}
    assert covered == {(s, t, e) for s in ("a", "b") for t in (0, 1) for e in range(4)}
    assert len(shards) == 16  # fine chunks (chunk=1) so fast slots can steal work


def test_shard_config_sets_suite_offset_and_count() -> None:
    """Run test_shard_config_sets_suite_offset_and_count."""
    base = {
        "benchmark": {"suites": ["a", "b"], "episodes_per_task": 50},
        "server": {"ckpt_path": "x"},
        "coordinator": {"n_workers": 4},
        "servers": [{"gpus": [0, 1]}],
        "pools": {"cpu_sim": {"n_sims_per_server": 2}},
    }
    shard = _shard_config(base, ("b", 3, 10, 20), "ws://h:1", "/tmp/out")
    assert "coordinator" not in shard and "servers" not in shard and "pools" not in shard
    assert "suites" not in shard["benchmark"]
    assert shard["benchmark"]["suite"] == "b"
    assert shard["benchmark"]["task_ids"] == [3]
    assert shard["benchmark"]["episode_offset"] == 10
    assert shard["benchmark"]["episodes_per_task"] == 20
    assert shard["server"]["external_url"] == "ws://h:1"
    assert shard["run"]["timestamped_output"] is False
    assert "coordinator" in base  # base not mutated


def test_shard_config_defaults_max_retries_but_respects_explicit() -> None:
    """Coordinated units retry infra episode failures by default; an explicit value wins."""
    base = {"benchmark": {"suites": ["a"], "episodes_per_task": 2}, "server": {"ckpt_path": "x"}}
    shard = _shard_config(base, ("a", 0, 0, 2), "ws://h:1", "/tmp/out")
    assert shard["run"]["max_retries"] == 2
    explicit = {**base, "run": {"max_retries": 0}}
    assert _shard_config(explicit, ("a", 0, 0, 2), "ws://h:1", "/tmp/out")["run"]["max_retries"] == 0


def test_shard_config_applies_per_suite_overrides() -> None:
    """A per-suite override (e.g. max_steps) wins over the shared benchmark value."""
    base = {
        "benchmark": {
            "suites": ["libero_10", "libero_goal"],
            "episodes_per_task": 2,
            "max_steps": 400,
            "suite_overrides": {"libero_10": {"max_steps": 700}},
        },
        "server": {"ckpt_path": "x"},
    }
    long_tail = _shard_config(base, ("libero_10", 0, 0, 1), "ws://h:1", "/tmp/out")
    other = _shard_config(base, ("libero_goal", 0, 0, 1), "ws://h:1", "/tmp/out")
    assert long_tail["benchmark"]["max_steps"] == 700
    assert other["benchmark"]["max_steps"] == 400
    assert "suite_overrides" not in long_tail["benchmark"]


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen`` in the dispatch loop."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    def poll(self) -> int:
        """Run poll."""
        return self.returncode

    def terminate(self) -> None:
        """Run terminate."""

    def wait(self, timeout: float | None = None) -> int:
        """Run wait."""
        return self.returncode

    def kill(self) -> None:
        """Run kill."""


def test_run_retries_a_failed_unit_once(monkeypatch: Any, tmp_path: Any) -> None:
    """A unit that exits non-zero is re-queued once; a later success is not a failure."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    launches: dict[int, int] = {}

    def fake_launch(config: Any, shard: Any, index: int, url: str, output_dir: Any, tmp: Any) -> _FakeProc:
        launches[index] = launches.get(index, 0) + 1
        return _FakeProc(1 if (index == 0 and launches[index] == 1) else 0)

    monkeypatch.setattr(coord, "_launch_unit", fake_launch)
    monkeypatch.setattr(coord, "start_server", lambda args, env=None: _FakeProc(0))
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)
    monkeypatch.setattr(coord, "_aggregate", lambda *args, **kwargs: {})

    config = {
        "benchmark": {"suite": "libero_10", "task_ids": [0], "episodes_per_task": 2},
        "servers": [{"gpus": [0]}],
        "pools": {"cpu_sim": {"n_sims_per_server": 1}},
        "run": {"output_dir": str(tmp_path)},
    }
    result = coord.run(config, "")
    assert result["n_units"] == 2
    assert launches[0] == 2  # failed once, retried
    assert result["failed_units"] == []
