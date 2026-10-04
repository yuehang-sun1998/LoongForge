# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RECAP Step 2 — train the state-value model.

Two backends:

* ``placeholder`` — the tiny MLP value head used by the CPU smoke test. Self
  contained; no GPU or backbone assets needed.
* ``critic`` — the ported RLinf value critic (SigLIP2 + Gemma3 + categorical
  head). This **delegates to** :class:`RecapValueTrainer`, the single
  implementation of critic training, so it inherits DDP, gradient clipping, the
  NaN/loss-spike guards, checkpointing and validation. Run it under ``torchrun``
  for multi-GPU; plain ``python -m ...`` runs one process.

Usage:
    python -m loongforge.embodied.recap.cli.train_value \
        --dataset-path /tmp/recap_mock --output /tmp/recap_mock/value.pt

    # real critic, single process
    python -m loongforge.embodied.recap.cli.train_value \
        --dataset-path <ds> --output <ds>/recap/critic.pt --backend critic \
        --siglip-path .../siglip2-so400m-patch14-224 --gemma3-path .../gemma-3-270m

    # real critic, 8 GPUs (see examples/embodied/recap/run_recap_value.sh)
    torchrun --nproc_per_node=8 -m loongforge.embodied.recap.cli.train_value \
        --dataset-path <ds> --output <ds>/recap/critic.pt --backend critic ...
"""

from __future__ import annotations

import argparse
import math
import os
import random
from pathlib import Path

import torch

from ._common import (
    add_config_args,
    add_dataset_compat_args,
    add_subset_args,
    episode_subset,
    load_config,
)
from ..data.compat import build_recap_dataset_auto
from ..data.manifest import (
    dataset_determinant,
    record_stage,
    resolve_tag,
    reward_determinant,
    sha256_file,
)
from ..data.placeholder_value import RecapValueModel, save_value_checkpoint, value_loss


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RECAP: train value model.")
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--output", required=True, help="Value checkpoint path (.pt).")
    parser.add_argument("--returns-tag", default=None)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--lr", type=float, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--backend", default="placeholder", choices=["placeholder", "critic"],
                        help="placeholder: tiny MLP value head (fast smoke); critic: the "
                             "ported RLinf SigLIP2+Gemma3 value model.")
    parser.add_argument("--siglip-path", default=None)
    parser.add_argument("--gemma3-path", default=None)
    parser.add_argument("--tokenizer-path", default=None)
    parser.add_argument("--critic-expert-variant", default="gemma_1m")
    parser.add_argument("--critic-v-min", type=float, default=-1.0)
    parser.add_argument("--critic-v-max", type=float, default=0.0)
    parser.add_argument("--critic-lr", type=float, default=1.0e-4,
                        help="Learning rate for the critic (upstream value_lr).")
    # ── critic trainer controls (mirror the RecapValueTrainer training args) ──
    parser.add_argument("--train-iters", type=int, default=None,
                        help="Optimizer steps. Overrides --epochs (which is converted "
                             "to steps from the frame count).")
    parser.add_argument("--num-workers", type=int, default=None)
    parser.add_argument("--save-interval", type=int, default=0,
                        help="LoongForge checkpoint interval (0 disables, the default); "
                             "the final critic.pt is always exported. Set >0 to enable "
                             "--resume on a later run.")
    parser.add_argument("--val-episodes", type=int, default=0,
                        help="Hold out the last N episodes for validation monitoring.")
    parser.add_argument("--val-interval", type=int, default=500)
    parser.add_argument("--val-batches", type=int, default=0,
                        help="Cap each validation pass to N batches (0 = all).")
    add_config_args(parser)
    add_subset_args(parser)
    add_dataset_compat_args(parser)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args)
    if args.backend == "critic":
        return _train_critic(args, cfg)
    return _train_placeholder(args, cfg)


def _train_placeholder(args, cfg) -> None:
    """Tiny MLP value head — the CPU smoke path (no GPU, no backbones)."""
    vcfg = cfg.value
    if args.epochs is not None:
        vcfg.epochs = args.epochs
    if args.batch_size is not None:
        vcfg.batch_size = args.batch_size
    if args.lr is not None:
        vcfg.lr = args.lr
    if args.device is not None:
        vcfg.device = args.device

    dataset = build_recap_dataset_auto(
        args.dataset_path,
        action_horizon=1,
        returns_tag=resolve_tag(args.dataset_path, "returns", args.returns_tag),
        image_key=None,
        episodes=episode_subset(args.dataset_path, args.max_episodes),
        video_cache_size=args.video_cache_size,
        frame_cache_root=args.frame_cache_root,
        frame_reader_cache_size=args.frame_reader_cache_size,
        force_success=(args.dataset_type == "sft"),
        compat=args.dataset_compat,
    )
    samples = dataset.value_samples(image_size=vcfg.image_size if vcfg.use_image else None)
    if not samples:
        raise RuntimeError(
            "no samples with returns found — run compute_returns first "
            f"(looked for returns sidecar under {args.dataset_path}/meta)"
        )

    state_dim = int(samples[0]["state"].numel())
    image_key = dataset.image_key if vcfg.use_image else None
    device = torch.device(vcfg.device)
    torch.manual_seed(vcfg.seed)
    rng = random.Random(vcfg.seed)

    model = RecapValueModel(state_dim, vcfg).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=vcfg.lr)

    n = len(samples)
    print(
        f"[recap] value training: {n} samples, state_dim={state_dim}, "
        f"distributional={vcfg.distributional}, device={vcfg.device}"
    )
    for epoch in range(vcfg.epochs):
        order = list(range(n))
        rng.shuffle(order)
        epoch_loss = 0.0
        batches = 0
        for start in range(0, n, vcfg.batch_size):
            index = order[start : start + vcfg.batch_size]
            states = torch.stack([samples[i]["state"] for i in index]).to(device)
            targets = torch.stack([samples[i]["target"] for i in index]).to(device)
            images = None
            if image_key is not None:
                images = torch.stack([samples[i]["image"] for i in index]).to(device)

            outputs = model(states, images)
            loss = value_loss(outputs, targets, vcfg)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach())
            batches += 1
        print(f"[recap]   epoch {epoch + 1}/{vcfg.epochs} loss={epoch_loss / max(1, batches):.6f}")

    path = save_value_checkpoint(args.output, model, vcfg)
    print(f"[recap] value checkpoint -> {path}")
    record_stage(
        args.dataset_path, "critic", path.name,
        {
            "backend": "placeholder",
            "sha256": sha256_file(path),
            "determinants": {
                "dataset": dataset_determinant(
                    args.dataset_path, episode_subset(args.dataset_path, args.max_episodes), args.dataset_type
                ),
                "reward": reward_determinant(cfg),
                "critic": {"backend": "placeholder", "epochs": vcfg.epochs, "seed": vcfg.seed},
            },
        },
    )


def _count_train_frames(args) -> int:
    """Frames the trainer will actually see (working set minus validation)."""
    from ..data.dataset import episode_split
    from ..data.value_dataset import value_frame_indices

    train_episodes, _ = episode_split(
        args.dataset_path, args.max_episodes, args.val_episodes
    )
    base = build_recap_dataset_auto(
        args.dataset_path,
        action_horizon=1,
        returns_tag=resolve_tag(args.dataset_path, "returns", args.returns_tag),
        episodes=train_episodes,
        compat=args.dataset_compat,
    )
    return len(value_frame_indices(base))


def _resolve_train_iters(args, cfg) -> int:
    """Steps to run: ``--train-iters`` wins, else ``--epochs`` × steps/epoch."""
    if args.train_iters:
        return int(args.train_iters)
    batch_size = args.batch_size or cfg.value.batch_size
    world_size = int(os.environ.get("WORLD_SIZE", "1") or 1)
    frames = _count_train_frames(args)
    steps_per_epoch = max(1, math.ceil(frames / (world_size * batch_size)))
    epochs = args.epochs or cfg.value.epochs
    return steps_per_epoch * epochs


def _build_training_args(args, cfg, model_cfg, train_iters):
    """Assemble a TrainingArgs for RecapValueTrainer from the CLI flags."""
    from omegaconf import OmegaConf

    from ...train.training_args import TrainingArgs

    overrides = {
        "trainer_type": "RecapValueTrainer",
        "dataset_path": args.dataset_path,
        "output_dir": str(Path(args.output).parent),
        "train_iters": int(train_iters),
        "per_device_batch_size": args.batch_size or cfg.value.batch_size,
        "lr_base": args.critic_lr,
        "seed": cfg.value.seed,
        "dtype": "bfloat16",
        "distributed_strategy": "ddp",
        "tokenizer_path": model_cfg.tokenizer_path,
        "recap_returns_tag": args.returns_tag,
        "recap_dataset_type": args.dataset_type,
        "recap_dataset_compat": args.dataset_compat,
        "recap_max_episodes": args.max_episodes,
        "recap_val_episodes": args.val_episodes,
        "recap_val_interval": args.val_interval,
        "recap_val_batches": args.val_batches,
        "recap_frame_cache_root": args.frame_cache_root,
        "recap_frame_reader_cache_size": args.frame_reader_cache_size,
        "recap_value_video_cache_size": args.video_cache_size,
    }
    if args.num_workers is not None:
        overrides["num_workers"] = args.num_workers
    overrides["save_interval"] = args.save_interval

    base = OmegaConf.structured(TrainingArgs)
    return OmegaConf.to_object(OmegaConf.merge(base, OmegaConf.create(overrides)))


def _train_critic(args, cfg) -> None:
    """Train the ported RLinf value critic via :class:`RecapValueTrainer`.

    Delegating (rather than keeping a second hand-rolled loop) is deliberate: the
    trainer brings DDP, gradient clipping, the NaN/loss-spike guards, checkpoint
    + resume, validation and the RLinf-format export — none of which the previous
    loop had. It also makes this CLI multi-GPU when launched under ``torchrun``.
    """
    from ...train.trainers.trainer_builder import build_model_trainer
    from ..value_model import RecapValueDataConfig, RecapValueModelConfig

    if not args.siglip_path or not args.gemma3_path:
        raise SystemExit("--backend critic requires --siglip-path and --gemma3-path")
    if args.device and not str(args.device).startswith("cuda"):
        print(
            f"[recap] warning: --device {args.device} is ignored for the critic "
            "backend; the trainer uses the distributed context's device."
        )

    output = Path(args.output)
    model_cfg = RecapValueModelConfig(
        siglip_path=args.siglip_path,
        gemma3_path=args.gemma3_path,
        tokenizer_path=args.tokenizer_path or args.gemma3_path,
        critic_expert_variant=args.critic_expert_variant,
        num_bins=cfg.value.num_bins,
        v_min=args.critic_v_min,
        v_max=args.critic_v_max,
        export_name=output.name,
    )
    train_iters = _resolve_train_iters(args, cfg)
    training_args = _build_training_args(args, cfg, model_cfg, train_iters)
    print(
        f"[recap] critic training via RecapValueTrainer: {train_iters} steps, "
        f"batch={training_args.per_device_batch_size}, output={output}"
    )

    trainer = build_model_trainer(training_args, model_cfg, RecapValueDataConfig())
    trainer.train()


if __name__ == "__main__":
    main()
