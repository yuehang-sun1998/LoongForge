# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Temporary utility: import normalization stats from a LeRobot pi0.5 checkpoint.

LeRobot checkpoints carry their dataset statistics inside the policy preprocessor
(``policy_preprocessor_step_*_normalizer_processor.safetensors``), not in the
dataset's ``meta/stats.json``. Our reader expects the stats on the dataset side,
so this script extracts ``observation.state`` / ``action`` stats (min/max/mean/std
and q01/q99) from that file and merges them into ``<dataset>/meta/stats.json``.

This is a stop-gap for external rollout datasets that ship without stats; the
model weights are not touched.

Usage:
    python -m loongforge.embodied.recap.cli.import_stats \
        --normalizer /path/to/policy_preprocessor_step_2_normalizer_processor.safetensors \
        --dataset-path /path/to/rollout_dataset
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict

STAT_KEYS = ("min", "max", "mean", "std", "q01", "q99")
FEATURES = ("observation.state", "action")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import pi0.5 normalizer stats into a dataset.")
    parser.add_argument(
        "--normalizer",
        required=True,
        help="Path to the normalizer safetensors, or a checkpoint dir containing one.",
    )
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument(
        "--mode",
        default="mean_std",
        choices=["mean_std", "q99"],
        help="Normalization mode these stats will be used with (recorded for traceability).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the stats without writing.")
    return parser


def _resolve_normalizer(path: str) -> Path:
    path = Path(path)
    if path.is_dir():
        matches = sorted(path.glob("policy_preprocessor_step_*_normalizer_processor.safetensors"))
        if not matches:
            raise FileNotFoundError(f"no normalizer processor safetensors found in {path}")
        return matches[-1]
    return path


def extract_stats(normalizer_path: Path) -> Dict[str, Dict[str, list]]:
    from safetensors.torch import load_file

    state_dict = load_file(str(normalizer_path))
    stats: Dict[str, Dict[str, list]] = {}
    for feature in FEATURES:
        entry = {}
        for key in STAT_KEYS:
            tensor_key = f"{feature}.{key}"
            if tensor_key in state_dict:
                entry[key] = state_dict[tensor_key].reshape(-1).tolist()
        if entry:
            stats[feature] = entry
    if not stats:
        raise RuntimeError(f"no {FEATURES} stats found in {normalizer_path}")
    return stats


def main() -> None:
    args = build_parser().parse_args()
    normalizer = _resolve_normalizer(args.normalizer)
    stats = extract_stats(normalizer)

    print(f"[recap] stats from {normalizer}")
    for feature, entry in stats.items():
        dim = len(next(iter(entry.values())))
        print(f"[recap]   {feature}: dim={dim} keys={sorted(entry)} mode={args.mode}")
    if args.dry_run:
        print(json.dumps(stats, indent=2)[:1200])
        return

    stats_path = Path(args.dataset_path) / "meta" / "stats.json"
    existing = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    existing.update(stats)
    stats_path.write_text(json.dumps(existing, indent=2))
    print(f"[recap] merged into {stats_path} (now has {sorted(existing)})")


if __name__ == "__main__":
    main()
