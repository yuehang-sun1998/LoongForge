# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Value-model dataloader for RECAP critic training.

The critic consumes a *frame*, not an episode step: the observation is the
image(s) + the task prompt, and the regression target is the normalised return
``return / |return_min|`` in ``[-1, 0]`` (RLinf's ``ReturnNormalizer`` with
``normalize_to_minus_one_zero=True``; see
``rlinf/models/embodiment/value_model/recap`` for the upstream definition).

This module is the *multi-GPU* data path used by
:class:`~loongforge.embodied.train.trainers.custom.recap_value.RecapValueTrainer`.
It reuses :class:`RecapLeRobotV2Dataset` for frame decoding (including the
offline frame cache) and :func:`preprocess_image` for the critic's own image
geometry, so a critic trained here sees exactly the same input distribution as
one scored by ``recap.cli.compute_advantages``.

Only frames that carry a ``return`` sidecar entry participate; the valid index
list is computed once from the sidecar (never by decoding frames), so every rank
shards the same set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import torch
from torch.utils.data import DataLoader, Dataset

from .compat import build_recap_dataset_auto
from .dataset import episode_split
from .manifest import resolve_tag
from ..value_model.config import DATASET_IMAGE_MAP
from ..value_model.processing import preprocess_image


@dataclass
class RecapValueBatch:
    """A batch of critic observations + regression targets (CPU tensors)."""

    images: Dict[str, torch.Tensor] = field(default_factory=dict)
    image_masks: Dict[str, torch.Tensor] = field(default_factory=dict)
    tokenized_prompt: Optional[torch.Tensor] = None
    tokenized_prompt_mask: Optional[torch.Tensor] = None
    target_values: Optional[torch.Tensor] = None

    def to(self, device) -> "RecapValueBatch":
        self.images = {key: value.to(device) for key, value in self.images.items()}
        self.image_masks = {key: value.to(device) for key, value in self.image_masks.items()}
        for name in ("tokenized_prompt", "tokenized_prompt_mask", "target_values"):
            value = getattr(self, name)
            if isinstance(value, torch.Tensor):
                setattr(self, name, value.to(device))
        return self

    def as_observation(self) -> Dict[str, Any]:
        """The ``observation`` dict :meth:`ValueCriticModel.forward` expects."""
        return {
            "images": self.images,
            "image_masks": self.image_masks,
            "tokenized_prompt": self.tokenized_prompt,
            "tokenized_prompt_mask": self.tokenized_prompt_mask,
        }


def _resolve_model_image_keys(base, image_map: Dict[str, str]) -> Dict[str, str]:
    """Keep the image-map entries the dataset can actually provide.

    Video-backed datasets advertise their cameras in ``_video_keys``; for
    inline-image datasets that list is empty, so fall back to probing one sample.
    """
    available = set()
    for key in getattr(base, "_video_keys", []) or []:
        available.add(key)
        available.add(key if key.startswith("observation.images.") else f"observation.images.{key}")
    if not available and len(base) > 0:
        available.update(base[0].keys())
    resolved = {dataset_key: model_key for dataset_key, model_key in image_map.items()
                if dataset_key in available}
    if not resolved:
        raise RuntimeError(
            "value dataloader found no matching image columns: none of "
            f"{sorted(image_map)} are present in the dataset "
            f"(available: {sorted(available)})."
        )
    return resolved


def value_frame_indices(base) -> List[int]:
    """Dataset indices of the frames that carry a ``return`` sidecar entry.

    Computed from the sidecar and the dataset's step index only — no frame is
    decoded. Both the training dataset and the "how many steps is an epoch"
    probe use this, so they can never disagree.
    """
    returns_map = getattr(base, "_returns_map", None) or {}
    return [
        index
        for index, (episode_index, step_index) in enumerate(base._step_index)
        if (int(episode_index), int(step_index)) in returns_map
    ]


class RecapValueFrameDataset(Dataset):
    """Per-frame ``(images, prompt, target_return)`` samples for the value critic."""

    def __init__(
        self,
        base,
        tokenizer,
        image_map: Dict[str, str],
        *,
        text_template: str = "Task: {prompt}.",
        max_token_len: int = 200,
        image_size: int = 224,
        return_min: float = -1.0,
        image_dtype: Optional[torch.dtype] = None,
    ):
        self.base = base
        self.tokenizer = tokenizer
        self.image_map = _resolve_model_image_keys(base, image_map)
        self.text_template = text_template
        self.max_token_len = max_token_len
        self.image_size = image_size
        self.image_dtype = image_dtype
        # |return_min| is the ReturnNormalizer denominator; guard against a
        # degenerate (all-zero) return so the target never divides by zero.
        self.denom = abs(float(return_min)) or 1.0

        self._indices: List[int] = value_frame_indices(base)
        if not self._indices:
            raise RuntimeError(
                "value dataloader found no frames with a 'return' entry — run "
                "compute_returns first, or pass the matching --recap-returns-tag."
            )

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        sample = self.base[self._indices[index]]

        images = {
            model_key: preprocess_image(sample[dataset_key], self.image_size, self.image_dtype)
            for dataset_key, model_key in self.image_map.items()
        }
        prompt = self.text_template.format(prompt=str(sample.get("task", "perform the task")))
        encoded = self.tokenizer(
            prompt,
            return_tensors="pt",
            max_length=self.max_token_len,
            padding="max_length",
            truncation=True,
        )
        return {
            "images": images,
            "tokenized_prompt": encoded["input_ids"][0],
            "tokenized_prompt_mask": encoded["attention_mask"][0].bool(),
            "target": torch.tensor(float(sample["return"]) / self.denom, dtype=torch.float32),
        }


def collate_recap_value(examples: List[Dict[str, Any]]) -> RecapValueBatch:
    """Stack per-sample dicts into a :class:`RecapValueBatch` (CPU tensors)."""
    image_keys = list(examples[0]["images"].keys())
    images = {
        key: torch.stack([example["images"][key] for example in examples]) for key in image_keys
    }
    image_masks = {
        key: torch.ones(len(examples), dtype=torch.bool) for key in image_keys
    }
    return RecapValueBatch(
        images=images,
        image_masks=image_masks,
        tokenized_prompt=torch.stack([example["tokenized_prompt"] for example in examples]),
        tokenized_prompt_mask=torch.stack(
            [example["tokenized_prompt_mask"] for example in examples]
        ),
        target_values=torch.stack([example["target"] for example in examples]),
    )


def _read_return_min(dataset_path: str) -> float:
    """``meta/stats.json``'s return minimum, or ``-1.0`` when unavailable."""
    import json

    stats_path = Path(dataset_path) / "meta" / "stats.json"
    if not stats_path.exists():
        return -1.0
    try:
        stats = json.loads(stats_path.read_text())
    except (OSError, ValueError):
        return -1.0
    return float(stats.get("return", {}).get("min", -1.0))


def _build_base_dataset(training_args, episodes):
    return build_recap_dataset_auto(
        training_args.dataset_path,
        action_horizon=1,
        returns_tag=resolve_tag(
            training_args.dataset_path,
            "returns",
            getattr(training_args, "recap_returns_tag", None),
        ),
        episodes=episodes,
        frame_cache_root=getattr(training_args, "recap_frame_cache_root", None),
        frame_reader_cache_size=getattr(training_args, "recap_frame_reader_cache_size", 16),
        video_cache_size=getattr(training_args, "recap_value_video_cache_size", 8),
        force_success=(getattr(training_args, "recap_dataset_type", "rollout") == "sft"),
        compat=getattr(training_args, "recap_dataset_compat", "auto"),
    )


def _build_tokenizer(training_args, model_cfg):
    import os

    from transformers import AutoTokenizer

    tokenizer_path = (
        getattr(model_cfg, "tokenizer_path", None)
        or training_args.tokenizer_path
        or os.environ.get("TOKENIZER_PATH", "")
    )
    if not tokenizer_path:
        raise ValueError(
            "RecapValueTrainer requires a tokenizer: set model.tokenizer_path in "
            "the recap_value config, --tokenizer-path, or TOKENIZER_PATH."
        )
    return AutoTokenizer.from_pretrained(tokenizer_path)


def _build_frame_dataset(base, tokenizer, data_cfg, return_min) -> RecapValueFrameDataset:
    return RecapValueFrameDataset(
        base,
        tokenizer,
        DATASET_IMAGE_MAP,
        text_template=getattr(data_cfg, "text_template", "Task: {prompt}."),
        max_token_len=getattr(data_cfg, "max_token_len", 200),
        image_size=getattr(data_cfg, "image_size", 224),
        return_min=return_min,
    )


def build_recap_value_loaders(training_args, model_cfg, data_cfg, ctx):
    """Build ``(train_loader, val_loader_or_None)`` for the value critic.

    The held-out split is the **last** ``--recap-val-episodes`` episodes of the
    working set (``--recap-max-episodes`` bounds the set first). Validation runs
    on rank 0 only — other ranks have no val loader and stay in the training
    loop — so it needs no cross-rank reduction.
    """
    train_episodes, val_episodes = episode_split(
        training_args.dataset_path,
        getattr(training_args, "recap_max_episodes", None),
        getattr(training_args, "recap_val_episodes", 0),
    )
    tokenizer = _build_tokenizer(training_args, model_cfg)
    return_min = _read_return_min(training_args.dataset_path)

    train_dataset = _build_frame_dataset(
        _build_base_dataset(training_args, train_episodes), tokenizer, data_cfg, return_min
    )
    train_loader = _make_loader(train_dataset, training_args, ctx, shuffle=True)

    val_loader = None
    if val_episodes and ctx.is_main:
        val_dataset = _build_frame_dataset(
            _build_base_dataset(training_args, val_episodes), tokenizer, data_cfg, return_min
        )
        # num_workers=0: a bounded, monitoring-only pass, run on rank 0 while the
        # other ranks keep training.
        val_loader = DataLoader(
            val_dataset,
            batch_size=training_args.per_device_batch_size,
            shuffle=False,
            num_workers=0,
            collate_fn=collate_recap_value,
            pin_memory=False,
        )
    return train_loader, val_loader


def _make_loader(dataset, training_args, ctx, shuffle: bool) -> DataLoader:
    sampler = None
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
        collate_fn=collate_recap_value,
        drop_last=bool(getattr(training_args, "batch_drop_last", False)),
        pin_memory=False,
    )


def build_recap_value_dataloader(training_args, model_cfg, data_cfg, ctx) -> DataLoader:
    """Training dataloader only (see :func:`build_recap_value_loaders`)."""
    return build_recap_value_loaders(training_args, model_cfg, data_cfg, ctx)[0]
