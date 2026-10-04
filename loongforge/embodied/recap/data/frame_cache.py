# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Offline frame cache for RECAP rollout datasets.

Why
---
The stock reader opens the video container once **per requested frame** (~77 ms on
LIBERO 256x256 h264). Under shuffled random access that dominates training time
(GPU utilisation ~0%). Decoding a whole episode in one container open and caching
the *encoded frames* removes the container-open cost while keeping random access.

What it stores (and what it deliberately does not)
--------------------------------------------------
The cache stores the **raw decoded frames at their native resolution** (256x256
uint8 for LIBERO) — exactly what the video decoder returns — and nothing else.
Resize / normalisation stay in the runtime ``ImageTransform``, so the training
input distribution is **unchanged by construction**. With ``png`` the round-trip
is lossless (bit-identical to decoding the video); ``jpeg`` trades a small,
measurable loss for ~3.5x less space.

Layout::

    <cache_root>/
    ├── meta.json                     # version, format, quality, source dataset
    └── <view>/episode_XXXXXX.npz     # offsets[int64,T+1] + blob[uint8]

One file per (view, episode) — never one file per frame (that would be millions
of files on a network filesystem).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

CACHE_VERSION = 1
FORMAT_PNG = "png"
FORMAT_JPEG = "jpeg"
FORMATS = (FORMAT_PNG, FORMAT_JPEG)


# ── encoding ──────────────────────────────────────────────────────────────
def encode_frame(frame: np.ndarray, fmt: str, quality: int = 95, png_level: int = 6) -> bytes:
    """Encode one ``(H, W, C)`` uint8 frame to PNG (lossless) or JPEG."""
    from PIL import Image

    buffer = io.BytesIO()
    if fmt == FORMAT_JPEG:
        Image.fromarray(frame).save(buffer, format="JPEG", quality=int(quality))
    elif fmt == FORMAT_PNG:
        Image.fromarray(frame).save(buffer, format="PNG", compress_level=int(png_level))
    else:
        raise ValueError(f"unknown frame cache format {fmt!r}; expected one of {FORMATS}")
    return buffer.getvalue()


def decode_frame(payload: bytes) -> np.ndarray:
    """Decode an encoded frame back to ``(H, W, C)`` uint8."""
    from PIL import Image

    # np.array (not asarray) copies, so the result is writable and safe for
    # torch.from_numpy downstream.
    return np.array(Image.open(io.BytesIO(payload)).convert("RGB"), dtype=np.uint8)


def encode_episode(
    frames: np.ndarray, fmt: str, quality: int = 95, png_level: int = 6
) -> Tuple[np.ndarray, np.ndarray]:
    """Encode ``[T, H, W, C]`` uint8 frames into ``(offsets[T+1], blob)``."""
    blobs: List[bytes] = [encode_frame(f, fmt, quality, png_level) for f in frames]
    offsets = np.zeros(len(blobs) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum([len(b) for b in blobs])
    blob = np.frombuffer(b"".join(blobs), dtype=np.uint8)
    return offsets, blob


# ── on-disk layout ────────────────────────────────────────────────────────
@dataclass
class CacheSpec:
    """Where a frame cache lives and how it was encoded."""

    root: Path
    fmt: str = FORMAT_PNG
    quality: int = 95

    @classmethod
    def load(cls, root: str | Path) -> "CacheSpec":
        root = Path(root)
        meta_path = root / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"{root} is not a frame cache (no meta.json); run "
                f"`python -m loongforge.embodied.recap.cli.predecode` first"
            )
        meta = json.loads(meta_path.read_text())
        return cls(root=root, fmt=meta.get("format", FORMAT_PNG), quality=int(meta.get("quality", 95)))


def episode_cache_path(root: str | Path, view: str, episode_index: int) -> Path:
    return Path(root) / view / f"episode_{episode_index:06d}.npz"


def write_meta(
    root: str | Path,
    fmt: str,
    quality: int,
    source_dataset: str,
    views: Sequence[str],
    fps: int,
) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    meta = {
        "cache_version": CACHE_VERSION,
        "format": fmt,
        "quality": int(quality),
        "lossless": fmt == FORMAT_PNG,
        "source_dataset": str(source_dataset),
        "views": list(views),
        "fps": int(fps),
        "note": "raw native-resolution frames; resize/normalisation stay in the runtime pipeline",
    }
    path = root / "meta.json"
    path.write_text(json.dumps(meta, indent=2))
    return path


def write_episode(root: str | Path, view: str, episode_index: int, offsets, blob) -> Path:
    path = episode_cache_path(root, view, episode_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, offsets=offsets, blob=blob)
    return path


class EpisodeFrameReader:
    """Random access to one cached episode view (offsets + encoded blob)."""

    def __init__(self, root: str | Path, view: str, episode_index: int):
        path = episode_cache_path(root, view, episode_index)
        if not path.exists():
            raise FileNotFoundError(f"missing cached episode: {path}")
        archive = np.load(path)
        self._offsets = archive["offsets"]
        self._blob = archive["blob"]

    def __len__(self) -> int:
        return len(self._offsets) - 1

    def frame(self, index: int) -> np.ndarray:
        start, end = int(self._offsets[index]), int(self._offsets[index + 1])
        return decode_frame(self._blob[start:end].tobytes())


def cache_stats(root: str | Path) -> Dict[str, int]:
    """Total size and file count of a cache (for reporting)."""
    root = Path(root)
    total = 0
    files = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            files += 1
    return {"bytes": total, "files": files}
