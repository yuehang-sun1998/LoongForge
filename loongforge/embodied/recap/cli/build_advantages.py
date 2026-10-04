# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""One-shot orchestration of the RECAP advantage pipeline.

Runs ``compute_returns -> train_value -> compute_advantages`` for one dataset,
threading a single derived tag through all three stages and recording the
determinants in ``<dataset>/meta/recap_pipeline_manifest.json``.

The tag is derived from (dataset version, reward config, advantage config,
value checkpoint), so a stale sidecar cannot be silently reused: change any
determinant and the tag changes with it.

Usage:
    # full pipeline (trains the value critic)
    python -m loongforge.embodied.recap.cli.build_advantages \
        --dataset-path /path/to/rollout --preset baseline \
        --siglip-path .../siglip2-so400m-patch14-224 --gemma3-path .../gemma-3-270m

    # reuse an existing value checkpoint (skip training)
    python -m loongforge.embodied.recap.cli.build_advantages \
        --dataset-path /path/to/rollout --preset baseline \
        --value-checkpoint /path/to/critic.pt

    # only recompute returns + advantages from an existing critic
    python -m loongforge.embodied.recap.cli.build_advantages --stages advantages ...
"""

from __future__ import annotations

import argparse
import sys
from typing import List

from ._common import add_config_args, add_dataset_compat_args, add_subset_args, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the RECAP advantage pipeline end to end.")
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--output-dir", default=None,
                        help="Where to write the value checkpoint (default: <dataset>/recap).")
    parser.add_argument("--value-checkpoint", default=None,
                        help="Reuse an existing value checkpoint instead of training one.")
    parser.add_argument("--stages", default="all",
                        choices=["all", "returns", "value", "advantages"],
                        help="all: returns -> value -> advantages; or run a single stage.")
    # value-model assets
    parser.add_argument("--backend", default="critic", choices=["placeholder", "critic"])
    parser.add_argument("--siglip-path", default=None)
    parser.add_argument("--gemma3-path", default=None)
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--critic-expert-variant", default="gemma_1m")
    parser.add_argument("--critic-lr", type=float, default=1.0e-4)
    parser.add_argument("--critic-v-min", type=float, default=-1.0)
    parser.add_argument("--critic-v-max", type=float, default=0.0)
    parser.add_argument("--critic-batch-size", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--device", default=None)
    add_config_args(parser)
    add_subset_args(parser)
    add_dataset_compat_args(parser)
    return parser


def _run_stage(module_main, argv: List[str]) -> None:
    """Invoke a stage's ``main()`` with a synthesized argv."""
    print(f"[pipeline] {' '.join(argv)}", flush=True)
    saved = sys.argv
    # Name argv[0] after the stage so argparse errors are attributable.
    sys.argv = [f"recap.cli.{module_main.__module__.rsplit('.', 1)[-1]}"] + argv
    try:
        module_main()
    finally:
        sys.argv = saved


def main() -> None:
    args = build_parser().parse_args()
    load_config(args)  # fail fast on an invalid config

    # Args every stage understands.
    common = ["--dataset-path", args.dataset_path, "--dataset-type", args.dataset_type]
    if args.config:
        common += ["--config", args.config]
    if args.preset:
        common += ["--preset", args.preset]
    if args.max_episodes is not None:
        common += ["--max-episodes", str(args.max_episodes)]

    # Only the image-consuming stages (value / advantages) take these.
    data_flags = []
    if args.dataset_compat:
        data_flags += ["--dataset-compat", args.dataset_compat]
    if args.frame_cache_root:
        data_flags += ["--frame-cache-root", args.frame_cache_root]
    if args.frame_reader_cache_size is not None:
        data_flags += ["--frame-reader-cache-size", str(args.frame_reader_cache_size)]

    run_returns = args.stages in ("all", "returns")
    # "value" means train the value model, then (re)compute advantages from it.
    run_value = args.stages in ("all", "value") and args.value_checkpoint is None
    run_advantages = args.stages in ("all", "advantages", "value")

    if run_returns:
        from .compute_returns import main as returns_main

        _run_stage(returns_main, common)

    checkpoint = args.value_checkpoint
    if run_value:
        from .train_value import main as value_main

        output = args.output_dir or f"{args.dataset_path}/recap/critic.pt"
        stage_args = common + data_flags + [
            "--output", output,
            "--backend", args.backend,
            "--batch-size", str(args.batch_size or args.critic_batch_size),
            "--critic-expert-variant", args.critic_expert_variant,
            "--critic-lr", str(args.critic_lr),
            "--critic-v-min", str(args.critic_v_min),
            "--critic-v-max", str(args.critic_v_max),
        ]
        for name, value in (("--siglip-path", args.siglip_path), ("--gemma3-path", args.gemma3_path),
                            ("--tokenizer-path", args.tokenizer_path), ("--device", args.device),
                            ("--epochs", args.epochs)):
            if value is not None:
                stage_args += [name, str(value)]
        _run_stage(value_main, stage_args)
        checkpoint = output

    if run_advantages:
        if not checkpoint:
            raise SystemExit(
                "--stages advantages/value needs --value-checkpoint, or use --stages all"
            )
        from .compute_advantages import main as advantages_main

        stage_args = common + data_flags + [
            "--value-checkpoint", checkpoint,
            "--value-backend", args.backend,
            "--critic-batch-size", str(args.critic_batch_size),
        ]
        for name, value in (("--siglip-path", args.siglip_path), ("--gemma3-path", args.gemma3_path),
                            ("--tokenizer-path", args.tokenizer_path), ("--device", args.device)):
            if value is not None:
                stage_args += [name, str(value)]
        _run_stage(advantages_main, stage_args)

    from ..data.manifest import manifest_path, read_manifest

    manifest = read_manifest(args.dataset_path)
    print(f"[pipeline] manifest -> {manifest_path(args.dataset_path)}")
    for stage, record in manifest.get("stages", {}).items():
        print(f"[pipeline]   {stage}: {record.get('filename')} tag={record.get('tag')}")


if __name__ == "__main__":
    main()
