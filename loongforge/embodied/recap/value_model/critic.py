# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: rlinf/models/embodiment/value_model/recap/modeling_critic.py
# Categorical value head and the two-stage critic forward.
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

"""Categorical value critic for RECAP.

The value is read from a learnable ``[CLS]`` suffix token that cross-attends to
the frozen/trainable Gemma3 prefix cache, then projected onto 201 bins spanning
``[v_min, v_max]`` (``[-1, 0]`` upstream). Training uses a two-hot target
projection, i.e. linear interpolation between the two neighbouring bins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from .config import ValueCriticConfig, get_expert_config
from .expert import ValueExpert

logger = logging.getLogger(__name__)


def make_att_2d_masks(pad_masks: Tensor, att_masks: Tensor) -> Tensor:
    """Cumulative AR mask AND padding mask (see the upstream helper of the same name)."""
    if att_masks.ndim != 2 or pad_masks.ndim != 2:
        raise ValueError("att_masks and pad_masks must be 2D")
    cumsum = torch.cumsum(att_masks, dim=1)
    att_2d_masks = cumsum[:, None, :] <= cumsum[:, :, None]
    pad_2d_masks = pad_masks[:, None, :] * pad_masks[:, :, None]
    return att_2d_masks & pad_2d_masks


def prepare_target_values(
    target_values: Tensor, *, v_min: float, v_max: float, expected_batch_size: int
) -> Tensor:
    """Flatten, validate and clamp the regression targets."""
    target_values = target_values.float().view(-1)
    if expected_batch_size != target_values.shape[0]:
        raise ValueError(
            f"target batch {target_values.shape[0]} != logits batch {expected_batch_size}"
        )
    return target_values.clamp(v_min, v_max)


class ValueHead(nn.Module):
    """Learnable ``[CLS]`` embedding plus a categorical projection over value bins."""

    def __init__(
        self, hidden_size: int, num_bins: int, v_min: float, v_max: float, dropout: float = 0.0
    ):
        super().__init__()
        self.hidden_size = hidden_size
        self.cls_embedding = nn.Embedding(1, hidden_size)
        nn.init.normal_(self.cls_embedding.weight, std=0.02)
        self.dropout = nn.Dropout(dropout) if dropout > 0.0 else nn.Identity()
        self.value_proj = nn.Linear(hidden_size, num_bins)
        self.register_buffer("atoms", torch.linspace(v_min, v_max, num_bins), persistent=False)
        self.num_bins = num_bins
        self.v_min = v_min
        self.v_max = v_max
        self.delta_z = (v_max - v_min) / (num_bins - 1)

    def get_cls_embedding(self, batch_size: int) -> Tensor:
        """``[B, 1, hidden]`` CLS embedding (embedding lookup keeps FSDP happy)."""
        ids = torch.zeros(batch_size, 1, dtype=torch.long, device=self.cls_embedding.weight.device)
        return self.cls_embedding(ids)

    def forward(self, hidden_states: Tensor) -> Tensor:
        hidden_states = hidden_states.to(self.value_proj.weight.dtype)
        return self.value_proj(self.dropout(hidden_states))


@dataclass
class CriticOutput:
    """Value-critic outputs."""

    loss: Optional[Tensor] = None
    predicted_values: Optional[Tensor] = None
    logits: Optional[Tensor] = None
    probs: Optional[Tensor] = None
    cat_acc_best: Optional[Tensor] = None
    cat_acc_neighbor: Optional[Tensor] = None
    mae: Optional[Tensor] = None


class ValueCriticModel(nn.Module):
    """RECAP value function: VLM observation encoding + categorical value head."""

    def __init__(self, config: ValueCriticConfig):
        super().__init__()
        config.validate()
        self.config = config

        expert_config = get_expert_config(config.critic_expert_variant)
        self.value_expert = ValueExpert(
            expert_configs={"value": expert_config},
            siglip_path=config.siglip_path,
            gemma3_path=config.gemma3_path,
            precision=config.precision,
            mixed_precision=getattr(config, "mixed_precision", False),
            freeze_vision_encoder=config.freeze_vision_encoder,
            freeze_vlm=config.freeze_vlm,
        )
        self.value_head = ValueHead(
            hidden_size=expert_config.width,
            num_bins=config.num_bins,
            v_min=config.v_min,
            v_max=config.v_max,
            dropout=config.value_dropout,
        )
        self.num_bins = config.num_bins
        self.v_min = config.v_min
        self.v_max = config.v_max
        self.delta_z = (config.v_max - config.v_min) / (config.num_bins - 1)
        self.gradient_checkpointing_enabled = False
        if config.gradient_checkpointing:
            self.gradient_checkpointing_enable()

    # ── precision / checkpointing helpers ────────────────────────────────
    def _get_model_dtype(self) -> torch.dtype:
        return self.value_expert.gemma3.model.layers[0].self_attn.q_proj.weight.dtype

    def _apply_checkpoint(self, func, *args, **kwargs):
        if self.gradient_checkpointing_enabled and self.training:
            return torch.utils.checkpoint.checkpoint(
                func, *args, use_reentrant=False, preserve_rng_state=False, **kwargs
            )
        return func(*args, **kwargs)

    def gradient_checkpointing_enable(self) -> None:
        self.gradient_checkpointing_enabled = True
        self.value_expert.gemma3.model.gradient_checkpointing = True
        self.value_expert.vision_tower.gradient_checkpointing = True
        for expert in self.value_expert.experts.values():
            expert.model.gradient_checkpointing = True

    def gradient_checkpointing_disable(self) -> None:
        self.gradient_checkpointing_enabled = False
        self.value_expert.gemma3.model.gradient_checkpointing = False
        self.value_expert.vision_tower.gradient_checkpointing = False
        for expert in self.value_expert.experts.values():
            expert.model.gradient_checkpointing = False

    def _prepare_attention_masks_4d(self, att_2d_masks: Tensor) -> Tensor:
        dtype = self._get_model_dtype()
        att_2d_masks_4d = att_2d_masks[:, None, :, :]
        return torch.where(
            att_2d_masks_4d,
            torch.tensor(0.0, dtype=dtype, device=att_2d_masks.device),
            torch.tensor(-2.3819763e38, dtype=dtype, device=att_2d_masks.device),
        )

    # ── embeddings ───────────────────────────────────────────────────────
    def embed_prefix(self, images, img_masks, lang_tokens, lang_masks):
        embs, pad_masks = [], []
        bsize = lang_tokens.shape[0]
        for img, img_mask in zip(images, img_masks, strict=True):
            img_emb = self._apply_checkpoint(self.value_expert.embed_image, img)
            embs.append(img_emb)
            pad_masks.append(img_mask[:, None].expand(bsize, img_emb.shape[1]))
        lang_emb = self._apply_checkpoint(
            self.value_expert.embed_language_tokens, lang_tokens
        )
        embs.append(lang_emb)
        pad_masks.append(lang_masks)
        return torch.cat(embs, dim=1), torch.cat(pad_masks, dim=1)

    def embed_suffix(self, batch_size: int):
        cls_emb = self.value_head.get_cls_embedding(batch_size)
        pad_mask = torch.ones(batch_size, 1, dtype=torch.bool, device=cls_emb.device)
        ar_mask = torch.ones(batch_size, 1, dtype=torch.long, device=cls_emb.device)
        return cls_emb, pad_mask, ar_mask

    # ── forward ──────────────────────────────────────────────────────────
    def forward(self, observation, target_values: Optional[Tensor] = None, **kwargs) -> CriticOutput:
        """Run the two-stage forward and, when targets are given, the categorical loss."""
        images = observation["images"]
        image_masks = observation.get("image_masks", {})
        lang_tokens = observation["tokenized_prompt"]
        lang_masks = observation["tokenized_prompt_mask"]

        sorted_keys = sorted(images.keys())
        img_list = [images[k] for k in sorted_keys]
        img_mask_list = [
            image_masks.get(
                k, torch.ones(lang_tokens.shape[0], dtype=torch.bool, device=lang_tokens.device)
            )
            for k in sorted_keys
        ]

        prefix_embs, prefix_pad_masks = self.embed_prefix(
            img_list, img_mask_list, lang_tokens, lang_masks
        )
        suffix_embs, suffix_pad_masks, suffix_ar_masks = self.embed_suffix(lang_tokens.shape[0])

        values, logits, probs = self._forward_expert(
            prefix_embs, prefix_pad_masks, suffix_embs, suffix_pad_masks, suffix_ar_masks
        )

        loss = None
        metrics = None
        if target_values is not None:
            loss, metrics = self._compute_categorical_loss(logits, target_values)
        return CriticOutput(
            loss=loss.mean() if loss is not None else None,
            predicted_values=values,
            logits=logits,
            probs=probs,
            cat_acc_best=metrics["acc_best"] if metrics else None,
            cat_acc_neighbor=metrics["acc_neighbor"] if metrics else None,
            mae=metrics["mae"] if metrics else None,
        )

    def _forward_expert(
        self, prefix_embs, prefix_pad_masks, suffix_embs, suffix_pad_masks, suffix_ar_masks
    ):
        model_dtype = self._get_model_dtype()
        if model_dtype == torch.bfloat16:
            prefix_embs = prefix_embs.to(torch.bfloat16)
            suffix_embs = suffix_embs.to(torch.bfloat16)
        _, suffix_out = self._forward_expert_two_stage(
            prefix_embs, prefix_pad_masks, suffix_embs, suffix_pad_masks, suffix_ar_masks
        )
        cls_hidden = suffix_out[:, -1, :].to(self.value_head.value_proj.weight.dtype)
        logits = self.value_head(cls_hidden)
        probs = F.softmax(logits, dim=-1)
        values = (probs * self.value_head.atoms.to(probs.dtype)).sum(dim=-1)
        return values, logits, probs

    def _forward_expert_two_stage(
        self, prefix_embs, prefix_pad_masks, suffix_embs, suffix_pad_masks, suffix_ar_masks
    ) -> Tuple[Tensor, Tensor]:
        # Phase 1: the prefix is fully bidirectional, so attention is the pad-mask
        # outer product. Gemma3 fills a DynamicCache.
        prefix_attn = prefix_pad_masks[:, None, :] * prefix_pad_masks[:, :, None]
        prefix_pos = torch.cumsum(prefix_pad_masks, dim=1) - 1
        (prefix_out, _), past_kv = self.value_expert.forward(
            attention_mask=self._prepare_attention_masks_4d(prefix_attn),
            position_ids=prefix_pos,
            past_key_values=None,
            inputs_embeds=[prefix_embs, None],
            use_cache=True,
        )

        if self.config.stop_gradient_to_vlm and past_kv is not None:
            for i in range(len(past_kv.key_cache)):
                past_kv.key_cache[i] = past_kv.key_cache[i].detach()
                past_kv.value_cache[i] = past_kv.value_cache[i].detach()

        # Phase 2: the expert runs the CLS suffix against the cached prefix.
        batch_size = prefix_pad_masks.shape[0]
        prefix_len, suffix_len = prefix_pad_masks.shape[1], suffix_pad_masks.shape[1]
        prefix_2d = prefix_pad_masks[:, None, :].expand(batch_size, suffix_len, prefix_len)
        suffix_attn = make_att_2d_masks(suffix_pad_masks, suffix_ar_masks)
        full_attn_4d = self._prepare_attention_masks_4d(
            torch.cat([prefix_2d, suffix_attn], dim=2)
        )
        suffix_pos = (
            prefix_pad_masks.sum(dim=-1)[:, None]
            + torch.cumsum(suffix_pad_masks, dim=1)
            - 1
        )
        (_, suffix_out), _ = self.value_expert.forward(
            attention_mask=full_attn_4d,
            position_ids=suffix_pos,
            past_key_values=past_kv,
            inputs_embeds=[None, suffix_embs],
            use_cache=False,
            expert_name="value",
        )
        return prefix_out, suffix_out

    # ── loss ─────────────────────────────────────────────────────────────
    def _compute_categorical_loss(self, logits: Tensor, target_values: Tensor):
        """Two-hot cross-entropy over the value bins."""
        targets = prepare_target_values(
            target_values,
            v_min=self.v_min,
            v_max=self.v_max,
            expected_batch_size=logits.shape[0],
        )
        b = (targets - self.v_min) / self.delta_z
        lower = b.floor().long().clamp(0, self.num_bins - 1)
        upper = b.ceil().long().clamp(0, self.num_bins - 1)
        d_to_lower, d_to_upper = b - lower.float(), upper.float() - b
        same_bin = lower == upper
        d_to_lower = torch.where(same_bin, torch.zeros_like(d_to_lower), d_to_lower)
        d_to_upper = torch.where(same_bin, torch.ones_like(d_to_upper), d_to_upper)

        target_probs = torch.zeros(
            targets.shape[0], self.num_bins, device=targets.device, dtype=logits.dtype
        )
        batch_idx = torch.arange(targets.shape[0], device=targets.device)
        target_probs[batch_idx, lower] += d_to_upper
        target_probs[batch_idx, upper] += d_to_lower

        loss = -(target_probs * F.log_softmax(logits, dim=-1)).sum(dim=-1)
        with torch.no_grad():
            best = logits.argmax(dim=-1)
            metrics = {
                "acc_best": (best == b.round().long().clamp(0, self.num_bins - 1)).float().mean(),
                "acc_neighbor": ((best - b.round().long()).abs() <= 1).float().mean(),
                "mae": (
                    (self.value_head.atoms[best] - targets).abs().mean()
                ),
            }
        return loss, metrics

    # ── inference helper ─────────────────────────────────────────────────
    @torch.no_grad()
    def predict_values(self, observation) -> Tensor:
        """Continuous expected values for a batch of observations (eval mode)."""
        was_training = self.training
        self.eval()
        try:
            out = self.forward(observation)
        finally:
            if was_training:
                self.train()
        return out.predicted_values


def build_value_critic(config: ValueCriticConfig) -> ValueCriticModel:
    """Construct the critic described by ``config``."""
    return ValueCriticModel(config)


def save_critic(path, model: ValueCriticModel, config: ValueCriticConfig) -> Path:
    """Persist the critic weights together with the config needed to rebuild it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "config": config.__dict__}, path)
    return path


def load_critic(path, device: str = "cpu", dtype: Optional[torch.dtype] = None) -> ValueCriticModel:
    """Rebuild a critic from :func:`save_critic`."""
    payload = torch.load(path, map_location="cpu", weights_only=False)
    model = build_value_critic(ValueCriticConfig(**payload["config"]))
    model.load_state_dict(payload["state_dict"])
    if dtype is not None:
        model.to(dtype=dtype)
    return model.to(device).eval()
