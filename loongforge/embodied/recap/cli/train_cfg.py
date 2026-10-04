# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RECAP Step 4 — advantage-conditioned (CFG) policy training.

Two backends:

* ``mock`` (default, CPU) — validates the whole base/positive/negative routing and
  the RECAP objective on a tiny stand-in policy, using an already-prepared
  advantages sidecar. This is what the smoke script runs.
* ``pi05`` — delegates to the stock embodied trainer with
  ``--trainer-type RecapCfgTrainer``; the remaining CLI args are the standard
  pi0.5 training args (needs a pi0.5 checkpoint + tokenizer).

Usage:
    python -m loongforge.embodied.recap.cli.train_cfg \
        --dataset-path /tmp/recap_mock --preset baseline --policy-backend mock
"""

from __future__ import annotations

import argparse
import sys

from ._common import (
    add_config_args,
    add_dataset_compat_args,
    add_subset_args,
    episode_subset,
    load_config,
)
from ..data.compat import build_recap_dataset_auto
from ..data.manifest import resolve_tag
from ..testing.mock_policy import run_mock_cfg_training


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RECAP: advantage-conditioned policy training.")
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--advantages-tag", default=None)
    parser.add_argument("--policy-backend", default="mock", choices=["mock", "pi05"])
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-2)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--seed", type=int, default=0)
    add_config_args(parser)
    add_subset_args(parser)
    add_dataset_compat_args(parser)
    return parser


def main() -> None:
    parser = build_parser()
    args, passthrough = parser.parse_known_args()
    cfg = load_config(args)

    if args.policy_backend == "pi05":
        _delegate_to_embodied_trainer(passthrough)
        return

    dataset = build_recap_dataset_auto(
        args.dataset_path,
        action_horizon=1,
        advantages_tag=resolve_tag(args.dataset_path, "advantages", args.advantages_tag),
        episodes=episode_subset(args.dataset_path, args.max_episodes),
        video_cache_size=args.video_cache_size,
        frame_cache_root=args.frame_cache_root,
        frame_reader_cache_size=args.frame_reader_cache_size,
        force_success=(args.dataset_type == "sft"),
        compat=args.dataset_compat,
    )
    if "advantage_indicator" not in dataset[0]:
        raise RuntimeError(
            "no advantages sidecar found — run compute_advantages first "
            f"(looked under {args.dataset_path}/meta)"
        )

    print(
        f"[recap] cfg training (mock backend): {len(dataset)} frames, "
        f"mode={cfg.mode} alpha={cfg.policy.alpha} "
        f"base_branch={cfg.policy.use_base_branch} "
        f"advantage_dropout={cfg.policy.use_advantage_dropout}"
    )
    last = run_mock_cfg_training(
        dataset,
        cfg,
        steps=args.steps,
        batch_size=args.batch_size,
        lr=args.lr,
        seed=args.seed,
        device=args.device,
    )
    print(f"[recap] cfg smoke finished: {last}")


def _delegate_to_embodied_trainer(passthrough: list[str]) -> None:
    """Hand the remaining args to the stock embodied training entry point."""
    sys.argv = [sys.argv[0]] + passthrough
    from loongforge.embodied.train import main as embodied_main

    embodied_main()


if __name__ == "__main__":
    main()
