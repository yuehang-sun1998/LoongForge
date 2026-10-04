# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Temporary compatibility layer for externally-produced RECAP rollout datasets.

Our pipeline expects the canonical LeRobot layout (``observation.state`` /
``action`` / ``observation.images.*``). Some external rollout datasets use the
pi0.5 LeRobot naming instead (``state`` / ``actions`` / ``image`` /
``wrist_image``). This module adapts such a dataset **in memory** so the rest of
the RECAP pipeline stays unaware of the difference — nothing on disk is rewritten.

This is a stop-gap. For anything long-lived, re-export the rollout dataset in the
canonical layout instead of relying on this shim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import pandas as pd

from .dataset import RecapLeRobotV2Dataset

# External (pi0.5-style) column -> canonical column expected by our reader.
LEGACY_COLUMN_RENAMES = {
    "state": "observation.state",
    "actions": "action",
}

COMPAT_AUTO = "auto"
COMPAT_NONE = "none"
COMPAT_LEGACY = "legacy"


class LegacyRecapDataset(RecapLeRobotV2Dataset):
    """RECAP dataset that renames pi0.5-style columns to the canonical names."""

    def _get_episode_data(self, episode_index: int) -> pd.DataFrame:
        fresh = self._cached_ep_idx != episode_index
        data = super()._get_episode_data(episode_index)
        if not fresh:
            return data
        renames = {
            old: new
            for old, new in LEGACY_COLUMN_RENAMES.items()
            if old in data.columns and new not in data.columns
        }
        if renames:
            data = data.rename(columns=renames)
            self._cached_ep_data = data
        return data


def detect_legacy(root: str | Path) -> bool:
    """Whether a dataset declares pi0.5-style feature names in ``meta/info.json``."""
    info_path = Path(root) / "meta" / "info.json"
    if not info_path.exists():
        return False
    features = json.loads(info_path.read_text()).get("features", {})
    if "observation.state" in features or "action" in features:
        return False
    return "state" in features or "actions" in features


def build_recap_dataset_auto(
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
    compat: str = COMPAT_AUTO,
) -> RecapLeRobotV2Dataset:
    """Build a RECAP dataset, applying the legacy shim when needed.

    ``compat`` is ``auto`` (detect from ``meta/info.json``), ``legacy`` (force the
    shim) or ``none`` (assume the canonical layout).
    """
    use_legacy = compat == COMPAT_LEGACY or (compat == COMPAT_AUTO and detect_legacy(root))
    cls = LegacyRecapDataset if use_legacy else RecapLeRobotV2Dataset
    return cls(
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


def legacy_notes(root: str | Path) -> Optional[str]:
    """One-line description of the shim applied, for logging (None when unused)."""
    if not detect_legacy(root):
        return None
    return (
        "legacy column rename applied: "
        + ", ".join(f"{old}->{new}" for old, new in LEGACY_COLUMN_RENAMES.items())
    )


__all__ = [
    "LEGACY_COLUMN_RENAMES",
    "LegacyRecapDataset",
    "detect_legacy",
    "build_recap_dataset_auto",
    "legacy_notes",
    "COMPAT_AUTO",
    "COMPAT_NONE",
    "COMPAT_LEGACY",
]
