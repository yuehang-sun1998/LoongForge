# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""State-value model for RECAP.

Stage 1 uses a small scalar value head over ``observation.state`` (and, when
enabled, an image view). Stage 3 of the roadmap flips ``distributional=True`` to
predict a 201-bin distribution ``p_phi(V | observation, language)`` while keeping
the same :meth:`RecapValueModel.predict_expected_value` interface, so the
downstream advantage estimator never needs to read raw logits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..core.config import ValueConfig


class RecapValueModel(nn.Module):
    """Value head over state (and optional image)."""

    def __init__(
        self,
        state_dim: int,
        cfg: ValueConfig,
        image_channels: int = 3,
    ):
        super().__init__()
        self.cfg = cfg
        self.state_dim = int(state_dim)
        self.image_channels = int(image_channels)

        hidden = int(cfg.hidden_dim)
        layers: List[nn.Module] = []
        in_dim = self.state_dim
        for _ in range(max(1, int(cfg.num_layers))):
            layers += [nn.Linear(in_dim, hidden), nn.LayerNorm(hidden), nn.SiLU()]
            in_dim = hidden
        self.state_mlp = nn.Sequential(*layers)

        feature_dim = hidden
        if cfg.use_image:
            self.image_encoder = nn.Sequential(
                nn.Conv2d(image_channels, 16, kernel_size=3, stride=2, padding=1),
                nn.ReLU(),
                nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),
                nn.ReLU(),
                nn.AdaptiveAvgPool2d(1),
                nn.Flatten(),
            )
            feature_dim += 32
        else:
            self.image_encoder = None

        out_dim = int(cfg.num_bins) if cfg.distributional else 1
        self.head = nn.Linear(feature_dim, out_dim)

        if cfg.distributional:
            bins = torch.linspace(float(cfg.value_min), float(cfg.value_max), int(cfg.num_bins))
            self.register_buffer("value_bins", bins, persistent=False)

    def forward(
        self, state: torch.Tensor, image: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Return scalar value ``[B]`` or value logits ``[B, num_bins]``."""
        features = self.state_mlp(state.float())
        if self.image_encoder is not None and image is not None:
            features = torch.cat([features, self.image_encoder(image.float())], dim=-1)
        out = self.head(features)
        if self.cfg.distributional:
            return out  # logits
        return out.squeeze(-1)

    def predict_expected_value(
        self, state: torch.Tensor, image: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Continuous value: the scalar head directly, or the bin expectation."""
        out = self.forward(state, image)
        if not self.cfg.distributional:
            return out
        probs = torch.softmax(out, dim=-1)
        return (probs * self.value_bins.to(probs.dtype)).sum(dim=-1)


def discretize_targets(targets: torch.Tensor, cfg: ValueConfig) -> torch.Tensor:
    """Map continuous returns onto value-bin indices (distributional loss)."""
    bins = torch.linspace(
        float(cfg.value_min), float(cfg.value_max), int(cfg.num_bins) + 1, device=targets.device
    )
    # bucketize gives index in [0, num_bins]; clamp to valid bin range.
    indices = torch.bucketize(targets, bins) - 1
    return indices.clamp(0, int(cfg.num_bins) - 1)


def value_loss(
    outputs: torch.Tensor, targets: torch.Tensor, cfg: ValueConfig
) -> torch.Tensor:
    """MSE for scalar value, cross-entropy for distributional value."""
    targets = targets.float()
    if cfg.distributional:
        target_bins = discretize_targets(targets, cfg)
        return F.cross_entropy(outputs, target_bins)
    return F.mse_loss(outputs, targets)


@torch.no_grad()
def predict_values(
    model: RecapValueModel,
    samples: Iterable[Dict[str, torch.Tensor]],
    batch_size: int = 256,
    device: str = "cpu",
) -> Dict[Tuple[int, int], float]:
    """Run the value model over samples and return ``{(ep, frame): value}``.

    Each sample must carry ``state``, optional ``image`` and the
    ``episode_index`` / ``frame_index`` keys.
    """
    model.eval()
    device = torch.device(device)
    model.to(device)
    samples = list(samples)
    out: Dict[Tuple[int, int], float] = {}
    for start in range(0, len(samples), batch_size):
        chunk = samples[start : start + batch_size]
        states = torch.stack([s["state"] for s in chunk]).to(device)
        images = None
        if model.image_encoder is not None and chunk[0].get("image") is not None:
            images = torch.stack([s["image"] for s in chunk]).to(device)
        values = model.predict_expected_value(states, images).detach().cpu()
        for sample, value in zip(chunk, values):
            out[(int(sample["episode_index"]), int(sample["frame_index"]))] = float(value)
    return out


def save_value_checkpoint(
    path: str | Path,
    model: RecapValueModel,
    cfg: ValueConfig,
) -> Path:
    """Persist the value model weights + config needed to rebuild it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "state_dim": model.state_dim,
            "image_channels": model.image_channels,
            "value_cfg": cfg.__dict__,
        },
        path,
    )
    return path


def load_value_checkpoint(path: str | Path, device: str = "cpu") -> RecapValueModel:
    """Rebuild a value model from a checkpoint written by :func:`save_value_checkpoint`."""
    payload = torch.load(path, map_location=device, weights_only=False)
    cfg = ValueConfig(**payload["value_cfg"])
    model = RecapValueModel(payload["state_dim"], cfg, payload.get("image_channels", 3))
    model.load_state_dict(payload["state_dict"])
    model.to(device)
    model.eval()
    return model
