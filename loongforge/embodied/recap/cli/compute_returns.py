# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: examples/offline_rl/advantage_labeling/recap/process/compute_returns.py
# CLI mirror of the upstream step-1 script.
# Copyright 2026 The RLinf Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""RECAP Step 1 — compute returns and write the ``returns`` sidecar.

Usage:
    python -m loongforge.embodied.recap.cli.compute_returns \
        --dataset-path /tmp/recap_mock --preset baseline
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ._common import (
    add_config_args,
    add_subset_args,
    episode_subset,
    load_config,
    read_episode_specs,
)
from ..core.returns import returns_to_dataframe
from ..data.manifest import record_stage, returns_tag
from ..data.sidecar import SIDECAR_RETURNS, write_sidecar


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RECAP: compute returns sidecar.")
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--tag", default=None, help="Optional sidecar tag -> returns_<tag>.parquet")
    parser.add_argument("--gamma", type=float, default=None)
    parser.add_argument("--no-discount", action="store_true")
    add_config_args(parser)
    add_subset_args(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args)

    specs = read_episode_specs(args.dataset_path, args.max_episodes, args.dataset_type)
    frame = returns_to_dataframe(specs, cfg)

    # Derive the tag from (dataset version, reward config) unless one is given.
    derived = returns_tag(
        args.dataset_path, cfg,
        episodes=episode_subset(args.dataset_path, args.max_episodes),
        dataset_type=args.dataset_type,
    )
    tag = args.tag or f"{derived['summary']}_{derived['tag']}"
    path = write_sidecar(args.dataset_path, SIDECAR_RETURNS, frame, tag)
    record_stage(
        args.dataset_path, "returns", path.name,
        {"tag": tag, "determinants": derived["determinants"]},
    )

    successes = [spec for spec in specs if spec.is_success]
    failures = [spec for spec in specs if not spec.is_success]
    success_mean = frame.loc[frame["is_success"], "return"].mean() if successes else float("nan")
    failure_mean = frame.loc[~frame["is_success"], "return"].mean() if failures else float("nan")
    print(
        f"[recap] returns: {len(specs)} episodes, {len(frame)} frames -> {path}\n"
        f"[recap]   mode={cfg.mode} gamma={cfg.reward.gamma} use_discount={cfg.reward.use_discount}\n"
        f"[recap]   reward: success={cfg.reward.success_reward} failure={cfg.reward.failure_reward} "
        f"step={cfg.reward.step_reward}\n"
        f"[recap]   success episodes={len(successes)} mean_return={success_mean:.4f}\n"
        f"[recap]   failure episodes={len(failures)} mean_return={failure_mean:.4f}"
    )
    # Keep a resolved copy next to the dataset so every downstream stage is traceable.
    cfg.save(Path(args.dataset_path) / "meta" / "recap_config.yaml")
    _update_return_stats(args.dataset_path, frame)


def _update_return_stats(dataset_path, frame) -> None:
    """Merge return/reward stats into meta/stats.json (upstream does this too).

    compute_advantages reads the global return range from these stats to build the
    normaliser, so this must run before it.
    """
    import json

    stats_path = Path(dataset_path) / "meta" / "stats.json"
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    for column in ("return", "reward"):
        values = frame[column].to_numpy()
        stats[column] = {
            "mean": float(values.mean()),
            "std": float(values.std()),
            "min": float(values.min()),
            "max": float(values.max()),
        }
    stats_path.write_text(json.dumps(stats, indent=2))
    print(f"[recap]   updated {stats_path} (return range [{stats['return']['min']:.3f}, {stats['return']['max']:.3f}])")


if __name__ == "__main__":
    main()
