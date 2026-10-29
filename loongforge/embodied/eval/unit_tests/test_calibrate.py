# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the calibration entry (pure helpers)."""

from __future__ import annotations

from loongforge.embodied.eval.orchestrator.calibrate import (
    _point_config,
    _read_timing,
    _sweep_rows,
    fallback_n,
    is_gpu_sim,
)


def test_fallback_n_picks_smallest_reaching_threshold() -> None:
    """Run test_fallback_n_picks_smallest_reaching_threshold."""
    rows = [
        {"N": 1, "throughput_ep_per_min": 1.4},
        {"N": 2, "throughput_ep_per_min": 2.5},
        {"N": 4, "throughput_ep_per_min": 3.8},
        {"N": 8, "throughput_ep_per_min": 5.1},
        {"N": 16, "throughput_ep_per_min": 6.8},
    ]
    # best = 6.8, 95% = 6.46 -> smallest N reaching it is 16
    assert fallback_n(rows) == 16


def test_fallback_n_empty_or_missing_throughput() -> None:
    """Run test_fallback_n_empty_or_missing_throughput."""
    assert fallback_n([]) is None
    assert fallback_n([{"N": 1, "throughput_ep_per_min": None}]) is None


def test_sweep_rows_reads_result_and_timing(tmp_path) -> None:
    """Run test_sweep_rows_reads_result_and_timing."""
    out = tmp_path / "N4" / "out"
    out.mkdir(parents=True)
    (out / "coordinator_result.json").write_text(
        '{"elapsed_sec": 100.0, "n_workers": 4, "summary": {"n_episodes": 4}}'
    )
    (tmp_path / "N4" / "timing.json").write_text('{"count": 400, "mean": 50.0}')
    rows = _sweep_rows([4], tmp_path)
    assert rows[0]["N"] == 4
    assert rows[0]["throughput_ep_per_min"] == 2.4  # 4 / 100 * 60
    assert rows[0]["server_busy_frac"] == 0.2  # 400 * 50ms / 1000 / 100s


def test_read_timing_aggregates_per_server_files(tmp_path) -> None:
    """GPU-sim starts one server per GPU; each writes its own timing file."""
    (tmp_path / "timing.json.server0").write_text('{"count": 100, "mean": 10.0, "max": 20.0}')
    (tmp_path / "timing.json.server1").write_text('{"count": 300, "mean": 30.0, "max": 40.0}')
    timing = _read_timing(tmp_path / "timing.json")
    assert timing["count"] == 400
    assert timing["mean"] == 25.0  # weighted by request count
    assert timing["max"] == 40.0
    assert timing["servers"] == 2


def test_point_config_gpu_sim_uses_all_gpus_and_covers_slots(tmp_path) -> None:
    """GPU-sim: N is sims per server and every point uses all server GPUs."""
    config = {
        "benchmark": {"name": "robotwin", "episodes_per_task": 4},
        "servers": [{"gpus": [0, 1]}],
        "pools": {"gpu_sim": {"placement": "co_locate"}},
    }
    cfg, cfg_path = _point_config(config, 2, 0, tmp_path, gpu_sim=True, gpus=[0, 1])
    assert cfg["servers"] == [{"gpus": [0, 1]}]
    assert cfg["pools"]["gpu_sim"]["n_sims_per_server"] == 2
    assert cfg["benchmark"]["episodes_per_task"] == 4  # 2 GPUs x 2 sims
    assert "coordinator" not in cfg
    assert cfg_path.exists()


def test_point_config_cpu_sim_sets_workers(tmp_path) -> None:
    config = {"benchmark": {"name": "libero"}, "servers": [{"gpus": [0]}]}
    cfg, _ = _point_config(config, 8, 0, tmp_path, gpu_sim=False, gpus=[0])
    assert cfg["coordinator"]["n_workers"] == 8
    assert "pools" not in cfg or "gpu_sim" not in (cfg.get("pools") or {})


def test_is_gpu_sim_detects_sapien_families() -> None:
    assert is_gpu_sim({"benchmark": {"name": "robotwin"}})
    assert is_gpu_sim({"benchmark": {"name": "simplerenv"}})
    assert is_gpu_sim({"benchmark": {"name": "maniskill"}})
    assert not is_gpu_sim({"benchmark": {"name": "libero"}})
    assert not is_gpu_sim({"benchmark": {"name": "calvin"}})
