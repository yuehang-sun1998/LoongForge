# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: rlinf/models/embodiment/value_model/recap/processing.py and
#            rlinf/models/embodiment/value_model/value_common/image_text_processing.py
# Value-model observation preprocessing (224x224, [-1,1] images, "Task: {prompt}." text).
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
"""Observation preprocessing for the RECAP value critic.

Mirrors the upstream value-model processor: images are resized with aspect-preserving
padding to 224x224 and normalised to ``[-1, 1]`` (SigLIP mean=std=0.5), and the
language input uses the plain ``Task: {prompt}.`` template tokenised with the
Gemma3 tokenizer. Unlike the policy, the value model takes no proprioception.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import torch

from ...data.datasets.transforms.utils.image_transform import resize_with_pad


def normalize_signed(img: torch.Tensor) -> torch.Tensor:
    """Map an image in ``[0, 1]`` (or uint8-range) to ``[-1, 1]``."""
    img = img.float()
    if img.max() > 1.0:
        img = img / 255.0 * 2.0 - 1.0
    else:
        img = img * 2.0 - 1.0
    return img


def preprocess_image(
    image: torch.Tensor, image_size: int = 224, dtype: Optional[torch.dtype] = None
) -> torch.Tensor:
    """Resize a ``[C, H, W]`` image to ``image_size`` (with pad) and normalise to [-1, 1]."""
    if image.dim() != 3:
        raise ValueError(f"expected a [C, H, W] image, got shape {tuple(image.shape)}")
    if image.dtype == torch.uint8:
        image = image.float() / 255.0
    if image.shape[-2] != image_size or image.shape[-1] != image_size:
        image = resize_with_pad(image.unsqueeze(0), image_size, image_size).squeeze(0)
    image = normalize_signed(image)
    if dtype is not None:
        image = image.to(dtype)
    return image


class ValueObservationBuilder:
    """Turns dataset samples into the critic's ``observation`` dict."""

    def __init__(
        self,
        tokenizer,
        image_map: Dict[str, str],
        text_template: str = "Task: {prompt}.",
        max_token_len: int = 200,
        image_size: int = 224,
        dtype: Optional[torch.dtype] = None,
        device: str = "cpu",
    ):
        self.tokenizer = tokenizer
        self.image_map = dict(image_map)
        self.text_template = text_template
        self.max_token_len = max_token_len
        self.image_size = image_size
        self.dtype = dtype
        self.device = device

    def build(self, samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Build an observation dict for a list of dataset samples."""
        images: Dict[str, torch.Tensor] = {}
        image_masks: Dict[str, torch.Tensor] = {}
        prompts: List[str] = []
        for sample in samples:
            prompts.append(self.text_template.format(prompt=str(sample.get("task", "perform the task"))))
            for dataset_key, model_key in self.image_map.items():
                if dataset_key not in sample:
                    continue
                image = preprocess_image(sample[dataset_key], self.image_size, self.dtype)
                images.setdefault(model_key, []).append(image)

        stacked = {
            key: torch.stack(values).to(self.device) for key, values in images.items()
        }
        for key, value in stacked.items():
            image_masks[key] = torch.ones(value.shape[0], dtype=torch.bool, device=self.device)

        encoded = self.tokenizer(
            prompts,
            return_tensors="pt",
            max_length=self.max_token_len,
            padding="max_length",
            truncation=True,
        )
        return {
            "images": stacked,
            "image_masks": image_masks,
            "tokenized_prompt": encoded["input_ids"].to(self.device),
            "tokenized_prompt_mask": encoded["attention_mask"].to(self.device).bool(),
        }
