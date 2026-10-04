# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: examples/offline_rl/advantage_labeling/recap/process/compute_advantages.py
# Where this file and RLinf disagree, RLinf is authoritative.
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

"""RECAP Step 3 — compute advantages and write the ``advantages`` sidecar.

Usage:
    python -m loongforge.embodied.recap.cli.compute_advantages \
        --dataset-path /tmp/recap_mock --value-checkpoint /tmp/recap_mock/value.pt
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from ._common import (
    add_config_args,
    add_dataset_compat_args,
    add_subset_args,
    episode_subset,
    load_config,
)
from ..core.advantage import advantages_to_dataframe
from ..data.compat import build_recap_dataset_auto
from ..data.manifest import advantages_tag, record_stage, resolve_tag
from ..data.sidecar import SIDECAR_ADVANTAGES, write_sidecar
from ..data.placeholder_value import load_value_checkpoint, predict_values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RECAP: compute advantage sidecar.")
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--value-checkpoint", required=True)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--returns-tag", default=None,
                        help="Returns sidecar tag; defaults to the manifest record.")
    parser.add_argument("--horizon", type=int, default=None)
    parser.add_argument(
        "--positive-quantile",
        type=float,
        default=None,
        help="Top fraction of advantages marked positive (positive_quantile).",
    )
    parser.add_argument("--no-discount", action="store_true")
    parser.add_argument("--return-min", type=float, default=None,
                        help="Global return range for normalisation (else from meta/stats.json).")
    parser.add_argument("--return-max", type=float, default=None)
    parser.add_argument("--value-backend", default="placeholder", choices=["placeholder", "critic"],
                        help="placeholder: tiny MLP checkpoint; critic: the ported RLinf value model.")
    parser.add_argument("--siglip-path", default=None)
    parser.add_argument("--gemma3-path", default=None)
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--critic-expert-variant", default="gemma_1m")
    parser.add_argument("--critic-batch-size", type=int, default=16)
    parser.add_argument("--device", default=None,
                        help="Device for the value critic (default: the config's value.device).")
    add_config_args(parser)
    add_subset_args(parser)
    add_dataset_compat_args(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args)

    if args.value_backend == "critic":
        return _run_critic(args, cfg)

    model = load_value_checkpoint(args.value_checkpoint, device=cfg.value.device)
    dataset = build_recap_dataset_auto(
        args.dataset_path,
        action_horizon=1,
        image_key="observation.images.image" if cfg.value.use_image else None,
        returns_tag=resolve_tag(args.dataset_path, "returns", args.returns_tag),
        episodes=episode_subset(args.dataset_path, args.max_episodes),
        video_cache_size=args.video_cache_size,
        frame_cache_root=args.frame_cache_root,
        frame_reader_cache_size=args.frame_reader_cache_size,
        force_success=(args.dataset_type == "sft"),
        compat=args.dataset_compat,
    )
    specs = dataset.episode_specs()
    samples = dataset.value_samples(
        require_returns=False,
        image_size=model.cfg.image_size if model.cfg.use_image else None,
    )
    value_lookup = predict_values(
        model, samples, batch_size=cfg.value.batch_size, device=cfg.value.device
    )

    def value_fn(episode_index: int, frame_index: int) -> float:
        return value_lookup.get((episode_index, frame_index), 0.0)

    return_range = _resolve_return_range(args, cfg)
    frame, path = _write_advantages(args, cfg, specs, value_fn, return_range, dataset)

    per_task = {}
    for spec in specs:
        rows = frame[frame.episode_index == spec.episode_index]
        per_task.setdefault(spec.task_index, []).append(rows.advantage.to_numpy())
    print(
        f"[recap] advantages: {len(frame)} frames -> {path}\n"
        f"[recap]   mode={cfg.mode} horizon={cfg.advantage.horizon} "
        f"use_discount={cfg.advantage.use_discount} "
        f"positive_quantile={cfg.advantage.positive_quantile}"
    )
    for task, blocks in sorted(per_task.items()):
        flat = np.concatenate(blocks)
        print(f"[recap]   task {task}: positive_ratio={flat.mean():.3f} (n={flat.size})")


def _write_advantages(args, cfg, specs, value_fn, return_range, dataset):
    """Compute the advantage table, derive its tag, and record it in the manifest."""
    frame = advantages_to_dataframe(
        specs, value_fn, cfg, dataset_type=args.dataset_type, return_range=return_range
    )
    derived = advantages_tag(
        args.dataset_path, cfg, args.value_checkpoint, return_range,
        episodes=episode_subset(args.dataset_path, args.max_episodes),
        dataset_type=args.dataset_type,
    )
    tag = args.tag or f"{derived['summary']}_{derived['tag']}"
    path = write_sidecar(args.dataset_path, SIDECAR_ADVANTAGES, frame, tag)
    record_stage(
        args.dataset_path, "advantages", path.name,
        {"tag": tag, "determinants": derived["determinants"]},
    )
    return frame, path


def _resolve_return_range(args, cfg):
    """Global (ret_min, ret_max) for the normaliser: CLI > meta/stats.json > data."""
    import json

    if args.return_min is not None and args.return_max is not None:
        print(f"[recap] return range from CLI: [{args.return_min}, {args.return_max}]")
        return (float(args.return_min), float(args.return_max))
    stats_path = Path(args.dataset_path) / "meta" / "stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text())
        if "return" in stats and "min" in stats["return"]:
            print(f"[recap] return range from stats.json: "
                  f"[{stats['return']['min']:.3f}, {stats['return']['max']:.3f}]")
            return (float(stats["return"]["min"]), float(stats["return"]["max"]))
    print("[recap] WARNING: no return range in meta/stats.json; falling back to this "
          "dataset's returns (run compute_returns first, or pass --return-min/--return-max)")
    return None


def _run_critic(args, cfg):
    """Advantage computation using the ported RLinf value critic.

    Runs under ``torchrun`` when launched with multiple processes: each rank scores
    a strided shard of the frames, the per-frame values are gathered to rank 0,
    and rank 0 computes the **global** threshold and writes the sidecar. The
    threshold must be global (it is a percentile over every frame), so it can only
    be evaluated after the gather.

    Without torchrun this degrades to the previous single-process behaviour.
    """
    import torch
    from transformers import AutoTokenizer

    from ...distributed.context import DistributedContext
    from ..value_model import (
        DATASET_IMAGE_MAP,
        ValueObservationBuilder,
        load_critic,
    )

    if not args.siglip_path or not args.gemma3_path:
        raise SystemExit("--value-backend critic requires --siglip-path and --gemma3-path")
    tokenizer_path = args.tokenizer_path or args.gemma3_path

    ctx = DistributedContext()
    ctx.init()
    try:
        # Under torchrun each rank owns one GPU; otherwise `--device` wins, falling
        # back to the config (whose default is cpu for the placeholder head).
        device = str(ctx.device) if ctx.is_distributed else (args.device or cfg.value.device)

        dataset = build_recap_dataset_auto(
            args.dataset_path,
            action_horizon=1,
            image_key=None,
            frame_cache_root=args.frame_cache_root,
            frame_reader_cache_size=args.frame_reader_cache_size,
            episodes=episode_subset(args.dataset_path, args.max_episodes),
            force_success=(args.dataset_type == "sft"),
            compat=args.dataset_compat,
        )
        critic = load_critic(args.value_checkpoint, device=device)
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
        builder = ValueObservationBuilder(
            tokenizer,
            DATASET_IMAGE_MAP,
            text_template=critic.config.text_template,
            max_token_len=critic.config.max_token_len,
            image_size=critic.config.image_size,
            dtype=torch.bfloat16 if str(device).startswith("cuda") else torch.float32,
            device=device,
        )

        # Strided shard so every rank gets a comparable amount of work.
        shard = list(range(ctx.rank, len(dataset), ctx.world_size))
        values = _score_frames(critic, builder, dataset, shard, args.critic_batch_size, torch)
        print(
            f"[recap] rank {ctx.rank}/{ctx.world_size} predicted {len(values)} frame values "
            f"on {device}",
            flush=True,
        )

        if ctx.is_distributed:
            gathered = [None] * ctx.world_size
            torch.distributed.all_gather_object(gathered, values)
            if ctx.is_main:
                merged = {}
                for part in gathered:
                    merged.update(part)
                values = merged
            else:
                values = None

        if not ctx.is_main:
            ctx.barrier()
            return

        specs = dataset.episode_specs()
        return_range = _resolve_return_range(args, cfg)

        def value_fn(episode_index, frame_index):
            return values.get((episode_index, frame_index), 0.0)

        frame, path = _write_advantages(args, cfg, specs, value_fn, return_range, dataset)
        print(f"[recap] advantages: {len(frame)} frames -> {path}")
        ctx.barrier()
    finally:
        ctx.destroy()


def _score_frames(critic, builder, dataset, indices, batch_size, torch):
    """Run the critic over ``indices`` and return ``{(episode, frame): value}``."""
    values = {}
    with torch.no_grad():
        for start in range(0, len(indices), batch_size):
            chunk = indices[start : start + batch_size]
            samples = [dataset[i] for i in chunk]
            observation = builder.build(samples)
            predicted = critic.predict_values(observation).detach().float().cpu().tolist()
            for sample, value in zip(samples, predicted):
                values[(int(sample["episode_index"]), int(sample["frame_index"]))] = float(value)
    return values


if __name__ == "__main__":
    main()
