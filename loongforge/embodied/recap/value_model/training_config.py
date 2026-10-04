# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Typed configs for the RECAP value-critic trainer (``--model-name recap_value``).

These are the *LoongForge training* configs, not the RLinf-ported
:class:`~loongforge.embodied.recap.value_model.config.ValueCriticConfig`: the
latter describes the network geometry, while these bind the network to the
trainer (asset paths + geometry) and to the value dataloader (preprocessing).

Keeping them separate lets one critic definition be driven either by the
standalone CLI (``recap.cli.train_value --backend critic``, single process) or by
:class:`~loongforge.embodied.train.trainers.custom.recap_value.RecapValueTrainer`
under ``torchrun`` (multi-GPU), without the CLI needing a model-name/config-file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import ValueCriticConfig


@dataclass
class RecapValueModelConfig:
    """Model-side config for :class:`RecapValueTrainer`."""

    model_type: str = "recap_value"

    # ── backbone assets (HuggingFace paths) ──
    siglip_path: str = ""
    gemma3_path: str = ""
    tokenizer_path: str = ""

    # ── critic geometry (mirrors ValueCriticConfig) ──
    critic_expert_variant: str = "gemma_1m"
    num_bins: int = 201
    v_min: float = -1.0
    v_max: float = 0.0
    value_dropout: float = 0.0
    precision: str = "bfloat16"

    # ── freezing / memory ──
    freeze_vision_encoder: bool = False
    freeze_vlm: bool = False
    stop_gradient_to_vlm: bool = False
    gradient_checkpointing: bool = False

    # ── export ──
    # Name of the RLinf-format critic checkpoint written under --output-dir at
    # the end of training (loadable by recap.value_model.load_critic, and hence
    # by recap.cli.compute_advantages).
    export_name: str = "critic.pt"

    def to_critic_config(
        self,
        tokenizer_path: Optional[str] = None,
        *,
        image_size: Optional[int] = None,
        max_token_len: Optional[int] = None,
        text_template: Optional[str] = None,
    ) -> ValueCriticConfig:
        """Expand into the network-level :class:`ValueCriticConfig`.

        ``image_size`` / ``max_token_len`` / ``text_template`` live on the data
        config (that is where the dataloader consumes them) but are also stored in
        the exported critic checkpoint, so they are threaded through here to keep
        the two in sync.
        """
        return ValueCriticConfig(
            siglip_path=self.siglip_path,
            gemma3_path=self.gemma3_path,
            tokenizer_path=tokenizer_path or self.tokenizer_path,
            critic_expert_variant=self.critic_expert_variant,
            num_bins=self.num_bins,
            v_min=self.v_min,
            v_max=self.v_max,
            value_dropout=self.value_dropout,
            precision=self.precision,
            freeze_vision_encoder=self.freeze_vision_encoder,
            freeze_vlm=self.freeze_vlm,
            stop_gradient_to_vlm=self.stop_gradient_to_vlm,
            gradient_checkpointing=self.gradient_checkpointing,
            image_size=224 if image_size is None else image_size,
            max_token_len=200 if max_token_len is None else max_token_len,
            text_template=text_template or "Task: {prompt}.",
        )


@dataclass
class RecapValueDataConfig:
    """Data-side (preprocessing) config for :class:`RecapValueTrainer`."""

    # Tokenisation / image geometry for the critic observation. These mirror the
    # upstream value-model processor and must match what compute_advantages uses
    # so the trained critic sees the same distribution it will be evaluated on.
    max_token_len: int = 200
    image_size: int = 224
    text_template: str = "Task: {prompt}."
