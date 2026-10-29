# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Formal calibration entry: sweep N, build the report, pick a fallback N.

One command runs the whole M0 calibration:

    python -m loongforge.embodied.eval.orchestrator.calibrate --config run.yaml --n 1,2,4,8,16,32

For each N it launches a coordinator (1 server + ~N workers) on its own GPU, in
parallel, and collects throughput + the server busy fraction into the report
produced by ``calibration_report.py``. It also derives a **fallback** N (the
smallest N reaching >=95% of the best measured throughput), validates it with
``calibration_config.validate_parallel_config`` and caches it keyed by
(model, benchmark, gpu). An agent's decision, when present, supersedes the
fallback -- the fallback is the deterministic safety net.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

from loongforge.embodied.eval.orchestrator.calibration_config import (
    calibration_key,
    save_calibration,
    validate_parallel_config,
)
from loongforge.embodied.eval.orchestrator.calibration_report import build_report, render_markdown
from loongforge.embodied.eval.orchestrator.planning import benchmark_family


def fallback_n(rows: Sequence[Dict[str, Any]], plateau_frac: float = 0.95) -> Any:
    """Smallest N whose throughput is >= ``plateau_frac`` of the best measured."""
    best = max((r.get("throughput_ep_per_min") or 0.0) for r in rows) if rows else 0.0
    if not best:
        return None
    for row in sorted(rows, key=lambda r: r["N"]):
        if (row.get("throughput_ep_per_min") or 0.0) >= plateau_frac * best:
            return row["N"]
    return None


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _sweep_rows(ns: Sequence[int], root: Path) -> List[Dict[str, Any]]:
    """Collect per-N metrics from each coordinator's result + server timing."""
    rows: List[Dict[str, Any]] = []
    for n in ns:
        result = _read_json(root / f"N{n}" / "out" / "coordinator_result.json")
        timing = _read_json(root / f"N{n}" / "timing.json")
        elapsed = result.get("elapsed_sec")
        summary = result.get("summary") or {}
        episodes = summary.get("n_episodes")
        requests, mean_ms = timing.get("count"), timing.get("mean")
        busy = round(requests * mean_ms / 1000.0 / elapsed, 3) if (requests and mean_ms and elapsed) else None
        rows.append({
            "N": n,
            "workers": result.get("n_workers"),
            "wall_sec": elapsed,
            "episodes": episodes,
            "requests": requests,
            "throughput_ep_per_min": round(episodes / elapsed * 60, 2) if (episodes and elapsed) else None,
            "server_busy_frac": busy,
            "service_mean_ms": round(mean_ms, 1) if mean_ms else None,
        })
    return rows


def run(
    config: Dict[str, Any],
    config_path: str = "",
    ns: Sequence[int] = (1, 2, 4, 8, 16),
    gpus: Sequence[int] | None = None,
    out_dir: str = "",
) -> Dict[str, Any]:
    """Run the N sweep, build the report, and cache a fallback calibrated config."""
    root = Path(out_dir or (config.get("calibrate") or {}).get("out_dir") or "/tmp/vla_calibrate")
    root.mkdir(parents=True, exist_ok=True)
    gpus = list(gpus or range(len(ns)))

    procs = []
    for index, n in enumerate(ns):
        cfg = copy.deepcopy(config)
        cfg.pop("calibrate", None)
        cfg["coordinator"] = {"n_workers": n}
        out = root / f"N{n}"
        cfg.setdefault("run", {})["output_dir"] = str(out / "out")
        server = cfg.setdefault("server", {})
        server["port"] = 14560 + 20 * index
        server["health_port"] = 14561 + 20 * index
        server["log"] = str(out / "policy_server.log")
        cfg_path = root / f"N{n}.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = str(gpus[index % len(gpus)])
        env["LOONGFORGE_EVAL_TIMING"] = "1"
        env["LOONGFORGE_EVAL_TIMING_PATH"] = str(out / "timing.json")
        env["LOONGFORGE_EVAL_TIMING_FLUSH_EVERY"] = "100"
        log = open(root / f"N{n}.log", "a", buffering=1)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "loongforge.embodied.eval.orchestrator.run", "--config", str(cfg_path)],
            cwd=str(Path(config_path).parent) if config_path else None,
            env=env, stdout=log, stderr=subprocess.STDOUT,
        ))
    returncodes = [p.wait() for p in procs]

    rows = _sweep_rows(ns, root)
    report = build_report(rows, _read_json(root / f"N{ns[0]}" / "timing.json"), env={})
    report_dir = root / "calibration_report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (report_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")

    # deterministic fallback: validate + cache it (an agent's decision supersedes).
    n_fallback = fallback_n(rows)
    pool_name = "gpu_sim" if benchmark_family(config) in {"robotwin", "simplerenv", "maniskill"} else "cpu_sim"
    env_facts = dict((config.get("resources") or {}).get(pool_name) or {})
    if env_facts:
        report["env"] = env_facts
        (report_dir / "env.json").write_text(json.dumps(env_facts, indent=2), encoding="utf-8")
    calibrated = {"calibrated": {"pools": {pool_name: {"n_sims_per_server": n_fallback}}}}
    validation = validate_parallel_config(calibrated, env_facts, 0.0) if n_fallback else {"result": "SKIP"}
    key = calibration_key(
        str((config.get("model") or {}).get("name") or "model"),
        str((config.get("benchmark") or {}).get("suite") or "benchmark"),
        str(gpus[0]),
    )
    if n_fallback:
        save_calibration(str(report_dir / "cache.json"), key, calibrated, report)

    return {
        "returncodes": returncodes,
        "sweep": rows,
        "fallback_n": n_fallback,
        "validation": validation,
        "cache_key": key,
        "report_dir": str(report_dir),
    }


def main() -> None:
    """Run main."""
    parser = argparse.ArgumentParser(description="Sweep N, build the calibration report, cache a fallback N.")
    parser.add_argument("--config", required=True, help="eval config (model/benchmark/server)")
    parser.add_argument("--n", required=True, help="comma-separated N values, e.g. 1,2,4,8,16,32")
    parser.add_argument("--gpus", default="", help="comma-separated GPU indices (default: 0..len(n)-1)")
    parser.add_argument("--out", default="", help="output directory for the sweep")
    args = parser.parse_args()

    from loongforge.embodied.eval.orchestrator.config import load_config

    config_path = str(Path(args.config).resolve())
    config = load_config(config_path)
    ns = [int(x) for x in args.n.split(",")]
    gpus = [int(x) for x in args.gpus.split(",")] if args.gpus else None
    print(json.dumps(run(config, config_path, ns, gpus, args.out), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
