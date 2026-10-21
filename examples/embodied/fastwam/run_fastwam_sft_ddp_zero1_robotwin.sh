#!/usr/bin/env bash
# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

# ═══════════════════════════════════════════════════════════════
# run_fastwam_sft_ddp_zero1_robotwin.sh
# FastWAM SFT — RoboTwin 2.0, official-aligned (first-frame/uncond, 30k).
#
# Mirrors the official standalone task
#   FastWAM/configs/task/robotwin_uncond_3cam_384_1e-4.yaml (+ run.sh):
#   - data       : RoboTwin 2.0 single dataset (3-cam 240x320), T layout (camera_layout=robotwin_t)
#   - norm       : z-score (data.norm_stats_path = official dataset_stats.json, data.fastwam_norm_mode=z-score)
#   - action     : 14D; proprio 14D; no delta dims (delta_action_dim_mask = 14 x false)
#   - video DiT  : pretrained (skip_dit_load_from_pretrain=false)
#   - action DiT : null (random init, same as standalone)
#   - text cond  : precomputed cache (load_text_encoder=false)
#   - lr         : 1e-4, warmup 1500 (5% of 30k steps), min-lr 1e-6 (standalone: 5% warmup + eta_min=1e-6)
#   - optim      : AdamW, wd 1e-2, cosine; grad clip 1.0 (official train.yaml max_grad_norm)
#   - steps      : 30000, save every 2500, seed 42
#
# GBS: the official paper uses 64 GPUs (GBS 1024); we have 8, so we match the
# *effective* GBS 1024 via gradient accumulation:
#     per_device 16 x grad_accum 8 x 8 GPU = 1024
# per_device 16 == official's per-device batch. It needs gradient checkpointing
# (model.mot_checkpoint_mixed_attn=true), which the official repo hard-forbids
# (mot.py:28 raise) but LoongForge supports -- numerically neutral, memory for compute.
#
# Usage:
#   bash run_fastwam_sft_ddp_zero1_robotwin.sh [extra args passed to train.py]
#
# Key env vars: LOONGFORGE_PATH, DIFFSYNTH_MODEL_BASE_PATH, TOKENIZER_PATH,
#   DATASET_PATH, TEXT_EMBEDDING_CACHE_DIR, PRETRAINED_NORM_STATS, OUTPUT_DIR,
#   TRAIN_ITERS, PER_DEVICE_BATCH_SIZE, GRADIENT_ACCUMULATION_STEPS, SEED
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export LOONGFORGE_PATH=${LOONGFORGE_PATH:-"$(cd "$SCRIPT_DIR/../../.." && pwd)"}
export DIFFSYNTH_MODEL_BASE_PATH=${DIFFSYNTH_MODEL_BASE_PATH:-"/path/to/models"}
export TOKENIZER_PATH=${TOKENIZER_PATH:-"$DIFFSYNTH_MODEL_BASE_PATH/Wan-AI/Wan2.2-TI2V-5B"}
export DATASET_PATH=${DATASET_PATH:-"$DIFFSYNTH_MODEL_BASE_PATH/data/robotwin2.0/robotwin2.0"}
export TEXT_EMBEDDING_CACHE_DIR=${TEXT_EMBEDDING_CACHE_DIR:-"/path/to/text_embeds_cache/robotwin"}
export PRETRAINED_NORM_STATS=${PRETRAINED_NORM_STATS:-"$DIFFSYNTH_MODEL_BASE_PATH/data/robotwin2.0/dataset_stats.json"}
export OUTPUT_DIR=${OUTPUT_DIR:-"$LOONGFORGE_PATH/outputs/fastwam_sft_robotwin_gbs1024"}
export TRAIN_ITERS=${TRAIN_ITERS:-30000}
export PER_DEVICE_BATCH_SIZE=${PER_DEVICE_BATCH_SIZE:-16}
export GRADIENT_ACCUMULATION_STEPS=${GRADIENT_ACCUMULATION_STEPS:-8}   # 16 * 8 * 8 GPU = 1024
export SAVE_INTERVAL=${SAVE_INTERVAL:-2500}
export SEED=${SEED:-42}
# RoboTwin standalone uses a random action expert (action_dit_pretrained_path=null)
export ACTION_DIT_PRETRAINED_PATH=""

# ── Official-aligned recipe (env knobs read by the finetune launcher) ──
export LR_DECAY_STYLE=${LR_DECAY_STYLE:-linear_warmup_cosine_annealing}
export SAMPLER_EPOCH_MODE=${SAMPLER_EPOCH_MODE:-fixed}
export ZERO_MASTER_PARAM_DTYPE=${ZERO_MASTER_PARAM_DTYPE:-fp32}
export CLIP_GRAD=${CLIP_GRAD:-1.0}          # official train.yaml: max_grad_norm = 1.0
export SAVE_FORMAT=${SAVE_FORMAT:-dcp}
export ASYNC_SAVE=${ASYNC_SAVE:-1}

# ── Model / data dotlist overrides (the launcher injects these BEFORE its own
#    optionals, so argparse's positional `overrides` never lands after a flag) ──
export EXTRA_MODEL_OVERRIDES="${EXTRA_MODEL_OVERRIDES:-} \
model.mot_checkpoint_mixed_attn=true model.mot_compile_blocks=none \
model.disable_train_autocast=false model.drop_all_true_cross_attn_mask=false \
model.action_dim=14 model.proprio_dim=14 \
model.action_dit_pretrained_path=null model.redirect_common_files=false \
model.action_scheduler.train_shift=1.0 model.action_scheduler.infer_shift=1.0"
export EXTRA_DATA_OVERRIDES="${EXTRA_DATA_OVERRIDES:-} \
data.camera_layout=robotwin_t data.norm_stats_path=$PRETRAINED_NORM_STATS \
data.fastwam_norm_mode=z-score data.text_embedding_cache_dir=$TEXT_EMBEDDING_CACHE_DIR \
data.delta_action_dim_mask=[false,false,false,false,false,false,false,false,false,false,false,false,false,false]"

echo "════════════════════════════════════════════════════════════"
echo "  LoongForge FastWAM SFT (RoboTwin 2.0, 30k, GBS 1024)"
echo "  Data:      $DATASET_PATH"
echo "  Iters:     $TRAIN_ITERS  batch=$PER_DEVICE_BATCH_SIZE ga=$GRADIENT_ACCUMULATION_STEPS  (GBS=$((PER_DEVICE_BATCH_SIZE * GRADIENT_ACCUMULATION_STEPS * 8)))"
echo "  Output:    $OUTPUT_DIR"
echo "════════════════════════════════════════════════════════════"

bash "$LOONGFORGE_PATH/examples/embodied/fastwam/run_fastwam_sft_ddp_zero1_finetune.sh" \
    --lr-base 1e-4 --lr-warmup-iters 1500 --min-lr 1e-6 \
    "$@"
