# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Post-run coverage validation for a coordinated evaluation.

A coordinated run is only trustworthy if the episodes that *ran* are exactly the
episodes the plan *asked for*: nothing missing, nothing duplicated, nothing
extra. ``_aggregate`` merely dedups and counts successes, so a dropped or
re-run unit would silently skew the rate.

This module only *reports*; the coordinator turns a "missing" report into a
repair round (it re-dispatches the missing episodes) rather than aborting the
run. Infra-level failures (``episode_error`` / timeouts / ``server_unreachable``)
are reported separately from model failures: a model failure is a legitimate
outcome, an infra failure means the episode did not really run.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Failure reasons that mean "the episode did not actually run" (see
# libero_runner._classify_exception + the retry set).
INFRA_FAILURE_REASONS = frozenset(
    {"policy_timeout", "env_timeout", "episode_timeout", "server_unreachable", "episode_error"}
)

EpisodeKey = Tuple[str, int, int]
Shard = Tuple[str, int, int, int]
_EPISODE_RE = re.compile(r"^[^/]+/([^/]+)/task=(\d+)/episode=(\d+)$")


def expected_keys(plan: Sequence[Shard]) -> set:
    """Expected ``(suite, task, episode)`` keys from a shard plan.

    ``plan`` items are ``(suite, task_id, episode_offset, episode_count)``.
    """
    return {(str(suite), int(task), ep) for suite, task, offset, count in plan for ep in range(offset, offset + count)}


def key_of(record: Dict[str, Any]) -> Optional[EpisodeKey]:
    """Canonical key for a result record, or None when it cannot be derived."""
    suite, task, episode = record.get("task_suite"), record.get("task_id"), record.get("episode_idx")
    if suite is not None and task is not None and episode is not None:
        return (str(suite), int(task), int(episode))
    match = _EPISODE_RE.match(str(record.get("episode_id") or ""))
    if match:
        return (match.group(1), int(match.group(2)), int(match.group(3)))
    return None


def read_records(output_dir: Path, n_units: int) -> List[Dict[str, Any]]:
    """Read every episode record from ``worker_*/results.jsonl``."""
    records: List[Dict[str, Any]] = []
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
                records.append(record)
    return records


def _split(records: Iterable[Dict[str, Any]]) -> Tuple[Dict[EpisodeKey, Dict[str, Any]], List[EpisodeKey], int]:
    """Dedup records by key; return (unique, duplicates, unkeyed count)."""
    seen: Dict[EpisodeKey, Dict[str, Any]] = {}
    duplicates: List[EpisodeKey] = []
    unkeyed = 0
    for record in records:
        key = key_of(record)
        if key is None:
            unkeyed += 1
            continue
        if key in seen:
            duplicates.append(key)
            continue
        seen[key] = record
    return seen, duplicates, unkeyed


def missing_keys(plan: Sequence[Shard], records: Iterable[Dict[str, Any]]) -> List[EpisodeKey]:
    """Episodes the plan asked for that produced no record at all."""
    seen, _, _ = _split(records)
    return sorted(expected_keys(plan) - set(seen))


def repair_shards(keys: Iterable[EpisodeKey]) -> List[Shard]:
    """Coalesce missing keys back into contiguous ``(suite, task, offset, count)`` shards."""
    by_task: Dict[Tuple[str, int], List[int]] = {}
    for suite, task, episode in keys:
        by_task.setdefault((suite, task), []).append(episode)
    shards: List[Shard] = []
    for (suite, task), episodes in sorted(by_task.items()):
        episodes = sorted(set(episodes))
        start = prev = episodes[0]
        for episode in episodes[1:]:
            if episode == prev + 1:
                prev = episode
                continue
            shards.append((suite, task, start, prev - start + 1))
            start = prev = episode
        shards.append((suite, task, start, prev - start + 1))
    return shards


def validate(plan: Sequence[Shard], records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare the plan's episode set against what the run actually produced."""
    seen, duplicates, unkeyed = _split(records)
    expected = expected_keys(plan)
    actual = set(seen)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)

    failures: Dict[str, int] = {}
    infra_failed = 0
    successes = 0
    for record in seen.values():
        if record.get("success"):
            successes += 1
            continue
        reason = str(record.get("failure_reason") or "unknown")
        failures[reason] = failures.get(reason, 0) + 1
        if reason in INFRA_FAILURE_REASONS:
            infra_failed += 1

    n_expected, n_actual = len(expected), len(actual)
    return {
        "ok": not missing and not duplicates and not unexpected and unkeyed == 0,
        "expected": n_expected,
        "actual": n_actual,
        "missing": len(missing),
        "duplicates": len(duplicates),
        "unexpected": len(unexpected),
        "unkeyed": unkeyed,
        "successes": successes,
        "success_rate": (successes / n_actual) if n_actual else None,
        "failures": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        "infra_failed": infra_failed,
        "missing_examples": [f"{s}/task={t}/episode={e}" for s, t, e in missing[:10]],
        "duplicate_examples": [f"{s}/task={t}/episode={e}" for s, t, e in duplicates[:10]],
        "unexpected_examples": [f"{s}/task={t}/episode={e}" for s, t, e in unexpected[:10]],
    }


def format_report(report: Dict[str, Any]) -> str:
    """One-paragraph human summary of a validation report."""
    status = "OK" if report["ok"] else "INCOMPLETE"
    parts = [
        f"coverage {status}: {report['actual']}/{report['expected']} episodes",
        f"missing={report['missing']}",
        f"duplicates={report['duplicates']}",
        f"unexpected={report['unexpected']}",
    ]
    if report["unkeyed"]:
        parts.append(f"unkeyed={report['unkeyed']}")
    if report["infra_failed"]:
        parts.append(f"infra_failed={report['infra_failed']}")
    if report["success_rate"] is not None:
        parts.append(f"success_rate={report['success_rate']:.4f}")
    return " | ".join(parts)


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate episode coverage of a coordinated eval run.")
    parser.add_argument("--output-dir", required=True, help="coordinator output dir (holds worker_*/results.jsonl)")
    parser.add_argument("--n-units", type=int, required=True, help="number of units the coordinator launched")
    parser.add_argument("--plan", default="", help="JSON file with the shard plan: [[suite, task, offset, count], ...]")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when coverage is incomplete")
    return parser


def main() -> None:
    """Run main."""
    args = _build_argparser().parse_args()
    plan = [tuple(item) for item in json.loads(Path(args.plan).read_text(encoding="utf-8"))] if args.plan else []
    report = validate(plan, read_records(Path(args.output_dir), args.n_units))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
