# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Advantage-conditioned policy objective for RECAP.

The advantage never multiplies the loss and never changes the action target
Instead it selects a language condition and the
loss stays a plain flow-matching MSE::

    loss_conditioned = flow_matching(batch, advantage_condition=codes)
    loss_base        = flow_matching(batch, advantage_condition=None)
    loss             = loss_base + alpha * loss_conditioned     # paper profile
    loss             = alpha * loss_conditioned                # baseline

``policy`` is anything exposing ``compute_action_loss(batch, advantage_condition)``
where ``advantage_condition`` is ``None`` (base) or a per-sample long tensor of
routing codes in ``{-1 base, 0 negative, 1 positive}``. See
``loongforge/embodied/recap/condition.py`` for the code constants.
"""

from __future__ import annotations

import random
from typing import Dict, Optional, Tuple

import torch

from .condition import CODE_BASE, CODE_NEGATIVE, CODE_POSITIVE, code_to_label, route_codes
from .config import RecapConfig


def _as_indicator_list(batch) -> list:
    """Extract the per-sample advantage indicator from a batch (tensor or dict)."""
    indicator = batch["advantage_indicator"] if isinstance(batch, dict) else getattr(
        batch, "advantage_indicator", None
    )
    if indicator is None:
        raise ValueError("batch is missing 'advantage_indicator'; run compute_advantages first")
    if isinstance(indicator, torch.Tensor):
        return [int(v) for v in indicator.detach().cpu().reshape(-1).tolist()]
    return [int(v) for v in indicator]


def recap_cfg_loss(
    policy,
    batch,
    cfg: RecapConfig,
    rng: Optional[random.Random] = None,
) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
    """Compute the advantage-conditioned objective for one batch.

    Returns ``(loss, log_loss_dict)`` where ``loss`` is the scalar to backprop.
    """
    indicators = _as_indicator_list(batch)
    codes = route_codes(
        indicators,
        cfg.condition,
        rng=rng,
        apply_dropout=cfg.policy.use_advantage_dropout,
    )

    log: Dict[str, torch.Tensor] = {}
    total: Optional[torch.Tensor] = None

    if cfg.policy.use_base_branch:
        loss_base = policy.compute_action_loss(batch, advantage_condition=None)
        log["loss_base"] = loss_base.detach()
        total = loss_base

    code_tensor = torch.tensor(codes, dtype=torch.long, device=_batch_device(batch))
    loss_conditioned = policy.compute_action_loss(batch, advantage_condition=code_tensor)
    log["loss_conditioned"] = loss_conditioned.detach()

    weighted = cfg.policy.alpha * loss_conditioned
    total = weighted if total is None else total + weighted

    log["loss"] = total.detach()
    # Routing diagnostics: fraction of samples sent to each branch.
    n = max(1, len(codes))
    log["route_base_frac"] = torch.tensor(
        sum(c == CODE_BASE for c in codes) / n, dtype=torch.float32
    )
    log["route_pos_frac"] = torch.tensor(
        sum(c == CODE_POSITIVE for c in codes) / n, dtype=torch.float32
    )
    log["route_neg_frac"] = torch.tensor(
        sum(c == CODE_NEGATIVE for c in codes) / n, dtype=torch.float32
    )
    return total, log


def _batch_device(batch) -> torch.device:
    """Best-effort device of a batch (tensor / dict / attribute object)."""
    for source in (
        batch if isinstance(batch, dict) else None,
        getattr(batch, "__dict__", None),
    ):
        if isinstance(source, dict):
            for value in source.values():
                if isinstance(value, torch.Tensor):
                    return value.device
    return torch.device("cpu")


__all__ = ["recap_cfg_loss", "code_to_label", "CODE_BASE", "CODE_NEGATIVE", "CODE_POSITIVE"]
