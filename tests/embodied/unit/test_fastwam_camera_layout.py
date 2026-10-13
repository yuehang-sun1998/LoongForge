# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for FastWAM multi-camera packing (``data.camera_layout``).

Covers the ``robotwin_t`` T layout (RoboTwin 3-cam) and the default
``horizontal2`` LIBERO path of :class:`FastWAMKeyMappingTransform`.
"""

import pytest
import torch

from loongforge.embodied.data.datasets.fastwam.transforms.fastwam_transform import (
    FastWAMKeyMappingTransform,
)


def _video(value, t=2, c=3, h=240, w=320):
    """A constant-valued multi-frame image tensor [T, C, H, W] in [0, 1]."""
    return torch.full((t, c, h, w), float(value))


def test_robotwin_t_packs_three_cams_into_t_layout():
    # Keys sort to [cam_high, cam_left_wrist, cam_right_wrist] = [head, left, right].
    # Constant frames survive bilinear resize, so normalize(0.5, 0.5) maps the
    # three fill values 0.0 / 0.5 / 1.0 to -1.0 / 0.0 / 1.0 and we can read regions.
    data = {
        "observation.images.cam_high": _video(0.0),
        "observation.images.cam_left_wrist": _video(0.5),
        "observation.images.cam_right_wrist": _video(1.0),
    }
    out = FastWAMKeyMappingTransform(camera_layout="robotwin_t").apply(data)

    video = out["video"]
    assert video.shape == (3, 2, 384, 320)  # [C, T, 384, 320]
    # Top 256 rows = head; bottom 128 rows = [left | right] split at col 160.
    # atol loosened: bilinear+antialias leaves ~1e-7 noise on constant fields.
    assert torch.allclose(video[:, :, :256, :], torch.full_like(video[:, :, :256, :], -1.0), atol=1e-6)
    assert torch.allclose(video[:, :, 256:, :160], torch.zeros_like(video[:, :, 256:, :160]), atol=1e-6)
    assert torch.allclose(video[:, :, 256:, 160:], torch.ones_like(video[:, :, 256:, 160:]), atol=1e-6)


def test_robotwin_t_requires_exactly_three_cams():
    data = {
        "observation.images.cam_high": _video(0.0),
        "observation.images.cam_left_wrist": _video(0.5),
    }
    with pytest.raises(ValueError, match="requires exactly 3 cameras"):
        FastWAMKeyMappingTransform(camera_layout="robotwin_t").apply(data)


def test_horizontal2_default_two_cam_shape_unchanged():
    data = {
        "observation.images.cam0": _video(0.3, h=256, w=256),
        "observation.images.cam1": _video(0.7, h=256, w=256),
    }
    out = FastWAMKeyMappingTransform(image_size=224).apply(data)  # default horizontal2
    assert out["video"].shape == (3, 2, 224, 448)  # [C, T, 224, 2*224]


def test_invalid_camera_layout_raises():
    with pytest.raises(ValueError, match="Unsupported fastwam camera_layout"):
        FastWAMKeyMappingTransform(camera_layout="bogus")
