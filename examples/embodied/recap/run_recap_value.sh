#!/usr/bin/env bash
# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# ═══════════════════════════════════════════════════════════════
# run_recap_value.sh — RECAP Step 2: train the state-value critic
# (SigLIP2-so400m + Gemma3-270M + Gemma-1 expert, 201-bin head).
#
# Multi-GPU via torchrun: RecapValueTrainer is registered in --trainer-type, so
# the critic gets DDP wrapping, the shared optimizer/scheduler, grad clipping,
# NaN/loss-spike guards, checkpointing + --resume, and logging.
#
# Hyperparameters follow the RECAP training recipe:
# micro batch 32, global batch 256 (= 8 GPUs x 32), backbone lr 5e-5,
# value-head lr 1e-4, constant schedule, warmup 500, scheduler total 8000 steps,
# checkpoint every 3000, validation every 500 steps.
#
# Validation holds out the LAST VAL_EPISODES episodes of the dataset (they are
# NOT trained on); set VAL_EPISODES=0 to disable and use every episode.
#
# Needs the returns sidecar (compute_returns) and the two backbone assets.
# Output: <output-dir>/critic.pt in the RLinf format that compute_advantages
# loads (--value-checkpoint), plus LoongForge checkpoints/ for resume.
#
# Usage:
#   bash run_recap_value.sh
#   TRAIN_ITERS=20 LR_WARMUP_ITERS=5 bash run_recap_value.sh   # short run
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export LOONGFORGE_PATH=${LOONGFORGE_PATH:-"$(cd "$SCRIPT_DIR/../../.." && pwd)"}
export LOCAL_VLA_ARTIFACTS_ROOT=${LOCAL_VLA_ARTIFACTS_ROOT:-"/ssd2/loongforge_embodied_ci/vla_artifacts"}

GPUS_PER_NODE=${GPUS_PER_NODE:-8}
MASTER_ADDR=${MASTER_ADDR:-"localhost"}
MASTER_PORT=${MASTER_PORT:-"29500"}
NNODES=${NNODES:-${WORLD_SIZE:-1}}
NODE_RANK=${NODE_RANK:-${RANK:-0}}

DISTRIBUTED_ARGS=(
    --nproc_per_node "$GPUS_PER_NODE"
    --nnodes "$NNODES"
    --node_rank "$NODE_RANK"
    --master_addr "$MASTER_ADDR"
    --master_port "$MASTER_PORT"
)

export NO_ALBUMENTATIONS_UPDATE=1

# ── Paths ─────────────────────────────────────────────────────
SIGLIP_PATH=${SIGLIP_PATH:-"$LOCAL_VLA_ARTIFACTS_ROOT/recap/siglip2-so400m-patch14-224"}
GEMMA3_PATH=${GEMMA3_PATH:-"$LOCAL_VLA_ARTIFACTS_ROOT/recap/gemma-3-270m"}
TOKENIZER_PATH=${TOKENIZER_PATH:-"$GEMMA3_PATH"}
# Rollout dataset with meta/returns*.parquet (produced by compute_returns)
DATA_PATH=${DATA_PATH:-"$LOCAL_VLA_ARTIFACTS_ROOT/pi05/rollouts/libero_from_A"}
OUTPUT_DIR=${OUTPUT_DIR:-"$LOONGFORGE_PATH/outputs/recap_value"}
# Optional: pre-decoded frame cache (recap.cli.predecode) to remove video decode
# from the critical path. Leave empty to decode from the video container.
FRAME_CACHE_ROOT=${FRAME_CACHE_ROOT:-}

# ── Recipe hyperparameters ─────────
TRAIN_ITERS=${TRAIN_ITERS:-8000}                 # scheduler total_training_steps
PER_DEVICE_BATCH_SIZE=${PER_DEVICE_BATCH_SIZE:-32}   # micro batch 32
GRADIENT_ACCUMULATION_STEPS=${GRADIENT_ACCUMULATION_STEPS:-1}  # 8 x 32 = 256 global
LR_BASE=${LR_BASE:-5.0e-5}                       # backbone lr
LR_GROUP=${LR_GROUP:-"value_head=1.0e-4"}        # value-head lr
LR_DECAY_STYLE=${LR_DECAY_STYLE:-constant}
LR_WARMUP_ITERS=${LR_WARMUP_ITERS:-500}
LR_DECAY_ITERS=${LR_DECAY_ITERS:-8000}
SAVE_INTERVAL=${SAVE_INTERVAL:-3000}
NUM_WORKERS=${NUM_WORKERS:-16}
# Validation: hold out the last N episodes (0 disables) and evaluate every N steps.
VAL_EPISODES=${VAL_EPISODES:-8}
VAL_INTERVAL=${VAL_INTERVAL:-500}
VAL_BATCHES=${VAL_BATCHES:-0}   # 0 = evaluate the whole held-out split

# ── Launch ────────────────────────────────────────────────────
EXTRA_ARGS=()
if [[ -n "$FRAME_CACHE_ROOT" ]]; then
    EXTRA_ARGS+=(--recap-frame-cache-root "$FRAME_CACHE_ROOT")
fi

PYTHONPATH=$LOONGFORGE_PATH:${PYTHONPATH:-} \
    torchrun "${DISTRIBUTED_ARGS[@]}" \
    "$LOONGFORGE_PATH/loongforge/embodied/train.py" \
    --model-name recap_value \
    --trainer-type RecapValueTrainer \
    --dataset-path "$DATA_PATH" \
    --output-dir "$OUTPUT_DIR" \
    --train-iters "$TRAIN_ITERS" \
    --per-device-batch-size "$PER_DEVICE_BATCH_SIZE" \
    --gradient-accumulation-steps "$GRADIENT_ACCUMULATION_STEPS" \
    --lr-base "$LR_BASE" \
    --lr-group "$LR_GROUP" \
    --lr-decay-style "$LR_DECAY_STYLE" \
    --lr-warmup-iters "$LR_WARMUP_ITERS" \
    --lr-decay-iters "$LR_DECAY_ITERS" \
    --optimizer AdamW \
    --clip-grad 1.0 \
    --weight-decay 0.01 \
    --adam-beta1 0.9 --adam-beta2 0.95 --adam-eps 1e-8 \
    --save-interval "$SAVE_INTERVAL" \
    --num-workers "$NUM_WORKERS" \
    --recap-val-episodes "$VAL_EPISODES" \
    --recap-val-interval "$VAL_INTERVAL" \
    --recap-val-batches "$VAL_BATCHES" \
    --distributed-strategy "${DISTRIBUTED_STRATEGY:-ddp}" \
    --dtype bfloat16 \
    "${EXTRA_ARGS[@]}" \
    model.siglip_path="$SIGLIP_PATH" \
    model.gemma3_path="$GEMMA3_PATH" \
    model.tokenizer_path="$TOKENIZER_PATH" \
    "$@"
