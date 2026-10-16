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
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from loongforge.embodied.eval.orchestrator.config import apply_config
from loongforge.embodied.eval.orchestrator.server_manager import (
    build_argparser as build_server_argparser,
    start_server,
    wait_healthz,
)
from loongforge.embodied.eval.transport.client import PolicyClient
from loongforge.embodied.eval.orchestrator.validation import (
    missing_keys,
    read_records,
    repair_shards,
    format_report,
    validate,
)

PORT_STRIDE = 20
UNITS_PER_SLOT = 4  # shards per worker slot: slack so fast slots can steal work
DEFAULT_MAX_RETRIES = 2  # episode-level infra retries (episode_error / timeouts) per unit
MAX_REPAIR_ROUNDS = 3  # auto re-dispatch rounds for episodes the plan asked for but that never ran
WATCHDOG_INTERVAL_SEC = 15.0  # how often the coordinator checks its servers are alive
WATCHDOG_RESTART_TIMEOUT_SEC = 600.0  # cap on waiting for a restarted server to get healthy
WATCHDOG_PROBE_INTERVAL_SEC = 60.0  # how often a live server is probed with a real ping
WATCHDOG_UNHEALTHY_LIMIT = 2  # consecutive failed probes before a hung server is restarted
MAX_SERVER_RESTARTS = 3  # a server that keeps dying is not worth restarting forever
PROBE_CONNECT_TIMEOUT_SEC = 3.0  # the probe must never stall the watchdog for long
PROBE_PING_TIMEOUT_SEC = 15.0  # a ping only waits behind ~N queued inferences (~1s at N=16)
# PolicyClient pops these while connecting (a proxy breaks a local websocket); the
# probe runs inside the coordinator, so put them back afterwards.
_PROXY_ENV_KEYS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
UNIT_TIMEOUT_SLACK_SEC = 300  # head-room over the episode budget before a unit is killed
POLICY_TIMEOUT_SAFETY = 2.0  # queue wait is (N-1) x service; keep 2x head-room
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


def _warn_policy_timeout(config: Dict[str, Any], n_per_server: int) -> None:
    """Warn when the per-call timeout is below the queue-wait floor.

    A server serialises requests, so the last of N sims waits about ``(N-1) * t_service``
    per step. If ``timeouts.policy_call_ms`` is below ``N * service_ms * safety`` the
    units hit spurious ``policy_timeout`` failures that look like model failures.
    ``timeouts.service_ms`` is the mean per-request service time from the calibration
    report (``service_mean_ms``); without it the check is skipped.
    """
    timeouts = config.get("timeouts") or {}
    service_ms = timeouts.get("service_ms")
    timeout_ms = timeouts.get("policy_call_ms")
    if not service_ms or not timeout_ms:
        return
    floor_ms = n_per_server * float(service_ms) * POLICY_TIMEOUT_SAFETY
    if float(timeout_ms) < floor_ms:
        print(
            f"[coordinator] WARNING: timeouts.policy_call_ms={timeout_ms}ms is below the queue-wait floor "
            f"{floor_ms:.0f}ms (N={n_per_server} x service={service_ms}ms x {POLICY_TIMEOUT_SAFETY}) -- "
            "units will hit spurious policy_timeout failures; raise it",
            file=sys.stderr,
            flush=True,
        )


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


def _unit_timeout_sec(config: Dict[str, Any], shards: List[tuple]) -> float:
    """Wall-clock budget for one unit.

    A unit runs ``episodes_per_task`` episodes of one task, so its budget is that
    many ``per_episode_sec`` timeouts plus connection setup and slack. This is a
    backstop against a unit that hangs in native code (where the runner's SIGALRM
    cannot fire) -- without it ``_await_slot`` would block forever. Override with
    ``run.unit_timeout_sec``.
    """
    explicit = float((config.get("run") or {}).get("unit_timeout_sec") or 0)
    if explicit > 0:
        return explicit
    per_episode = float((config.get("timeouts") or {}).get("per_episode_sec") or 1800)
    connect = float((config.get("server") or {}).get("connect_timeout_sec") or 60)
    max_episodes = max((count for *_, count in shards), default=1)
    return max_episodes * per_episode + connect + UNIT_TIMEOUT_SLACK_SEC


def _await_slot(
    pool: Dict[subprocess.Popen, tuple], started_at: Dict[int, float], timeout_sec: float
) -> Tuple[subprocess.Popen, bool]:
    """Block until a unit finishes or blows its wall-clock budget.

    Returns ``(proc, timed_out)``; a timed-out unit is killed here so its slot frees
    and it goes through the usual retry/repair path.
    """
    while True:
        for proc in list(pool):
            if proc.poll() is not None:
                return proc, False
        if timeout_sec > 0:
            now = time.time()
            for proc, (_, index) in list(pool.items()):
                if now - started_at[index] > timeout_sec:
                    print(f"[coordinator] unit {index} exceeded {timeout_sec:.0f}s; killing it", flush=True)
                    proc.kill()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                    return proc, True
        time.sleep(1)


def _terminate(proc: subprocess.Popen, timeout: float = 30) -> None:
    """Terminate a process, escalating to SIGKILL if it does not exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()


class _ServerWatchdog(threading.Thread):
    """Restart a policy server that has died, in place (same port).

    Units connect through ``server.external_url``, so they cannot restart the
    server they depend on -- only its owner (the coordinator) can. Restarting on
    the *same* port keeps every unit's URL valid, so in-flight units just
    reconnect on their next retry.
    """

    def __init__(
        self,
        servers: List[tuple],
        gpus: List[int],
        interval_sec: float = WATCHDOG_INTERVAL_SEC,
        probe_interval_sec: float = WATCHDOG_PROBE_INTERVAL_SEC,
    ) -> None:
        super().__init__(daemon=True, name="server-watchdog")
        self._servers = servers  # list of (proc, args); replaced in place on restart
        self._gpus = gpus
        self._interval = interval_sec
        self._probe_interval = probe_interval_sec
        self._stop_event = threading.Event()  # NB: not ``_stop`` -- Thread reserves that name
        self._unhealthy: Dict[int, int] = {}
        self._last_probe: Dict[int, float] = {}
        self._restart_attempts: Dict[int, int] = {}
        self.given_up: set = set()
        self.restarts = 0

    def stop(self) -> None:
        """Run stop."""
        self._stop_event.set()

    def run(self) -> None:
        """Poll each server: restart a dead process, and a live-but-hung one."""
        while not self._stop_event.wait(self._interval):
            now = time.time()
            for index, (proc, args) in enumerate(self._servers):
                if proc.poll() is not None:
                    self._unhealthy[index] = 0
                    self._restart(index)
                    continue
                # A live process is not necessarily a *serving* one: the healthz endpoint
                # is a separate thread, so it stays green even if the policy loop is
                # wedged. Only a real request proves liveness.
                if now - self._last_probe.get(index, 0.0) < self._probe_interval:
                    continue
                self._last_probe[index] = now
                if self._alive(args):
                    self._unhealthy[index] = 0
                    continue
                self._unhealthy[index] = self._unhealthy.get(index, 0) + 1
                if self._unhealthy[index] >= WATCHDOG_UNHEALTHY_LIMIT:
                    self._unhealthy[index] = 0
                    self._restart(index)

    def _alive(self, args: Any) -> bool:
        """A bounded ping round-trip against a live server."""
        saved = {key: os.environ[key] for key in _PROXY_ENV_KEYS if key in os.environ}
        client = None
        try:
            client = PolicyClient(host=args.host, port=args.port, timeout=PROBE_CONNECT_TIMEOUT_SEC)
            client.ping(timeout=PROBE_PING_TIMEOUT_SEC)
            return True
        except Exception:
            return False
        finally:
            os.environ.update(saved)  # PolicyClient pops these; restore them
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _restart(self, index: int) -> None:
        """Run _restart."""
        if self._stop_event.is_set() or index in self.given_up:
            return
        self._restart_attempts[index] = self._restart_attempts.get(index, 0) + 1
        if self._restart_attempts[index] > MAX_SERVER_RESTARTS:
            # A server that cannot come back (bad GPU, CUDA OOM, broken ckpt) is not
            # worth reloading forever -- stop, say so, and let the run report it.
            self.given_up.add(index)
            print(
                f"[watchdog] server {index} failed to come back {MAX_SERVER_RESTARTS} times; giving up "
                "-- this run needs a human",
                file=sys.stderr,
                flush=True,
            )
            return
        _, args = self._servers[index]
        print(f"[watchdog] server {index} died; restarting on port {args.port}", flush=True)
        try:
            proc = start_server(args, {"CUDA_VISIBLE_DEVICES": str(self._gpus[index])})
            wait_healthz(
                f"http://{args.host}:{args.health_port}/healthz",
                min(args.server_start_timeout_sec, WATCHDOG_RESTART_TIMEOUT_SEC),
                proc,
            )
        except Exception as exc:  # a failed restart is retried on the next tick
            print(f"[watchdog] restart of server {index} failed: {exc}", flush=True)
            return
        if self._stop_event.is_set():
            # We were shut down while this restart was in flight: the run's cleanup
            # has already terminated the old servers, so terminate this one too
            # rather than leaking a process that would hold a GPU.
            _terminate(proc)
            return
        self._servers[index] = (proc, args)
        self.restarts += 1
        print(f"[watchdog] server {index} back up (pid={proc.pid})", flush=True)


def _run_batch(
    config: Dict[str, Any],
    shards: List[tuple],
    indices: List[int],
    urls: List[str],
    concurrency: int,
    output_dir: Path,
    tmp: Path,
    timeout_sec: float = 0.0,
) -> Tuple[Dict[int, int], List[int]]:
    """Run ``indices`` (into ``shards``) through a bounded pool fed by a global queue.

    A unit that exits non-zero -- or that blows its ``timeout_sec`` budget -- is
    re-queued once, then reported as failed.
    Returns ``(returncodes_by_index, failed_indices)``.
    """
    returncodes: Dict[int, int] = {}
    failures: List[int] = []
    pending = list(indices)
    free_slots = list(range(concurrency))  # slot -> server = slot % len(urls)
    attempts: Dict[int, int] = {}
    started_at: Dict[int, float] = {}
    pool: Dict[subprocess.Popen, tuple] = {}
    while pending or pool:
        while pending and free_slots:
            slot = free_slots.pop()
            index = pending.pop(0)
            url = urls[slot % len(urls)]
            started_at[index] = time.time()
            pool[_launch_unit(config, shards[index], index, url, output_dir, tmp)] = (slot, index)
        finished, timed_out = _await_slot(pool, started_at, timeout_sec)
        slot, index = pool.pop(finished)
        free_slots.append(slot)
        returncodes[index] = finished.returncode
        if finished.returncode != 0 or timed_out:
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
    finished = output_dir / "coordinator_result.json"
    if finished.exists() and not (config.get("run") or {}).get("resume"):
        raise SystemExit(
            f"{finished} already exists -- refusing to overwrite a finished run. "
            "Use a fresh run.output_dir, or set run.resume: true to continue it."
        )
    _warn_policy_timeout(config, n_per_server)

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

    unit_timeout = _unit_timeout_sec(config, plan)
    all_shards: List[tuple] = list(plan)
    failed_units: List[int] = []
    repair_rounds = 0
    watchdog = _ServerWatchdog(servers, gpus)  # started once the servers are healthy
    started = time.time()
    try:
        for proc, args in servers:
            wait_healthz(f"http://{args.host}:{args.health_port}/healthz", args.server_start_timeout_sec, proc)
        started = time.time()  # worker wall only (exclude model load)
        watchdog.start()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            _, failed_units = _run_batch(
                config, all_shards, list(range(len(all_shards))), urls, concurrency, output_dir, tmp, unit_timeout
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
                    config, all_shards, list(range(first, len(all_shards))), urls, concurrency, output_dir, tmp, unit_timeout
                )
                failed_units.extend(extra_failed)
                repair_rounds += 1
    finally:
        watchdog.stop()
        watchdog.join(timeout=WATCHDOG_INTERVAL_SEC + 5)
        for proc, _ in servers:
            _terminate(proc)

    validation = validate(plan, read_records(output_dir, len(all_shards)))
    print(f"[coordinator] {format_report(validation)}", flush=True)
    if not validation["ok"]:
        print(
            f"[coordinator] WARNING: coverage still incomplete after {repair_rounds} repair round(s) "
            "-- this run needs a human; the score is partial",
            file=sys.stderr,
            flush=True,
        )
    elif validation["infra_failed"]:
        print(
            f"[coordinator] WARNING: {validation['infra_failed']} episode(s) failed at the infra level "
            "(they did not really run) -- the score understates the model; see validation.failures",
            file=sys.stderr,
            flush=True,
        )
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
        "unit_timeout_sec": unit_timeout,
        "failed_units": failed_units,
        "server_restarts": watchdog.restarts,
        "servers_given_up": sorted(watchdog.given_up),
        "elapsed_sec": round(time.time() - started, 1),
        "output_dir": str(output_dir),
        "validation": validation,
        "summary": _aggregate(output_dir, len(all_shards)),
    }
    (output_dir / "coordinator_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
