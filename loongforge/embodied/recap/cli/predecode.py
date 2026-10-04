# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Pre-decode a LeRobot v2 rollout dataset's videos into a frame cache.

Decodes every frame of every (view, episode) in a single container open and stores
the encoded frames next to the dataset. The training pipeline then reads frames
from the cache instead of the video (``--frame-cache-root``), which removes the
per-frame container-open cost without changing the observation distribution.

Usage:
    # lossless (default)
    python -m loongforge.embodied.recap.cli.predecode \
        --dataset-path /path/to/rollout --output /path/to/frames_png

    # lossy, ~3.5x smaller
    python -m loongforge.embodied.recap.cli.predecode \
        --dataset-path /path/to/rollout --output /path/to/frames_jpeg \
        --format jpeg --quality 95
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import time
from pathlib import Path
from typing import List, Optional, Tuple

from ..data.frame_cache import (
    FORMAT_PNG,
    FORMATS,
    cache_stats,
    encode_episode,
    write_episode,
    write_meta,
)

_TASK = {}


def _init_worker(dataset_root: str, output: str, view_tpl: str, fps: int, chunks_size: int,
                 fmt: str, quality: int, png_level: int) -> None:
    _TASK.update(
        dataset_root=dataset_root, output=output, view_tpl=view_tpl, fps=fps,
        chunks_size=chunks_size, fmt=fmt, quality=quality, png_level=png_level,
    )


def _video_path(view: str, episode_index: int) -> Path:
    chunk = episode_index // _TASK["chunks_size"]
    return Path(_TASK["dataset_root"]) / _TASK["view_tpl"].format(
        episode_chunk=chunk, video_key=view, episode_index=episode_index
    )


def _decode_episode(view: str, episode_index: int, length: int) -> Tuple[str, int, int]:
    """Decode one (view, episode) in one container open and return the encoded blobs."""
    from loongforge.embodied.data.datasets.video_backends import decode_video_frames_by_timestamps

    path = _video_path(view, episode_index)
    frames = decode_video_frames_by_timestamps(  # [T, H, W, C] uint8
        str(path), [i / _TASK["fps"] for i in range(length)]
    )
    offsets, blob = encode_episode(frames, _TASK["fmt"], _TASK["quality"], _TASK["png_level"])
    write_episode(_TASK["output"], view, episode_index, offsets, blob)
    return view, episode_index, len(frames)


def _task(payload: Tuple[str, int, int]) -> Tuple[str, int, int]:
    return _decode_episode(*payload)


def _load_episodes(dataset_root: Path, max_episodes: Optional[int]) -> List[Tuple[int, int]]:
    entries = []
    with open(dataset_root / "meta" / "episodes.jsonl") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    if max_episodes is not None:
        entries = entries[:max_episodes]
    return [(int(e["episode_index"]), int(e["length"])) for e in entries]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pre-decode LeRobot v2 videos into a frame cache.")
    parser.add_argument("--dataset-path", required=True)
    parser.add_argument("--output", required=True, help="Frame cache root to create.")
    parser.add_argument("--format", default=FORMAT_PNG, choices=list(FORMATS),
                        help="png = lossless (default, bit-identical); jpeg = lossy, ~3.5x smaller.")
    parser.add_argument("--quality", type=int, default=95, help="JPEG quality (ignored for png).")
    parser.add_argument("--png-level", type=int, default=6, help="PNG compress_level 0-9 (0=fast/big).")
    parser.add_argument("--views", nargs="+", default=None, help="Video keys to cache (default: all).")
    parser.add_argument("--max-episodes", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--verify", type=int, default=0,
                        help="After writing, bit-compare N random frames against the video path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dataset_root = Path(args.dataset_path)
    info = json.loads((dataset_root / "meta" / "info.json").read_text())
    features = info.get("features", {})
    views = args.views or [k for k, v in features.items() if v.get("dtype") == "video"]
    if not views:
        raise RuntimeError(f"no video features found in {dataset_root}/meta/info.json")
    fps = int(info.get("fps", 10))
    chunks_size = int(info.get("chunks_size", 1000))
    view_tpl = info.get("video_path", "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4")

    episodes = _load_episodes(dataset_root, args.max_episodes)
    write_meta(args.output, args.format, args.quality, str(dataset_root), views, fps)
    tasks = [(view, ep, length) for view in views for ep, length in episodes]

    print(f"[predecode] {len(tasks)} (view, episode) tasks, format={args.format} "
          f"quality={args.quality} workers={args.workers} -> {args.output}")
    start = time.time()
    done = 0
    if args.workers > 1:
        ctx = mp.get_context("spawn")
        with ctx.Pool(
            processes=args.workers,
            initializer=_init_worker,
            initargs=(str(dataset_root), str(args.output), view_tpl, fps, chunks_size,
                      args.format, args.quality, args.png_level),
        ) as pool:
            for view, episode, count in pool.imap_unordered(_task_worker, tasks):
                done += 1
                if done % 100 == 0 or done == len(tasks):
                    print(f"[predecode]   {done}/{len(tasks)} ({time.time() - start:.0f}s)", flush=True)
    else:
        _init_worker(str(dataset_root), str(args.output), view_tpl, fps, chunks_size,
                     args.format, args.quality, args.png_level)
        for task in tasks:
            _decode_episode(*task)
            done += 1
            if done % 100 == 0 or done == len(tasks):
                print(f"[predecode]   {done}/{len(tasks)} ({time.time() - start:.0f}s)", flush=True)

    stats = cache_stats(args.output)
    print(f"[predecode] done in {time.time() - start:.0f}s: {stats['files']} files, "
          f"{stats['bytes'] / 1e9:.1f} GB -> {args.output}")

    if args.verify:
        _verify(dataset_root, args.output, args.format, views, episodes, fps, chunks_size, view_tpl, args.verify)


def _task_worker(payload):
    return _task(payload)


def _verify(dataset_root, cache_root, fmt, views, episodes, fps, chunks_size, view_tpl, count):
    """Bit-compare cached frames against the video path (meaningful for png)."""
    import numpy as np

    from ..data.frame_cache import CacheSpec, EpisodeFrameReader
    from loongforge.embodied.data.datasets.video_backends import decode_video_frame

    spec = CacheSpec.load(cache_root)
    rng = np.random.default_rng(0)
    mismatches = 0
    max_diff = 0
    for _ in range(count):
        view = views[rng.integers(len(views))]
        episode, length = episodes[rng.integers(len(episodes))]
        frame_index = int(rng.integers(length))
        chunk = episode // chunks_size
        video = Path(dataset_root) / view_tpl.format(
            episode_chunk=chunk, video_key=view, episode_index=episode
        )
        reference = decode_video_frame(str(video), frame_index / fps)  # [H,W,C] uint8
        cached = EpisodeFrameReader(cache_root, view, episode).frame(frame_index)
        if reference.shape != cached.shape or not np.array_equal(reference, cached):
            mismatches += 1
            if reference.shape == cached.shape:
                max_diff = max(max_diff, int(np.abs(reference.astype(int) - cached.astype(int)).max()))
    verdict = "BIT-IDENTICAL" if mismatches == 0 else f"{mismatches} mismatches (max|d|={max_diff})"
    print(f"[predecode] verify {count} frames ({fmt}): {verdict}")


if __name__ == "__main__":
    main()
