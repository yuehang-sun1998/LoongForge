# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: examples/offline_rl/advantage_labeling/recap/process/compute_returns.py
# is_success / prompt / dataset-type resolution follows the upstream script.
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

"""Shared helpers for the RECAP CLI scripts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

import pandas as pd

from ..core.config import MODE_BASELINE, RecapConfig
from ..core.reward import EpisodeSpec
from ..data.dataset import episode_subset  # noqa: F401  (re-exported for the CLI modules)


def add_config_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared --config / --preset / reward-override flags."""
    parser.add_argument("--config", default=None, help="Path to a RECAP yaml config.")
    parser.add_argument(
        "--preset",
        default=None,
        choices=["baseline", "paper_aligned", "ablation"],
        help="Named profile to use when --config is not given.",
    )
    parser.add_argument("--success-reward", type=float, default=None,
                        help="Terminal reward on success (overrides the profile).")
    parser.add_argument("--failure-reward", type=float, default=None,
                        help="Terminal reward on failure (overrides the profile).")
    parser.add_argument("--step-reward", type=float, default=None,
                        help="Reward on every non-terminal step (overrides the profile).")


def load_config(args: argparse.Namespace) -> RecapConfig:
    """Resolve a RecapConfig from --config / --preset, then apply CLI overrides."""
    if getattr(args, "config", None):
        cfg = RecapConfig.from_yaml(args.config)
    else:
        cfg = RecapConfig.preset(getattr(args, "preset", None) or MODE_BASELINE)

    for name, target in (
        ("gamma", cfg.reward),
        ("success_reward", cfg.reward),
        ("failure_reward", cfg.reward),
        ("step_reward", cfg.reward),
        ("horizon", cfg.advantage),
        ("positive_quantile", cfg.advantage),
    ):
        value = getattr(args, name, None)
        if value is not None and hasattr(target, name):
            setattr(target, name, value)
    if getattr(args, "no_discount", False):
        cfg.reward.use_discount = False
        cfg.advantage.use_discount = False
    cfg.validate()
    return cfg


def read_episode_specs(
    root: str | Path,
    max_episodes: Optional[int] = None,
    dataset_type: str = "rollout",
) -> List[EpisodeSpec]:
    """Read episode specs from a LeRobot v2 dataset without loading any samples.

    Mirrors the upstream semantics: ``is_success`` comes from the **last frame's
    ``is_success`` column** of the episode parquet (jsonl is only a fallback), and
    ``sft`` datasets are forced to success. The prompt is taken from the parquet
    ``prompt`` column when present, else resolved via ``meta/tasks.jsonl``.
    ``max_episodes`` limits the result to the first N episodes.
    """
    root = Path(root)
    episodes_path = root / "meta" / "episodes.jsonl"
    if not episodes_path.exists():
        raise FileNotFoundError(f"missing {episodes_path}")

    entries = []
    with open(episodes_path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    if max_episodes is not None:
        entries = entries[:max_episodes]

    tasks: dict = {}
    tasks_path = root / "meta" / "tasks.jsonl"
    if tasks_path.exists():
        with open(tasks_path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    obj = json.loads(line)
                    tasks[int(obj["task_index"])] = obj["task"]

    is_sft = str(dataset_type).lower() == "sft"
    specs: List[EpisodeSpec] = []
    for entry in entries:
        episode_index = int(entry["episode_index"])
        length = int(entry["length"])
        parquet = _episode_parquet(root, episode_index, int(entry.get("chunk", episode_index // 1000)))
        frame = pd.read_parquet(parquet) if parquet.exists() else None

        success = entry.get("is_success")
        if frame is not None and "is_success" in frame.columns:
            success = bool(frame.iloc[-1]["is_success"])
        success = bool(success) if success is not None else False
        if is_sft:
            success = True

        task_index = entry.get("task_index")
        if frame is not None and "task_index" in frame.columns:
            task_index = int(frame.iloc[0]["task_index"])
        task_index = int(task_index) if task_index is not None else 0

        prompt = None
        if frame is not None and "prompt" in frame.columns:
            prompt = str(frame.iloc[0]["prompt"])
        if not prompt:
            prompt = tasks.get(task_index, "perform the task")

        specs.append(
            EpisodeSpec(
                episode_index=episode_index,
                length=length,
                is_success=success,
                task_index=task_index,
                prompt=prompt,
            )
        )
    return specs


def _episode_parquet(root: Path, episode_index: int, chunk: Optional[int] = None) -> Path:
    chunk = episode_index // 1000 if chunk is None else chunk
    return root / "data" / f"chunk-{chunk:03d}" / f"episode_{episode_index:06d}.parquet"


def add_subset_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared --max-episodes quick-validation flag."""
    parser.add_argument(
        "--max-episodes",
        type=int,
        default=None,
        help="Limit the pipeline to the first N episodes (quick validation).",
    )
    parser.add_argument(
        "--dataset-type",
        default="rollout",
        choices=["rollout", "sft"],
        help="rollout: is_success from the data; sft: every episode forced successful "
        "and every frame forced positive (upstream behaviour).",
    )


def add_dataset_compat_args(parser: argparse.ArgumentParser) -> None:
    """Add the shared --dataset-compat / --video-cache-size flags."""
    parser.add_argument(
        "--dataset-compat",
        default="auto",
        choices=["auto", "none", "legacy"],
        help="Column-naming compatibility shim for external rollout datasets: "
        "auto (detect), none (canonical layout), legacy (force pi0.5-style names).",
    )
    parser.add_argument(
        "--video-cache-size",
        type=int,
        default=1,
        help="Episode-level decoded-frame cache size (LRU over episodes). 0 disables "
        "it and falls back to the stock per-frame decode path.",
    )
    parser.add_argument(
        "--frame-cache-root",
        default=None,
        help="Offline frame cache produced by "
        "`python -m loongforge.embodied.recap.cli.predecode`; when set, frames are "
        "read from it instead of the videos.",
    )
    parser.add_argument(
        "--frame-reader-cache-size",
        type=int,
        default=16,
        help="LRU size (in (view, episode) entries) for offline frame-cache readers. "
        "It should cover every (view, episode) pair in the working subset, otherwise "
        "the readers thrash; each entry holds one episode's encoded frames in memory.",
    )
