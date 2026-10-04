# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: examples/offline_rl/advantage_labeling/recap/process/compute_returns.py
# Reward/return semantics reproduced from the upstream script.
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

"""Discounted / undiscounted return computation for RECAP.

The baseline profile uses the standard backward recursion ``G_t = r_t + gamma * G_{t+1}``.
``paper_aligned`` sets ``use_discount=False``, which reduces to the plain suffix
sum ``G_t = sum_{k>=t} r_k`` (an episode-level return-to-go).
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

from .config import RecapConfig
from .reward import EpisodeSpec, compute_rewards


def compute_returns(
    rewards: np.ndarray, gamma: float = 0.99, use_discount: bool = True
) -> np.ndarray:
    """Backward return-to-go for a single episode.

    Args:
        rewards: per-step rewards, shape ``[T]``.
        gamma: discount factor; ignored when ``use_discount`` is False.
        use_discount: when False, returns the undiscounted suffix sum.
    """
    rewards = np.asarray(rewards, dtype=np.float32)
    out = np.zeros_like(rewards)
    running = 0.0
    discount = float(gamma) if use_discount else 1.0
    for t in range(len(rewards) - 1, -1, -1):
        running = float(rewards[t]) + discount * running
        out[t] = running
    return out


def returns_to_dataframe(
    specs: List[EpisodeSpec], cfg: RecapConfig
) -> pd.DataFrame:
    """Compute returns for every frame of every episode.

    Returns a frame with columns ``episode_index, frame_index, reward, return``
    — the sidecar written to ``meta/returns.parquet``.
    """
    reward_map = compute_rewards(specs, cfg)
    rows: List[Dict[str, float]] = []
    for spec in specs:
        rewards = reward_map[spec.episode_index]
        returns = compute_returns(
            rewards, gamma=cfg.reward.gamma, use_discount=cfg.reward.use_discount
        )
        for frame_index in range(spec.length):
            rows.append(
                {
                    "episode_index": int(spec.episode_index),
                    "frame_index": int(frame_index),
                    "reward": float(rewards[frame_index]),
                    "return": float(returns[frame_index]),
                    "prompt": spec.prompt,
                    "is_success": bool(spec.is_success),
                }
            )
    return pd.DataFrame(
        rows,
        columns=["episode_index", "frame_index", "reward", "return", "prompt", "is_success"],
    )
