# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Generate a tiny mock LeRobot v2 rollout dataset for smoke tests.

The generated dataset is a *valid* LeRobot v2.0 dataset (parquet + jsonl meta) that
:class:`~loongforge.embodied.recap.dataset.RecapLeRobotV2Dataset` can read without
any external artifacts. Images are stored inline as uint8 ``(H, W, C)`` parquet
cells (the reader already supports this path), so no video files are needed.

Success is deterministic (``episode_index % 2 == 0``) so that returns, value
targets and advantage indicators are non-degenerate and smoke assertions are
stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

TASKS = [
    "pick up the black bowl and place it on the plate",
    "open the top drawer and put the bowl inside",
    "push the plate to the front of the stove",
    "put the wine bottle on the rack",
]


def _task_color(task_index: int) -> np.ndarray:
    """Distinct base colour per task so the value model has learnable signal."""
    palette = np.array(
        [[200, 60, 60], [60, 200, 60], [60, 60, 200], [200, 200, 60]], dtype=np.float32
    )
    return palette[task_index % len(palette)]


def _make_image(task_index: int, frame: int, length: int, size: int, rng: np.random.Generator) -> np.ndarray:
    """Simple synthetic image: task colour + progress gradient + noise."""
    base = _task_color(task_index)
    progress = frame / max(1, length - 1)
    img = np.zeros((size, size, 3), dtype=np.float32)
    img[:] = base
    img[:, :, :] *= (0.4 + 0.6 * (1.0 - progress))
    img += rng.normal(0.0, 8.0, size=img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _make_episode(
    episode_index: int,
    task_index: int,
    length: int,
    state_dim: int,
    action_dim: int,
    image_size: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    success = episode_index % 2 == 0
    states = rng.normal(loc=task_index * 0.5, scale=1.0, size=(length, state_dim)).astype(np.float32)
    actions = rng.normal(loc=0.0, scale=0.5, size=(length, action_dim)).astype(np.float32)
    if success:
        # Successful episodes converge the last state toward a task-specific target.
        states[-1] = task_index * 0.5

    images = [_make_image(task_index, t, length, image_size, rng) for t in range(length)]
    wrist = [_make_image(task_index, t, length, image_size, rng) for t in range(length)]

    frame = pd.DataFrame(
        {
            "observation.state": [row.tolist() for row in states],
            "action": [row.tolist() for row in actions],
            "observation.images.image": [img.tolist() for img in images],
            "observation.images.wrist_image": [img.tolist() for img in wrist],
            "task_index": np.full(length, task_index, dtype=np.int64),
            "episode_index": np.full(length, episode_index, dtype=np.int64),
            "frame_index": np.arange(length, dtype=np.int64),
            "index": np.arange(length, dtype=np.int64),
            "timestamp": (np.arange(length, dtype=np.float32) / 10.0),
            "is_success": np.full(length, success, dtype=bool),
        }
    )
    return frame


def _write_stats(root: Path, state_dim: int, action_dim: int) -> None:
    def stats_for(dim: int) -> Dict[str, list]:
        zeros = [0.0] * dim
        return {
            "min": zeros,
            "max": zeros,
            "mean": zeros,
            "std": [1.0] * dim,
            "q01": zeros,
            "q99": zeros,
            "count": [1],
        }

    stats = {
        "observation.state": stats_for(state_dim),
        "action": stats_for(action_dim),
    }
    (root / "meta" / "stats.json").write_text(json.dumps(stats))


def make_mock_rollout_dataset(
    root: str | Path,
    num_tasks: int = 2,
    episodes_per_task: int = 2,
    episode_length: int = 8,
    state_dim: int = 7,
    action_dim: int = 7,
    image_size: int = 32,
    fps: int = 10,
    seed: int = 0,
) -> Path:
    """Write a mock LeRobot v2 rollout dataset and return its root path."""
    root = Path(root)
    (root / "meta").mkdir(parents=True, exist_ok=True)
    (root / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    episodes: List[Dict] = []
    total_frames = 0
    episode_index = 0
    for task_index in range(num_tasks):
        for _ in range(episodes_per_task):
            frame = _make_episode(
                episode_index, task_index, episode_length, state_dim, action_dim, image_size, rng
            )
            out = root / "data" / "chunk-000" / f"episode_{episode_index:06d}.parquet"
            frame.to_parquet(out, index=False)
            episodes.append(
                {
                    "episode_index": episode_index,
                    "length": episode_length,
                    "task_index": task_index,
                    "tasks": [TASKS[task_index % len(TASKS)]],
                    "is_success": bool(episode_index % 2 == 0),
                }
            )
            total_frames += episode_length
            episode_index += 1

    with open(root / "meta" / "episodes.jsonl", "w") as handle:
        for episode in episodes:
            handle.write(json.dumps(episode) + "\n")

    with open(root / "meta" / "tasks.jsonl", "w") as handle:
        for task_index in range(num_tasks):
            handle.write(
                json.dumps({"task_index": task_index, "task": TASKS[task_index % len(TASKS)]}) + "\n"
            )

    info = {
        "codebase_version": "v2.0",
        "robot_type": "mock_franka",
        "total_episodes": len(episodes),
        "total_frames": total_frames,
        "fps": fps,
        "chunks_size": 1000,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.images.image": {
                "dtype": "video",
                "shape": [3, image_size, image_size],
                "names": ["channels", "height", "width"],
            },
            "observation.images.wrist_image": {
                "dtype": "video",
                "shape": [3, image_size, image_size],
                "names": ["channels", "height", "width"],
            },
            "observation.state": {"dtype": "float32", "shape": [state_dim], "names": None},
            "action": {"dtype": "float32", "shape": [action_dim], "names": None},
        },
    }
    (root / "meta" / "info.json").write_text(json.dumps(info, indent=2))
    _write_stats(root, state_dim, action_dim)
    return root
