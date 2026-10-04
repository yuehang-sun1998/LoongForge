# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: examples/offline_rl/config/{recap_compute_returns,recap_compute_advantages,cfg_rl_openpi}.yaml
# Profile default values mirror the upstream recipe configs.
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

"""RECAP (advantage-conditioned offline RL) — config system.

Stage 1 of the RECAP roadmap: reuse the stock pi0.5 SFT pipeline
and bolt an offline-RL data pipeline on top of an already-collected LeRobot
rollout dataset::

    rollout LeRobot dataset
        -> compute_returns      (reward + return sidecar)
        -> train_value          (state value model)
        -> compute_advantages   (N-step advantage + binary indicator sidecar)
        -> CFG policy training  (advantage-conditioned flow matching)

Two reward/return profiles are kept distinct on purpose:

* ``baseline``       — the recipe values (step=-1 / success=0 / failure=-300,
  gamma=1.0), see the RECAP training recipe.
* ``paper_aligned``  — paper reward (success=0 / failure=-C_fail / step=-1) and
  **no discount**.

The ``ablation`` profile is a copy of ``paper_aligned`` with individual features
switchable, so an experiment can disable exactly one ingredient at a time.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

import yaml

logger = logging.getLogger(__name__)

MODE_BASELINE = "baseline"
MODE_PAPER_ALIGNED = "paper_aligned"
MODE_ABLATION = "ablation"

_MODES = (MODE_BASELINE, MODE_PAPER_ALIGNED, MODE_ABLATION)

_SectionT = TypeVar("_SectionT")


def _section(cls: Type[_SectionT], raw: Optional[Dict[str, Any]], name: str) -> _SectionT:
    """Build a sub-config, dropping keys the dataclass no longer declares.

    ``<dataset>/meta/recap_config.yaml`` is a machine-written traceability
    artifact that users also pass back via ``--config``. Tolerating (with a
    warning) keys that were removed or never existed keeps old configs loadable
    instead of turning a stale file into a hard crash.
    """
    raw = dict(raw or {})
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        logger.warning(
            "Ignoring unknown %s config keys: %s (known: %s)",
            name, sorted(unknown), sorted(known),
        )
        raw = {key: value for key, value in raw.items() if key in known}
    return cls(**raw)


@dataclass
class RewardConfig:
    """Per-step reward definition and discount policy.

    All three rewards are free parameters. Defaults are the documented recipe
    values (the RECAP training recipe: step=-1 / success=0 /
    ``failure_reward``=-300). The paper leaves ``C_fail``
    symbolic, so ``paper_aligned`` picks its own value explicitly.
    """

    mode: str = MODE_BASELINE
    # Terminal step reward.
    success_reward: float = 0.0
    failure_reward: float = -300.0
    # Reward on every non-terminal step.
    step_reward: float = -1.0
    use_discount: bool = True
    gamma: float = 0.99

    def validate(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"reward.mode must be one of {_MODES}, got {self.mode!r}")
        if self.use_discount and not (0.0 < self.gamma <= 1.0):
            raise ValueError(f"reward.gamma must be in (0, 1], got {self.gamma}")


@dataclass
class ValueConfig:
    """State-value model config.

    Stage 1 uses a small MLP/CNN value head (``distributional=False``). Stage 3 of
    the roadmap flips ``distributional=True`` and keeps the same
    ``predict_expected_value`` interface for the downstream advantage step.
    """

    backend: str = "mlp"  # "mlp" (state only) | "cnn_mlp" (state + image)
    hidden_dim: int = 256
    num_layers: int = 2
    use_image: bool = False
    image_size: int = 32
    # Distributional value (paper §6). Off in stage 1.
    distributional: bool = False
    num_bins: int = 201
    value_min: float = -100.0
    value_max: float = 0.0
    # Training knobs for the standalone value trainer.
    lr: float = 1e-3
    batch_size: int = 64
    epochs: int = 5
    device: str = "cpu"
    seed: int = 0

    def validate(self) -> None:
        if self.backend not in ("mlp", "cnn_mlp"):
            raise ValueError(f"value.backend must be 'mlp' or 'cnn_mlp', got {self.backend!r}")
        if self.distributional and self.num_bins < 2:
            raise ValueError("value.num_bins must be >= 2 for distributional value")
        if self.distributional and not (self.value_min < self.value_max):
            raise ValueError("value.value_min must be < value.value_max")


@dataclass
class AdvantageConfig:
    """N-step advantage estimation + binarisation config.

    ``positive_quantile`` uses the top-q convention: the top ``q`` fraction of
    advantages is marked positive, i.e. the threshold is the ``1 - q`` quantile.

    Binarisation is always the quantile rule, matching RLinf — there is no
    "threshold at zero" switch, and the reward-sum normalisation is a fixed part
    of the estimator (see :mod:`loongforge.embodied.recap.core.advantage`), so
    neither is exposed here.
    """

    mode: str = MODE_BASELINE
    horizon: int = 10
    use_discount: bool = True
    gamma: float = 0.99
    positive_quantile: float = 0.3
    task_dependent: bool = False

    def validate(self) -> None:
        if self.horizon < 1:
            raise ValueError(f"advantage.horizon must be >= 1, got {self.horizon}")
        if not (0.0 <= self.positive_quantile <= 1.0):
            raise ValueError(
                f"advantage.positive_quantile must be in [0, 1], got {self.positive_quantile}"
            )


@dataclass
class ConditionConfig:
    """How the advantage indicator is injected as a policy condition.

    The condition is a language suffix appended to the task prompt; the
    base branch keeps the raw prompt and only omits the extra advantage clause.
    """

    positive_suffix: str = "\nAdvantage: positive"
    negative_suffix: str = "\nAdvantage: negative"
    advantage_dropout_prob: float = 0.0
    positive_only_conditional: bool = False
    unconditional_prob: float = 0.0

    def validate(self) -> None:
        for name in ("advantage_dropout_prob", "unconditional_prob"):
            value = getattr(self, name)
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"condition.{name} must be in [0, 1], got {value}")


@dataclass
class PolicyConfig:
    """Advantage-conditioned policy objective config."""

    alpha: float = 1.0
    use_base_branch: bool = False
    use_advantage_dropout: bool = False

    def validate(self) -> None:
        if self.alpha < 0:
            raise ValueError(f"policy.alpha must be >= 0, got {self.alpha}")


@dataclass
class RecapConfig:
    """Top-level RECAP config, expanded to a fully-resolved dict for logging."""

    mode: str = MODE_BASELINE
    reward: RewardConfig = field(default_factory=RewardConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    advantage: AdvantageConfig = field(default_factory=AdvantageConfig)
    condition: ConditionConfig = field(default_factory=ConditionConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    # ── construction ──────────────────────────────────────────────
    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "RecapConfig":
        raw = dict(raw or {})
        mode = raw.pop("mode", MODE_BASELINE)
        cfg = cls(
            mode=mode,
            reward=_section(RewardConfig, raw.pop("reward", {}), "reward"),
            value=_section(ValueConfig, raw.pop("value", {}), "value"),
            advantage=_section(AdvantageConfig, raw.pop("advantage", {}), "advantage"),
            condition=_section(ConditionConfig, raw.pop("condition", {}), "condition"),
            policy=_section(PolicyConfig, raw.pop("policy", {}), "policy"),
        )
        if raw:
            raise ValueError(f"Unknown recap config keys: {sorted(raw)}")
        # Propagate the top-level mode into the sub-configs that branch on it.
        cfg.reward.mode = cfg.mode
        cfg.advantage.mode = cfg.mode
        cfg.validate()
        return cfg

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RecapConfig":
        path = Path(path)
        with open(path) as handle:
            raw = yaml.safe_load(handle) or {}
        # Allow both a flat recap block and a file that *is* the recap block.
        raw = raw.get("recap", raw)
        return cls.from_dict(raw)

    @classmethod
    def preset(cls, name: str) -> "RecapConfig":
        """Return one of the three canonical profiles."""
        if name == MODE_BASELINE:
            # Values taken from the RECAP training recipe: step=-1 / success=0 / failure=-300,
            # gamma=1.0 (no discount), N=10, top-30% positive, positive-only
            # conditional with unconditional_prob=0.1.
            return cls.from_dict(
                {
                    "mode": MODE_BASELINE,
                    "reward": {"mode": MODE_BASELINE, "success_reward": 0.0,
                               "failure_reward": -300.0, "step_reward": -1.0,
                               "use_discount": False, "gamma": 1.0},
                    "advantage": {"mode": MODE_BASELINE, "horizon": 10, "use_discount": False,
                                  "gamma": 1.0,
                                  "positive_quantile": 0.3, "task_dependent": False},
                    "condition": {"positive_only_conditional": True,
                                  "unconditional_prob": 0.1},
                    "policy": {"use_base_branch": False, "use_advantage_dropout": False},
                }
            )
        if name == MODE_PAPER_ALIGNED:
            return cls.from_dict(
                {
                    "mode": MODE_PAPER_ALIGNED,
                    "reward": {"mode": MODE_PAPER_ALIGNED, "success_reward": 0.0,
                               "failure_reward": -1.0, "step_reward": -1.0,
                               "use_discount": False, "gamma": 1.0},
                    "value": {"distributional": True, "num_bins": 201},
                    "advantage": {"mode": MODE_PAPER_ALIGNED, "horizon": 50, "use_discount": False,
                                  "gamma": 1.0,
                                  "positive_quantile": 0.5, "task_dependent": True},
                    "condition": {"advantage_dropout_prob": 0.1},
                    "policy": {"use_base_branch": True, "use_advantage_dropout": True},
                }
            )
        if name == MODE_ABLATION:
            cfg = cls.preset(MODE_PAPER_ALIGNED)
            cfg.mode = MODE_ABLATION
            return cfg
        raise ValueError(f"Unknown recap preset {name!r}; expected one of {_MODES}")

    # ── serialization / validation ────────────────────────────────
    def validate(self) -> None:
        if self.mode not in _MODES:
            raise ValueError(f"recap.mode must be one of {_MODES}, got {self.mode!r}")
        self.reward.validate()
        self.value.validate()
        self.advantage.validate()
        self.condition.validate()
        self.policy.validate()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def resolved_yaml(self) -> str:
        """Fully-expanded config (never log only the profile name)."""
        return yaml.safe_dump(self.to_dict(), sort_keys=False)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.resolved_yaml())
