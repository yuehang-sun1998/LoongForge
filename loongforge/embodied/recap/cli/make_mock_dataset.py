# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Generate a mock LeRobot v2 rollout dataset for RECAP smoke tests.

Usage:
    python -m loongforge.embodied.recap.cli.make_mock_dataset --output /tmp/recap_mock
"""

from __future__ import annotations

import argparse

from ..testing.mock import make_mock_rollout_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a mock RECAP rollout dataset.")
    parser.add_argument("--output", required=True, help="Dataset root to create.")
    parser.add_argument("--num-tasks", type=int, default=2)
    parser.add_argument("--episodes-per-task", type=int, default=2)
    parser.add_argument("--episode-length", type=int, default=8)
    parser.add_argument("--state-dim", type=int, default=7)
    parser.add_argument("--action-dim", type=int, default=7)
    parser.add_argument("--image-size", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    root = make_mock_rollout_dataset(
        args.output,
        num_tasks=args.num_tasks,
        episodes_per_task=args.episodes_per_task,
        episode_length=args.episode_length,
        state_dim=args.state_dim,
        action_dim=args.action_dim,
        image_size=args.image_size,
        seed=args.seed,
    )
    print(f"[recap] mock rollout dataset written to {root}")


if __name__ == "__main__":
    main()
