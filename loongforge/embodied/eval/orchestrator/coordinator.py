# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Minimal 1+N coordinator: one command starts one server and N worker clients.

A config is *coordinated* when it carries a ``coordinator:`` section; ``run.py``
dispatches such configs here. The coordinator:

1. starts one policy server (from the config's ``server:`` section),
2. splits the benchmark's tasks across N workers,
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
from pathlib import Path
from typing import Any, Dict, List

from loongforge.embodied.eval.orchestrator.config import apply_config
from loongforge.embodied.eval.orchestrator.server_manager import (
    build_argparser as build_server_argparser,
    start_server,
    wait_healthz,
)


def split_round_robin(items: List[Any], groups: int) -> List[List[Any]]:
    """Split items into at most ``groups`` round-robin groups (empty ones dropped)."""
    buckets: List[List[Any]] = [[] for _ in range(max(1, groups))]
    for index, item in enumerate(items):
        buckets[index % len(buckets)].append(item)
    return [bucket for bucket in buckets if bucket]


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


def _shard_config(config: Dict[str, Any], tasks: List[int], external_url: str, output_dir: str) -> Dict[str, Any]:
    """Build one worker's config: a copy of the base, minus coordinator, plus shard/endpoint."""
    shard = copy.deepcopy(config)
    shard.pop("coordinator", None)
    shard.setdefault("benchmark", {})["task_ids"] = tasks
    run_cfg = shard.setdefault("run", {})
    run_cfg["output_dir"] = output_dir
    run_cfg["timestamped_output"] = False  # deterministic path so the coordinator can aggregate
    shard.setdefault("server", {})["external_url"] = external_url
    return shard


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

    shards = split_round_robin(_task_ids(config), n_workers)
    proc = start_server(server_args)
    returncodes: List[int] = []
    try:
        wait_healthz(
            f"http://{server_args.host}:{server_args.health_port}/healthz",
            server_args.server_start_timeout_sec,
            proc,
        )
        workers = []
        with tempfile.TemporaryDirectory() as tmp:
            for index, tasks in enumerate(shards):
                shard = _shard_config(config, tasks, external_url, str(output_dir / f"worker_{index}"))
                cfg_path = Path(tmp) / f"shard_{index}.json"
                cfg_path.write_text(json.dumps(shard), encoding="utf-8")
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

    return {
        "benchmark": (config.get("benchmark") or {}).get("suite") or (config.get("benchmark") or {}).get("name"),
        "n_workers": len(shards),
        "external_url": external_url,
        "worker_returncodes": returncodes,
        "output_dir": str(output_dir),
        "summary": _aggregate(output_dir, len(shards)),
    }
