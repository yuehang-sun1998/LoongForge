# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the calibration entry (pure helpers)."""

from __future__ import annotations

from loongforge.embodied.eval.orchestrator.calibrate import _sweep_rows, fallback_n


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
