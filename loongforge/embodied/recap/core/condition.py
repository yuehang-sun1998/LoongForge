# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Modified from RLinf (https://github.com/RLinf/RLinf) under the Apache-2.0 License.
# Reference: rlinf/data/datasets/recap/cfg_model.py
# The advantage prompt suffixes and base/positive/negative routing follow the upstream model.
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

"""Advantage-condition encoding for RECAP.

The advantage indicator is *not* multiplied into the loss and does not change the
action target. It selects a language condition appended to the task prompt::

    base      : <task prompt>                       (no extra clause)
    positive  : <task prompt> + "\\nAdvantage: positive"
    negative  : <task prompt> + "\\nAdvantage: negative"

Note that ``base`` still contains the original task language — it only omits the
extra advantage clause.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .config import ConditionConfig

BASE = "base"
POSITIVE = "positive"
NEGATIVE = "negative"

# Integer routing codes used across the objective / policy boundary.
CODE_BASE = -1
CODE_NEGATIVE = 0
CODE_POSITIVE = 1

_CODE_TO_LABEL = {CODE_BASE: BASE, CODE_NEGATIVE: NEGATIVE, CODE_POSITIVE: POSITIVE}


@dataclass
class AdvantageConditioner:
    """Maps a binary advantage indicator onto a prompt condition."""

    cfg: ConditionConfig

    def suffix(self, indicator: int | bool) -> str:
        """Prompt suffix for the given indicator."""
        return self.cfg.positive_suffix if indicator else self.cfg.negative_suffix

    def label(self, indicator: int | bool) -> str:
        return POSITIVE if indicator else NEGATIVE

    def apply(self, prompt: str, indicator: int | bool) -> str:
        """Append the advantage clause to a task prompt."""
        return f"{prompt}{self.suffix(indicator)}"

    def route(
        self, indicator: int | bool, rng: Optional[random.Random] = None
    ) -> Optional[str]:
        """Choose the condition for one sample.

        Returns the suffix to append, or ``None`` for the base (no extra clause)
        branch. Applies ``unconditional_prob`` (drop condition) and
        ``positive_only_conditional`` (only positive samples are conditioned).
        """
        rng = rng or random
        if rng.random() < self.cfg.unconditional_prob:
            return None
        if self.cfg.positive_only_conditional and not indicator:
            return None
        return self.suffix(indicator)

    def route_with_dropout(
        self, indicator: int | bool, rng: Optional[random.Random] = None
    ) -> Optional[str]:
        """Like :meth:`route`, but also applies the advantage-dropout probability.

        Used by the conditioned branch when ``policy.use_advantage_dropout`` is on:
        a fraction of samples fall back to the base prompt so the policy keeps its
        unconditioned behaviour.
        """
        rng = rng or random
        if rng.random() < self.cfg.advantage_dropout_prob:
            return None
        return self.route(indicator, rng=rng)


def code_to_label(code: int) -> str:
    """Human-readable label for a routing code (``-1`` -> ``base``)."""
    return _CODE_TO_LABEL[int(code)]


def route_codes(
    indicators: Iterable[int | bool],
    cfg: ConditionConfig,
    rng: Optional[random.Random] = None,
    apply_dropout: bool = False,
) -> List[int]:
    """Map advantage indicators to routing codes.

    Returns one code per sample: ``CODE_BASE`` (-1), ``CODE_NEGATIVE`` (0) or
    ``CODE_POSITIVE`` (1). ``apply_dropout`` enables ``advantage_dropout_prob``.
    """
    rng = rng or random
    conditioner = AdvantageConditioner(cfg)
    codes: List[int] = []
    for indicator in indicators:
        route = (
            conditioner.route_with_dropout(indicator, rng=rng)
            if apply_dropout
            else conditioner.route(indicator, rng=rng)
        )
        if route is None:
            codes.append(CODE_BASE)
        else:
            codes.append(CODE_POSITIVE if indicator else CODE_NEGATIVE)
    return codes
