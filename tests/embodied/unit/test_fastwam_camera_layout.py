# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for FastWAM multi-camera packing (``data.camera_layout``).

The ``robotwin_t`` layout is checked for **bit-identical** parity against the
official FastWAM implementation: we exec the real
``concat_multi_camera == 'robotwin'`` source block from the FastWAM repo
(``$FASTWAM_REPO`` or ``/workspace/FastWAM``) and compare, so the test tracks
the official code rather than a transcription of it.
"""

import os
import re
import textwrap
from pathlib import Path

import pytest
import torch
import torchvision.transforms.functional as TF

from loongforge.embodied.data.datasets.fastwam.transforms.fastwam_transform import (
    FastWAMKeyMappingTransform,
)


def _video(value, t=2, c=3, h=240, w=320):
    """A constant-valued multi-frame image tensor [T, C, H, W] in [0, 1]."""
    return torch.full((t, c, h, w), float(value))


def _load_official_robotwin_pack():
    """Return a callable running the official FastWAM robotwin packing block.

    Extracts the exact ``cam_top ... torch.cat([cam_top, bottom], dim=-2)``
    source from ``robot_video_dataset.py`` and execs it (video is a
    ``[num_cameras, T, C, H, W]`` tensor, as in the official ``_get``).
    Returns ``None`` if the official repo is not present.
    """
    repo = Path(os.environ.get("FASTWAM_REPO", "/workspace/FastWAM"))
    src = repo / "src/fastwam/datasets/lerobot/robot_video_dataset.py"
    if not src.exists():
        return None
    text = src.read_text()
    m = re.search(
        r"(?s)\n([ \t]*cam_top = transforms_F\.resize\(.*?"
        r"video = torch\.cat\(\[cam_top, bottom\], dim=-2\))",
        text,
    )
    if not m:
        return None
    body = textwrap.dedent(m.group(1))

    def _pack(video):
        ns = {"torch": torch, "transforms_F": TF, "video": video}
        exec(body, ns)  # noqa: S102 - runs the official snippet verbatim
        return ns["video"]

    return _pack


def test_robotwin_t_matches_official_fastwam_packing():
    official = _load_official_robotwin_pack()
    if official is None:
        pytest.skip("official FastWAM repo not found (set $FASTWAM_REPO)")
    torch.manual_seed(0)
    # Raw RoboTwin-ish frames [T, C, H, W] in [0, 1], sorted [head, left, right].
    cams = [torch.rand(2, 3, 240, 320) for _ in range(3)]
    lf = FastWAMKeyMappingTransform(camera_layout="robotwin_t")._pack_robotwin_t(cams)
    ref = official(torch.stack(cams, dim=0))  # official indexes video[0..2]
    assert lf.shape == (2, 3, 384, 320)  # [T, C, 384, 320]
    assert torch.equal(lf, ref)  # bit-identical to the official implementation


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
