# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RecapValueTrainer — multi-GPU training of the RECAP value critic.

Reuses the standard finetune trainer but swaps in:

* a value-model dataloader (:func:`build_recap_value_dataloader`) that emits the
  critic observation plus the normalised-return target, and
* a forward that runs the two-stage SigLIP2 + Gemma3-270M critic and its
  categorical (two-hot cross-entropy) loss.

Registering this as a ``--trainer-type`` means the critic inherits DDP/FSDP
wrapping, the optimizer/scheduler, gradient clipping, the NaN/loss-spike guards,
checkpointing and logging from the shared ``BaseTrainer``/``FinetuneTrainer``
machinery, instead of the hand-rolled single-process loop it replaces (which had
no grad clipping and no NaN checks).

The critic network itself is ported from RLinf — see
``loongforge/embodied/recap/value_model/`` for the attribution headers.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Tuple

import torch
from torch.utils.data import DataLoader

from loongforge.embodied.distributed.utils import unwrap_model
from loongforge.embodied.train.trainers.supervised.finetune_trainer import FinetuneTrainer

logger = logging.getLogger(__name__)


class RecapValueTrainer(FinetuneTrainer):
    """State-value critic trainer for RECAP (SigLIP2 + Gemma3 + categorical head)."""

    # ── compute ──────────────────────────────────────────────────────────
    def _build_model(self):
        from loongforge.embodied.recap.value_model import build_value_critic

        # ``model_cfg`` is a RecapValueModelConfig (--model-name recap_value).
        # The image/text geometry lives on the data config; thread it through so
        # the exported checkpoint records what the dataloader actually used.
        self.value_critic_config = self.model_cfg.to_critic_config(
            tokenizer_path=self.training_args.tokenizer_path or None,
            image_size=getattr(self.data_cfg, "image_size", 224),
            max_token_len=getattr(self.data_cfg, "max_token_len", 200),
            text_template=getattr(self.data_cfg, "text_template", "Task: {prompt}."),
        )
        if self.ctx.is_main:
            logger.info(
                "RecapValueTrainer: expert=%s num_bins=%d range=[%.3f, %.3f] "
                "precision=%s freeze_vlm=%s",
                self.value_critic_config.critic_expert_variant,
                self.value_critic_config.num_bins,
                self.value_critic_config.v_min,
                self.value_critic_config.v_max,
                self.value_critic_config.precision,
                self.value_critic_config.freeze_vlm,
            )
        return build_value_critic(self.value_critic_config)

    def _build_dataloaders(self) -> Dict[str, DataLoader]:
        from loongforge.embodied.recap.data.value_dataset import build_recap_value_loaders

        train_loader, self._val_loader = build_recap_value_loaders(
            self.training_args, self.model_cfg, self.data_cfg, self.ctx
        )
        self._val_interval = int(getattr(self.training_args, "recap_val_interval", 500) or 0)
        self._val_batches = int(getattr(self.training_args, "recap_val_batches", 0) or 0)
        self._last_val_metrics: Dict[str, float] = {}
        if self.ctx.is_main:
            logger.info(
                "RecapValueTrainer: train frames=%d, validation=%s (every %d steps)",
                len(train_loader.dataset),
                "on" if self._val_loader is not None else "off",
                self._val_interval,
            )
        return {"vla": train_loader}

    def _on_step_end(self, metrics: Dict[str, float]) -> None:
        """Run the held-out validation pass and surface it as sticky metrics."""
        if getattr(self, "_val_loader", None) is None or self._val_interval <= 0:
            return
        if self.completed_steps % self._val_interval == 0:
            self._last_val_metrics = self._run_validation()
        metrics.update(self._last_val_metrics)

    @torch.no_grad()
    def _run_validation(self) -> Dict[str, float]:
        """Loss/MAE on the held-out episodes (rank 0 only, bounded by batches)."""
        critic = unwrap_model(self.model)
        device = next(critic.parameters()).device
        was_training = self.model.training
        self.model.eval()
        total_loss = total_mae = 0.0
        batches = 0
        try:
            for index, batch in enumerate(self._val_loader):
                if self._val_batches and index >= self._val_batches:
                    break
                batch = batch.to(device)
                out = critic(batch.as_observation(), target_values=batch.target_values)
                total_loss += float(out.loss.detach())
                total_mae += float(out.mae.detach())
                batches += 1
        finally:
            if was_training:
                self.model.train()
        if batches == 0:
            return {}
        return {
            "val_value_loss": total_loss / batches,
            "val_value_mae": total_mae / batches,
        }

    def _train_forward(self, batch) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        # No autocast / fp8 wrapper here: the critic manages its own dtype
        # (ValueExpert casts to bf16 internally), matching the single-process
        # path. DDP/FSDP wrap the critic, so unwrap before the objective (DDP
        # does not forward arbitrary attribute access to the wrapped module).
        critic = unwrap_model(self.model)
        out = critic(batch.as_observation(), target_values=batch.target_values)
        log_loss_dict: Dict[str, torch.Tensor] = {"value_loss": out.loss.detach()}
        for name, value in (
            ("value_mae", out.mae),
            ("value_acc_best", out.cat_acc_best),
            ("value_acc_neighbor", out.cat_acc_neighbor),
        ):
            if value is not None:
                log_loss_dict[name] = value.detach()
        return out.loss, log_loss_dict

    # ── infrastructure ───────────────────────────────────────────────────
    def _load_pretrained(self, path: str):
        """Load either a LoongForge checkpoint or a RLinf-format critic ``.pt``.

        ``recap.value_model.save_critic`` writes ``{"state_dict", "config"}``,
        which is what the value pipeline (``compute_advantages``) consumes and
        therefore the natural warm-start format for this trainer.
        """
        payload = None
        if os.path.isfile(path):
            try:
                payload = torch.load(path, map_location="cpu", weights_only=False)
            except Exception:  # not a torch pickle (e.g. a safetensors file)
                payload = None
        if isinstance(payload, dict) and "state_dict" in payload and "config" in payload:
            unwrap_model(self.model).load_state_dict(payload["state_dict"])
            self.logger.log_pretrained_loaded(path)
            if self.ctx.is_main:
                logger.info("RecapValueTrainer: loaded RECAP critic weights from %s", path)
            return
        super()._load_pretrained(path)

    def _finalize(self):
        # Export the critic before super()._finalize() tears down the process
        # group / logging.
        self._export_critic()
        super()._finalize()

    def _export_critic(self) -> None:
        """Write the RLinf-format critic under ``--output-dir`` and record it.

        The LoongForge checkpoint written by ``super()._finalize`` is for resume;
        this artifact is the one the advantage stage loads.
        """
        from loongforge.embodied.recap.value_model import save_critic

        if not self.ctx.is_main:
            return
        output_dir = Path(self.training_args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / self.model_cfg.export_name
        save_critic(path, unwrap_model(self.model), self.value_critic_config)
        logger.info("RecapValueTrainer: exported critic -> %s", path)
        self._record_critic_stage(path)

    def _record_critic_stage(self, path: Path) -> None:
        """Best-effort manifest entry so the artifact is traceable."""
        try:
            from loongforge.embodied.recap.data.dataset import episode_split
            from loongforge.embodied.recap.data.manifest import (
                dataset_determinant,
                record_stage,
                resolve_tag,
                sha256_file,
            )

            cfg = self.value_critic_config
            # The determinant must describe what was actually trained on: the
            # working-set-minus-validation episodes.
            train_episodes, _ = episode_split(
                self.training_args.dataset_path,
                getattr(self.training_args, "recap_max_episodes", None),
                getattr(self.training_args, "recap_val_episodes", 0),
            )
            record_stage(
                self.training_args.dataset_path,
                "critic",
                path.name,
                {
                    "backend": "critic",
                    "sha256": sha256_file(path),
                    "returns_tag": resolve_tag(
                        self.training_args.dataset_path,
                        "returns",
                        getattr(self.training_args, "recap_returns_tag", None),
                    ),
                    "determinants": {
                        "dataset": dataset_determinant(
                            self.training_args.dataset_path,
                            train_episodes,
                            getattr(self.training_args, "recap_dataset_type", "rollout"),
                        ),
                        "critic": {
                            "backend": "critic",
                            "expert_variant": cfg.critic_expert_variant,
                            "num_bins": cfg.num_bins,
                            "v_min": cfg.v_min,
                            "v_max": cfg.v_max,
                            "train_iters": self.training_args.train_iters,
                            "lr": self.training_args.lr_base,
                            "seed": self.training_args.seed,
                        },
                    },
                },
            )
        except Exception as exc:  # manifest is observability, not correctness
            logger.warning("RecapValueTrainer: could not record critic stage: %s", exc)
