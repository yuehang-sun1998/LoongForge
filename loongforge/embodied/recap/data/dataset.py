# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RECAP dataset wrappers over the LeRobot v2 reader.

``RecapLeRobotV2Dataset`` extends :class:`LeRobotV2Dataset` with the three things
RECAP needs and the stock reader does not expose:

* per-episode ``is_success`` (from ``meta/episodes.jsonl`` or the last parquet row),
* the ``returns`` sidecar (``return`` / ``reward`` per frame),
* the ``advantages`` sidecar (``advantage`` / ``advantage_indicator`` per frame).

The original parquet/video files are never modified.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset

from ...data.datasets.lerobot_dataset import LeRobotV2Dataset
from .frame_cache import CacheSpec, EpisodeFrameReader
from ..core.reward import EpisodeSpec
from .sidecar import SIDECAR_ADVANTAGES, SIDECAR_RETURNS, lookup_by_frame, read_sidecar


def _to_uint8_image(cell) -> "np.ndarray":
    """Normalise an inline parquet image cell to a ``(H, W, C)`` uint8 array.

    With pandas >= 3, nested-list image columns round-trip as nested *object*
    arrays rather than a plain 3-D array, which the stock reader cannot reshape.
    This collapses that nesting back into ``(H, W, C)``; already-plain arrays are
    returned unchanged.
    """
    array = np.asarray(cell)
    if array.dtype == object:
        return np.array([_to_uint8_image(item) for item in array], dtype=np.uint8)
    return array.astype(np.uint8)


def _resize_image(image: torch.Tensor, size: Optional[int]) -> torch.Tensor:
    """Optionally downsample a ``[C, H, W]`` image to ``size x size``."""
    if not size or image.shape[-1] == size:
        return image
    resized = torch.nn.functional.interpolate(
        image.unsqueeze(0).float(),
        size=(size, size),
        mode="bilinear",
        align_corners=False,
        antialias=True,
    )
    return resized.squeeze(0)


class RecapLeRobotV2Dataset(LeRobotV2Dataset):
    """LeRobot v2 rollout dataset with returns/advantages/is_success attached."""

    def __init__(
        self,
        root: str | Path,
        action_horizon: int = 1,
        returns_tag: Optional[str] = None,
        advantages_tag: Optional[str] = None,
        image_key: Optional[str] = None,
        video_cache_size: int = 1,
        frame_cache_root: Optional[str] = None,
        frame_reader_cache_size: int = 16,
        force_success: bool = False,
        **kwargs: Any,
    ):
        super().__init__(root, action_horizon=action_horizon, **kwargs)
        self.image_key = image_key or (self._video_keys[0] if self._video_keys else None)

        # Optional offline frame cache (see recap/frame_cache.py). When set, frames
        # are read from pre-decoded shards instead of the video container. The
        # cached frames are the raw decoded frames, so the runtime resize /
        # normalisation (and thus the input distribution) is unchanged.
        self.frame_cache_root = Path(frame_cache_root) if frame_cache_root else None
        self.frame_cache_spec = CacheSpec.load(self.frame_cache_root) if self.frame_cache_root else None
        self.frame_reader_cache_size = int(frame_reader_cache_size)
        self._frame_reader_cache: "OrderedDict[Tuple[str, int], EpisodeFrameReader]" = OrderedDict()
        # sft datasets are treated as all-success (upstream behaviour).
        self.force_success = bool(force_success)

        # Episode-level decoded-frame cache (LRU over episodes, holding every
        # camera view of a cached episode). The stock reader opens the video
        # container once per requested frame (~77 ms/frame on LIBERO h264);
        # decoding a whole episode in one container open and caching it makes
        # episode-major iteration (value / advantage stages) ~48x faster.
        # ``video_cache_size`` is the number of *episodes* kept; 0 disables the
        # cache and falls back to the stock per-frame path.
        self.video_cache_size = int(video_cache_size)
        self._video_cache: "OrderedDict[int, Dict[str, torch.Tensor]]" = OrderedDict()

        returns_frame = read_sidecar(root, SIDECAR_RETURNS, returns_tag)
        advantages_frame = read_sidecar(root, SIDECAR_ADVANTAGES, advantages_tag)
        self.returns = returns_frame
        self.advantages = advantages_frame
        self._returns_map = (
            lookup_by_frame(returns_frame, ("return", "reward"))
            if returns_frame is not None
            else {}
        )
        self._advantages_map = (
            lookup_by_frame(
                advantages_frame, ("advantage_continuous", "advantage", "threshold")
            )
            if advantages_frame is not None
            else {}
        )
        self._episodes_by_index = {int(ep["episode_index"]): ep for ep in self._episodes}
        self._success_cache: Dict[int, bool] = {}

    # ── episode metadata ──────────────────────────────────────────
    def _get_episode_data(self, episode_index: int) -> pd.DataFrame:
        """Load an episode, normalising inline parquet image cells once per episode.

        Only relevant for datasets that store images *inside* the parquet (our
        mock and other inline-image datasets); video-backed datasets have no such
        columns and this is a no-op. With pandas >= 3 the nested-list cells
        round-trip as nested object arrays which the stock reader cannot reshape
        back to ``(H, W, C)``.
        """
        fresh = self._cached_ep_idx != episode_index
        data = super()._get_episode_data(episode_index)
        if not fresh:
            return data
        changed = False
        for key in self._video_keys:
            if key in data.columns:
                data[key] = pd.Series(
                    [_to_uint8_image(value) for value in data[key]],
                    index=data.index,
                    dtype=object,
                )
                changed = True
        if changed:
            self._cached_ep_data = data
        return data

    # ── batched video decode ──────────────────────────────────────
    def _decode_video_frame(self, video_key: str, episode_index: int, frame_index: int) -> torch.Tensor:
        """Decode one frame, reusing an episode-level batched decode cache.

        Reads from the offline frame cache when one is configured. Otherwise falls
        back to the episode-batched decode cache, and to the stock per-frame path
        when ``video_cache_size <= 0``.
        """
        if self.frame_cache_root is not None:
            return self._decode_cached_frame(video_key, episode_index, frame_index)
        if self.video_cache_size <= 0:
            return super()._decode_video_frame(video_key, episode_index, frame_index)

        views = self._video_cache.get(episode_index)
        if views is None:
            views = {}
            self._video_cache[episode_index] = views
            while len(self._video_cache) > self.video_cache_size:
                self._video_cache.popitem(last=False)
        else:
            self._video_cache.move_to_end(episode_index)

        frames = views.get(video_key)
        if frames is None:
            frames = self._decode_episode_video(video_key, episode_index)
            views[video_key] = frames
        index = min(int(frame_index), frames.shape[0] - 1)
        return frames[index].float() / 255.0

    def _decode_cached_frame(
        self, video_key: str, episode_index: int, frame_index: int
    ) -> torch.Tensor:
        """Read one frame from the offline frame cache (LRU over episode views)."""
        key = (video_key, episode_index)
        reader = self._frame_reader_cache.get(key)
        if reader is None:
            reader = EpisodeFrameReader(self.frame_cache_root, video_key, episode_index)
            self._frame_reader_cache[key] = reader
            while len(self._frame_reader_cache) > self.frame_reader_cache_size:
                self._frame_reader_cache.popitem(last=False)
        else:
            self._frame_reader_cache.move_to_end(key)
        index = min(int(frame_index), len(reader) - 1)
        frame = reader.frame(index)  # (H, W, C) uint8
        return torch.from_numpy(frame).permute(2, 0, 1).float() / 255.0

    def _decode_episode_video(self, video_key: str, episode_index: int) -> torch.Tensor:
        """Decode every frame of one episode video as uint8 [T, C, H, W]."""
        length = int(self._episodes_by_index.get(episode_index, {}).get("length", 0))
        if length <= 0:
            length = len(self._get_episode_data(episode_index))
        episode_chunk = episode_index // self.chunks_size
        video_path = self.root / self._video_path_tpl.format(
            episode_chunk=episode_chunk, video_key=video_key, episode_index=episode_index,
        )

        from ...data.datasets.video_backends import decode_video_frames_by_timestamps

        frames = decode_video_frames_by_timestamps(  # [T, H, W, C] uint8, one container open
            str(video_path), [i / self.fps for i in range(length)], backend=self.video_backend,
        )
        return torch.from_numpy(frames).permute(0, 3, 1, 2).to(torch.uint8)  # [T, C, H, W]

    def episode_success(self, episode_index: int) -> bool:
        """Per-episode success flag (cached).

        Mirrors upstream: the last frame's ``is_success`` column wins, with the
        episodes.jsonl entry as a fallback. ``force_success`` (sft datasets)
        overrides both.
        """
        if self.force_success:
            return True
        if episode_index in self._success_cache:
            return self._success_cache[episode_index]
        episode = self._episodes_by_index.get(episode_index, {})
        success: Optional[bool] = None
        data = self._get_episode_data(episode_index)
        if "is_success" in data.columns:
            success = bool(data.iloc[-1]["is_success"])
        elif "is_success" in episode:
            success = bool(episode["is_success"])
        success = bool(success) if success is not None else False
        self._success_cache[episode_index] = success
        return success

    def episode_task_index(self, episode_index: int) -> int:
        """Task index for an episode (jsonl first, else first parquet row)."""
        episode = self._episodes_by_index.get(episode_index, {})
        if "task_index" in episode:
            return int(episode["task_index"])
        data = self._get_episode_data(episode_index)
        if "task_index" in data.columns:
            return int(data.iloc[0]["task_index"])
        return 0

    def episode_specs(self) -> List[EpisodeSpec]:
        """Episode specs for reward/return/advantage computation."""
        return [
            EpisodeSpec(
                episode_index=int(ep["episode_index"]),
                length=int(ep["length"]),
                is_success=self.episode_success(int(ep["episode_index"])),
                task_index=self.episode_task_index(int(ep["episode_index"])),
            )
            for ep in self._episodes
        ]

    # ── samples ───────────────────────────────────────────────────
    def __getitem__(self, index: int) -> Dict[str, Any]:
        sample = super().__getitem__(index)
        episode_index = int(sample["episode_index"])
        frame_index = int(sample["frame_index"])
        key = (episode_index, frame_index)

        sample["is_success"] = self.episode_success(episode_index)
        sample.update(self._returns_map.get(key, {}))
        sample.update(self._advantages_map.get(key, {}))
        if "advantage" in sample:  # upstream bool column
            sample["advantage_indicator"] = int(bool(sample["advantage"]))
        return sample

    # ── value-model helpers ───────────────────────────────────────
    def value_samples(
        self, require_returns: bool = True, image_size: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Flatten the dataset into value-model samples (state, image, target).

        Args:
            require_returns: when True (default) skip frames without a return
                sidecar entry; set False to collect every frame (used by the
                advantage step, which only needs the state/image inputs).
            image_size: optional square size to downsample images to before the
                value model sees them (the value head only needs a coarse view;
                full-resolution frames are wasted compute).
        """
        samples: List[Dict[str, Any]] = []
        for index in range(len(self)):
            sample = self[index]
            if require_returns and "return" not in sample:
                continue
            entry = {
                "state": sample["observation.state"],
                "episode_index": int(sample["episode_index"]),
                "frame_index": int(sample["frame_index"]),
            }
            if require_returns:
                entry["target"] = torch.tensor(float(sample["return"]), dtype=torch.float32)
            if self.image_key is not None and self.image_key in sample:
                entry["image"] = _resize_image(sample[self.image_key], image_size)
            samples.append(entry)
        return samples


def episode_subset(root: str | Path, max_episodes: Optional[int]) -> Optional[List[int]]:
    """Return ``[0..N)`` for a dataset with at least N episodes, else ``None``.

    Used to pass a bounded episode list into the dataset for quick validation.
    Reads only ``meta/episodes.jsonl`` — no frames are decoded.
    """
    if not max_episodes:
        return None
    episodes_path = Path(root) / "meta" / "episodes.jsonl"
    with open(episodes_path) as handle:
        total = sum(1 for line in handle if line.strip())
    return list(range(min(int(max_episodes), total)))


def episode_split(
    root: str | Path,
    max_episodes: Optional[int] = None,
    val_episodes: int = 0,
) -> "tuple[Optional[List[int]], Optional[List[int]]]":
    """Split the episode list into ``(train, val)``.

    ``max_episodes`` bounds the working set to the first N episodes (quick
    validation); ``val_episodes`` then carves the **last** N of that set off for
    validation. Returns ``(None, None)`` when neither is set (whole dataset,
    no validation), and ``(episodes, None)`` when only ``max_episodes`` is set.
    """
    total = 0
    episodes_path = Path(root) / "meta" / "episodes.jsonl"
    with open(episodes_path) as handle:
        total = sum(1 for line in handle if line.strip())

    if not max_episodes and not val_episodes:
        return None, None
    limit = min(int(max_episodes), total) if max_episodes else total
    if not val_episodes:
        return list(range(limit)), None
    val = min(int(val_episodes), limit - 1)
    if val <= 0:
        raise ValueError(
            f"val_episodes={val_episodes} leaves no training episodes "
            f"(working set: {limit} of {total})."
        )
    return list(range(limit - val)), list(range(limit - val, limit))


class RecapValueDataset(Dataset):
    """Torch dataset of ``(state, [image], target_return)`` for value training."""

    def __init__(self, samples: List[Dict[str, Any]]):
        self.samples = samples

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        return self.samples[index]


def build_recap_dataset(
    root: str | Path,
    action_horizon: int = 1,
    returns_tag: Optional[str] = None,
    advantages_tag: Optional[str] = None,
    image_key: Optional[str] = None,
    episodes: Optional[List[int]] = None,
    video_cache_size: int = 1,
    frame_cache_root: Optional[str] = None,
    frame_reader_cache_size: int = 16,
    force_success: bool = False,
) -> RecapLeRobotV2Dataset:
    """Build a :class:`RecapLeRobotV2Dataset` (map-style, v2.0/v2.1 layout)."""
    return RecapLeRobotV2Dataset(
        root=root,
        action_horizon=action_horizon,
        returns_tag=returns_tag,
        advantages_tag=advantages_tag,
        image_key=image_key,
        episodes=episodes,
        video_cache_size=video_cache_size,
        frame_cache_root=frame_cache_root,
        frame_reader_cache_size=frame_reader_cache_size,
        force_success=force_success,
    )
