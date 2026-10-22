# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-benchmark shard planners."""

from __future__ import annotations

from typing import Any

import pytest

from loongforge.embodied.eval.orchestrator import planning
from loongforge.embodied.eval.orchestrator.planning import (
    benchmark_family,
    chunk_size,
    plan_calvin,
    plan_episode_indexed,
    plan_libero,
    plan_robotwin,
    plan_shards,
)


def test_plan_libero_explicit_episode_chunk() -> None:
    """A worker can run several episodes per process to amortise startup."""
    config: dict = {
        "benchmark": {
            "name": "libero",
            "suites": ["a"],
            "task_ids": [0],
            "episodes_per_task": 10,
            "episodes_per_chunk": 4,
        }
    }
    shards = plan_libero(config, 64)
    assert [(s["overrides"]["benchmark"]["episode_offset"], s["expected"]) for s in shards] == [
        (0, 4),
        (4, 4),
        (8, 2),
    ]
    assert sum(s["expected"] for s in shards) == 10


def test_plan_libero_explicit_chunk_cannot_exceed_the_task() -> None:
    """A chunk larger than the task still yields one shard covering it."""
    config: dict = {
        "benchmark": {"name": "libero", "suites": ["a"], "task_ids": [0], "episodes_per_task": 10, "episodes_per_chunk": 32}
    }
    shards = plan_libero(config, 64)
    assert len(shards) == 1
    assert shards[0]["overrides"]["benchmark"]["episode_offset"] == 0
    assert shards[0]["expected"] == 10


def test_plan_libero_per_task_ignores_explicit_chunk() -> None:
    """per_task correctness beats the startup optimisation: one task, one shard."""
    config: dict = {
        "benchmark": {
            "name": "libero",
            "suites": ["a"],
            "task_ids": [0],
            "episodes_per_task": 10,
            "episodes_per_chunk": 4,
            "env_seed_mode": "per_task",
        }
    }
    shards = plan_libero(config, 64)
    assert len(shards) == 1
    assert shards[0]["expected"] == 10


def test_benchmark_family_normalises_libero_variants() -> None:
    """Run test_benchmark_family_normalises_libero_variants."""
    assert benchmark_family({"benchmark": {"name": "libero"}}) == "libero"
    assert benchmark_family({"benchmark": {"name": "libero_10"}}) == "libero"
    assert benchmark_family({"benchmark": {"name": "robotwin"}}) == "robotwin"


def test_chunk_size_leaves_slack_per_slot() -> None:
    """Run test_chunk_size_leaves_slack_per_slot."""
    assert chunk_size(100, 4) == 7  # ceil(100 / (4 slots x 4 units))
    assert chunk_size(10, 4) == 1
    assert chunk_size(0, 4) == 1


def test_plan_libero_covers_every_episode_exactly_once() -> None:
    """Run test_plan_libero_covers_every_episode_exactly_once."""
    config: dict = {"benchmark": {"name": "libero", "suites": ["a", "b"], "task_ids": [0, 1], "episodes_per_task": 4}}
    shards = plan_libero(config, 4)
    assert len(shards) == 16  # chunk=1 so fast slots can steal work
    assert sum(shard["expected"] for shard in shards) == 16
    ids = [shard["id"] for shard in shards]
    assert len(set(ids)) == len(ids)  # unique ids -> coverage attribution is unambiguous
    assert {shard["overrides"]["benchmark"]["suite"] for shard in shards} == {"a", "b"}


def test_plan_libero_applies_suite_overrides() -> None:
    """A per-suite override (e.g. LIBERO's max_steps) rides along in the shard patch."""
    config: dict = {
        "benchmark": {
            "name": "libero",
            "suites": ["libero_10", "libero_goal"],
            "task_ids": [0],
            "episodes_per_task": 2,
            "max_steps": 400,
            "suite_overrides": {"libero_10": {"max_steps": 700}},
        }
    }
    by_suite = {shard["overrides"]["benchmark"]["suite"]: shard["overrides"]["benchmark"] for shard in plan_libero(config, 2)}
    assert by_suite["libero_10"]["max_steps"] == 700
    assert "max_steps" not in by_suite["libero_goal"]  # inherited from the base config


def test_plan_episode_indexed_is_one_episode_per_shard() -> None:
    """SimplerEnv / ManiSkill run a single episode per invocation."""
    config: dict = {
        "benchmark": {"name": "maniskill", "task_name": "PickCube-v1", "episodes_per_task": 3, "episode_offset": 5}
    }
    shards = plan_episode_indexed(config, 4)
    assert [shard["overrides"]["benchmark"]["episode_idx"] for shard in shards] == [5, 6, 7]
    assert all(shard["expected"] == 1 for shard in shards)


def test_plan_robotwin_slices_by_seed_range() -> None:
    """Run test_plan_robotwin_slices_by_seed_range."""
    config: dict = {
        "benchmark": {"name": "robotwin", "task_name": "adjust_bottle", "task_config": "demo_clean", "episodes_per_task": 100}
    }
    shards = plan_robotwin(config, 4)
    assert sum(shard["expected"] for shard in shards) == 100
    assert shards[0]["overrides"]["benchmark"]["start_seed"] == 0
    assert all("test_num" in shard["overrides"]["benchmark"] for shard in shards)


def test_plan_calvin_is_one_shard_until_the_runner_takes_an_offset() -> None:
    """CALVIN has no sequence offset yet, so it cannot be sliced further."""
    config: dict = {"benchmark": {"name": "calvin", "task_suite_name": "task_D_D", "num_sequences": 10}}
    shards = plan_calvin(config, 4)
    assert len(shards) == 1
    assert shards[0]["overrides"]["benchmark"]["num_sequences"] == 10


def test_plan_libero_per_task_keeps_one_shard_per_task() -> None:
    """per_task seeds once per process, so a task must not be split across processes."""
    config: dict = {
        "benchmark": {
            "name": "libero",
            "suites": ["a", "b"],
            "task_ids": [0, 1],
            "episodes_per_task": 4,
            "env_seed_mode": "per_task",
        }
    }
    shards = plan_libero(config, 32)  # many workers must not induce intra-task chunks
    assert len(shards) == 4
    assert all(shard["overrides"]["benchmark"]["episode_offset"] == 0 for shard in shards)
    assert all(shard["overrides"]["benchmark"]["episodes_per_task"] == 4 for shard in shards)
    assert sum(shard["expected"] for shard in shards) == 16


def test_plan_libero_per_episode_still_splits() -> None:
    """The default per_episode seeding keeps the steal-work slicing."""
    config: dict = {"benchmark": {"name": "libero", "suites": ["a"], "task_ids": [0], "episodes_per_task": 16}}
    shards = plan_libero(config, 1)
    assert len(shards) > 1
    assert any(shard["overrides"]["benchmark"]["episode_offset"] > 0 for shard in shards)


def test_plan_libero_per_task_suite_override_is_honoured() -> None:
    """A suite-level per_task override gets the same one-shard-per-task treatment."""
    config: dict = {
        "benchmark": {
            "name": "libero",
            "suites": ["libero_10", "libero_goal"],
            "task_ids": [0],
            "episodes_per_task": 4,
            "suite_overrides": {"libero_10": {"env_seed_mode": "per_task"}},
        }
    }
    shards = plan_libero(config, 32)
    per_task = [s for s in shards if s["overrides"]["benchmark"]["suite"] == "libero_10"]
    per_episode = [s for s in shards if s["overrides"]["benchmark"]["suite"] == "libero_goal"]
    assert len(per_task) == 1
    assert len(per_episode) > 1


def test_plan_shards_rejects_intra_task_split_for_per_task(monkeypatch: Any) -> None:
    """Backstop: a planner that splits a task under per_task must fail loudly."""

    def split_planner(config: dict, total_workers: int) -> list:
        return [
            {
                "id": f"a/task=0/eps={offset}-{offset}",
                "overrides": {
                    "benchmark": {
                        "suite": "a",
                        "task_ids": [0],
                        "episode_offset": offset,
                        "episodes_per_task": 1,
                        "env_seed_mode": "per_task",
                    }
                },
                "expected": 1,
            }
            for offset in (0, 1)
        ]

    monkeypatch.setitem(planning.PLANNERS, "libero", split_planner)
    config: dict = {"benchmark": {"name": "libero", "env_seed_mode": "per_task"}}
    with pytest.raises(ValueError, match="cannot split a task"):
        plan_shards(config, 2)


def test_plan_shards_rejects_duplicate_task_for_per_task(monkeypatch: Any) -> None:
    """Two shards with the same task (offset 0) are still a split under per_task."""

    def duplicate_planner(config: dict, total_workers: int) -> list:
        return [
            {
                "id": f"a/task=0/copy{i}",
                "overrides": {
                    "benchmark": {"suite": "a", "task_ids": [0], "episode_offset": 0, "episodes_per_task": 4}
                },
                "expected": 4,
            }
            for i in range(2)
        ]

    monkeypatch.setitem(planning.PLANNERS, "libero", duplicate_planner)
    config: dict = {"benchmark": {"name": "libero", "env_seed_mode": "per_task"}}
    with pytest.raises(ValueError, match="cannot split a task"):
        plan_shards(config, 2)


def test_plan_shards_dispatches_on_the_family(monkeypatch: Any) -> None:
    """Run test_plan_shards_dispatches_on_the_family."""
    libero = plan_shards({"benchmark": {"name": "libero", "suite": "a", "task_ids": [0], "episodes_per_task": 1}}, 1)
    maniskill = plan_shards({"benchmark": {"name": "maniskill", "task_name": "t", "episodes_per_task": 1}}, 1)
    assert libero[0]["overrides"]["benchmark"]["suite"] == "a"
    assert maniskill[0]["overrides"]["benchmark"]["task_name"] == "t"
