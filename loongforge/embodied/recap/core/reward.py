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

"""Per-step reward construction for RECAP.

The reward is defined at the *episode* level: every non-final step gets the
intermediate reward, the final step gets either the success or the failure
reward. This matches the paper definition::

    r_t = 0            (last step, success)
    r_t = -C_fail      (last step, failure)
    r_t = -1           (every other step)

``C_fail`` is a free constant; the ``baseline`` profile uses -300 and the
``paper_aligned`` profile uses -1. Both are produced by
:func:`compute_episode_reward`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np

from .config import RecapConfig, RewardConfig


@dataclass
class EpisodeSpec:
    """Minimal per-episode metadata needed for reward/return computation."""

    episode_index: int
    length: int
    is_success: bool
    task_index: int = 0
    prompt: str = "perform the task"


def compute_episode_reward(spec: EpisodeSpec, cfg: RewardConfig) -> np.ndarray:
    """Return the per-step reward vector for one episode (shape ``[length]``)."""
    if spec.length <= 0:
        return np.zeros(0, dtype=np.float32)

    rewards = np.full(spec.length, float(cfg.step_reward), dtype=np.float32)
    terminal = float(cfg.success_reward if spec.is_success else cfg.failure_reward)
    rewards[-1] = terminal
    return rewards


def compute_rewards(
    specs: List[EpisodeSpec], cfg: RecapConfig
) -> Dict[int, np.ndarray]:
    """Compute per-step rewards for a list of episodes, keyed by episode index."""
    return {spec.episode_index: compute_episode_reward(spec, cfg.reward) for spec in specs}


def success_rate(specs: List[EpisodeSpec]) -> float:
    """Fraction of successful episodes (handy for smoke assertions / logging)."""
    if not specs:
        return 0.0
    return sum(1.0 for spec in specs if spec.is_success) / len(specs)
