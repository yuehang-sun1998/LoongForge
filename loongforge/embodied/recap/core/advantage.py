# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: examples/offline_rl/advantage_labeling/recap/process/compute_advantages.py
# Where this file and RLinf disagree, RLinf is authoritative.
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

"""N-step advantage estimation and binarisation for RECAP.

Mirrors the upstream implementation exactly (``compute_advantages.py``):

    num_valid     = min(N, episode_end - t)
    v_next        = V(o_{t+N}) if t+N < episode_end else 0.0
    reward_sum_raw= G_t - G_{t+N}                 (gamma == 1, else sum of gamma^i r_{t+i})
    reward_sum    = normalize(reward_sum_raw)     (global affine map onto [-1, 0])
    gamma_k       = gamma ** num_valid if discount_next_value else 1.0
    advantage     = reward_sum + gamma_k * v_next - V(o_t)

Key differences from a naive reading (all confirmed against the upstream source):

* the **reward sum** is normalised, not the advantage, and the map is a global
  affine ``(x - ret_min) / (ret_max - ret_min) - 1`` onto ``[-1, 0]``;
* the bootstrap exponent is ``num_valid`` (truncated near the episode end), not N;
* the binarisation threshold is the **(1 - q) percentile over the concatenated
  advantages of all datasets**, applied **inclusively** (``>=``);
* ``sft`` datasets bypass the threshold entirely -- every frame is positive.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import RecapConfig
from .reward import EpisodeSpec, compute_rewards
from .returns import compute_returns


def compute_advantage_episode(
    returns: np.ndarray,
    rewards: np.ndarray,
    values: np.ndarray,
    horizon: int,
    gamma: float,
    use_discount: bool,
    normalize: Callable[[float], float],
) -> Dict[str, np.ndarray]:
    """Per-frame advantage terms for one episode (upstream formula)."""
    length = len(rewards)
    advantage = np.zeros(length, dtype=np.float64)
    reward_sum_raw = np.zeros(length, dtype=np.float64)
    value_next = np.zeros(length, dtype=np.float64)
    num_valid = np.zeros(length, dtype=np.int64)
    undiscounted = abs(float(gamma) - 1.0) < 1e-8

    for t in range(length):
        next_index = t + horizon
        padded = next_index >= length
        valid = min(horizon, length - t)
        num_valid[t] = valid
        v_next = 0.0 if padded else float(values[next_index])
        value_next[t] = v_next

        if undiscounted:
            # G_t - G_{t+N}; when the window crosses the end, G_t already
            # contains the terminal reward, so it is used as-is.
            raw = float(returns[t]) if padded else float(returns[t] - returns[next_index])
        else:
            raw = float(np.sum([gamma**i * rewards[t + i] for i in range(valid)]))
        reward_sum_raw[t] = raw

        gamma_k = (float(gamma) ** valid) if use_discount else 1.0
        advantage[t] = normalize(raw) + gamma_k * v_next - float(values[t])
    return {
        "advantage": advantage,
        "reward_sum_raw": reward_sum_raw,
        "reward_sum": np.array([normalize(x) for x in reward_sum_raw]),
        "value_next": value_next,
        "num_valid_rewards": num_valid,
    }


def make_return_normalizer(ret_min: float, ret_max: float) -> Callable[[float], float]:
    """Global affine map ``[ret_min, ret_max] -> [-1, 0]`` (upstream `normalize`)."""
    span = float(ret_max) - float(ret_min)
    if span <= 0:
        return lambda x: -0.5
    return lambda x: (float(x) - float(ret_min)) / span - 1.0


def quantile_threshold(advantages: Sequence[float], positive_quantile: float) -> float:
    """(1 - q) percentile over the concatenated advantages (inclusive `>=`)."""
    array = np.asarray(list(advantages), dtype=np.float64)
    if array.size == 0:
        return 0.0
    return float(np.percentile(array, (1.0 - float(positive_quantile)) * 100.0))


def advantages_to_dataframe(
    specs: List[EpisodeSpec],
    value_fn: Callable[[int, int], float],
    cfg: RecapConfig,
    dataset_type: str = "rollout",
    return_range: Optional[Tuple[float, float]] = None,
) -> pd.DataFrame:
    """Compute per-frame advantages with the upstream semantics.

    Args:
        specs: episode metadata (index / length / success / task).
        value_fn: ``(episode_index, frame_index) -> V(o_t)`` (values in [-1, 0]
            for the upstream critic; the placeholder is unnormalised).
        cfg: full RECAP config (advantage section drives N / gamma / quantile).
        dataset_type: ``"sft"`` forces every frame positive (upstream behaviour).
        return_range: ``(ret_min, ret_max)`` for the global normaliser; when
            omitted it is taken from the returns of this dataset.
    """
    adv_cfg = cfg.advantage
    reward_map = compute_rewards(specs, cfg)

    per_episode: List[Dict[str, object]] = []
    for spec in specs:
        rewards = reward_map[spec.episode_index]
        returns = compute_returns(rewards, gamma=adv_cfg.gamma, use_discount=adv_cfg.use_discount)
        values = np.asarray(
            [value_fn(spec.episode_index, frame) for frame in range(spec.length)], dtype=np.float64
        )
        per_episode.append({"spec": spec, "rewards": rewards, "returns": returns, "values": values})

    if return_range is None:
        flat_returns = np.concatenate([e["returns"] for e in per_episode]) if per_episode else np.zeros(0)
        return_range = (
            (float(flat_returns.min()), float(flat_returns.max())) if flat_returns.size else (0.0, 0.0)
        )
    normalize = make_return_normalizer(*return_range)

    for entry in per_episode:
        entry["terms"] = compute_advantage_episode(
            entry["returns"], entry["rewards"], entry["values"],
            horizon=adv_cfg.horizon, gamma=adv_cfg.gamma,
            use_discount=adv_cfg.use_discount, normalize=normalize,
        )

    all_advantages = [entry["terms"]["advantage"] for entry in per_episode]
    flat_advantage = np.concatenate(all_advantages) if all_advantages else np.zeros(0)
    global_threshold = quantile_threshold(flat_advantage, adv_cfg.positive_quantile)
    task_thresholds: Dict[int, float] = {}
    if adv_cfg.task_dependent:
        flat_task = np.concatenate(
            [np.full(entry["spec"].length, entry["spec"].task_index) for entry in per_episode]
        ) if per_episode else np.zeros(0)
        for task in np.unique(flat_task):
            mask = flat_task == task
            task_thresholds[int(task)] = quantile_threshold(flat_advantage[mask], adv_cfg.positive_quantile)
    is_sft = str(dataset_type).lower() == "sft"

    rows: List[Dict[str, object]] = []
    for entry in per_episode:
        spec: EpisodeSpec = entry["spec"]
        terms = entry["terms"]
        for frame in range(spec.length):
            threshold = task_thresholds.get(spec.task_index, global_threshold) if adv_cfg.task_dependent else global_threshold
            positive = True if is_sft else bool(terms["advantage"][frame] >= threshold)
            rows.append(
                {
                    "episode_index": int(spec.episode_index),
                    "frame_index": int(frame),
                    "advantage_continuous": float(terms["advantage"][frame]),
                    "return": float(entry["returns"][frame]),
                    "value_current": float(entry["values"][frame]),
                    "value_next": float(terms["value_next"][frame]),
                    "reward_sum": float(terms["reward_sum"][frame]),
                    "reward_sum_raw": float(terms["reward_sum_raw"][frame]),
                    "num_valid_rewards": int(terms["num_valid_rewards"][frame]),
                    "advantage": bool(positive),
                    "threshold": float(threshold),
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "episode_index", "frame_index", "advantage_continuous", "return",
            "value_current", "value_next", "reward_sum", "reward_sum_raw",
            "num_valid_rewards", "advantage", "threshold",
        ],
    )
