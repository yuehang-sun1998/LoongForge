# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Minimal 1+N coordinator: one command starts one server and N worker clients.

A config is *coordinated* when it carries a ``coordinator:`` section; ``run.py``
dispatches such configs here. The coordinator:

1. starts one policy server (from the config's ``server:`` section),
2. splits the benchmark into episode-chunk shards (so N can exceed the task count),
3. launches each worker as an ordinary ``orchestrator.run`` subprocess pointed at
   the shared server via ``server.external_url``, and
4. waits for all workers, then aggregates their per-worker results.

This is the minimal launcher only -- no work queue, no dynamic dispatch, no
watchdog (those are the M2 / T2-2 upgrade).
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from loongforge.embodied.eval.orchestrator.config import apply_config
from loongforge.embodied.eval.orchestrator.server_manager import (
    build_argparser as build_server_argparser,
    start_server,
    wait_healthz,
)


def build_shards(tasks: List[int], episodes_per_task: int, n_workers: int) -> List[tuple]:
    """Split the (task, episode) space into ~``n_workers`` episode-chunk shards.

    Each shard is ``(task_id, episode_offset, episode_count)``. Chunk size is
    chosen so the shard count is close to ``n_workers``. Sharding at the *episode*
    level (not the task level) is what lets N exceed the task count -- required
    once the server needs more concurrent sims than there are tasks.
    """
    total = max(1, len(tasks) * max(1, episodes_per_task))
    chunk = max(1, -(-total // max(1, n_workers)))  # ceil division
    shards: List[tuple] = []
    for task in tasks:
        offset = 0
        while offset < episodes_per_task:
            count = min(chunk, episodes_per_task - offset)
            shards.append((task, offset, count))
            offset += count
    return shards


def _task_ids(config: Dict[str, Any]) -> List[int]:
    """Resolve the task list to shard over (explicit ``task_ids`` or the suite's tasks)."""
    benchmark = config.get("benchmark") or {}
    explicit = benchmark.get("task_ids")
    if explicit:
        return [int(task) for task in explicit]
    # Lazy: only the eval environment has the benchmark package.
    from libero.libero import benchmark as libero_benchmark

    suite_name = str(benchmark.get("suite") or benchmark.get("name"))
    n_tasks = libero_benchmark.get_benchmark_dict()[suite_name]().n_tasks
    max_tasks = int(benchmark.get("max_tasks") or 0)
    if max_tasks > 0:
        n_tasks = min(n_tasks, max_tasks)
    return list(range(n_tasks))


def _shard_config(config: Dict[str, Any], shard: tuple, external_url: str, output_dir: str) -> Dict[str, Any]:
    """Build one worker's config: a copy of the base, minus coordinator, plus this shard."""
    task_id, episode_offset, episode_count = shard
    shard_cfg = copy.deepcopy(config)
    shard_cfg.pop("coordinator", None)
    benchmark = shard_cfg.setdefault("benchmark", {})
    benchmark["task_ids"] = [task_id]
    benchmark["episode_offset"] = episode_offset
    benchmark["episodes_per_task"] = episode_count
    run_cfg = shard_cfg.setdefault("run", {})
    run_cfg["output_dir"] = output_dir
    run_cfg["timestamped_output"] = False  # deterministic path so the coordinator can aggregate
    shard_cfg.setdefault("server", {})["external_url"] = external_url
    return shard_cfg


def _aggregate(output_dir: Path, n_workers: int) -> Dict[str, Any]:
    """Merge worker_*/results.jsonl into one deduped summary."""
    seen: Dict[str, bool] = {}
    per_worker: List[int] = []
    for i in range(n_workers):
        path = output_dir / f"worker_{i}" / "results.jsonl"
        count = 0
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "episode_id" in record:
                    seen[str(record["episode_id"])] = bool(record.get("success"))
                    count += 1
        per_worker.append(count)
    total = len(seen)
    successes = sum(seen.values())
    return {
        "n_episodes": total,
        "successes": successes,
        "success_rate": (successes / total) if total else None,
        "per_worker_episodes": per_worker,
    }


def run(config: Dict[str, Any], config_path: str = "") -> Dict[str, Any]:
    """Run a coordinated (1 server : N worker) evaluation."""
    n_workers = max(1, int((config.get("coordinator") or {}).get("n_workers") or 1))

    server_args = apply_config(build_server_argparser().parse_args([]), config)
    server_args.config = config_path
    external_url = f"ws://{server_args.host}:{server_args.port}"
    output_dir = Path(str((config.get("run") or {}).get("output_dir") or "/tmp/vla_eval"))
    output_dir.mkdir(parents=True, exist_ok=True)

    episodes_per_task = int((config.get("benchmark") or {}).get("episodes_per_task") or 1)
    shards = build_shards(_task_ids(config), episodes_per_task, n_workers)
    proc = start_server(server_args)
    returncodes: List[int] = []
    started = time.time()
    try:
        wait_healthz(
            f"http://{server_args.host}:{server_args.health_port}/healthz",
            server_args.server_start_timeout_sec,
            proc,
        )
        started = time.time()  # measure worker wall only (exclude model load)
        workers = []
        with tempfile.TemporaryDirectory() as tmp:
            for index, shard_spec in enumerate(shards):
                shard_cfg = _shard_config(config, shard_spec, external_url, str(output_dir / f"worker_{index}"))
                cfg_path = Path(tmp) / f"shard_{index}.json"
                cfg_path.write_text(json.dumps(shard_cfg), encoding="utf-8")
                log = open(output_dir / f"worker_{index}.log", "a", buffering=1)
                workers.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "loongforge.embodied.eval.orchestrator.run", "--config", str(cfg_path)],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                )
            returncodes = [worker.wait() for worker in workers]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    result = {
        "benchmark": (config.get("benchmark") or {}).get("suite") or (config.get("benchmark") or {}).get("name"),
        "n_workers": len(shards),
        "external_url": external_url,
        "worker_returncodes": returncodes,
        "elapsed_sec": round(time.time() - started, 1),
        "output_dir": str(output_dir),
        "summary": _aggregate(output_dir, len(shards)),
    }
    (output_dir / "coordinator_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
