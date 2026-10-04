# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RECAP — advantage-conditioned offline RL on top of the pi0.5 SFT pipeline.

Layout:

* ``core/``   — pure logic (config, reward, returns, advantage, condition, objective);
* ``data/``   — dataset readers, sidecars, frame cache, compatibility, manifest;
* ``value_model/`` — the ported RLinf value critic;
* ``cli/``    — command-line entry points (``python -m loongforge.embodied.recap.cli.<name>``);
* ``testing/``— mock dataset generator and stand-in policy.

Heavy dependencies (lerobot-backed readers, the value critic) are imported lazily
by the modules that need them, so the pure-logic core stays importable on its own.
"""

from .core.advantage import (
    advantages_to_dataframe,
    compute_advantage_episode,
    make_return_normalizer,
    quantile_threshold,
)
from .core.condition import (
    CODE_BASE,
    CODE_NEGATIVE,
    CODE_POSITIVE,
    AdvantageConditioner,
    route_codes,
)
from .core.config import (
    MODE_ABLATION,
    MODE_BASELINE,
    MODE_PAPER_ALIGNED,
    AdvantageConfig,
    ConditionConfig,
    PolicyConfig,
    RecapConfig,
    RewardConfig,
    ValueConfig,
)
from .core.policy_objective import recap_cfg_loss
from .core.returns import compute_returns, returns_to_dataframe
from .core.reward import EpisodeSpec, compute_episode_reward
from .data.manifest import advantages_tag, build_tag, read_manifest, returns_tag
from .data.placeholder_value import (
    RecapValueModel,
    load_value_checkpoint,
    predict_values,
    save_value_checkpoint,
    value_loss,
)
from .data.sidecar import (
    SIDECAR_ADVANTAGES,
    SIDECAR_RETURNS,
    read_sidecar,
    sidecar_path,
    write_sidecar,
)

__all__ = [
    "RecapConfig",
    "RewardConfig",
    "ValueConfig",
    "AdvantageConfig",
    "ConditionConfig",
    "PolicyConfig",
    "MODE_BASELINE",
    "MODE_PAPER_ALIGNED",
    "MODE_ABLATION",
    "EpisodeSpec",
    "compute_episode_reward",
    "compute_returns",
    "returns_to_dataframe",
    "compute_advantage_episode",
    "advantages_to_dataframe",
    "make_return_normalizer",
    "quantile_threshold",
    "AdvantageConditioner",
    "route_codes",
    "CODE_BASE",
    "CODE_NEGATIVE",
    "CODE_POSITIVE",
    "recap_cfg_loss",
    "RecapValueModel",
    "value_loss",
    "predict_values",
    "save_value_checkpoint",
    "load_value_checkpoint",
    "SIDECAR_RETURNS",
    "SIDECAR_ADVANTAGES",
    "sidecar_path",
    "read_sidecar",
    "write_sidecar",
    "advantages_tag",
    "returns_tag",
    "build_tag",
    "read_manifest",
]
