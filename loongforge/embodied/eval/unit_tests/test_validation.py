# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coordinated-run coverage validation."""

from __future__ import annotations

from typing import Any, Dict

from loongforge.embodied.eval.orchestrator.validation import (
    expected_keys,
    format_report,
    key_of,
    missing_keys,
    repair_shards,
    validate,
)


def _rec(suite: str, task: int, episode: int, success: bool = True, reason: str | None = None) -> Dict[str, Any]:
    return {
        "episode_id": f"libero/{suite}/task={task}/episode={episode}",
        "task_suite": suite,
        "task_id": task,
        "episode_idx": episode,
        "success": success,
        "failure_reason": reason,
    }


def test_expected_keys_and_key_of_agree() -> None:
    """Run test_expected_keys_and_key_of_agree."""
    plan = [("libero_10", 0, 0, 2), ("libero_10", 1, 5, 2)]
    assert expected_keys(plan) == {("libero_10", 0, 0), ("libero_10", 0, 1), ("libero_10", 1, 5), ("libero_10", 1, 6)}
    assert key_of(_rec("libero_10", 3, 7)) == ("libero_10", 3, 7)
    assert key_of({"episode_id": "libero/libero_goal/task=2/episode=9"}) == ("libero_goal", 2, 9)
    assert key_of({"episode_id": "weird"}) is None


def test_repair_shards_coalesces_contiguous_runs() -> None:
    """Run test_repair_shards_coalesces_contiguous_runs."""
    keys = [("a", 0, 3), ("a", 0, 4), ("a", 0, 5), ("a", 0, 9), ("b", 1, 2)]
    assert repair_shards(keys) == [("a", 0, 3, 3), ("a", 0, 9, 1), ("b", 1, 2, 1)]


def test_validate_flags_missing_duplicates_and_unexpected() -> None:
    """Run test_validate_flags_missing_duplicates_and_unexpected."""
    plan = [("a", 0, 0, 3)]
    records = [_rec("a", 0, 0), _rec("a", 0, 1), _rec("a", 0, 1), _rec("a", 0, 9)]
    report = validate(plan, records)
    assert not report["ok"]
    assert report["missing"] == 1
    assert report["duplicates"] == 1
    assert report["unexpected"] == 1
    assert report["missing_examples"] == ["a/task=0/episode=2"]


def test_validate_separates_infra_failures_from_model_failures() -> None:
    """Run test_validate_separates_infra_failures_from_model_failures."""
    plan = [("a", 0, 0, 3)]
    records = [
        _rec("a", 0, 0, success=False, reason="not_successful_within_max_steps"),
        _rec("a", 0, 1, success=False, reason="episode_error"),
        _rec("a", 0, 2, success=True),
    ]
    report = validate(plan, records)
    assert report["ok"]
    assert report["successes"] == 1
    assert report["infra_failed"] == 1
    assert report["failures"] == {"not_successful_within_max_steps": 1, "episode_error": 1}
    assert "success_rate=0.3333" in format_report(report)


def test_missing_keys_is_empty_for_a_complete_run() -> None:
    """Run test_missing_keys_is_empty_for_a_complete_run."""
    plan = [("a", 0, 0, 2)]
    assert missing_keys(plan, [_rec("a", 0, 0), _rec("a", 0, 1)]) == []
    assert missing_keys(plan, [_rec("a", 0, 0)]) == [("a", 0, 1)]
