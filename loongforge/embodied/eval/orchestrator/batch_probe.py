# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Batch-scaling probe: is server-side batching (M3) worth building?

The M3 go/no-go question is not "is the GPU busy" but "where does a request's wall
time go, and how does the model's own compute scale with batch size". A serial
server is the binding resource, so making each request cheaper raises the ceiling
whether or not the GPU is saturated.

Two measurements, no eval-path changes:

1. **Split** -- profile a real ``predict_action`` and report wall vs CUDA kernel
   time (plus the top kernels). If host/overhead dominates, batching amortises it.
2. **Scaling** -- capture the real ``FastWAM.infer_action`` call, replay it with the
   batch dimension expanded to 1/2/4/8, and report the per-sample time. This uses
   the batch-general inner entry point, so it does not need the public
   ``predict_action`` batch contract (which asserts batch == 1).

Usage:
    python -m loongforge.embodied.eval.orchestrator.batch_probe \
        --config examples/embodied/fastwam/eval/configs/libero/libero_10.yaml \
        --calls 5 --batches 1,2,4,8
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Any, Dict, List, Tuple

import numpy as np
import torch


def _build_model(config_path: str) -> Any:
    """Build the eval model exactly the way the policy server does."""
    from loongforge.embodied.eval.factories.registry import build_model_spec
    from loongforge.embodied.eval.servers.eval_server_config import parse_eval_server_config

    server_args, raw_model_dict = parse_eval_server_config(config_path)
    return build_model_spec(server_args, raw_model_dict).model


def _dummy_call(model: Any) -> None:
    """One representative call, shaped like the server's warmup."""
    metadata = getattr(model, "metadata", {}) or {}
    n_views = max(1, int(metadata.get("num_camera_views", 1) or 1))
    state_dim = int(metadata.get("state_dim", 0) or 0) or (14 if n_views == 3 else 8)
    image = np.zeros((224, 224, 3), dtype=np.uint8)
    model.predict_action(
        images=[[image] * n_views],
        instructions=["pick up the object and place it in the basket"],
        state=np.zeros(state_dim, dtype=np.float32),
        dataset_stats=None,
    )


def _profile(model: Any, calls: int) -> Dict[str, Any]:
    """Wall time vs CUDA kernel time for a real predict_action."""
    _dummy_call(model)  # warmup (lazy imports, cudnn autotune)
    torch.cuda.synchronize()
    walls: List[float] = []
    with torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CUDA]) as prof:
        for _ in range(calls):
            start = time.perf_counter()
            _dummy_call(model)
            torch.cuda.synchronize()
            walls.append((time.perf_counter() - start) * 1000.0)
    cuda_ms = sum(event.device_time_total for event in prof.key_averages()) / 1000.0  # us -> ms
    top = sorted(
        ((event.key, event.device_time_total / 1000.0) for event in prof.key_averages() if event.device_time_total),
        key=lambda kv: -kv[1],
    )[:5]
    mean_wall = statistics.mean(walls)
    return {
        "wall_ms": round(mean_wall, 1),
        "cuda_ms": round(cuda_ms, 1),
        "host_ms": round(mean_wall - cuda_ms, 1),
        "gpu_share": round(cuda_ms / mean_wall, 3) if mean_wall else None,
        "top_kernels_ms": [(name, round(ms, 1)) for name, ms in top],
    }


def _capture_infer_call(model: Any) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    """Run one predict_action and capture the kwargs it hands to FastWAM.infer_action."""
    from loongforge.embodied.model.fastwam.mot.fastwam import FastWAM

    captured: Dict[str, Any] = {}
    original = FastWAM.infer_action

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not captured:
            captured["args"] = tuple(t.detach().clone() if torch.is_tensor(t) else t for t in args)
            captured["kwargs"] = {k: (v.detach().clone() if torch.is_tensor(v) else v) for k, v in kwargs.items()}
        return original(self, *args, **kwargs)

    FastWAM.infer_action = spy
    try:
        _dummy_call(model)
    finally:
        FastWAM.infer_action = original
    return captured["args"], captured["kwargs"]


def _expand(value: Any, batch: int) -> Any:
    """Tile a batch-1 tensor along a leading batch dim; leave everything else alone."""
    if torch.is_tensor(value) and value.ndim >= 2 and value.shape[0] == 1:
        return value.repeat(batch, *([1] * (value.ndim - 1)))
    return value


def _scaling(model: Any, batches: List[int]) -> List[Dict[str, Any]]:
    """Replay the captured inner call with an expanded batch and time per sample."""
    from loongforge.embodied.model.fastwam.mot.fastwam import FastWAM

    args, kwargs = _capture_infer_call(model)
    core = model.core
    rows: List[Dict[str, Any]] = []
    for batch in batches:
        big_args = tuple(_expand(a, batch) for a in args)
        big_kwargs = {k: _expand(v, batch) for k, v in kwargs.items()}
        try:
            FastWAM.infer_action(core, *big_args, **big_kwargs)  # warmup
            torch.cuda.synchronize()
            start = time.perf_counter()
            for _ in range(3):
                FastWAM.infer_action(core, *big_args, **big_kwargs)
            torch.cuda.synchronize()
            total_ms = (time.perf_counter() - start) / 3 * 1000.0
        except Exception as exc:  # the inner path may still not be batch-general
            rows.append({"batch": batch, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append(
            {
                "batch": batch,
                "total_ms": round(total_ms, 1),
                "per_sample_ms": round(total_ms / batch, 1),
            }
        )
    return rows


def main() -> None:
    """Run main."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="a server eval config (server.ckpt_path etc.)")
    parser.add_argument("--calls", type=int, default=5)
    parser.add_argument("--batches", default="1,2,4,8")
    args = parser.parse_args()

    model = _build_model(args.config)
    split = _profile(model, args.calls)
    scaling = _scaling(model, [int(b) for b in args.batches.split(",") if b])
    print(json.dumps({"split": split, "scaling": scaling}, indent=2))


if __name__ == "__main__":
    main()
