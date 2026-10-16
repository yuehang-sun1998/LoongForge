# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Coordinator: one command drives a G-server x N-sim evaluation with dynamic dispatch.

A config is *coordinated* when it carries ``coordinator`` / ``servers`` / ``pools``
sections; ``run.py`` dispatches such configs here. The coordinator:

1. starts one policy server per GPU (from ``servers[0].gpus``),
2. splits the benchmark into **fine** episode-chunk shards across every suite,
3. runs a **bounded pool** of ``len(gpus) * pools.cpu_sim.n_sims_per_server``
   concurrent unit processes, pulling the next shard from a global queue as one
   finishes -- so a fast slot picks up the next unit instead of idling (this is
   what flattens the long tail; a static one-shard-per-slot launch does not), and
4. aggregates the per-unit results.

Each unit is an ordinary ``orchestrator.run`` subprocess pointed at a shared
server via ``server.external_url``. Failed units are retried once.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loongforge.embodied.eval.orchestrator.config import apply_config
from loongforge.embodied.eval.orchestrator.server_manager import (
    build_argparser as build_server_argparser,
    start_server,
    wait_healthz,
)
from loongforge.embodied.eval.orchestrator.validation import (
    missing_keys,
    read_records,
    repair_shards,
    validate,
)

PORT_STRIDE = 20
UNITS_PER_SLOT = 4  # shards per worker slot: slack so fast slots can steal work
DEFAULT_MAX_RETRIES = 2  # episode-level infra retries (episode_error / timeouts) per unit
MAX_REPAIR_ROUNDS = 3  # auto re-dispatch rounds for episodes the plan asked for but that never ran
MAX_ATTEMPTS = 2  # a failed unit is re-queued once before it is reported as failed


def chunk_size(total_episodes: int, total_workers: int, units_per_slot: int = UNITS_PER_SLOT) -> int:
    """Episodes per shard so there are ~``units_per_slot`` shards per worker slot."""
    target = max(1, total_workers) * max(1, units_per_slot)
    return max(1, -(-max(1, total_episodes) // target))  # ceil division


def build_shards(tasks: List[int], episodes_per_task: int, chunk: int) -> List[tuple]:
    """Split one suite's (task, episode) space into ``(task, offset, count)`` chunks."""
    shards: List[tuple] = []
    for task in tasks:
        offset = 0
        while offset < episodes_per_task:
            count = min(chunk, episodes_per_task - offset)
            shards.append((task, offset, count))
            offset += count
    return shards


def _suites(config: Dict[str, Any]) -> List[str]:
    """Suites to evaluate: ``benchmark.suites`` (list) or the single ``benchmark.suite``."""
    benchmark = config.get("benchmark") or {}
    suites = benchmark.get("suites")
    if suites:
        return [str(s) for s in suites]
    return [str(benchmark.get("suite") or benchmark.get("name"))]


def _task_ids(config: Dict[str, Any], suite: str) -> List[int]:
    """Task list for a suite (explicit ``task_ids`` or the suite's own task count)."""
    benchmark = config.get("benchmark") or {}
    explicit = benchmark.get("task_ids")
    if explicit:
        return [int(task) for task in explicit]
    from libero.libero import benchmark as libero_benchmark  # only the eval env has this

    n_tasks = libero_benchmark.get_benchmark_dict()[suite]().n_tasks
    max_tasks = int(benchmark.get("max_tasks") or 0)
    if max_tasks > 0:
        n_tasks = min(n_tasks, max_tasks)
    return list(range(n_tasks))


def _shards(config: Dict[str, Any], total_workers: int) -> List[tuple]:
    """All shards across suites: ``(suite, task, offset, count)``."""
    episodes_per_task = int((config.get("benchmark") or {}).get("episodes_per_task") or 1)
    per_suite = [(suite, _task_ids(config, suite)) for suite in _suites(config)]
    total = max(1, sum(len(tasks) for _, tasks in per_suite) * max(1, episodes_per_task))
    chunk = chunk_size(total, total_workers)
    shards: List[tuple] = []
    for suite, tasks in per_suite:
        shards.extend(
            (suite, task, offset, count) for task, offset, count in build_shards(tasks, episodes_per_task, chunk)
        )
    return shards


def _shard_config(config: Dict[str, Any], shard: tuple, external_url: str, output_dir: str) -> Dict[str, Any]:
    """Build one unit's config: a copy of the base, minus coordinator, plus this shard."""
    suite, task_id, episode_offset, episode_count = shard
    shard_cfg = copy.deepcopy(config)
    shard_cfg.pop("coordinator", None)
    shard_cfg.pop("servers", None)
    shard_cfg.pop("pools", None)
    benchmark = shard_cfg.setdefault("benchmark", {})
    benchmark.pop("suites", None)
    benchmark["suite"] = suite
    benchmark["task_ids"] = [task_id]
    benchmark["episode_offset"] = episode_offset
    benchmark["episodes_per_task"] = episode_count
    # Per-suite overrides (e.g. LIBERO's official max_steps differ per suite:
    # 700 for libero_10, 400 for the rest) so one global queue can span suites.
    overrides = (config.get("benchmark") or {}).get("suite_overrides") or {}
    for key, value in (overrides.get(suite) or {}).items():
        benchmark[key] = value
    benchmark.pop("suite_overrides", None)
    run_cfg = shard_cfg.setdefault("run", {})
    run_cfg["output_dir"] = output_dir
    run_cfg["timestamped_output"] = False  # deterministic path so the coordinator can aggregate
    # Retry infra-level episode failures (episode_error / timeouts / server_unreachable) by
    # default: a transient sim<->server disconnect must not be scored as a model failure.
    # Model failures (not_successful_within_max_steps) are never retried.
    run_cfg.setdefault("max_retries", DEFAULT_MAX_RETRIES)
    shard_cfg.setdefault("server", {})["external_url"] = external_url
    return shard_cfg


def _aggregate(output_dir: Path, n_units: int) -> Dict[str, Any]:
    """Merge worker_*/results.jsonl into one deduped summary."""
    seen: Dict[str, bool] = {}
    for i in range(n_units):
        path = output_dir / f"worker_{i}" / "results.jsonl"
        if not path.exists():
            continue
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
    total = len(seen)
    successes = sum(seen.values())
    return {"n_episodes": total, "successes": successes, "success_rate": (successes / total) if total else None}


def _resolve_placement(config: Dict[str, Any]) -> tuple[List[int], int]:
    """Resolve (gpus, n_sims_per_server) from servers/pools, falling back to coordinator."""
    servers = config.get("servers") or []
    coordinator = config.get("coordinator") or {}
    gpus = (servers[0].get("gpus") if servers else None) or coordinator.get("gpus") or [0]
    n_per_server = (
        ((config.get("pools") or {}).get("cpu_sim") or {}).get("n_sims_per_server") or coordinator.get("n_workers") or 1
    )
    return [int(g) for g in gpus], max(1, int(n_per_server))


def _launch_unit(
    config: Dict[str, Any], shard: tuple, index: int, external_url: str, output_dir: Path, tmp: Path
) -> subprocess.Popen:
    """Launch one unit as an ``orchestrator.run`` subprocess pointed at its server."""
    shard_cfg = _shard_config(config, shard, external_url, str(output_dir / f"worker_{index}"))
    cfg_path = tmp / f"unit_{index}.json"
    cfg_path.write_text(json.dumps(shard_cfg), encoding="utf-8")
    log = open(output_dir / f"worker_{index}.log", "a", buffering=1)
    return subprocess.Popen(
        [sys.executable, "-m", "loongforge.embodied.eval.orchestrator.run", "--config", str(cfg_path)],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _drain_pool(pool: Dict[subprocess.Popen, int], wait: bool) -> Optional[subprocess.Popen]:
    """Return a finished process (or None if none finished and wait is False)."""
    while True:
        for proc in list(pool):
            if proc.poll() is not None:
                return proc
        if not wait:
            return None
        time.sleep(1)


def _run_batch(
    config: Dict[str, Any],
    shards: List[tuple],
    indices: List[int],
    urls: List[str],
    concurrency: int,
    output_dir: Path,
    tmp: Path,
) -> Tuple[Dict[int, int], List[int]]:
    """Run ``indices`` (into ``shards``) through a bounded pool fed by a global queue.

    A unit that exits non-zero is re-queued once, then reported as failed.
    Returns ``(returncodes_by_index, failed_indices)``.
    """
    returncodes: Dict[int, int] = {}
    failures: List[int] = []
    pending = list(indices)
    free_slots = list(range(concurrency))  # slot -> server = slot % len(urls)
    attempts: Dict[int, int] = {}
    pool: Dict[subprocess.Popen, tuple] = {}
    while pending or pool:
        while pending and free_slots:
            slot = free_slots.pop()
            index = pending.pop(0)
            url = urls[slot % len(urls)]
            pool[_launch_unit(config, shards[index], index, url, output_dir, tmp)] = (slot, index)
        finished = _drain_pool(pool, wait=True)
        slot, index = pool.pop(finished)
        free_slots.append(slot)
        returncodes[index] = finished.returncode
        if finished.returncode != 0:
            attempts[index] = attempts.get(index, 0) + 1
            if attempts[index] < MAX_ATTEMPTS:
                pending.append(index)  # re-queue once; a fresh unit may just be a flaky sim
            else:
                failures.append(index)
    return returncodes, failures


def run(config: Dict[str, Any], config_path: str = "") -> Dict[str, Any]:
    """Run a coordinated (G server : G x N worker) evaluation with dynamic dispatch."""
    gpus, n_per_server = _resolve_placement(config)
    concurrency = max(1, len(gpus) * n_per_server)
    plan = _shards(config, concurrency)

    output_dir = Path(str((config.get("run") or {}).get("output_dir") or "/tmp/vla_eval"))
    output_dir.mkdir(parents=True, exist_ok=True)

    base_args = apply_config(build_server_argparser().parse_args([]), config)
    base_args.config = config_path

    port_base = int(os.environ.get("LOONGFORGE_COORD_PORT_BASE", "14560"))
    servers = []
    for index, gpu in enumerate(gpus):
        port = port_base + PORT_STRIDE * index
        health_port = port + 1
        server_log = str(output_dir / f"policy_server_{index}.log")
        # The policy server reads its port from the config file, so a per-server
        # config is the only way to give each server its own port (``--port`` is
        # not a CLI flag of ``loongforge_server``).
        server_cfg = copy.deepcopy(config)
        server_cfg.setdefault("server", {})
        server_cfg["server"]["port"] = port
        server_cfg["server"]["health_port"] = health_port
        server_cfg["server"]["log"] = server_log
        server_cfg_path = output_dir / f"server_{index}.json"
        server_cfg_path.write_text(json.dumps(server_cfg), encoding="utf-8")

        server_args = copy.copy(base_args)
        server_args.config = str(server_cfg_path)
        server_args.port = port
        server_args.health_port = health_port
        server_args.server_log = server_log
        servers.append((start_server(server_args, {"CUDA_VISIBLE_DEVICES": str(gpu)}), server_args))
    urls = [f"ws://{args.host}:{args.port}" for _, args in servers]

    all_shards: List[tuple] = list(plan)
    failed_units: List[int] = []
    repair_rounds = 0
    started = time.time()
    try:
        for proc, args in servers:
            wait_healthz(f"http://{args.host}:{args.health_port}/healthz", args.server_start_timeout_sec, proc)
        started = time.time()  # worker wall only (exclude model load)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _, failed_units = _run_batch(
                config, all_shards, list(range(len(all_shards))), urls, concurrency, output_dir, tmp
            )
            # Coverage gate: re-dispatch episodes the plan asked for but that produced no
            # record at all, rather than aborting the run. Only a still-incomplete run
            # after MAX_REPAIR_ROUNDS is worth a human's attention.
            for _ in range(MAX_REPAIR_ROUNDS):
                missing = missing_keys(plan, read_records(output_dir, len(all_shards)))
                if not missing:
                    break
                first = len(all_shards)
                all_shards.extend(repair_shards(missing))
                _, extra_failed = _run_batch(
                    config, all_shards, list(range(first, len(all_shards))), urls, concurrency, output_dir, tmp
                )
                failed_units.extend(extra_failed)
                repair_rounds += 1
    finally:
        for proc, _ in servers:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()

    validation = validate(plan, read_records(output_dir, len(all_shards)))
    # Persist the plan so ``python -m ...validation`` can re-check a finished run offline.
    (output_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    result = {
        "benchmark": (config.get("benchmark") or {}).get("suites")
        or (config.get("benchmark") or {}).get("suite")
        or (config.get("benchmark") or {}).get("name"),
        "gpus": gpus,
        "n_sims_per_server": n_per_server,
        "concurrency": concurrency,
        "n_units": len(plan),
        "n_units_launched": len(all_shards),
        "repair_rounds": repair_rounds,
        "failed_units": failed_units,
        "elapsed_sec": round(time.time() - started, 1),
        "output_dir": str(output_dir),
        "validation": validation,
        "summary": _aggregate(output_dir, len(all_shards)),
    }
    (output_dir / "coordinator_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
