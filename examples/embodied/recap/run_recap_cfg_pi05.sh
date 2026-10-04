#!/usr/bin/env bash
# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0
#
# ═══════════════════════════════════════════════════════════════
# run_recap_cfg_pi05.sh — RECAP Stage-1 Step 5: advantage-conditioned CFG
# policy training on real pi0.5 weights.
#
# Hyperparameters follow the published RECAP recipe: micro batch 32, global batch 512 (= 8 GPUs x 32 x 2), lr 1e-5,
# cosine schedule, warmup 5000, total 30000 steps, min_lr 0.0.
#
# This is the *real* backend (the smoke test uses `train_cfg --policy-backend
# mock`). It needs:
#   * a LeRobot rollout dataset that already has meta/advantages.parquet
#     (produced by compute_returns -> train_value -> compute_advantages),
#   * a pi0.5 policy checkpoint,
#   * the PaliGemma tokenizer.
#
# Usage:
#   bash run_recap_cfg_pi05.sh
#   TRAIN_ITERS=50 LR_WARMUP_ITERS=10 bash run_recap_cfg_pi05.sh   # short run
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
TOKENIZER_PATH=${TOKENIZER_PATH:-"$LOCAL_VLA_ARTIFACTS_ROOT/pi05/tokenizers/paligemma-3b-pt-224"}
CHECKPOINT_PATH=${CHECKPOINT_PATH:-"$LOCAL_VLA_ARTIFACTS_ROOT/pi05/models/pi05_libero_finetuned"}  # pi0.5 policy checkpoint
# Rollout dataset collected from the policy, already carrying meta/advantages.parquet
DATA_PATH=${DATA_PATH:-"$LOCAL_VLA_ARTIFACTS_ROOT/pi05/rollouts/libero_from_A"}
RECAP_CONFIG=${RECAP_CONFIG:-"$SCRIPT_DIR/configs/recap_baseline.yaml"}
OUTPUT_DIR=${OUTPUT_DIR:-"$LOONGFORGE_PATH/outputs/recap_cfg_pi05"}

# ── Recipe hyperparameters ─────
TRAIN_ITERS=${TRAIN_ITERS:-30000}
PER_DEVICE_BATCH_SIZE=${PER_DEVICE_BATCH_SIZE:-32}        # micro batch 32
GRADIENT_ACCUMULATION_STEPS=${GRADIENT_ACCUMULATION_STEPS:-2}  # 8 x 32 x 2 = 512 global
LR_BASE=${LR_BASE:-1.0e-5}
LR_WARMUP_ITERS=${LR_WARMUP_ITERS:-5000}
MIN_LR=${MIN_LR:-0.0}
SAVE_INTERVAL=${SAVE_INTERVAL:-3000}

# ── Launch ────────────────────────────────────────────────────
PYTHONPATH=$LOONGFORGE_PATH:${PYTHONPATH:-} \
    torchrun "${DISTRIBUTED_ARGS[@]}" \
    "$LOONGFORGE_PATH/loongforge/embodied/train.py" \
    --model-name pi05 \
    --trainer-type RecapCfgTrainer \
    --recap-config "$RECAP_CONFIG" \
    --dataset-format lerobot_datasets \
    --dataset-path "$DATA_PATH" \
    --tokenizer-path "$TOKENIZER_PATH" \
    --robot-type libero_franka \
    --pretrained-checkpoint "$CHECKPOINT_PATH" \
    --train-iters "$TRAIN_ITERS" \
    --per-device-batch-size "$PER_DEVICE_BATCH_SIZE" \
    --gradient-accumulation-steps "$GRADIENT_ACCUMULATION_STEPS" \
    --lr-base "$LR_BASE" \
    --lr-decay-style cosine_with_min_lr \
    --lr-warmup-iters "$LR_WARMUP_ITERS" \
    --min-lr "$MIN_LR" \
    --optimizer AdamW \
    --clip-grad 1.0 \
    --weight-decay 0.01 \
    --adam-beta1 0.9 --adam-beta2 0.95 --adam-eps 1e-8 \
    --save-interval "$SAVE_INTERVAL" \
    --num-workers "${NUM_WORKERS:-16}" \
    --output-dir "$OUTPUT_DIR" \
    --distributed-strategy "${DISTRIBUTED_STRATEGY:-ddp}" \
    --dtype bfloat16 \
    --activation-checkpoint-module-patterns "model.paligemma_with_expert.paligemma.model.vision_tower.vision_model.encoder.layers.*" \
    model.gradient_checkpointing=true \
    data.normalization_mode=mean_std \
    "$@"
