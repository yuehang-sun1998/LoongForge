# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RecapCfgTrainer — advantage-conditioned (CFG) policy training.

Reuses the standard finetune trainer but swaps in:

* a RECAP CFG dataloader that emits base / positive / negative tokenisations plus
  the binary advantage indicator (``--dataset-strategy`` is not used; the loader
  is built directly from ``--dataset-path`` + the ``advantages`` sidecar), and
* the advantage-conditioned objective :func:`recap_cfg_loss`, which keeps the
  flow-matching loss unchanged and only routes the language condition.

The RECAP profile is read from ``--recap-config`` (defaults to the ``baseline``
preset). This trainer is the real Stage-1 Step 5 path; the CPU smoke test uses
``loongforge.embodied.recap.train_cfg --policy-backend mock`` instead.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Dict, Tuple

import torch
from torch.utils.data import DataLoader

from loongforge.embodied.distributed.utils import unwrap_model
from loongforge.embodied.train.utils.utils import resolve_dtype
from loongforge.embodied.train.trainers.supervised.finetune_trainer import FinetuneTrainer

logger = logging.getLogger(__name__)


class RecapCfgTrainer(FinetuneTrainer):
    """Advantage-conditioned flow-matching trainer for RECAP."""

    def _build_dataloaders(self) -> Dict[str, DataLoader]:
        from loongforge.embodied.recap.data.cfg_dataset import (
            build_recap_cfg_dataloader,
            load_recap_config,
        )

        self.recap_cfg = load_recap_config(self.training_args)
        if self.ctx.is_main:
            logger.info(
                "RecapCfgTrainer: mode=%s alpha=%s base_branch=%s advantage_dropout=%s",
                self.recap_cfg.mode,
                self.recap_cfg.policy.alpha,
                self.recap_cfg.policy.use_base_branch,
                self.recap_cfg.policy.use_advantage_dropout,
            )
        return {
            "vla": build_recap_cfg_dataloader(
                self.training_args, self.model_cfg, self.data_cfg, self.ctx
            )
        }

    def _train_forward(self, batch) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        from loongforge.embodied.recap.core.policy_objective import recap_cfg_loss

        dtype = getattr(self, "_compute_dtype", None)
        if dtype is None:
            dtype = resolve_dtype(self.training_args.dtype)
            self._compute_dtype = dtype

        autocast_ctx = (
            nullcontext()
            if self._cfg_bool("disable_train_autocast", False)
            else torch.autocast("cuda", dtype=dtype)
        )
        with self._fp8_forward_ctx(), autocast_ctx:
            # DDP/FSDP wrap the policy, so unwrap before calling the
            # advantage-conditioned objective (DDP does not forward arbitrary
            # attribute access to the wrapped module).
            policy = unwrap_model(self.model)
            loss, log_loss_dict = recap_cfg_loss(policy, batch, self.recap_cfg)
        return loss, log_loss_dict
