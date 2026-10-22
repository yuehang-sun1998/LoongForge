# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Benchmark-aware planning: turn a run config into the shards the coordinator dispatches.

Dispatching is benchmark-agnostic (a shard is just a subprocess with some config
overrides), but *how you slice a benchmark into units* is not: LIBERO is a
suite x task x episode grid, RoboTwin slices by seed, CALVIN by sequence count,
SimplerEnv/ManiSkill run a single episode per invocation.

So a shard is a plain ``{"id", "overrides", "expected"}`` record -- an id for
attribution, a config override patch, and how many episodes it must produce --
and one planner per benchmark family produces them. The family key comes from
``benchmark_family``, the same function ``run.py`` uses to pick a runner, so
"which runner" and "how to shard" cannot drift apart.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

Shard = Dict[str, Any]
Planner = Callable[[Dict[str, Any], int], List[Shard]]


def benchmark_family(config: Dict[str, Any]) -> str:
    """Normalise ``benchmark.name`` to the family that owns this config."""
    benchmark = config.get("benchmark") or {}
    if not isinstance(benchmark, dict):
        raise ValueError("benchmark section must be a mapping")
    name = str(benchmark.get("name") or "").lower()
    if name == "libero" or name.startswith("libero_"):
        return "libero"
    if name in {"calvin", "simplerenv", "robotwin", "maniskill"}:
        return name
    raise ValueError(f"Unsupported benchmark.name: {benchmark.get('name')!r}")


def chunk_size(total_episodes: int, total_workers: int, units_per_slot: int = 4) -> int:
    """Episodes per shard so there are ~``units_per_slot`` shards per worker slot."""
    target = max(1, total_workers) * max(1, units_per_slot)
    return max(1, -(-max(1, total_episodes) // target))  # ceil division


def _ranges(start: int, count: int, chunk: int) -> List[Tuple[int, int]]:
    """Split ``count`` items from ``start`` into contiguous ``(start, count)`` chunks."""
    out: List[Tuple[int, int]] = []
    offset = start
    while offset < start + count:
        size = min(chunk, start + count - offset)
        out.append((offset, size))
        offset += size
    return out


def _benchmark(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("benchmark") or {}


def _seed_mode(config: Dict[str, Any]) -> str:
    """Env seeding granularity (``per_episode`` default, ``per_task`` official FastWAM)."""
    return str(_benchmark(config).get("env_seed_mode") or "per_episode")


def _suites(config: Dict[str, Any]) -> List[str]:
    """Suites to evaluate: ``benchmark.suites`` (list) or the single ``benchmark.suite``."""
    benchmark = _benchmark(config)
    suites = benchmark.get("suites")
    if suites:
        return [str(s) for s in suites]
    return [str(benchmark.get("suite") or benchmark.get("name"))]


def _libero_task_ids(config: Dict[str, Any], suite: str) -> List[int]:
    """Task list for a LIBERO suite (explicit ``task_ids`` or the suite's own task count)."""
    benchmark = _benchmark(config)
    explicit = benchmark.get("task_ids")
    if explicit:
        return [int(task) for task in explicit]
    from libero.libero import benchmark as libero_benchmark  # only the eval env has this

    n_tasks = libero_benchmark.get_benchmark_dict()[suite]().n_tasks
    max_tasks = int(benchmark.get("max_tasks") or 0)
    if max_tasks > 0:
        n_tasks = min(n_tasks, max_tasks)
    return list(range(n_tasks))


def plan_libero(config: Dict[str, Any], total_workers: int) -> List[Shard]:
    """LIBERO: suite x task x episode, chunked so a fast slot can steal work."""
    benchmark = _benchmark(config)
    episodes_per_task = int(benchmark.get("episodes_per_task") or 1)
    per_suite = [(suite, _libero_task_ids(config, suite)) for suite in _suites(config)]
    total = max(1, sum(len(tasks) for _, tasks in per_suite) * max(1, episodes_per_task))
    chunk = chunk_size(total, total_workers)
    # Explicit chunk size: one worker runs several episodes in the same process,
    # amortising the torch/robosuite import that otherwise dominates short units.
    explicit_chunk = int(benchmark.get("episodes_per_chunk") or 0)
    if explicit_chunk > 0:
        chunk = max(1, explicit_chunk)
    overrides = benchmark.get("suite_overrides") or {}
    shards: List[Shard] = []
    for suite, tasks in per_suite:
        # In per_task mode the env is seeded once per process and reused, so the
        # scene an episode sees is (run.seed, reset count in this process).
        # Splitting a task across processes would replay the first scenes, so
        # one task must map to exactly one shard.
        suite_mode = str((overrides.get(suite) or {}).get("env_seed_mode") or _seed_mode(config))
        suite_chunk = max(1, episodes_per_task) if suite_mode == "per_task" else chunk
        for task in tasks:
            for offset, count in _ranges(0, episodes_per_task, suite_chunk):
                patch = {
                    "suite": suite,
                    "task_ids": [task],
                    "episode_offset": offset,
                    "episodes_per_task": count,
                }
                patch.update(overrides.get(suite) or {})
                shards.append(
                    {
                        "id": f"{suite}/task={task}/eps={offset}-{offset + count - 1}",
                        "overrides": {"benchmark": patch},
                        "expected": count,
                    }
                )
    return shards


def plan_episode_indexed(config: Dict[str, Any], total_workers: int) -> List[Shard]:
    """SimplerEnv / ManiSkill: the runner plays exactly one episode per invocation."""
    benchmark = _benchmark(config)
    task = str(benchmark.get("task_name") or benchmark.get("suite") or benchmark.get("name"))
    episodes = int(benchmark.get("episodes_per_task") or benchmark.get("num_episodes") or 1)
    start = int(benchmark.get("episode_offset") or 0)
    return [
        {
            "id": f"{task}/episode={episode}",
            "overrides": {"benchmark": {"task_name": task, "episode_idx": episode}},
            "expected": 1,
        }
        for episode in range(start, start + episodes)
    ]


def plan_robotwin(config: Dict[str, Any], total_workers: int) -> List[Shard]:
    """RoboTwin: task x task-config x seed range (episodes are seeds, not an index)."""
    benchmark = _benchmark(config)
    task = str(benchmark.get("task_name") or "")
    task_config = str(benchmark.get("task_config") or "demo_clean")
    episodes = int(benchmark.get("episodes_per_task") or benchmark.get("test_num") or 1)
    start = int(benchmark.get("episode_offset") or benchmark.get("start_seed") or 0)
    chunk = chunk_size(episodes, total_workers)
    return [
        {
            "id": f"{task}/{task_config}/seed={seed}-{seed + count - 1}",
            "overrides": {
                "benchmark": {"task_name": task, "task_config": task_config, "start_seed": seed, "test_num": count}
            },
            "expected": count,
        }
        for seed, count in _ranges(start, episodes, chunk)
    ]


def plan_calvin(config: Dict[str, Any], total_workers: int) -> List[Shard]:
    """CALVIN: task-suite x sequence count.

    One shard only -- the runner takes ``num_sequences`` from the front of the
    sequence list and has no offset, so it cannot be sliced further yet.
    """
    benchmark = _benchmark(config)
    suite = str(benchmark.get("task_suite_name") or benchmark.get("suite") or "task_D_D")
    sequences = int(benchmark.get("num_sequences") or benchmark.get("episodes_per_task") or 1)
    return [
        {
            "id": f"{suite}/sequences=0-{sequences - 1}",
            "overrides": {"benchmark": {"task_suite_name": suite, "num_sequences": sequences}},
            "expected": sequences,
        }
    ]


PLANNERS: Dict[str, Planner] = {
    "libero": plan_libero,
    "calvin": plan_calvin,
    "robotwin": plan_robotwin,
    "simplerenv": plan_episode_indexed,
    "maniskill": plan_episode_indexed,
}


def _assert_per_task_not_split(config: Dict[str, Any], shards: List[Shard]) -> None:
    """Reject a plan that spreads one task over several processes in ``per_task`` mode.

    The planner already keeps one task per shard here; this is the backstop for a
    hand-built or future planner, where the split would silently change scenes.
    """
    seen = set()
    for shard in shards:
        patch = (shard.get("overrides") or {}).get("benchmark") or {}
        mode = str(patch.get("env_seed_mode") or _seed_mode(config))
        if mode != "per_task":
            continue
        key = (patch.get("suite"), tuple(patch.get("task_ids") or ()))
        if int(patch.get("episode_offset") or 0) > 0 or key in seen:
            raise ValueError(
                "env_seed_mode='per_task' cannot split a task across processes: "
                f"shard {shard.get('id')!r} would replay earlier scenes. "
                "Use one shard per task (episode_offset=0) or per_episode seeding."
            )
        seen.add(key)


def plan_shards(config: Dict[str, Any], total_workers: int) -> List[Shard]:
    """Shards for this config, via the planner for its benchmark family."""
    family = benchmark_family(config)
    shards = PLANNERS[family](config, total_workers)
    if family == "libero":
        _assert_per_task_not_split(config, shards)
    return shards
