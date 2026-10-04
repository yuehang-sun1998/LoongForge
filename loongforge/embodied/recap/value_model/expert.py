# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: rlinf/models/embodiment/value_model/recap/value_expert.py
# Two-stage SigLIP2 + Gemma3-270M backbone with an independent Gemma expert.
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

"""Value-expert backbone: SigLIP2 vision + Gemma3-270M + an independent Gemma expert.

The VLM prefix (SigLIP2 patch tokens projected to Gemma3's width, then the
language tokens) is processed by Gemma3 with a KV cache; the expert then runs a
one-token suffix that cross-attends to that cache. The two stages cannot be
interleaved because Gemma3 uses sliding-window attention.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Literal, Optional

import torch
from torch import nn
from transformers import Gemma3ForCausalLM, GemmaForCausalLM, SiglipVisionModel
from transformers.cache_utils import DynamicCache
from transformers.models.auto import CONFIG_MAPPING

from .config import ExpertConfig

logger = logging.getLogger(__name__)


class ValueExpert(nn.Module):
    """SigLIP2 + Gemma3-270M backbone with a Gemma expert per value head."""

    def __init__(
        self,
        expert_configs: Dict[str, ExpertConfig],
        siglip_path: str,
        gemma3_path: str,
        freeze_vision_encoder: bool = False,
        freeze_vlm: bool = False,
        precision: Literal["bfloat16", "float32"] = "bfloat16",
        mixed_precision: bool = False,
        trainable_experts: Optional[List[str]] = None,
    ):
        super().__init__()
        self.freeze_vision_encoder = freeze_vision_encoder
        self.freeze_vlm = freeze_vlm
        self.mixed_precision = mixed_precision
        self.expert_names = list(expert_configs.keys())
        self.trainable_experts = (
            trainable_experts if trainable_experts is not None else self.expert_names
        )

        logger.info("Loading SigLIP2 from %s", siglip_path)
        self.vision_tower = SiglipVisionModel.from_pretrained(siglip_path)
        siglip_hidden = self.vision_tower.config.hidden_size

        logger.info("Loading Gemma3 from %s", gemma3_path)
        self.gemma3 = Gemma3ForCausalLM.from_pretrained(gemma3_path)
        gemma3_hidden = self.gemma3.config.hidden_size

        # Fresh image projection: SigLIP2 width -> Gemma3 width.
        self.multi_modal_proj = nn.Linear(siglip_hidden, gemma3_hidden, bias=True)
        nn.init.normal_(self.multi_modal_proj.weight, std=0.02)
        nn.init.zeros_(self.multi_modal_proj.bias)

        gemma3_head_dim = self.gemma3.config.head_dim
        for name, cfg in expert_configs.items():
            if cfg.head_dim != gemma3_head_dim:
                raise ValueError(
                    f"Expert {name!r} has head_dim={cfg.head_dim} but Gemma3 has "
                    f"head_dim={gemma3_head_dim}; they must match for KV cross-attention."
                )

        self.experts = nn.ModuleDict()
        for name, expert_config in expert_configs.items():
            expert_config_hf = CONFIG_MAPPING["gemma"](
                head_dim=expert_config.head_dim,
                hidden_size=expert_config.width,
                intermediate_size=expert_config.mlp_dim,
                num_attention_heads=expert_config.num_heads,
                num_hidden_layers=expert_config.depth,
                num_key_value_heads=expert_config.num_kv_heads,
                vocab_size=257152,
                hidden_activation="gelu_pytorch_tanh",
                torch_dtype="float32",
            )
            expert = GemmaForCausalLM(config=expert_config_hf)
            expert.model.embed_tokens = None  # the expert consumes embeddings, not ids
            self.experts[name] = expert

        self._apply_precision(precision)
        self._set_requires_grad()

    def embed_image(self, image: torch.Tensor) -> torch.Tensor:
        """SigLIP2 patch tokens projected into Gemma3's hidden space."""
        feats = self.vision_tower(pixel_values=image).last_hidden_state
        return self.multi_modal_proj(feats)

    def embed_language_tokens(self, tokens: torch.Tensor) -> torch.Tensor:
        """Token embeddings (Gemma3's embedding already applies sqrt(hidden) scaling)."""
        return self.gemma3.model.embed_tokens(tokens)

    def forward(
        self,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values=None,
        inputs_embeds: Optional[list] = None,
        use_cache: Optional[bool] = None,
        expert_name: Optional[str] = None,
        **kwargs,
    ):
        """Two-stage forward: ``[prefix, None]`` prefills the cache, ``[None, suffix]`` runs the expert."""
        prefix_embs, suffix_embs = inputs_embeds

        if suffix_embs is None:
            # Gemma3TextModel only builds a DynamicCache when not training, so
            # pre-create one to keep the cache path active during training.
            if past_key_values is None:
                past_key_values = DynamicCache()
            out = self.gemma3.model(
                inputs_embeds=prefix_embs,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                use_cache=True,
            )
            return [out.last_hidden_state, None], out.past_key_values

        if prefix_embs is None:
            name = self._resolve_expert_name(expert_name)
            out = self.experts[name].model(
                inputs_embeds=suffix_embs,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                use_cache=False,
            )
            return [None, out.last_hidden_state], None

        raise ValueError(
            "ValueExpert does not support interleaved forward; use the two-stage path."
        )

    def _set_requires_grad(self) -> None:
        if self.freeze_vision_encoder:
            for param in self.vision_tower.parameters():
                param.requires_grad = False
            self.vision_tower.eval()
        if self.freeze_vlm:
            for param in self.gemma3.parameters():
                param.requires_grad = False
            self.gemma3.eval()
        for name in self.expert_names:
            if name not in self.trainable_experts:
                for param in self.experts[name].parameters():
                    param.requires_grad = False
                self.experts[name].eval()
        self._freeze_unused_lm_heads()

    def _freeze_unused_lm_heads(self) -> None:
        """Keep the LM heads out of the optimizer — the value path never uses them.

        The critic reads the last hidden state directly, so ``lm_head`` is dead
        weight in AdamW. One caveat: when ``tie_word_embeddings`` is set (true for
        Gemma3-270M), ``lm_head.weight`` **is** ``embed_tokens.weight`` — the
        prefix stage does use the embedding, so those must stay trainable. Only
        the untied heads (e.g. the expert's, whose ``embed_tokens`` is removed)
        are frozen.
        """
        for module in (self.gemma3, *self.experts.values()):
            lm_head = getattr(module, "lm_head", None)
            if lm_head is None:
                continue
            embed = getattr(getattr(module, "model", None), "embed_tokens", None)
            tied_weight = getattr(embed, "weight", None) if embed is not None else None
            for param in lm_head.parameters():
                if param is tied_weight:
                    continue  # tied to the (used) embedding table
                param.requires_grad = False

    def train(self, mode: bool = True):
        super().train(mode)
        if self.freeze_vision_encoder:
            self.vision_tower.eval()
        if self.freeze_vlm:
            self.gemma3.eval()
        for name in self.expert_names:
            if name not in self.trainable_experts:
                self.experts[name].eval()
        return self

    def _apply_precision(self, precision: str) -> None:
        if precision == "float32":
            self.to(dtype=torch.float32)
            return
        if precision != "bfloat16":
            raise ValueError(f"Invalid precision: {precision}")
        self.to(dtype=torch.bfloat16)
        if not self.mixed_precision:
            # Uniform bf16 (what the upstream FSDP path uses). The fp32 keep-list
            # below requires an autocast context around the forward, so it is opt-in.
            return
        # Keep numerically sensitive layers in fp32 (upstream mixed-precision set).
        keep_fp32 = (
            "embeddings.patch_embedding.weight",
            "embeddings.patch_embedding.bias",
            "embeddings.position_embedding.weight",
            "input_layernorm",
            "post_attention_layernorm",
            "model.norm",
        )
        for name, param in self.named_parameters():
            if any(sel in name for sel in keep_fp32):
                param.data = param.data.to(dtype=torch.float32)

    def _resolve_expert_name(self, expert_name: Optional[str]) -> str:
        if expert_name is not None:
            if expert_name not in self.expert_names:
                raise ValueError(f"Unknown expert {expert_name!r}; available {self.expert_names}")
            return expert_name
        if len(self.expert_names) == 1:
            return self.expert_names[0]
        raise ValueError(f"expert_name is required when multiple experts exist: {self.expert_names}")
