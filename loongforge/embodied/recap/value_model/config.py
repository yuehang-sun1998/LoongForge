# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: rlinf/models/embodiment/value_model/recap/configuration.py
# Expert variant table and the critic config.
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

"""Configuration for the RECAP value critic.

The expert is a Gemma-1 architecture decoder with ``embed_tokens`` removed; its
``head_dim`` must match Gemma3-270M's (256) so it can cross-attend to the Gemma3
KV cache. ``gemma_1m`` is the upstream default (a small expert for fast
iteration); ``gemma_150m`` matches Gemma3-270M's width.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# Camera keys the value model consumes, in the order they are concatenated into
# the prefix. LIBERO provides the first two; the third is simply omitted.
IMAGE_KEYS = ("base_0_rgb", "left_wrist_0_rgb", "right_wrist_0_rgb")

# Maps our dataset's image columns onto the value model's camera keys.
DATASET_IMAGE_MAP = {
    "observation.images.image": "base_0_rgb",
    "observation.images.wrist_image": "left_wrist_0_rgb",
    "observation.images.image2": "left_wrist_0_rgb",
}


@dataclass(frozen=True)
class ExpertConfig:
    """Gemma-1 expert geometry (no embedding table)."""

    width: int
    depth: int
    mlp_dim: int
    num_heads: int
    num_kv_heads: int
    head_dim: int


_EXPERT_VARIANTS: Dict[str, ExpertConfig] = {
    "gemma_1m": ExpertConfig(width=128, depth=4, mlp_dim=448, num_heads=1, num_kv_heads=1, head_dim=256),
    "gemma_50m": ExpertConfig(width=384, depth=18, mlp_dim=1536, num_heads=8, num_kv_heads=1, head_dim=256),
    "gemma_100m": ExpertConfig(width=512, depth=18, mlp_dim=2048, num_heads=8, num_kv_heads=1, head_dim=256),
    "gemma_150m": ExpertConfig(width=640, depth=18, mlp_dim=2560, num_heads=8, num_kv_heads=1, head_dim=256),
    "gemma_300m": ExpertConfig(width=1024, depth=18, mlp_dim=4096, num_heads=8, num_kv_heads=1, head_dim=256),
    "gemma_2b": ExpertConfig(width=2048, depth=18, mlp_dim=16384, num_heads=8, num_kv_heads=1, head_dim=256),
}


def get_expert_config(variant: str) -> ExpertConfig:
    """Return the expert geometry for a variant name."""
    if variant not in _EXPERT_VARIANTS:
        raise ValueError(
            f"Unknown expert variant {variant!r}; expected one of {sorted(_EXPERT_VARIANTS)}"
        )
    return _EXPERT_VARIANTS[variant]


@dataclass
class ValueCriticConfig:
    """Config for the RECAP value critic."""

    siglip_path: str = ""
    gemma3_path: str = ""
    tokenizer_path: str = ""
    critic_expert_variant: str = "gemma_1m"
    num_bins: int = 201
    v_min: float = -1.0
    v_max: float = 0.0
    value_dropout: float = 0.0
    precision: str = "bfloat16"
    mixed_precision: bool = False
    freeze_vision_encoder: bool = False
    freeze_vlm: bool = False
    stop_gradient_to_vlm: bool = False
    gradient_checkpointing: bool = False
    max_token_len: int = 200
    text_template: str = "Task: {prompt}."
    image_size: int = 224

    def validate(self) -> None:
        if not self.siglip_path or not self.gemma3_path:
            raise ValueError("ValueCriticConfig requires siglip_path and gemma3_path")
        if self.num_bins < 2:
            raise ValueError("num_bins must be >= 2")
        if self.v_min >= self.v_max:
            raise ValueError("v_min must be < v_max")
