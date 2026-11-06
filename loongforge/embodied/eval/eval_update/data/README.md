# eval_update/data — 原始结果归档

按 `03_testing.md` §6「记录规范」：文档只放摘要，原始记录放这里。

## 目录

| 目录 | 内容 | 对应文档 |
|---|---|---|
| `armA_short_944c5ca/` | baseline `944c5ca`，`adjust_bottle` × 50 集（8 进程 × 6–7 集） | `02_progress.md` §2.49 |
| `armA_long_944c5ca/` | baseline `944c5ca`，`open_microwave` × 50 集 | §2.49 |
| `armB_short/` | 加速版，`adjust_bottle` × 50 集（N=8、`c=1`、offline cache） | §2.49 |
| `armB_long/` | 加速版，`open_microwave` × 50 集 | §2.49 |
| `nsweep_N6/` … `nsweep_N12/` | N 扫描，`adjust_bottle`，`E=16N`、`c=2`、offline cache | §2.48 |
| `5task_N12/` | 5 任务 × 100 集，N=12、`c=5`、offline cache | §2.51 |
| `cache_oec160/` | 160-seed 专家缓存（N=6/8/10 用） | §2.47 |
| `cache_oec256/` | 256-seed 专家缓存（N=12 用） | §2.47 |
| `cache_oec5/` | 5 任务缓存（`adjust_bottle` 256 + 其余各 100） | §2.51 |

其余历史 run（`ab_*`、`t8_*`、`oec_*`、`tc_*`、`sched_*`、`val_*`、`earlystop`、`phaseb` 等）也按**同名目录**归档，`manifest.json` 里记录了 wall / N / units / ok。对应 `02_progress.md` §2.25–§2.47 的各小节。

每个目录含：

- `results.jsonl`：逐集记录（`episode_id` / `seed` / `success` / `steps` / `episode_time_sec` / `failure_reason`），由各 worker 的 `results.jsonl` 拼接而成
- `coordinator_result.json`：协调器汇总（`n_units` / `elapsed_sec` / `repair_rounds` / `failed_units` / `validation`）——Arm A 是单进程模式，无此文件
- `manifest.json`：该目录的关键配置摘要

缓存目录只有 `<task>.pkl`（`schema` / `task` / `n` / `seeds` / `episode_info`）+ `manifest.json`。

## 没有归档的内容

- `artifacts/`（`trace.json`、视频、官方 eval 日志）：单个 run 几十 MB，**未入库**；分析时的派生指标（rollout 时长、启动时长）已写进文档
- `worker_*/`：只保留合并后的 `results.jsonl`，不保留每 worker 目录结构

## 复现口径

所有 run 的完整配置在 `02_progress.md` 对应小节；关键固定项：8 × A800、`co_locate`、官方 per-task step limit、`sim_cpu_threads=1`、`chunk_execute_steps=30` + `skip_intra_chunk_obs`、`plan_order=lpt`、fp32、`offline_expert_check`（除 baseline）。
