# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Parquet sidecars for RECAP.

Returns and advantages are written next to the rollout dataset, keyed by
``(episode_index, frame_index)``, exactly like the upstream sidecars::

    <dataset>/meta/returns.parquet
    <dataset>/meta/advantages.parquet

The original ``data/*.parquet`` files are never modified, so the same rollout
dataset can carry several tagged variants (e.g. ``returns_paper.parquet``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

import pandas as pd

SIDECAR_RETURNS = "returns"
SIDECAR_ADVANTAGES = "advantages"

SIDECAR_KEYS = ("episode_index", "frame_index")


def sidecar_path(root: str | Path, name: str, tag: Optional[str] = None) -> Path:
    """Path of a sidecar file: ``<root>/meta/<name>[_<tag>].parquet``."""
    filename = f"{name}_{tag}.parquet" if tag else f"{name}.parquet"
    return Path(root) / "meta" / filename


def write_sidecar(
    root: str | Path, name: str, frame: pd.DataFrame, tag: Optional[str] = None
) -> Path:
    """Write a sidecar parquet (creating ``meta/`` if needed). Returns the path."""
    missing = [key for key in SIDECAR_KEYS if key not in frame.columns]
    if missing:
        raise ValueError(f"sidecar '{name}' is missing key columns: {missing}")
    path = sidecar_path(root, name, tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def read_sidecar(
    root: str | Path, name: str, tag: Optional[str] = None
) -> Optional[pd.DataFrame]:
    """Read a sidecar parquet, or return ``None`` when it does not exist."""
    path = sidecar_path(root, name, tag)
    if not path.exists():
        return None
    return pd.read_parquet(path)


def lookup_by_frame(
    frame: pd.DataFrame, columns: Tuple[str, ...]
) -> Dict[Tuple[int, int], Dict[str, float]]:
    """Index a sidecar frame into ``{(episode_index, frame_index): {col: value}}``.

    Values are coerced to plain Python floats/bools so the lookup can be attached
    to dataset samples without leaking numpy scalars.
    """
    table: Dict[Tuple[int, int], Dict[str, float]] = {}
    episodes = frame["episode_index"].to_numpy()
    frames = frame["frame_index"].to_numpy()
    arrays = {col: frame[col].to_numpy() for col in columns if col in frame.columns}
    for row in range(len(frame)):
        key = (int(episodes[row]), int(frames[row]))
        table[key] = {col: arrays[col][row] for col in arrays}
    return table
