# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""A tiny stand-in policy used to smoke-test the RECAP objective.

The real pi0.5 CFG training needs a pi0.5 checkpoint + PaliGemma tokenizer, which a
CPU smoke test does not have. ``MockConditionedPolicy`` implements the same
``compute_action_loss(batch, advantage_condition)`` contract as the pi0.5 wrapper,
so :func:`~loongforge.embodied.recap.policy_objective.recap_cfg_loss` and the whole
base/positive/negative routing path can be exercised end to end without artifacts.
"""

from __future__ import annotations

import random
from typing import Dict, List, Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..core.condition import CODE_NEGATIVE, CODE_POSITIVE
from ..core.config import RecapConfig
from ..core.policy_objective import recap_cfg_loss


class TinyTokenizer:
    """Deterministic char-level tokenizer (no external artifacts)."""

    def __init__(self, vocab_size: int = 128, max_length: int = 64):
        self.vocab_size = vocab_size
        self.max_length = max_length

    def __call__(self, text: str) -> List[int]:
        ids = [0] * self.max_length
        for position, char in enumerate(text[: self.max_length]):
            ids[position] = (ord(char) % (self.vocab_size - 1)) + 1
        return ids


class MockConditionedPolicy(nn.Module):
    """Predicts a single action from pooled prompt tokens + the action itself."""

    def __init__(
        self,
        vocab_size: int = 128,
        token_dim: int = 32,
        action_dim: int = 7,
        hidden: int = 64,
    ):
        super().__init__()
        self.embed = nn.Embedding(vocab_size, token_dim, padding_idx=0)
        self.action_proj = nn.Linear(action_dim, token_dim)
        self.head = nn.Linear(token_dim * 2, action_dim)

    def _pool(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        mask = attention_mask.unsqueeze(-1).float()
        summed = (self.embed(input_ids) * mask).sum(dim=1)
        return summed / mask.sum(dim=1).clamp(min=1.0)

    def forward(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        actions: torch.Tensor,
    ) -> torch.Tensor:
        pooled = self._pool(input_ids, attention_mask)
        return self.head(torch.cat([pooled, self.action_proj(actions)], dim=-1))

    def _select(
        self, batch: Dict[str, torch.Tensor], advantage_condition
    ) -> tuple[torch.Tensor, torch.Tensor]:
        base_ids = batch["input_ids_base"]
        base_mask = batch["attention_mask_base"]
        if advantage_condition is None:
            return base_ids, base_mask

        codes = advantage_condition
        if not isinstance(codes, torch.Tensor):
            codes = torch.tensor(list(codes), dtype=torch.long)
        codes = codes.to(base_ids.device)

        input_ids = base_ids.clone()
        attention_mask = base_mask.clone()
        positive = codes == CODE_POSITIVE
        negative = codes == CODE_NEGATIVE
        if positive.any():
            input_ids[positive] = batch["input_ids_pos"][positive]
            attention_mask[positive] = batch["attention_mask_pos"][positive]
        if negative.any():
            input_ids[negative] = batch["input_ids_neg"][negative]
            attention_mask[negative] = batch["attention_mask_neg"][negative]
        return input_ids, attention_mask

    def compute_action_loss(self, batch, advantage_condition=None) -> torch.Tensor:
        """Flow-matching-style action loss under a chosen advantage condition."""
        input_ids, attention_mask = self._select(batch, advantage_condition)
        actions = batch["actions"]
        prediction = self.forward(input_ids, attention_mask, actions)
        return F.mse_loss(prediction, actions)


def build_mock_cfg_batch(
    dataset,
    indices: Sequence[int],
    cfg: RecapConfig,
    tokenizer: TinyTokenizer,
    device: torch.device | str = "cpu",
) -> Dict[str, torch.Tensor]:
    """Build a base/positive/negative token batch from RECAP dataset samples."""
    base_ids, base_mask = [], []
    pos_ids, pos_mask = [], []
    neg_ids, neg_mask = [], []
    actions, indicators = [], []
    for index in indices:
        sample = dataset[int(index)]
        if "advantage_indicator" not in sample:
            raise RuntimeError(
                "sample has no advantage_indicator — run compute_advantages first"
            )
        prompt = f"Task: {sample['task']}; Action: "
        for text, ids, mask in (
            (prompt, base_ids, base_mask),
            (f"{prompt}{cfg.condition.positive_suffix}", pos_ids, pos_mask),
            (f"{prompt}{cfg.condition.negative_suffix}", neg_ids, neg_mask),
        ):
            tokens = tokenizer(text)
            ids.append(tokens)
            mask.append([1 if token else 0 for token in tokens])
        action = sample["action"]
        action = action[0] if action.dim() == 2 else action
        actions.append(action.float())
        indicators.append(int(sample["advantage_indicator"]))

    def tensor(rows, dtype=torch.long):
        return torch.tensor(rows, dtype=dtype, device=device)

    return {
        "input_ids_base": tensor(base_ids),
        "attention_mask_base": tensor(base_mask),
        "input_ids_pos": tensor(pos_ids),
        "attention_mask_pos": tensor(pos_mask),
        "input_ids_neg": tensor(neg_ids),
        "attention_mask_neg": tensor(neg_mask),
        "actions": torch.stack(actions).to(device),
        "advantage_indicator": tensor(indicators),
    }


def run_mock_cfg_training(
    dataset,
    cfg: RecapConfig,
    steps: int = 5,
    batch_size: int = 4,
    lr: float = 1e-2,
    seed: int = 0,
    device: str = "cpu",
    tokenizer: TinyTokenizer | None = None,
) -> Dict[str, float]:
    """Run the RECAP objective for a few steps on a mock policy (CPU-friendly)."""
    torch.manual_seed(seed)
    rng = random.Random(seed)
    tokenizer = tokenizer or TinyTokenizer()

    sample = dataset[0]
    action_dim = int(sample["action"].shape[-1])
    policy = MockConditionedPolicy(action_dim=action_dim).to(device)
    optimizer = torch.optim.Adam(policy.parameters(), lr=lr)

    n = len(dataset)
    last: Dict[str, float] = {}
    for step in range(steps):
        indices = [rng.randrange(n) for _ in range(min(batch_size, n))]
        batch = build_mock_cfg_batch(dataset, indices, cfg, tokenizer, device=device)
        loss, log = recap_cfg_loss(policy, batch, cfg, rng=rng)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        last = {key: float(value) for key, value in log.items()}
        print(
            f"[recap]   cfg step {step + 1}/{steps} loss={last['loss']:.6f} "
            f"base={last.get('loss_base', float('nan')):.6f} "
            f"cond={last['loss_conditioned']:.6f} "
            f"route(base/pos/neg)={last['route_base_frac']:.2f}/"
            f"{last['route_pos_frac']:.2f}/{last['route_neg_frac']:.2f}"
        )
    return last
