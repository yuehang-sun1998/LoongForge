# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""RECAP value critic (ported from RLinf).

Public entry points: :class:`ValueCriticModel`, :class:`ValueCriticConfig`,
:func:`build_value_critic`, :class:`ValueObservationBuilder`.
"""

from .config import DATASET_IMAGE_MAP, IMAGE_KEYS, ExpertConfig, ValueCriticConfig, get_expert_config
from .critic import (
    CriticOutput,
    ValueCriticModel,
    ValueHead,
    build_value_critic,
    load_critic,
    make_att_2d_masks,
    save_critic,
)
from .processing import ValueObservationBuilder, preprocess_image
from .training_config import RecapValueDataConfig, RecapValueModelConfig

__all__ = [
    "ValueCriticConfig",
    "RecapValueModelConfig",
    "RecapValueDataConfig",
    "ExpertConfig",
    "get_expert_config",
    "IMAGE_KEYS",
    "DATASET_IMAGE_MAP",
    "ValueCriticModel",
    "ValueHead",
    "CriticOutput",
    "build_value_critic",
    "save_critic",
    "load_critic",
    "make_att_2d_masks",
    "ValueObservationBuilder",
    "preprocess_image",
]
