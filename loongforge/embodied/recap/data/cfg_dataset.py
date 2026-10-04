# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: rlinf/data/datasets/recap/cfg_model.py
# The base/positive/negative tokenisation mirrors the upstream CFG data model.
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

"""Advantage-conditioned (CFG) dataset + collator for RECAP policy training.

This is the data side of Stage-1 Step 5. It reuses the pi0.5 per-sample transforms
(image / action / state discretisation) but replaces the single tokenisation step
with three: the base prompt and the prompt with the positive / negative advantage
clause. The resulting batch carries all three tokenisations plus the binary
advantage indicator, so :meth:`PI05Policy.compute_action_loss` can route each
sample without re-tokenising inside the model.

Kept inside the recap package (rather than the generic transform registry) so the
RECAP pipeline stays decoupled from the base SFT pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import torch
from torch.utils.data import DataLoader, Dataset

from ...data.datasets.transforms.base import ComposedTransform
from ...data.datasets.transforms.registry import TransformBuilderContext, get_transform_builder
from .compat import build_recap_dataset_auto
from .manifest import resolve_tag
from ..core.config import RecapConfig

_TOKENIZE_TRANSFORM = "Pi05TokenizeTransform"


@dataclass
class RecapCfgBatch:
    """Batch for advantage-conditioned CFG training.

    ``input_ids`` / ``attention_mask`` hold the base tokenisation so the batch is
    also a valid pi0.5 batch; ``input_ids_base`` aliases them and
    ``compute_action_loss`` swaps in the conditioned tokenisation per sample.
    """

    images_list: List[torch.Tensor]
    img_masks: List[torch.Tensor]
    input_ids: torch.Tensor
    attention_mask: torch.Tensor
    actions: torch.Tensor
    advantage_indicator: torch.Tensor
    input_ids_base: torch.Tensor = None
    attention_mask_base: torch.Tensor = None
    input_ids_pos: torch.Tensor = None
    attention_mask_pos: torch.Tensor = None
    input_ids_neg: torch.Tensor = None
    attention_mask_neg: torch.Tensor = None

    def to(self, device) -> "RecapCfgBatch":
        self.images_list = [image.to(device) for image in self.images_list]
        self.img_masks = [mask.to(device) for mask in self.img_masks]
        for name in (
            "input_ids",
            "attention_mask",
            "actions",
            "advantage_indicator",
            "input_ids_base",
            "attention_mask_base",
            "input_ids_pos",
            "attention_mask_pos",
            "input_ids_neg",
            "attention_mask_neg",
        ):
            value = getattr(self, name, None)
            if isinstance(value, torch.Tensor):
                setattr(self, name, value.to(device))
        return self


def build_recap_cfg_transforms(model_cfg, data_cfg, training_args, dataset, dataset_stats):
    """pi0.5 per-sample transforms with the tokenisation step removed."""
    ctx = TransformBuilderContext(
        model_cfg=model_cfg,
        data_cfg=data_cfg,
        training_args=training_args,
        dataset=dataset,
        dataset_stats=dataset_stats,
    )
    transforms = [
        transform
        for transform in get_transform_builder(model_cfg.model_type)(ctx)
        if type(transform).__name__ != _TOKENIZE_TRANSFORM
    ]
    return ComposedTransform(transforms)


class RecapCfgDataset(Dataset):
    """Wraps a RECAP dataset and emits base/positive/negative tokenisations."""

    def __init__(
        self,
        base,
        transforms: ComposedTransform,
        tokenizer,
        cfg: RecapConfig,
        max_token_len: int = 200,
    ):
        self.base = base
        self.transforms = transforms
        self.tokenizer = tokenizer
        self.cfg = cfg
        self.max_token_len = max_token_len

    def __len__(self) -> int:
        return len(self.base)

    def _tokenize(self, text: str) -> Dict[str, torch.Tensor]:
        from ...data.datasets.pi05.transforms.pi05_collator import tokenize_prompts

        encoded = tokenize_prompts([text], self.tokenizer, max_length=self.max_token_len)
        return {
            "input_ids": encoded["input_ids"].squeeze(0),
            "attention_mask": encoded["attention_mask"].squeeze(0).bool(),
        }

    def __getitem__(self, index: int) -> Dict[str, Any]:
        sample = self.base[index]
        if "advantage_indicator" not in sample:
            raise RuntimeError(
                "sample has no advantage_indicator — run compute_advantages before CFG training"
            )
        sample = self.transforms(sample)
        prompt = sample["prompt"]

        out: Dict[str, Any] = {
            "images_list": sample["images_list"],
            "img_masks": sample["img_masks"],
            "action": sample["action"],
            "advantage_indicator": int(sample["advantage_indicator"]),
        }
        variants = {
            "base": prompt,
            "pos": f"{prompt}{self.cfg.condition.positive_suffix}",
            "neg": f"{prompt}{self.cfg.condition.negative_suffix}",
        }
        for name, text in variants.items():
            tokens = self._tokenize(text)
            out[f"input_ids_{name}"] = tokens["input_ids"]
            out[f"attention_mask_{name}"] = tokens["attention_mask"]
        return out


def collate_recap_cfg(examples: List[Dict[str, Any]]) -> RecapCfgBatch:
    """Stack per-sample dicts into a :class:`RecapCfgBatch` (CPU tensors)."""
    num_views = len(examples[0]["images_list"])
    images_list = [
        torch.stack([example["images_list"][view] for example in examples]) for view in range(num_views)
    ]
    img_masks = [
        torch.tensor([example["img_masks"][view] for example in examples], dtype=torch.bool)
        for view in range(num_views)
    ]

    def stack(key: str) -> torch.Tensor:
        return torch.stack([example[key] for example in examples])

    return RecapCfgBatch(
        images_list=images_list,
        img_masks=img_masks,
        input_ids=stack("input_ids_base"),
        attention_mask=stack("attention_mask_base"),
        actions=torch.stack(
            [
                example["action"]
                if isinstance(example["action"], torch.Tensor)
                else torch.as_tensor(example["action"], dtype=torch.float32)
                for example in examples
            ]
        ),
        advantage_indicator=torch.tensor(
            [example["advantage_indicator"] for example in examples], dtype=torch.long
        ),
        input_ids_base=stack("input_ids_base"),
        attention_mask_base=stack("attention_mask_base"),
        input_ids_pos=stack("input_ids_pos"),
        attention_mask_pos=stack("attention_mask_pos"),
        input_ids_neg=stack("input_ids_neg"),
        attention_mask_neg=stack("attention_mask_neg"),
    )


def build_recap_cfg_dataloader(training_args, model_cfg, data_cfg, ctx) -> DataLoader:
    """Build the CFG training dataloader (advantage sidecar required)."""
    import os

    from transformers import AutoTokenizer

    cfg = load_recap_config(training_args)
    base = build_recap_dataset_auto(
        training_args.dataset_path,
        action_horizon=model_cfg.action_horizon,
        advantages_tag=resolve_tag(
            training_args.dataset_path,
            "advantages",
            getattr(training_args, "recap_advantages_tag", None),
        ),
        frame_cache_root=getattr(training_args, "recap_frame_cache_root", None),
        frame_reader_cache_size=getattr(training_args, "recap_frame_reader_cache_size", 16),
        compat=getattr(training_args, "recap_dataset_compat", "auto"),
    )
    transforms = build_recap_cfg_transforms(
        model_cfg, data_cfg, training_args, base, getattr(base, "stats", None)
    )
    tokenizer_path = training_args.tokenizer_path or os.environ.get("TOKENIZER_PATH", "")
    if not tokenizer_path:
        raise ValueError("CFG training requires --tokenizer-path (PaliGemma tokenizer).")
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)

    dataset = RecapCfgDataset(
        base, transforms, tokenizer, cfg, max_token_len=data_cfg.max_token_len
    )

    sampler = None
    shuffle = True
    if ctx.is_distributed:
        from torch.utils.data import DistributedSampler

        sampler = DistributedSampler(
            dataset, num_replicas=ctx.world_size, rank=ctx.rank, shuffle=True
        )
        shuffle = False

    return DataLoader(
        dataset,
        batch_size=training_args.per_device_batch_size,
        sampler=sampler,
        shuffle=shuffle,
        num_workers=training_args.num_workers,
        collate_fn=collate_recap_cfg,
        drop_last=bool(getattr(training_args, "batch_drop_last", False)),
        pin_memory=False,
    )


def load_recap_config(training_args) -> RecapConfig:
    """Load the RECAP config referenced by ``--recap-config`` (default: baseline)."""
    path = getattr(training_args, "recap_config", None)
    if path:
        return RecapConfig.from_yaml(path)
    return RecapConfig.preset("baseline")


def get_dataloader_length(loader: DataLoader) -> Optional[int]:
    """Best-effort length of a dataloader (for logging)."""
    try:
        return len(loader)
    except TypeError:
        return None
