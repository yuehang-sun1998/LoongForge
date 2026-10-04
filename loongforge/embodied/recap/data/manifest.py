# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Determinant hashing and the pipeline manifest for RECAP sidecars.

A RECAP sidecar (returns / critic / advantages) is only valid for the exact
inputs that produced it. Rather than trusting a hand-written ``--tag``, the tag
is derived from the determinants that actually change the artifact:

    returns    <- (dataset_version, reward)
    critic     <- (dataset_version, reward, critic training config)
    advantages <- (dataset_version, reward, advantage, value checkpoint)

The full determinant set is written to ``<dataset>/meta/recap_pipeline_manifest.json``
so any artifact can be traced back to what produced it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

MANIFEST_NAME = "recap_pipeline_manifest.json"
# Bump when the determinant schema changes: it changes every derived tag.
DETERMINANT_VERSION = 1

_CHUNK = 8 * 1024 * 1024


def sha256_file(path: str | Path) -> str:
    """SHA-256 of a file's bytes, streamed in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(payload: Any) -> str:
    """SHA-256 of a canonical JSON encoding (sorted keys, no whitespace)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_determinant(
    dataset_path: str | Path,
    episodes: Optional[Sequence[int]] = None,
    dataset_type: str = "rollout",
) -> Dict[str, Any]:
    """Identity of the data slice: split definition, selection, and semantics.

    ``episodes.jsonl`` carries each episode's length and ``is_success``, so
    hashing it captures the label semantics. The episode selection is included
    explicitly because the advantage threshold is a *global* percentile: a table
    built on a subset must never be reused for the full dataset.
    """
    root = Path(dataset_path)
    entry: Dict[str, Any] = {
        "episodes_sha256": sha256_file(root / "meta" / "episodes.jsonl"),
        "tasks_sha256": sha256_file(root / "meta" / "tasks.jsonl"),
        "episode_selection": sorted(int(e) for e in episodes) if episodes else "all",
        "dataset_type": str(dataset_type),
    }
    return entry


def reward_determinant(cfg) -> Dict[str, Any]:
    """Reward/return semantics that determine ``G_t``."""
    reward = cfg.reward
    return {
        "mode": reward.mode,
        "step_reward": reward.step_reward,
        "success_reward": reward.success_reward,
        "failure_reward": reward.failure_reward,
        "use_discount": reward.use_discount,
        "gamma": reward.gamma,
    }


def advantage_determinant(cfg, return_range: Optional[Sequence[float]]) -> Dict[str, Any]:
    """Advantage semantics that determine ``A_t`` and the threshold."""
    advantage = cfg.advantage
    entry: Dict[str, Any] = {
        "horizon": advantage.horizon,
        "use_discount": advantage.use_discount,
        "gamma": advantage.gamma,
        "positive_quantile": advantage.positive_quantile,
        "task_dependent": advantage.task_dependent,
    }
    if return_range is not None:
        entry["return_min"] = float(return_range[0])
        entry["return_max"] = float(return_range[1])
    return entry


def value_checkpoint_determinant(path: Optional[str | Path]) -> Dict[str, Any]:
    """Identity of the value checkpoint.

    NOTE: the critic *architecture* (``critic_expert_variant`` / ``num_bins`` /
    ``v_min`` / ``v_max`` / backbone paths) is deliberately NOT a separate
    determinant here. Those choices are already implied by the weights: a
    different architecture produces different parameters and therefore a
    different ``sha256``. Listing them again would add no discriminating power
    while making the determinant set drift out of sync with the config.
    """
    if path is None:
        return {"sha256": None, "name": None, "size": None}
    path = Path(path)
    if not path.exists():
        return {"sha256": None, "name": path.name, "size": None, "missing": True}
    return {"sha256": sha256_file(path), "name": path.name, "size": path.stat().st_size}


def build_tag(determinants: Dict[str, Any]) -> str:
    """Short, collision-resistant tag derived from the determinants."""
    payload = {"determinant_version": DETERMINANT_VERSION, **determinants}
    return sha256_json(payload)[:12]


def human_summary(cfg, dataset_type: str = "rollout") -> str:
    """Readable prefix for artifact filenames, e.g. ``baseline_N10_q0.3``."""
    q = cfg.advantage.positive_quantile
    summary = f"{cfg.mode}_N{cfg.advantage.horizon}_q{q:g}"
    if str(dataset_type).lower() == "sft":
        summary += "_sft"
    return summary


def returns_tag(dataset_path, cfg, episodes=None, dataset_type="rollout") -> Dict[str, Any]:
    """Determinants + tag for the returns sidecar."""
    determinants = {
        "dataset": dataset_determinant(dataset_path, episodes, dataset_type),
        "reward": reward_determinant(cfg),
    }
    return {"determinants": determinants, "tag": build_tag(determinants),
            "summary": human_summary(cfg, dataset_type)}


def advantages_tag(
    dataset_path, cfg, value_checkpoint, return_range=None, episodes=None, dataset_type="rollout"
) -> Dict[str, Any]:
    """Determinants + tag for the advantages sidecar (all four determinants)."""
    determinants = {
        "dataset": dataset_determinant(dataset_path, episodes, dataset_type),
        "reward": reward_determinant(cfg),
        "advantage": advantage_determinant(cfg, return_range),
        "value_checkpoint": value_checkpoint_determinant(value_checkpoint),
    }
    return {"determinants": determinants, "tag": build_tag(determinants),
            "summary": human_summary(cfg, dataset_type)}


def artifact_name(stage: str, summary: str, tag: str, suffix: str) -> str:
    """``<stage>_<summary>_<tag><suffix>`` — readable and collision-proof."""
    return f"{stage}_{summary}_{tag}{suffix}"


# ── manifest ──────────────────────────────────────────────────────────────
def manifest_path(dataset_path) -> Path:
    return Path(dataset_path) / "meta" / MANIFEST_NAME


def read_manifest(dataset_path) -> Dict[str, Any]:
    path = manifest_path(dataset_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def write_manifest(dataset_path, stage: str, record: Dict[str, Any]) -> Path:
    """Merge one stage's record into the dataset manifest."""
    path = manifest_path(dataset_path)
    manifest = read_manifest(dataset_path)
    manifest.setdefault("determinant_version", DETERMINANT_VERSION)
    manifest.setdefault("stages", {})
    manifest["stages"][stage] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, default=str))
    return path


def check_advantages_tag(dataset_path, expected_tag: str) -> Optional[str]:
    """Return a warning string when the manifest's advantages tag disagrees."""
    manifest = read_manifest(dataset_path)
    recorded = manifest.get("stages", {}).get("advantages", {}).get("tag")
    if recorded is None:
        return None  # nothing recorded; nothing to contradict
    if recorded != expected_tag:
        return (
            f"advantages tag mismatch: manifest has {recorded!r}, "
            f"requested {expected_tag!r} — the sidecar may be stale."
        )
    return None


def resolve_tag(dataset_path, stage: str, explicit_tag: Optional[str]) -> Optional[str]:
    """Tag to read for ``stage``: explicit wins, else the manifest's record."""
    if explicit_tag:
        return explicit_tag
    manifest = read_manifest(dataset_path)
    return manifest.get("stages", {}).get(stage, {}).get("tag")


def record_stage(dataset_path, stage: str, filename: str, info: Dict[str, Any]) -> Path:
    """Record one produced artifact (tag, filename, determinants) in the manifest."""
    record = {"filename": filename, **info}
    return write_manifest(dataset_path, stage, record)
