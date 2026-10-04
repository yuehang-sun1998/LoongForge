# RECAP (Stage 1) — advantage-conditioned offline RL on pi0.5

This directory holds the Stage-1 implementation of the RECAP roadmap: take an
already-collected LeRobot **rollout** dataset and run the offline-RL pipeline on
top of the stock pi0.5 SFT code.

```
rollout LeRobot dataset (already collected)
    -> compute_returns      reward + return sidecar (meta/returns.parquet)
    -> train_value          state-value model (value.pt)
    -> compute_advantages   N-step advantage + indicator (meta/advantages.parquet)
    -> train_cfg            advantage-conditioned (CFG) flow-matching policy
```

The rollout collector itself (LIBERO env + checkpoint-A inference) is **out of
scope** for Stage 1 — the pipeline starts from a dataset that already exists. The
existing `loongforge/embodied/eval/` LIBERO runner is untouched.

## Layout

| Path | What |
|---|---|
| `loongforge/embodied/recap/` | The RECAP package (`core/` `data/` `value_model/` `cli/` `testing/`) (config, reward, returns, value, advantage, condition, objective, dataset, CLIs). |
| `loongforge/embodied/recap/testing/mock.py` | Generates a tiny valid LeRobot v2 rollout dataset for smoke tests. |
| `loongforge/embodied/train/trainers/custom/recap_cfg.py` | `RecapCfgTrainer` — the real pi0.5 CFG trainer. |
| `loongforge/embodied/train/trainers/custom/recap_value.py` | `RecapValueTrainer` — multi-GPU state-value critic trainer. |
| `examples/embodied/recap/run_recap_smoke.sh` | CPU end-to-end smoke (mock dataset, mock policy backend). |
| `examples/embodied/recap/run_recap_value.sh` | Real value-critic training (8 GPUs, needs the two backbones). |
| `examples/embodied/recap/run_recap_cfg_pi05.sh` | Real pi0.5 CFG training (needs checkpoint A + tokenizer). |

## Quick start (smoke, CPU, no artifacts)

```bash
bash examples/embodied/recap/run_recap_smoke.sh
# paper-aligned profile instead of the baseline profile:
RECAP_PROFILE=paper bash examples/embodied/recap/run_recap_smoke.sh
```

It writes a mock dataset to `/tmp/recap_smoke/mock`, produces both sidecars, trains
a tiny value model, and runs the advantage-conditioned objective on a stand-in
policy. Nothing here needs a GPU, a pi0.5 checkpoint, or the PaliGemma tokenizer.

Each stage can also be run on its own against any LeRobot v2 rollout dataset:

```bash
export PYTHONPATH=$PWD
python -m loongforge.embodied.recap.cli.compute_returns    --dataset-path <ds> --config <cfg>
python -m loongforge.embodied.recap.cli.train_value        --dataset-path <ds> --output value.pt --config <cfg>
python -m loongforge.embodied.recap.cli.compute_advantages --dataset-path <ds> --value-checkpoint value.pt --config <cfg>
python -m loongforge.embodied.recap.cli.train_cfg          --dataset-path <ds> --config <cfg> --policy-backend mock
```

## Real value-critic training (multi-GPU)

Step 2 trains the state-value critic (SigLIP2-so400m + Gemma3-270M + Gemma-1
expert, 201-bin head). `RecapValueTrainer` is registered in `--trainer-type`, so
it runs under `torchrun` with DDP, the shared optimizer/scheduler, gradient
clipping, the NaN/loss-spike guards, checkpointing + `--resume`, and logging:

```bash
SIGLIP_PATH=/path/to/siglip2-so400m-patch14-224 \
GEMMA3_PATH=/path/to/gemma-3-270m \
DATA_PATH=/path/to/rollout_with_returns \
bash examples/embodied/recap/run_recap_value.sh
```

It writes `<output-dir>/critic.pt` in the RLinf format that
`compute_advantages --value-backend critic --value-checkpoint <that file>` loads,
plus LoongForge `checkpoints/` for resume. Defaults follow
the RECAP training recipe: micro batch 32, global batch 256 (8×32),
backbone lr 5e-5, value-head lr 1e-4 (`--lr-group value_head=1e-4`), constant
schedule, warmup 500, scheduler total 8000 steps, checkpoint every 3000, and
validation every 500 steps on the last `VAL_EPISODES` episodes (held out — set
`VAL_EPISODES=0` to train on everything).

The same trainer runs on one GPU (no `torchrun`) for a quick check, and
`--pretrained-checkpoint <critic.pt>` warm-starts from an existing critic.
`python -m loongforge.embodied.recap.cli.train_value --backend critic` is a thin
front-end for the same trainer (so it too gets grad clipping, the NaN guards and
validation), and becomes multi-GPU when launched under `torchrun`.

Useful flags for a quick multi-GPU sanity run:

- `--recap-max-episodes N` — limit the working set to the first N episodes;
- `--recap-val-episodes N` / `--recap-val-interval N` / `--recap-val-batches N` —
  held-out validation monitoring (`val_value_loss` / `val_value_mae`);
- `--recap-frame-cache-root <cache>` — read pre-decoded frames (see below);
- `--train-iters N` — steps, instead of `--epochs`.

## Real pi0.5 CFG training

```bash
CHECKPOINT_PATH=/path/to/checkpoint_A \
DATA_PATH=/path/to/rollout_from_A \
TOKENIZER_PATH=/path/to/paligemma-3b-pt-224 \
bash examples/embodied/recap/run_recap_cfg_pi05.sh
```

Under the hood this is just the stock trainer with a different trainer type:

```bash
torchrun ... loongforge/embodied/train.py \
    --model-name pi05 --trainer-type RecapCfgTrainer \
    --recap-config examples/embodied/recap/configs/recap_baseline.yaml \
    --dataset-path <rollout_with_advantages> --tokenizer-path <paligemma> \
    --pretrained-checkpoint <checkpoint_A> ...
```

`RecapCfgTrainer` builds a batch that carries the base / positive / negative
tokenisations plus the binary advantage indicator, and calls
`PI05Policy.compute_action_loss(batch, advantage_condition=...)`. The flow-matching
loss is unchanged — the advantage only selects the language condition.

The launch script defaults to the RECAP training recipe
hyperparameters: micro batch 32, global batch 512
(8 GPUs x 32 x 2), lr 1e-5, cosine schedule, warmup 5000, total 30000 steps,
min_lr 0.0, plus `model.gradient_checkpointing=true` and
`data.normalization_mode=mean_std`. Override any of them via the env vars at the
top of the script, e.g. `TRAIN_ITERS=50 LR_WARMUP_ITERS=10 bash run_recap_cfg_pi05.sh`
for a short run.

## Running on a real rollout dataset

Point the stages at an existing LeRobot v2 rollout dataset (with `is_success`).
Useful flags:

- `--max-episodes N` — limit to the first N episodes for a quick end-to-end check;
- `--dataset-compat {auto,none,legacy}` — external datasets sometimes use the
  pi0.5 column names (`state` / `actions` / `image` / `wrist_image`) instead of
  the canonical `observation.state` / `action` / `observation.images.*`. `auto`
  detects this from `meta/info.json` and applies the in-memory shim in
  `loongforge/embodied/recap/data/compat.py`; nothing on disk is rewritten;
- `--video-cache-size N` — episode-level decoded-frame cache (see below).

```bash
python -m loongforge.embodied.recap.cli.compute_returns    --dataset-path <ds> --preset paper_aligned --max-episodes 8
python -m loongforge.embodied.recap.cli.train_value        --dataset-path <ds> --output value.pt --preset paper_aligned --max-episodes 8
python -m loongforge.embodied.recap.cli.compute_advantages --dataset-path <ds> --value-checkpoint value.pt --preset paper_aligned --max-episodes 8
python -m loongforge.embodied.recap.cli.train_cfg          --dataset-path <ds> --preset paper_aligned --policy-backend mock
```

### Dataset statistics

External rollout datasets often ship without state/action normalization stats
(their `meta/stats.json` may only have `reward`/`return`). LeRobot pi0.5
checkpoints carry the stats inside the policy preprocessor, so they can be
extracted and merged into the dataset:

```bash
python -m loongforge.embodied.recap.cli.import_stats \
    --normalizer /path/to/pi05_ckpt \
    --dataset-path <ds> --mode mean_std
```

This writes `observation.state` / `action` (min/max/mean/std + q01/q99) into
`<ds>/meta/stats.json`. The checkpoint's own `normalization_mapping` decides the
mode; pass it to `data.normalization_mode` at training time (e.g.
`data.normalization_mode=mean_std`). Without stats the transforms silently skip
normalization, which is numerically wrong for real training.

### Video decoding

`LeRobotV2Dataset` opens the video container once **per requested frame** (~77 ms
on LIBERO 256x256 h264), which dominates training time under shuffled random
access (GPU utilisation ~0%). Two remedies, both in `RecapLeRobotV2Dataset`:

1. **In-memory episode cache** (`--video-cache-size`, default 1 episode): decode a
   whole episode in one container open and cache it. Helps episode-major access
   (value / advantage stages): 752 samples went from 8m25s to 11s (~46x).
2. **Offline frame cache** (recommended for CFG training, which shuffles):

```bash
# lossless (default) -- bit-identical to decoding the video
python -m loongforge.embodied.recap.cli.predecode \
    --dataset-path <ds> --output <cache> --format png --workers 32 --verify 200

# lossy, ~3.7x smaller than png (max per-pixel error is non-trivial -- see below)
python -m loongforge.embodied.recap.cli.predecode \
    --dataset-path <ds> --output <cache> --format jpeg --quality 95
```

Then point any stage (or `--recap-frame-cache-root` for `RecapCfgTrainer`) at it
with `--frame-cache-root <cache>`. The cache stores the **raw native-resolution
frames** — resize/normalisation stay in the runtime pipeline — so the observation
distribution is unchanged by construction. Verified: PNG cache reproduces the
video path's value checkpoint bit-identically; JPEG differs on every frame
(max|d| up to ~89/255 at q95).

## Profiles

| Profile | Reward | Discount | Value | Advantage | Policy |
|---|---|---|---|---|---|
| `baseline` | step=-1, success=0, failure=**-300** | **no** (γ=1.0) | scalar MLP | N=10, top-30% positive | positive-only conditional + `unconditional_prob=0.1` |
| `paper_aligned` | success=0 / fail=-1 / step=-1 | **no** | 201-bin distributional | N=50, task-dependent, top-50% | base + conditioned + dropout |

`baseline` uses the values documented in the RECAP training recipe.
`positive_quantile` uses the top-q convention: the **top q fraction** is marked
positive, i.e. the threshold is the `1 - q` quantile.

The three reward constants are plain config fields (`reward.success_reward`,
`reward.failure_reward`, `reward.step_reward`) and can be overridden per run:

```bash
python -m loongforge.embodied.recap.cli.compute_returns --dataset-path <ds> \
    --preset paper_aligned --failure-reward -50
```

Note the paper leaves `C_fail` symbolic; only the training recipe pins it
to `-300`.

Both profiles are wired through the same code; only the config differs. A resolved
copy of the config is written to `<dataset>/meta/recap_config.yaml` by
`compute_returns` so every downstream stage is traceable.

## Scope and boundaries

Implemented (Stage 1):

- reward/return computation for both profiles, with parquet sidecars;
- scalar **and** distributional value model with a shared `predict_expected_value`;
- N-step advantage + task-dependent threshold + binarisation;
- base / positive / negative condition routing with advantage dropout;
- the `base + alpha * conditioned` objective;
- `RecapCfgTrainer` wired into `--trainer-type` for real pi0.5 training;
- `RecapValueTrainer` wired into `--trainer-type` for **multi-GPU** value-critic
  training (DDP, grad clipping, `--resume`, and an RLinf-format `critic.pt` export);
- an in-memory compatibility shim (`recap/data/compat.py`) for external datasets that
  use pi0.5-style column names;
- episode-level batched video decode in `RecapLeRobotV2Dataset` (~46x on real
  LIBERO rollouts): an episode's frames are decoded in a single container open
  through the batched backend API.

Not implemented (Stages 2-6 of the roadmap, and out of scope here):

- the LIBERO rollout collector and LeRobot export (assumed to exist);
- multi-round / reference-policy advantage beyond checkpoint A;
- the full π0.6 structure (FAST head, semantic subtask head) — see the RECAP design notes.

See the RECAP design notes and training recipe for the full roadmap and the
reference behaviour.
