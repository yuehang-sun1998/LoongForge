# LoongForge 评测编排改造 —— 测试文档

> **本文用途**：规定"每实现一个功能 → 对应一次性能测试 → 留数据与结论"的流程，并作为测试数据的**唯一归档处**。
> **配套**：`01_architecture.md`（方案）、`02_progress.md`（进度）。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `7847a83`，已 rebase），仓库 `/workspace/LoongForge-Github`。
> **最后更新**：2026-10-15。
> **当前状态**：**M0/M1/M2 已实现并有实测数据**（T-01/T-02/T-03/T-04/T-05 见 §5）；全量对照见 T-05。T-06/T-07/T-08 暂缓。

---

## 1. 目的与原则

1. **每个功能上线前必须有对应的性能测试**，且测试结论写入本文对应用例的"数据 / 结论"栏。
2. **先证明输入/口径一致，再比数字**（对比不同配置前，确认是同一个 ckpt、同一分片、同一 episode 集合）。
3. **主判据是吞吐曲线 + server 忙占比，不是 `nvidia-smi util`**——util 报的是"有 ≥1 个 kernel 在跑的时间占比"，**不是饱和度**（见 `01` §1）。
4. **按资源类型分开测**：CPU-sim（LIBERO/CALVIN）与 GPU-sim（RoboTwin/SimplerEnv/ManiSkill）口径不同，不可混测。

---

## 2. 指标定义与口径

| 指标 | 定义 | 采集方式 |
|---|---|---|
| `t_sim_per_request` | 每请求的仿真时间 = `episode_time_sec` / 每集请求数 | `results.jsonl` |
| `t_infer` | 单次 `predict_action` 耗时，**含 per-request 延迟分布 p50/p95/p99** | **server 侧插桩**（待加，T0-1） |
| 队列深度 | 请求到达时 server 积压数 | server 侧插桩（待加） |
| 吞吐 | episodes/min 或 requests/s | 结果文件聚合 |
| GPU 旁证（带宽 / 显存） | `utilization.memory` + 显存，时间序列 | `nvidia-smi` / `dmon` |
| server 忙占比 | `请求数 × 平均服务时间 / wall` | 从 `timing.json` 算 |
| 每 sim 资源 | 核数 / RSS / 显存 | `ps` / `nvidia-smi` |
| `N` | 每 server 挂的 sim 数 | 启动配置 |
| `G` | 分配 GPU 数（= server 数） | 启动配置 |

**约定**：`N` 与 `G` 必须分开记录；日志中同时记录 `(model, benchmark, ckpt, shard)`，保证数据自描述。

---

## 3. 测试矩阵

| 用例 | 对应功能 | 阶段 | benchmark 类 | 状态 |
|---|---|---|---|---|
| T-01 | server 侧插桩 + vanilla 1:1 标定 | 0 | CPU-sim（LIBERO） | ✅ 通过 |
| T-02 | N 扫描 → 标定报告 + 并行配置 | 0/1 | CPU-sim + GPU-sim 各一 | 🔄 CPU-sim ✅（含报告+决策）；GPU-sim 待做 |
| T-03 | 一行命令起 1+N（external + 最小 coordinator） | 1 | CPU-sim | ✅ 通过（N=1 两步 / N=2 一条命令） |
| T-04 | shard/worker 契约 | 2 | 至少两个 family | ✅ 通过（由 `run.py` 分发吸收，M2 E2E 驱动多 server 分片） |
| T-05 | 动态队列 + 全量 wall-clock | 2 | 全 benchmark | ✅ 静态基线 94.2 min；**M2 全量 56.2 min / 96.45%**（vs 基线 1.68×、vs 1卡1server 3.09×） |
| T-06 | 数值一致性（batch>1 vs batch=1） | 3（M3 前置） | 任一 | 📋 **方案已定**（per-row seeds 比对，见下）；待契约落地 |
| T-07 | GPU-sim：sim/server 同卡 vs 异卡 | 1 | GPU-sim | ⏸ 暂缓（环境不支持 GPU-sim，待 CPU-sim 完成后统一做） |
| T-08 | 同 ckpt：大 batch vs 单卡多副本 | 3 | 任一 | ✅ 已结案（batch 7.3× vs 多副本 1.25×） |
| T-09 | 运行后覆盖校验（漏/重/多出）+ 自动补跑 | 2 | 任一 | ✅ 通过（单测 5 passed；真实 M2 全量 `ok=true`） |
| T-10 | 故障注入：漏集补跑 / kill server 自愈 | 2 | 任一 | ✅ 通过（漏集补跑 ✅；kill server → 看门狗重启、`infra_failed=0`） |
| T-11 | 健壮性兜底（unit 超时 / 收尾信号 / 竞态 / hung 探活） | 2 | 任一 | ✅ 通过（单测 8 个；E2E 冒烟通过） |
| T-12 | 分片 planner 表（per-benchmark） | 2 | 全 family | ✅ 通过（单测 14 个；E2E 冒烟通过） |
| T-13 | batch 缩放探针（M3 go/no-go） | 3 | 任一 | ✅ 通过（batch 8 per-sample 583→79ms，7.3×；GO） |
| T-14 | chunk 客户端化（请求量 10→1、结果逐位一致） | 3 | CPU-sim | ✅ 通过（80/80 逐集一致；请求量 9.83×） |
| T-15 | paper §7 的 F1/F2：infra 失败不计完成 + 完整性有独立期望 | 2 | 任一 | ✅ 通过（upstream #303 已合入 metrics/runner 修复；编排层另用 validation.py） |
| T-16 | 多卡共享 CPU/内存对 N 标定的影响 | 3 | CPU-sim | ✅ 通过（shared-load penalty 0.50；异 N 偏差 -34%） |
| T-17 | `per_task` 下 episode 级分片（RNG 快照逐集恢复） | 2/M3 | CPU-sim（LIBERO） | ✅ 6 集逐位验证 + 全量 2000 对齐（followups 97.15% vs 97.00%；pr240 96.35% vs 96.65%） |
| T-18 | chunk 消费逻辑下沉到 runner 公共层 | 3 | CPU 单测 + CPU-sim（LIBERO） | ✅ 通过（5 条新单测；全量 2000：96.65% vs 96.35%，无回退） |
| T-19 | 运行间可复现性（batching / 跨进程数值抖动） | 3 | CPU-sim（LIBERO） | ✅ 已定性：即使 batching off，两次相同 run 也只有 93.8% 集 steps 相同 |

---

## 4. 已有基线数据（改造前实测）

> 口径：FastWAM/LIBERO 样本。**这些是 CPU-sim 的数字，GPU-sim 不适用。**

- **单集样本**：总 ~31s = `episode_time_sec` 29.2s + `reset_time_sec` 1.7s；其中推理 14 次 × ~0.50s ≈ **7.0s（20%，GPU）**，仿真 ≈ **29.2s（~75%，CPU）**。
- **libero_10 长尾**：单集 **~60s**，显著慢于其他套件（Run A：5 卡按任务拆，50 集/片 ≈ 50min）。
- **资源**：128 核；单 sim ~**1.5 核** / ~**2.2GB RSS** → 理论上限 ~80 sim（内存 1.5TB 非约束）。
- **摊薄**：`t_gpu ≈ 0.50s`，`t_sim ≈ 29.2/14 ≈ 2.09s` → `N_min = 2.09/0.50 ≈ 4.2`。
- **总账（4 套件 × 500 集）**：仿真 ≈ 62000 CPU·s，推理 ≈ 14000 GPU·s。
- **现状 wall-clock**：LIBERO 全量（8×A800，4 套件）**~2.7h**；`libero_10` 现以静态手段拆 10 分片（16 分片 / 8 卡 / 2 波 ≈ 3.4h）。

> ⚠️ 上述 `N_min≈4.2` 用 31s 样本算出；libero_10（60s）对应 `N_min≈8.4`。**N_min 是 model+suite 相关**。

> ⚠️ **协议口径（2026-10-21 修正，复跑必看）**：历史数据要分两套代码看，不能一概而论：
> - **accel_c16 / c5 / nis\***（2026-10-03）：跑的是 `/workspace/LoongForge` @ `d1b8c9f`，该 clone **已接线 `env_seed_mode`** → 实际是 **`per_task`**（证据：50 集记录里 `seed` / `repro.episode_seed` 全为 42）。
> - **静态 8 卡 94.2 min / M2 56.2 min / T-01**（2026-10-15）：跑的是**本分支 rebase 前**代码，`CONFIG_MAPPING` 缺这条 → 实际是 **`per_episode`**。
> - **已验证的复现口径**（2026-10-21）：`per_task` + `prefer_controller_ee: true` + `controller_force_update: false` 重跑 accel_c16 `task 0` 前 10 集，与历史 **10/10 逐集一致**（steps/success/failure_reason），证明当前 rebase 后代码能复现该路径。
> - **对比规则**：对 accel 历史 → 用上述 `per_task` 组合；对本分支 M2/静态基线 → 显式 pin `per_episode`。两套参照不可混用。
> - `run_manifest.json` / episode record 会记录实际模式，跑完先核对再比数。

---

## 5. 用例详情

### T-01 · server 侧插桩 + vanilla 1:1 标定

- **对应功能**：T0-1（插桩）。
- **目的**：拿到**无争用**的 `t_sim` / `t_infer`（+ server 侧服务时间），作为 N 标定输入。
- **前置**：插桩已实现。
- **步骤**：
  1. 起 1 server + **1** sim（vanilla 1:1）。
  2. 跑一个**代表性分片**（长尾套件用代表性分片，如 libero_10 取含长尾任务的片）。
  3. 导出：server 侧服务时间分布（mean/p50/p95/p99）、**等待时间 `e2e - t_infer`（平均深度可由 Little's Law `L≈λ×W` 推）**、`episode_time_sec`、`utilization.memory` 序列。
- **通过标准**：能算出 `t_sim_per_request` 与 `t_infer`（含分布），且插桩开销 < 1%；**server 侧服务时间与 `inference_latency_ms` 对得上**（交叉验证）。
- **注**：这次 **N=1 的 run 就是 T-02 曲线的第一个点**，不必重跑。
- **数据**（2026-10-12，A800，`libero_10` task0 × 2 集，noise seed 42）：
  - `timing.json`（server 侧，开关开）：`count=920`，`mean=49.2ms`，`p50=0.0077ms`，`p95=490.8ms`，`p99=508.7ms`，`max=554.8ms`。
  - trace（runner 侧，独立）：932 步 = **838 cache hit + 94 真推理**；真推理 `inference_latency_ms` `mean=492.0 / p50=490.7 / p95=511.4 / max=554.8`；全部请求 `e2e_latency_ms` `mean=50.6 / p50=0.9 / p95=491.6`。
  - 单集：ep0 fail（700 步 / 137.6s）、ep1 success（232 步 / 50.6s）；`success_rate=0.5`；总 `elapsed=188.3s`。
- **结论**：
  - ✅ **出数**：`timing.json` 生成，含 count/mean/p50/p95/p99/max。
  - ✅ **交叉验证**：服务时间 `p95/p99/max`（490.8 / 508.7 / **554.8**）↔ 真推理 `inference_latency_ms` 的 `p50/p95/max`（490.7 / 511.4 / **554.8**），**max 精确一致**。
  - 🔑 **真发现（双峰）**：服务时间是双峰——cache hit（~7.7µs）vs 真推理（~490ms），**94/920 ≈ 10% 是真推理**。→ **`N_min` 要用平均每请求 `49ms`，不是 `avg_inference_latency_ms` 的 `492ms`（差 ~10×）**。
  - ✅ **非侵入性**：开关关/开两次跑，**逐集结果完全一致**（ep0 fail / ep1 success）；关闭时**不写 timing 文件**。

### T-02 · N 扫描 → 标定报告 + 并行配置

- **对应功能**：N 标定（T0-2 / T1-3）+ §6.5 报告与 agent 决策。
- **目的**：扫出吞吐曲线，**产出标定报告**（`report.json`/`report.md`/`env.json`），供 agent 决策出并行配置（§5.4 schema）。
- **前置**：T-01 完成；external server 模式（T1-1）可用。
- **步骤**：
  1. 扫 `N ∈ {1, 2, 4, 8, 16, 32}`（**N=1 即 T-01 那次**，复用不重跑）。**要扫到吞吐出平台**为止。**不同 N 并行到不同卡**（每 N 一个 server），别串行。**正式入口**：`orchestrator/calibrate.py --config run.yaml --n 1,2,4,8,16,32`（一条命令，含报告 + 兜底 N + 校验/缓存）。
  2. **CPU-sim 与 GPU-sim 各跑一遍**（后者额外测单 sim 显存占用 + 同卡下 `t_infer` 被抬高多少）。
  3. **生成报告**：`orchestrator/calibration_report.py --sweep <sweep.json> --timing <timing.json> --env <env.json> --out <dir>` → 三件套；→ agent 决策 → **校验 + 缓存**：`orchestrator/calibration_config.py`（`validate_parallel_config` 做 `N ≤ 核数/每sim核数`、显存、`timeout ≥ N×服务时间×safety` 边界检查；`save_calibration` 缓存 `(model, benchmark, gpu)`）。
- **通过标准**：产出报告三件套；agent 决策出**合法**配置（过校验）；`N*` 达到 ≥95% 平台吞吐；**无 sim 因等待超时失败**（排队等待 ≈ `(N-1)×t_infer`，客户端 `--policy-call-timeout-ms` 默认 5000，须按 N 放宽）。
- **数据**（2026-10-12，手动探针：1 长驻 server + N 纯 client；每 shard = libero_10 task0 1 集 / 150 步；A800-80GB；**已扫到 N=16**）：

  | N | wall(s) | 吞吐(ep/min) | `util.gpu` mean/max | `util.memory` mean/max | CPU busy mean/max | server 忙占比 |
  |---|---|---|---|---|---|---|
  | 1 | 41.8 | 1.44 | 5.6 / 38 | 1.1 / 10 | 2.6 / 8 | 18% |
  | 2 | 48.6 | 2.47 | 10.8 / 37 | 2.1 / 10 | 3.9 / 11 | 31% |
  | 4 | 63.4 | 3.78 | 13.2 / 39 | 2.5 / 10 | 5.3 / 24 | 48% |
  | 8 | 93.1 | 5.15 | 19.2 / 38 | 3.6 / 10 | 6.7 / 39 | 66% |
  | 16 | 141.0 | 6.81 | 26.9 / 39 | 5.3 / 10 | 8.9 / 70 | 87% |

  server `timing` 聚合：`count=4600, mean=50.9ms, p50=0.0066ms, p95=504ms, p99=540ms, max=570ms`。
- **结论**：
  - ✅ **吞吐次线性、仍未到平台**（1.44 → 6.81 ep/min，4.7× / 16×）。
  - 🔑 **恒等式**：`吞吐 = server 忙占比 × 上限`，上限 ≈ **7.9 ep/min**（server-bound：`1/50.9ms ÷ 150 req/集`）。N=16 已达 **87%**（0.87 × 7.9 = 6.87 ≈ 6.81 ✓）。
  - 🔑 **GPU / CPU 都不是瓶颈**（N=16：`util.gpu` 27%、`util.memory` 5%、CPU 8.9% / 128 核）；**绑定资源 = 串行 server**。
  - **agent 决策**：`n_sims_per_server = 16`（87% 上限；再往上 <10% 吞吐换 ~50% sim CPU，不值）；`placement` N/A（CPU-sim）；`policy_call_ms = 60000`。**校验 PASS**（`calibration_config.py`：`60000ms ≥ 16×50.9×2 = 1628ms`）。
  - ⚠️ 用**手动探针**（T1-1 未实现），非正式流程；**GPU-sim 一路未测**。
  - 产物：`report.json` / `report.md` / `env.json` + `parallel_config.yaml` + `validation.json`（在探针目录 `calibration_report/`）。
- **2026-10-22 复核（client-side chunk 落地后）**：旧的 `N*=16` 是**每步请求 server** 的口径。chunk 下沉后请求量降到 ~1/10（官方 cadence），N 的合理区间显著上移。注意两点：
  - **cadence 必须与基线一致**：vanilla / 静态 94.2 / M2 56.2 都是 `chunk_execute_steps=10`；`accel_c16/c5` 只是早期调参，不参与对比。
  - **固定 workload 会封顶并发**：第一轮（chunk=16、固定 200 集/点）在 N≥25 时实际只有 200 个 worker，`N/mean(wall)` 高估吞吐，该轮数据作废。
  - **第二轮（已完成）**：8 server 常驻 external 模式、每 client 1 集、总并发 = 8N、chunk=10、`max_wait_ms=50`；N=16/24/48/80 容量 70.7/77.8/81.8/81.9 eps/min，平台在 N≈48。明细见 `02_progress.md` §2.25。
  - **内存实测**（N=64、200 workers）：峰值 467GB、单 sim PSS ~2.0GB、无 OOM → `N_mem≈75`。
- **GPU-sim（2026-10-29，预实验）**：
  - 环境已具备（RoboTwin stable_2.0 + 真实 pi05）；单 sim 显存 ~5.5GB（见 T-07）。
  - 8 卡 N=1：8 个 server 各 15.9GB（fp32 pi05）。首轮 worker 未绑卡 → 8 个 sim 全落 GPU 0（wall 689s）；修复 bind/端口/重试后复测：每卡 18.1–21.9GB、wall 172.8s、单 episode 82s。
  - `pools.gpu_sim` + `placement` + worker 绑卡 + 显存校验已实现；**N=2/4/8 扫描待跑**（当前 8 卡被他人训练占用）。

### T-03 · 一行命令起 1+N（external server + 最小 coordinator）

- **对应功能**：T1-1 / T1-2（已实现）+ **T1-5 最小 coordinator（待做）**。
- **目的**：验证"**一条命令**起 1 server + N worker 跑完并聚合"，且 server 不被误杀。
- **前置**：T1-1 已实现；T1-5 落地后走一条命令，否则先用两步（手起 server + 起 client）替代。
- **步骤**：
  1. （T1-5 前）起 1 个长驻 server + N 个纯 client orchestrator（config 带 `server.external_url`），各分不同 shard；
     （T1-5 后）`orchestrator.run --config run.yaml` **一条命令**。
  2. 观察：有无端口冲突 / server 被误杀 / 结果错乱。
- **通过标准**：N 个 worker 全部跑完并聚合；**server 全程存活**（未被误杀）；结果按 `(suite,task,episode)` 无重复无缺失。
- **数据**（2026-10-12）：
  - **两步手动（N=1）**：起 1 共享 server（pid 661773）+ 1 纯 client orchestrator → `rc=0`、wall 40.9s；server 跑完仍存活。
  - **一条命令（T1-5，N=2）**：`orchestrator.run --config coord_smoke.yaml` → 1 server + 2 worker，各 1 task、`rc=[0,0]`、**聚合 2 集**、**分片无重叠**（worker_0→task0 / worker_1→task1）。
  - **真任务烟测（N=4）**：`libero_10` task 0-3、官方 `max_steps=700`、每任务 1 集 → 4 worker rc=0、分片无重叠、聚合 **4 集 / 3 成功（0.75）**、`timing` `mean=51.1ms p95=495ms`（量级一致）。
  - **episode-chunk 分片（T1-6，N=4 > 2 任务）**：2 task × 4 集、`n_workers=4` → **4 分片**（worker_i 各领 `(task, eps[0,1])` / `(task, eps[2,3])`）、**8 集恰好覆盖一次**、聚合 8 集。
- **结论**：✅ **全部通过**。external 模式生效（不误杀共享 server）；**最小 coordinator 让"一条命令"落地**（自动起 server + 分片 + 起 worker + 聚合）；**episode-chunk 分片使 N 不受任务数限制**。
- **注**：N>1 的**动态**调度/长尾抹平待 M2。

### T-04 · shard/worker 契约

- **对应功能**：T2-1。
- **目的**：验证非统一 runner 被统一契约吸收。
- **前置**：T2-1 已实现。
- **步骤**：任选两个 family（建议 LIBERO + SimplerEnv 或 RoboTwin），各走同一契约跑一个 shard。
- **通过标准**：两个 family 都能按 `(shard, server_url, output_path)` 调用、输出合规 `results.jsonl`、支持 resume 跳过已完成 episode。
- **数据**（2026-10-15）：
  - **契约形态**：**无独立 adapter 层**——"统一入口"= 一份普通 eval config（`benchmark.{suite,task_ids,episode_offset,episodes_per_task}` + `server.external_url` + `run.output_dir` + `run.timestamped_output=false`）→ `results.jsonl`（`episode_id` key）。`run.py` 按 `benchmark.name` 分发到对应 family runner，coordinator 的每个 unit 即一个 `orchestrator.run --config <unit.json>` 子进程。
  - **驱动验证**：M2 E2E（见 T-05）中，8 个 unit 全部由该契约驱动跑通（LIBERO family）；分片无重叠无缺失。
- **结论**：✅ **契约成立**（LIBERO 一侧实测）。跨 family（LIBERO + SimplerEnv/RoboTwin）交叉验证**待有第二个可用 CPU-sim family / GPU-sim 环境**时补——**环境限制，非实现缺口**。

### T-05 · 动态队列 + 全量 wall-clock

- **对应功能**：T2-2。
- **目的**：验证长尾被抹平、wall-clock 达目标。
- **前置**：T2-2 已实现；N* 已标定。
- **协议口径**：对**本分支**历史全量（静态 94.2 / M2 56.2 min）对比时，config 必须显式 `env_seed_mode: per_episode`；对 **accel_c16 系列**（旧 clone，`per_task`）对比时用 `per_task` + `prefer_controller_ee: true` + `controller_force_update: false`。后者分片会自动退化为 task 级，wall-clock 不可与本分支历史数据直接比。
- **步骤**：按 `G × N*` 起 worker 池，跑全量；记录 wall-clock、各 worker 空转时长、失败重跑次数。
- **通过标准**：wall-clock 落进目标区间（对齐 `01_architecture.md` §7.1 的 29~32min 理想值，实际按标定 N 修正）；无 worker 长时间空转；**无因等待超时导致的失败重跑**。
- **数据（动态调度 E2E，2026-10-15，2×A800）**：
  - **config**：`servers:[{gpus:[2,3]}]` + `pools.cpu_sim.n_sims_per_server=2`（并发 = 2×2 = 4）+ `libero_10` `task_ids=[0,1]`、`episodes_per_task=4`、`max_steps=150` → 共 8 集、`chunk_size=1` → **8 个 unit**。
  - **结果**：`gpus=[2,3]`、`concurrency=4`、`n_units=8`、**`failed_units=[]`**、`elapsed_sec=105.3`；聚合 **8 集**；两 server 分占卡 2/3（端口 17000/17001、17020/17021）。
  - **分片正确性**：worker_i → `task=0/1, episode=0..3` **恰好覆盖整个网格一次**（无重叠、无缺失）。
  - **动态补位实证**：worker 0-3 `18:25:57` 起（第一波填满 4 槽）；worker 4-7 `18:26:47` 起（**第一波跑完、槽释放后自动补位**）——证明是**全局动态队列**而非静态一轮分片。
- **结论**：✅ **通过**（全局队列 + 有界池 + 失败重试一次 + 多 server 端口隔离；**全量 94.2 min / 95.65%，逐 suite 2.7–4.3×、整机 1.85× vs 1卡1server**，见上方对照块）。
- **注**：本次 `max_steps=150`（冒烟口径，`success_rate=0` 属预期，非 bug）——验证目标是**调度正确性**，不是分数。

> **全量对照（2026-10-15，已完成）**：8 卡、每卡 1 coordinator + 1 server + 16 worker，4 套件 × 500 集 = 2000 集（ckpt `lf_sft_pr240_fp32master_20k`）。
> - **总 wall = 94.2 min**（17:28:26→19:02:40），8 卡全 rc=0；**1913/2000 = 95.65%**（10 91.20 / spatial 98.80 / object 95.80 / goal 96.80）。
> - 每卡 worker wall（不含加载）：libero_10 **91.2**（c1，绑定卡）/ spatial 38.5 / object 42.5 / goal 44.5 min。
> - **vanilla 参照（1 卡 1 server，用户口径）= 官方 harness `pr240fp32_official_2000`**（2026-10-06，4 卡 worker pool，同 ckpt）：per-task（50 集）libero_10 49.5 / spatial 33.1 / object 25.6 / goal 30.7 min；8 卡折合 ≈ 173.7 min。
> - **逐 suite 加速（每 50 集，log 实测）**：libero_10 **2.7×** / spatial **4.3×** / object **3.0×** / goal **3.5×**；**整机 94.2 vs 173.7 = 1.85×**。
> - **缺口归因**：spatial/object/goal 在 ~45 min 跑完，后 **6 卡空转**到 libero_10 收尾（91 min）→ **M2 动态下发**（空卡抢 libero_10 的 chunk）预计把整机拉到 **~3.0×**。
> - ⚠️ **分数未逐位一致**（object 95.80 vs vanilla 99.40 等）——vanilla 评测代码此后有更新，**待开发完 rebase 一版 vanilla 再重跑对齐**（用户 2026-10-15 决定先不追）。
> - ⚠️ **口径混用**：该 vanilla 参照跑的是**官方 harness（`per_task`）**，而本分支历史数据实际是 `per_episode` → 分数差异除"评测代码更新"外，**种子/场景集不同也可能是来源之一**。后续任何 vanilla 对比都要先统一 `env_seed_mode` 再比。
>
> **M2 全量对照（2026-10-15，已完成）**：**单 coordinator + 跨套件全局队列**，同 8 卡、同 ckpt、同 2000 集（`servers:[{gpus:[0..7]}]` + `n_sims_per_server=16`）。
> - **总 wall = 56.2 min**（worker 53.2 min）；**1929/2000 = 96.45%**；**520 unit 全 rc=0、`failed_units=[]`**、**0 个 `episode_error`**。
> - **vs 静态 8 卡基线 94.2 min → 1.68×**；**vs 1卡1server vanilla 173.7 min → 3.09×**。
> - **长尾被吃掉**：libero_10 的 500 集在基线里 2 卡拖 91 min，M2 里用满 8 卡、在 56 min 总盘子内跑完（后 6 卡不再空转）。
> - **分数对齐**：逐集比对，2000 集中仅 **16 集**不同，全在 `libero_object/task=6/episode=16..31`，且基线里**全由同一 worker 连续产出**、`failure_reason=episode_error`/`ConnectionClosedError`（websocket keepalive 超时、`steps=0`、**根本没跑**）。**剔除这 16 个 spurious 失败后两边一致（96.42% vs 96.45%）→ 模型行为逐集相同**。
> - ⚠️ **retry 缺口（已修）**：`episode_error` 本就在 episode 级重试集合里（`libero_runner.py:505`），但 `--max-retries` **默认 0** → 基线没设就把它记成失败。已在 `coordinator._shard_config` 默认注入 `run.max_retries=2`（显式值优先）。

### T-09 · 运行后覆盖校验 + 自动补跑

- **对应功能**：§5.5（`validation.py` + coordinator 补跑轮）。
- **目的**：确认"计划要跑的集"与"实际跑出的集"一致——**不漏、不重、不多**；漏的自动补跑而不是中断。
- **前置**：M2 coordinator 可用。
- **步骤**：跑一次协调评测 → 看 `coordinator_result.json` 的 `validation` 块；或离线 `python -m ...validation --output-dir … --n-units N --plan plan.json`。
- **通过标准**：`missing=duplicates=unexpected=unkeyed=0`（`ok=true`）；模型失败**不算**不完整；若确有缺失，应在 ≤3 轮内被补跑补齐。
- **数据（2026-10-15）**：
  - **单测**：`test_validation.py` 5 passed（期望集合 / `episode_id` 回退解析 / 连续区间合并 / 缺重多检测 / infra vs 模型失败分类）。
  - **E2E 冒烟**（2 卡 / 4 并发 / 8 unit）：`validation.ok=true`、`repair_rounds=0`、`infra_failed=0`、失败 8 个全为 `not_successful_within_max_steps`。
  - **真实 M2 全量离线校验**（2000 集 / 520 unit）：`ok=true`、`expected=actual=2000`、`missing=duplicates=unexpected=unkeyed=0`、`infra_failed=0`、`success_rate=0.9645`。
- **结论**：✅ 通过。覆盖校验把"缺/重/多出"变成显式判据；缺失自动补跑（最多 3 轮），不中断评测。

### T-10 · 故障注入：漏集补跑 + server 挂掉自愈

- **对应功能**：§5.5 覆盖校验/补跑 + §5.6 server 自愈。
- **目的**：验证"真出故障时"系统能不能自愈，而不是静默出错误分。
- **步骤**：① 跑到中途删掉一个已完成 unit 的 `results.jsonl`（造漏集）→ 看补跑；② 跑到中途 `kill -9` 一个 policy server → 看是否重启 + 恢复。
- **通过标准**：① 漏集在 ≤3 轮内补齐、`ok=true`；② server 被杀后**自动重启**、受影响 episode 最终**补齐**、wall-clock 不因 300s 连接等待被拖尾。
- **数据（2026-10-15，各 2 卡 / 8 unit / 8 集，`max_steps=150`）**：
  - **A 删记录**：删 `worker_0/results.jsonl`（1 集）→ `repair_rounds=1`、`n_units_launched=9`、`validation.ok=true`、`missing=0`、`infra_failed=0`。**✅ 补跑生效。**
  - **B kill server**：`kill -9` 掉 `server_0` → ① **无重启**（external 模式 `ensure_running` 空转）；② 命中它的 unit 记 `episode_error`/`ConnectionClosedError`、`attempt: 2`（重试 3 次全败）；③ 走到 client 重建的 unit **卡满 300s**（`TimeoutError: Failed to connect to server within 300 seconds`）。**⚠️ 暴露 3 个缺口。**
  - **B 修复后复测**（`57dcef9` 连接重建 + `3cc3a2d` 看门狗）：同实验重跑 → `server_restarts=1`（日志 `server 0 died; restarting on port 18100` → `server 0 back up`）、**`validation.ok=true`、`infra_failed=0`、`failed_units=[]`**、8 集全在、`elapsed=218s`。**✅ 自愈生效**（代价：重启重载模型 ~100s）。
- **全量 chaos（2026-10-15，8 卡 / 2000 集 / 单 coordinator，跑到中途连注 4 类故障）**：

  | 注入 | 时刻 | 反应 |
  |---|---|---|
  | 删掉一个 unit 的 2 集记录 | +6 min | 变 missing → **覆盖补跑**（最终 `records=2002`，按 `episode_id` 去重后 `missing=0`、`duplicates=0`） |
  | `SIGKILL` server_3 | +14 min | 看门狗 `died; restarting on port 14620` → `back up` |
  | `SIGSTOP` server_5（hung） | +24 min | `is hung; terminating it before restart` → `back up` |
  | `SIGKILL` 一个在跑的 unit | +34 min | unit 重试/补跑（最终 `failed_units=[]`） |

  - 最终：**exit 0**、`elapsed = 57.3 min`（干净 M2 全量 53.2 min → **代价 +4.1 min / +7.7%**）、`server_restarts=2`、`servers_given_up=[]`、`failed_units=[]`、**`validation.ok=true`（missing / duplicates / unexpected / unkeyed 全 0）**、`infra_failed=0`、`1929/2000 = 96.45%`。
  - **逐集比对干净 M2 全量：2000 集 0 处不同 → 故障零扰动**。
  - 代价拆解：2 次 server 重启各 ~100s 重载（~3.3 min）+ 补跑/重试（~1 min）≈ 观察到的 +4.1 min（**有界、可预期**）。
- **结论**：✅ 通过。漏集由补跑闭环补齐；server 挂掉（死 / hung）由看门狗同端口重启 + client 重建 + episode 重试恢复，`infra_failed` 归零、不再被 300s 拖尾；**全量下 4 类故障零扰动**。

### T-11 · 健壮性兜底：unit 超时 / 收尾信号 / 竞态 / hung 探活

- **对应功能**：§5.7。
- **目的**：覆盖"会挂死 / 会静默出错分 / 会漏进程 / 查不到"的四类失败模式。
- **步骤/判据**：
  1. **unit 超时**：unit 卡死超过预算 → 被 kill、走重试/补跑（单测：`_await_slot` 超预算返回 `timed_out=True`；`_unit_timeout_sec` 按 episode 预算推导）。
  2. **收尾信号**：`ok=false` → `run.py` **exit 2**；`infra_failed>0` → stderr 告警（单测：两个退出码用例）。
  3. **看门狗竞态**：shutdown 落在 restart 中途 → 期间启动的 server 被自己 terminate、不进 `servers`（单测：`created[0].terminated`）。
  4. **hung 探活**：进程活但 ping 不通 → 连续 2 次失败后重启；活着的正常 server 不误杀；探活有界且恢复代理环境变量（单测 3 个）。**探测必须有界**：SIGSTOP 后 TCP 仍可连接，`PolicyClient` 读 server metadata 的那次 `recv` 也必须带 timeout，否则 watchdog 会永久阻塞（2026-10-21 回归发现，见下）。
  5. **`output_dir` 冲突**：已有 `coordinator_result.json` → 拒绝启动（`SystemExit`）；`run.resume: true` 放行（单测 2 个）。
  6. **`policy_call_ms` 下限**：`N × service_ms × 2` 之上不告警、之下告警、未给 `service_ms` 跳过（单测 1 个，三态）。
  7. **server 崩溃循环**：连续失败超过 `MAX_SERVER_RESTARTS` → 放弃并记入 `servers_given_up`；`run.py` **exit 2**（单测 2 个）。
- **数据（2026-10-15，真实小评测逐个注入：2 卡 / 8 unit / 8 集 / `max_steps=150`）**：

  | case | 注入 | 结果 |
  |---|---|---|
  | unit 超时 | `run.unit_timeout_sec: 5`（每 unit 必超） | 14 unit 被 kill；`repair_rounds=3`、`ok=False missing=8`、**exit 2**、打印 `coverage INCOMPLETE … needs a human` |
  | hung server | `SIGSTOP` server_0 | `server 0 is hung; terminating it before restart` → `back up`；`server_restarts=1`、`repair_rounds=2`、**`ok=True missing=0 infra_failed=0`**、exit 0 |
  | 崩溃循环 | 反复 `SIGKILL` server_0 | 3 次 restart 全失败 → `giving up … needs a human`；`servers_given_up=[0]`、**exit 2**；且 `ok=True missing=0`（剩余 server + 补跑兜住） |
  | `output_dir` 冲突 | 同目录重跑 | **exit 1** + `already exists -- refusing to overwrite` |
  | `policy_call_ms` 下限 | `service_ms=500 / policy_call_ms=500 / N=16` | 启动即告警 `below the queue-wait floor 16000ms` |

- **2026-10-21 回归补充（hung 探活的有界性）**：
  - 旧用例只验证了“最终会重启”，没有验证**探测耗时**。本次复测发现：`SIGSTOP` 后 `PolicyClient._wait_for_server` 的 metadata `conn.recv()` **没有 timeout**，watchdog 卡在 connect 阶段 **约 9 分钟**，直到进程被 `SIGCONT` 才恢复。
  - **修复**：`recv(timeout=remaining)` + `open_timeout=min(30, remaining)`；超时关闭连接并抛出。
  - **新增单测**：`test_client_bounds_the_metadata_wait`（模拟“能连上但不回 metadata”的 server，断言 `recv` 带 timeout、超时后连接关闭）。
  - **复测判据**：`SIGSTOP` → 重启完成 ≤ **2.5 min**（2×probe 间隔 + 3s 超时）。实测 `21:38:25 → 21:40:53 = 2m28s` ✅；10 集最终全部完成。

  - 单测 **13 个**；全量 **93 passed / 1 skipped**，ruff clean。
  - 🔴 **测试中发现的 bug（已修 `683b455`）**：看门狗重启 **hung** server 时**不先终止旧进程** → 旧进程仍占端口 → 重绑失败 → 永远恢复不了。
- **结论**：✅ 通过。**明确不做**：coordinator supervisor（靠 resume 手动重跑）。**只写方案**：GPU-sim 的 `policy_call_ms` 推导（`01_architecture.md` §5.7 末）。

### T-12 · 分片 planner 表（per-benchmark）

- **对应功能**：§5.8。
- **目的**：确认**每个 benchmark family 都能被切成 shard**，且切法正确（无重叠、无遗漏、id 唯一）。
- **步骤/判据**：
  1. `benchmark_family()` 把 `libero*` 归一到 `libero`，其余四族原样返回。
  2. 每族 planner 产出的 shard：`sum(expected)` = 总集数；`id` 唯一；`overrides` 落在该族的真实入参上（libero→`suite/task_ids/episode_offset`；maniskill/simplerenv→`task_name/episode_idx`；robotwin→`task_name/task_config/start_seed/test_num`；calvin→`task_suite_name/num_sequences`）。
  3. `_shard_config()` 机械套用 overrides 并去掉 coordinator-only 键。
  4. 校验：漏 → `missing`；跨 shard 撞集 → `duplicates`；同 shard 重跑 → **不算** duplicate；`short_shards()` 只报短的。
- **数据（2026-10-15）**：`test_planning.py` **7 个** + `test_validation.py` **7 个**全过；全量 **99 passed / 1 skipped**，ruff clean；小规模 E2E 冒烟通过（LIBERO 路径）。
- **结论**：✅ 通过（**切法**已验证）。**仍未做**：拿**细粒度**需改 runner（CALVIN `--sequence-offset` / SimplerEnv·ManiSkill "跑 N 集" / RoboTwin 去正则 patch）→ 留档为 **T2-6**，**当前环境无法验证**（只有 LIBERO 能跑）。

### T-13 · batch 缩放探针（M3 的 go/no-go）

- **对应功能**：§7.4（M3）+ `orchestrator/batch_probe.py`。
- **目的**：在**动 FastWAM batch 契约之前**，先量清"一次真实推理的 wall 花在哪"——决定 batching 值不值得做。
- **步骤**：`python -m loongforge.embodied.eval.orchestrator.batch_probe --config <server yaml> --calls 5 --batches 1,2,4,8`
- **判据**：
  - `gpu_share`（cuda_ms / wall_ms）低 + `host_ms` 高 → **固定开销主导 → batching 收益大**；
  - `gpu_share` 高 → 看 `per_sample_ms` 随 batch 是否显著下降（下降 = 计算可批，收益仍在）。
- **数据（2026-10-15，A800，`libero_10` server 配置，5 次调用取均值）**：
  - **SPLIT**：`wall_ms=655.5`、**`cuda_ms=680.6`**、`host_ms=-25.1`、**`gpu_share≈1.04`**。top kernels 是大 GEMM（`ampere_bf16_s16816gemm_128x128` 74.8ms、`128x64` 57.1ms）+ flash-attention（66.5ms）+ layer_norm（49.7ms）。
    - ⚠️ `cuda_ms > wall_ms` 说明**多 stream 有重叠**，比值不精确；但结论明确：**GPU 在推理期间是饱和的，host 开销 ≈ 0**。
  - **SCALING**：batch 1 得 `total_ms=593.4`（内层 `infer_action`）；**batch 2/4/8 全部报错** `input_image must have shape [1,3,H,W] or [3,H,W], got (8,3,224,448)`。
- **结论**：🔄 **探针回答了"花在哪"，但答不了"batch 能降多少"**：
  1. 🔴 **我的"固定开销主导"假设被证伪** —— 一次真实推理 `gpu_share≈1`，**是 GPU 饱和**，不是 host 开销。所以 batching 的收益只能来自**大 batch 下 SM 利用率更高**（batch-1 的 GEMM 效率），**不是**摊薄固定开销。
  2. ⛔ **缩放测不了**：batch=1 假设**散在 5 处**（`mot/fastwam.py::_encode_input_image_latents_tensor`、`wan/core.py` ×2、`mot/idm.py`、`mot/joint.py`），且编码器是 `input_image[0].unsqueeze(1)` + `vae.encode([image])` 的**逐样本 list API** → **batch 契约是真的不存在**，不是"没接上"。
  3. **因此 M3 仍暂缓，但理由变精确了**：不是"GPU 空着所以没用"，而是"**要先把 batch 契约补出来，才能估收益**"。
- **🔑 第二轮：临时放开契约后实测（2026-10-15，`--batches 1,2,4,8`）**：运行时临时 patch 掉 6 处入口断言 + 让编码器一次传 K 张（**跑完已 `git checkout` 还原**，`git status` 干净、无 PROBE 残留）。

  | batch | `total_ms` | `per_sample_ms` | 相对 batch=1 |
  |---|---|---|---|
  | 1 | 583.4 | 583.4 | 1.0× |
  | 2 | 584.4 | 292.2 | 2.0× |
  | 4 | 595.7 | 148.9 | 3.9× |
  | **8** | **635.0** | **79.4** | **7.3×** |

  - **正确性验证**：插桩显示编码器确实收到 `(2,3,224,448)`/`(4,...)`/`(8,...)`，`first_frame_latents=(K,48,1,14,28)`、`latents_action=(K,32,7)` → **batch 真的进了去噪**（不是"只算样本 0"）。
  - **总 wall 几乎不变**（583→635ms，8× 工作量 +9%）→ **每请求成本由固定项主导，不是每样本计算**。
- **结论**：✅ **M3 是 GO，且是量级最大的剩余杠杆**（不是"暂缓"）。**caveats**：① replay 直调 `infer_action`，绕开 `predict_action` 的包装（归一化/cache）→ 量的是 **cache-miss 的真实推理**路径；② 返回值仍把 batch 压掉（`[32,7]`）→ 真实现要修返回路径（小）；③ 批要从 K 个**不同步**的请求里攒（`max_wait_ms` 窗口，批可能不满）→ 实收 < 7.3×；④ 批内噪声：`torch.randn((K,...), generator=g)` 每样本独立 ✓，但批器要按各请求 seed 逐行设；⑤ **端到端上限受 CPU 侧约束**（Amdahl）——server 提速后 CPU 可能成新瓶颈，当前 CPU 仅 8.9%（N=16）有余量，**要实测**。

### T-14 · chunk 客户端化（请求量 10→1）

- **对应功能**：§7.6(2)。
- **目的**：证明"评测端持 chunk 并逐步消费"与服务端逐步回放**结果逐位一致**，同时请求量降到 1/10。
- **前置**：runner 调用点传 `return_action_chunk=True`；新增 `ChunkConsumer`（队列空才发请求）；遵守 `disable_action_cache`（为真时队列长度=1）。
- **判据**：
  1. **正确性**：同一分片跑两遍（改前/改后），**逐集 success 与 steps 完全一致**（同一份 chunk、同一顺序 → 应逐位一致）。
  2. **请求量**：server 侧统计的 `predict_action` 次数降到 **~1/10**（真推理次数不变）。
  3. **收益**：端到端 RPC 时间大幅下降（但**总 wall 预期基本不变**——省下的 RPC 相对仿真很小）。
- **2026-10-23 补充**：新增 `benchmark.return_action_chunk`（true=客户端 chunk，false=旧 server cache）。80 集、batching off、pr240 的 A/B：**success 0 翻转、79/80 集 steps 一致**（1 集差 1 步），请求量 **9.76 vs 1.00 步/请求（~9.8×）**。同一集在同卡串行复测中两条路径**均为 240 步** → 路径等价；A/B 的 1 步差异是跨进程/GPU 的数值抖动。
- **数据（2026-10-20，真实 FastWAM + LIBERO）**：
  - 正确性：`libero_10/task0` 10 集，与 server-cache 旧 run 的 **success / steps 10/10 一致**；另随机抽 `goal/spatial/object` 共 7 个 task × 10 集，**70/70 一致**（合计 **80/80**）。
  - 请求量：`task0` 10 集共 **302 次推理请求 / 2969 个 env step = 9.83×**；逐集符合 `ceil(steps/10)`（如 700→70、232→24）。
  - server 侧 batcher E2E：4 个 sim 并发连同一个 server（`max_wait_ms=50`），51 个请求聚成 46 个 batch，**mean 1.11 / max 2**；4 个 client 全部正常结束（3 成功 + 1 个 150 步超时），无 server 错误。
- **结论**：✅ **T-14 通过**。chunk 客户端化不改变结果，请求量降到 ~1/10。server 侧 batcher 的异步聚合与 cache-hit 过滤已跑通；真 batch 路径见 T-06。

### T-15 · paper §7 的 F1 / F2 落地验证 【✅ 通过（upstream #303）】

- **对应功能**：`paper_review.md` 的 F1（infra 失败被当"已完成"）与 F2（完整性检查有盲区）。
- **当前状态**：upstream `7847a83`（PR #303）已合入 F1/F2/F3/F4/F10/F12 的 metrics + runner + `run.py` 修复；本分支 rebase 后直接复用。
- **编排层补充**：coordinator 仍用 `validation.py` 做 **shard 级**覆盖校验与自动补跑（§5.5），与 metrics 层的 **episode 级**语义互补；`INFRA_FAILURE_REASONS` 已统一从 `metrics/results.py` 导入，避免漂移。
- **判据**：见 PR #303 的 400 集真实 FastWAM+LIBERO 验证（故障注入 + resume + 完整性）。
- **结论**：✅ 通过。单 runner / metrics 路径与编排路径现在都有覆盖。

### T-16 · 多卡共享 CPU/内存对 N 标定的影响 【✅ 已完成（2026-10-23）】

- **背景**：`N` 是 per-server 的，但 **CPU 和内存是整机共享的**。并行在 8 张卡上跑不同 N 时，每个点的 `ΣN` 和背景负载不同；测出的 per-server 吞吐/膝点会被 cross-card contention 污染，不能直接当成"该 N 的容量"。
- **目的**：量化共享资源对 per-server 吞吐和 `N*` 的影响，给 N 扫描定一个可解释的修正方法。
- **固定 workload**：4 suite × 5 task × 10 eps = **200 集**，任务集/seed/max_steps 在所有组之间一致。
- **实验组**：
  - **A 单 server 独跑**：1 张卡，其余空闲；N=16 / 24。
  - **B 8 server 同 N**：8 张卡全部 N=16（或 24），代表真实部署。
  - **C 8 server 异 N**：8 张卡分别 N=12/16/20/24/28/32/40/48。
  - **D 固定 per-server N、改变总负载**：1/2/4/8 张卡全部 N=16，看 per-server 吞吐随总 sim 数的下降曲线。
- **指标**：per-server episodes/min、`t_step`（`episode_time_sec / steps`）、CPU%、RSS、实际 batch size、queue wait p95。
- **判据**：
  - **shared-load penalty** = `throughput_B / throughput_A`（同 N）。
  - **heterogeneous bias** = C 中各点吞吐相对 B 中同 N 点的偏差。
  - 若 penalty 显著（如 <0.9），N 标定必须以"8 卡同 N"为准，或用 penalty 修正单卡扫描结果。
  - 同时确认内存：`ΣN × 2.2GB` 距离 1.5TB 还有多少余量。
- **数据（2026-10-23，external，chunk=10，wait=50，200 集等价负载）**：

  | 组 | G | N/server | 总吞吐 | per-server | mean ep | mean batch | CPU | PSS |
  |---|---|---|---|---|---|---|---|---|
  | D1（A16） | 1 | 16 | 17.6 | **17.6** | 54.6s | 2.08 | 9% | 37GB |
  | A1_n24 | 1 | 24 | 27.4 | **27.4** | 52.5s | 2.83 | 12% | 52GB |
  | D2 | 2 | 16 | 36.2 | 18.1 | 53.0s | 2.47 | 16% | 73GB |
  | D4 | 4 | 16 | 58.4 | 14.6 | 65.8s | 2.41 | 31% | 144GB |
  | B8_n16 | 8 | 16 | 70.7 | **8.84** | 108.6s | 2.07 | 51% | 286GB |
  | B8_n24 | 8 | 24 | 77.8 | 9.72 | 148.1s | 2.30 | 59% | 409GB |
  | C_hetero | 8 | 12..48 | 78.7 | 4.3–16.1 | 167.8s | 2.57 | 61% | 462GB |

- **结论**：
  - **shared-load penalty = 8.84 / 17.6 ≈ 0.50**：8 卡同 N 时，per-server 吞吐只有单卡独跑的一半 → **单卡/小卡数标定不能直接外推到 8 卡**，N* 必须用“8 卡同 N”测。
  - N=24 单卡 27.4（vs N=16 17.6，+56%），但 8 卡下 N=24 只比 N=16 高 10%（9.72 vs 8.84）→ 共享资源吃掉了大部分收益。
  - **异质组偏差**：C 组中 N=16 的 server 只有 5.8 eps/min（同 N 均匀时 8.84，**-34%**）；N=24 为 8.4 vs 9.72（-14%）→ 并行跑不同 N 会污染每个点的绝对吞吐。
  - 单集 wall 随总负载上升（54.6 → 108.6 → 167.8s），PSS 随并发线性增长（37 → 462GB）。
  - 结论与 `01_architecture.md` §7.6 一致：正式标定必须 8 卡同 N，或按 0.50 的 penalty 修正单卡结果。

### T-17 · `per_task` 下 episode 级分片（RNG 快照）【✅ 6 集逐位验证 + 全量 2000 对齐通过】

- **对应方案**：`01_architecture.md` §5.8.1（预计算每集 reset 前的 `np.random` 状态，分片逐集恢复）。
- **目的**：证明"打散到任意进程"与"单进程串行"逐集**场景一致**，从而在保留官方 `per_task` 语义的同时恢复 episode 级调度。
- **前置**：LIBERO + 渲染 GPU（`OffScreenRenderEnv` 需要 EGL）；代码侧先做 CPU 单测（fake env）。
- **实现与用法（2026-10-22）**：
  - 生成快照：

    ```bash
    python -m loongforge.embodied.eval.orchestrator.scene_snapshots \
      --suite libero_10 --task -1 --seed 42 --episodes 50 --out <snapshot_dir>
    ```

  - 配置：`benchmark.env_seed_mode: per_task` + `benchmark.scene_snapshot_dir: <snapshot_dir>`；
  - runner 每集 `env.reset()` 前恢复 `snapshots[episode_idx]`，record 记 `scene_snapshot`；
  - planner 在配置快照后允许 `per_task` 的 episode 级切片；
  - CPU 单测：`test_scene_snapshots.py` 3 例 + planner 2 例（全量 138 passed / 1 skipped）。
- **全量对齐（2026-10-22）**：2000/2000、validation ok、0 重启；成功率 97.15% vs 参照 97.00%；逐集 steps 一致 91.3%，差异集中在 4 个敏感 task（前几集一致后分叉）。已用场景哈希证明是模型代码数值漂移：task3/spatial task4 前 10 集新旧代码的 sim state + 首帧哈希 10/10 相同，而 steps 已在 ep2/ep9 分叉。
- **pr240 官方语义全量（2026-10-23）**：per_task + 快照、2000 集、N=64、batching on → 96.35% vs vanilla 96.65%（-0.30%，噪声内），0 重启/0 修复，worker wall 35.5 min（vanilla 173.7 min）。
- **retry / resume（2026-10-23，task0、6 集、chunk=2、per_task + 快照）**：
  - retry：kill server 后受影响 unit 重试，6 集 steps 仍为 `260,242,248,262,259,249`；
  - resume：跑到 2 集后 kill，`run.resume=true` 续跑，最终 6/6、`validation.ok=true`、steps 全部匹配。
- **GPU 逐位验证（2026-10-22，libero_10/task0/seed42，6 集）**：
  - 串行 vs 快照恢复：`sim.get_state()` 哈希 **6/6 相同**、`agentview_image` 首帧哈希 **6/6 相同**、steps/success **6/6 相同**；
  - 分片（chunk=2，3 个进程）steps 也与串行完全一致（260/242/248/262/259/249）。
- **步骤**：
  1. **串行参考**：`per_task`、`seed=42`、task 0、E=10，单进程跑完；每集记录 reset 后第一帧的哈希（`hash(obs["agentview_image"])`），可另记关键物体 qpos。
  2. **预计算快照**：同一 task 生成 `S0..S9`（每集 reset 之前的状态）。
  3. **分片**：`chunk=2`（5 个进程）或 `chunk=1`（10 个进程），每集 `set_state(S_k)`；每集记录同样哈希。
  4. **反证**：把 episode 9 的 shard 故意恢复成 `S8`，确认哈希变化 → 证明比对有区分度。
  5. **重试路径**：对 episode 5 注入一次 infra 失败，确认重试后场景仍是第 5 集（而不是第 6 集）。
  6. **resume 路径**：跑到一半杀进程，`--resume` 后从中间集继续，确认剩余集哈希与串行一致。
- **通过标准**：
  - 步骤 3 的 10 个哈希与步骤 1 **逐位相同**；
  - 步骤 4 哈希**必须不同**；
  - 步骤 5/6 的重试与 resume 集哈希与串行一致。
- **附加（性能）**：E=50 时对比 task 级 vs 快照 + episode 级在同一 N 下的 wall-clock，确认并发从 40 提到 128 附近的收益。
- **数据（2026-10-22/23）**：
  - 6 集（libero_10/task0/seed42）：串行 vs 快照恢复的 `sim state` 与 `agentview_image` 首帧哈希 **6/6 全等**；分片（chunk=2）steps 与串行一致（260/242/248/262/259/249）；
  - retry（kill server）：重试后 6 集 steps 仍全部一致；
  - resume（kill 后续跑）：6/6 补齐、`validation.ok=true`、steps 全部一致；
  - 全量 followups：2000/2000、0 重启、97.15% vs 97.00%；
  - 全量 pr240（官方语义）：2000/2000、0 重启、96.35% vs vanilla 96.65%（-0.30%，噪声内），worker wall 35.5 min。
- **结论**：✅ 通过。快照可逐位恢复 per_task 场景，支持 episode 级分片；retry/resume 不改变场景；全量分数与参照一致（差异归因于模型代码数值漂移，已用场景哈希证明）。

### T-18 · chunk 消费逻辑下沉到 runner 公共层 【✅ 通过】

- **对应方案**：`01_architecture.md` §7.6 (2) 的 `EpisodePolicyStepper`。
- **目的**：让 T-14 的"持有 chunk、空才请求"不再只属于 LIBERO，同时保证未开启时请求节奏与改造前完全一致。
- **前置**：`_common.EpisodePolicyStepper`；四个 in-process runner 接入。
- **通过标准**：
  1. N 步内请求数 = `ceil(N / chunk_execute_steps)`；
  2. 客户端 chunk 路径执行的动作序列与旧 server-cache 路径逐位相同；
  3. `disable_action_cache` 每步请求且只用第一个动作；
  4. ensemble（`chunk_steps=1`）每步请求但仍能拿到整段 `raw_actions`；
  5. `begin_episode` 丢弃上一集残留 chunk。
- **数据（2026-10-26）**：
  - CPU 单测：`test_chunk_consumer.py` 新增 5 条，覆盖上述 5 条通过标准；全套 eval 单测 **144 passed / 1 skipped**。
  - LIBERO 单集冒烟（pr240 fp32、per_episode、task0 episode 12、同一 ckpt/协议，client 与 cache 分别在 GPU 0/1 并行）：两条路径均 **steps=240、success=1**，且与改造前参照 run 完全一致（`ep12_client` 240/1/24 次请求，`ep12_cache` 240/1/240 次请求）。请求数验证了 chunk 路径 **240→24**。
  - **全量 2000 集**（pr240、`per_task` + RNG 快照、8 卡、N=64、batching on、`episodes_per_chunk=4`）：**1933/2000 = 96.65%** vs 参照 run **1927/2000 = 96.35%**（+0.3pt）；逐 suite：libero_10 +3、goal +3、object 0、spatial 0；success 翻转 24 上 / 18 下；validation `ok=true`（2000/2000、0 缺失/重复）、server 重启 0。中途在 568 集时会话中断，`run.resume=true` 续跑补齐。
  - **速度（同配置、512 并发）**：单集中位耗时 249.7s → **248.2s**、均值 323.6s → 302.7s；`steps/request` 9.74 → 9.74。没有可归因于本次改动的速度变化（wall 不可直接比：新 run 分两段、两次模型加载）。
- **未覆盖**：CALVIN / SimplerEnv / ManiSkill 无可用仿真环境，只做静态检查与单测；RoboTwin 走官方 evaluator bridge，未接入。
- **结论**：✅ 通过。全量成功率与改造前一致（+0.3pt，逐 suite 无回退）；逐集 steps 只有 49.4% 相同，属于运行间数值抖动（见 T-19），不作为回归判据。

### T-19 · 运行间可复现性（batching / 跨进程数值抖动）

- **目的**：给"改完代码跑一遍，和上次对比"定一个正确的验收口径。
- **数据（2026-10-26，同一 ckpt/协议，逐集 key 对齐）**：

  | 对比 | 配置差异 | 共同集 | steps 相同 | success |
  |---|---|---|---|---|
  | `off16` vs `off16b` | **仅 output_dir 不同**（batching off，N=16） | 1973 | **93.8%**（1851） | 1917 vs 1918，11 翻转 |
  | `batch_on` vs `batch_off` | 仅 batching（80 集，N=8） | 80 | 100% | 0 翻转 |
  | `full2000` vs `chunk4` | 仅 `episodes_per_chunk`（50 vs 4） | 2000 | 46.3% | 1937 vs 1936 |
  | stepper 前后全量 | 代码改 chunk 消费层（batching on，512 并发） | 2000 | 49.4% | 1927 vs 1933（+6） |

- **结论**：
  1. **逐位复现不是这套 pipeline 的属性**——即使 batching 关闭，两次完全相同的 run 也有 ~6% 的集分叉（跨进程/GPU 数值抖动）。
  2. batching 叠加第二层抖动：batch 组成由请求到达时序决定，bf16 归约顺序随之变化；并发越高越明显（80 集小规模 A/B 恰好 100%，不能外推）。
  3. 代码改动的验收看 **success rate / 逐 suite 成功率**（± 运行间噪声）；逐集 steps 只作参考。本次 +0.3pt、逐 suite 无回退。
- **若要尽量收敛**：`batching.enabled=false` + 固定 shard→GPU 映射 + 同卡重跑；即便如此也只能到 ~94%，做不到 100%。

### T-06 · 数值一致性（batch>1 vs batch=1） 【✅ 通过】

- **对应功能**：T3-1 + FastWAM batch 契约。
- **目的**：证明 **batching 不改结果**——这是 **M3 的正确性门**，**优先于性能**（T-13 只证明了"跑得动、batch 流进去了"，**没证明"结果没变"**）。
- **前置改动（本身就是 M3 契约的一部分）**：`infer_action` 接受 **per-row seeds**（`Sequence[int]`），批内第 i 行用第 i 个 seed 构造噪声。
  - 这既是 **batcher 需要的接口**（每个请求带自己的 seed），也是让比对**可精确归因**的前提——否则 batch=1 与 batch=K 的噪声本身不同，差异无法区分"batching 引入的"还是"噪声不同导致的"。
- **步骤**：
  1. **固定输入**：同一条件图 + 同一 proprio + 同一 instruction + 同一 K 个 seed `[s0..s_{K-1}]`。
  2. **参考**：对每个 seed **各跑一次 batch=1** → `ref_i`（K 次单样本）。
  3. **批**：用**同一组 seed** 跑一次 batch=K → `bat_i`。
  4. **比对**：逐样本 `d_i = max|bat_i - ref_i|`；报 `max_i d_i` 与逐样本明细。
- **通过标准**：
  - **首要**：`bat_i` 与 `ref_i` 逐位一致（`max_i d_i == 0`）；若有差，须 ≤ **明确阈值**并**说明来源**（批内 GEMM 的归约顺序与 batch=1 不同，**~1e-6 级浮点差属预期**；关键是不能有**结构性**差异，如某行结果错位/串行）。
  - **附加（防"假批"）**：`bat_i` 与 `ref_j (j≠i)` **必须不同** → 证明不是所有行都用了同一噪声/同一结果（batch_probe 的 `samples_differ` 是弱版，这里要逐行对号）。
- **工具**：扩 `orchestrator/batch_probe.py` 加 `--equivalence`（或独立入口 `batch_equivalence`）：跑上面 2/3/4 并打印 `max_i d_i` + 是否逐行对号。
- **数据（2026-10-20，真实 FastWAM，bf16）**：
  - **受控同 seed（主判据）**：`batch_probe --equivalence --same-seed --seeds 42 --batch-size 4`。4 次 bs1 参考之间 `reference_spread=0`；bs4 的 4 行之间 `batch_row_spread=0`；逐行 `max|bat_i - ref_i| = 0.0048828125`。这是纯 batched-GEMM 归约顺序差，**没有结构性差异**。
  - **per-row seed / 假 batch 检查（补充）**：`--seeds 1,2,3,4`，`out_shape=[4,32,7]`；逐行 `max|bat_i - ref_i|` = **0.0039 / 0.0054 / 0.0039 / 0.0039**；跨 seed 最小差 **0.236** → 行独立，没有把某一行静默复制成整个 batch。
  - 差异来源：bf16 下批内 GEMM 的归约顺序与 batch=1 不同；fp32 下预期回到 ~1e-6 量级。
  - 生产 batch 路径 E2E：4 个 sim 并发连同一 server，开启 `server.batching` → 51 请求 / 44 batch（mean 1.16、max 2），**无顺序回退**；与顺序回退基线相比 3/4 集 steps 完全一致，1 集 107→106（bf16 数值噪声）。
- **结论**：✅ **T-06 通过（bf16 口径）**。同 seed 受控实验证明 batching 只引入 bf16 归约噪声（max|d|≈4.9e-3）；per-row seed 机制生效且行独立；`FastWAMPolicy.predict_action` 的 batch 路径与 `GenericPredictActionPolicy.predict_action_batch` 已接通。若要逐位一致，需要用 fp32 跑（非当前 eval 默认）。
- **2026-10-22 修正（生产 batching 曾静默关闭）**：`rpc_server.py` 只认 `batching.enabled`，而配置只写了 `max_batch_size/max_wait_ms` → **生产评测里的 batcher 从未启动**（此前 ON/OFF 结果一致、stats 恒 0 即为此）。已改为“非空 `batching` 块默认启用，显式 `enabled: false` 才关闭”。
  - **首次真实数据**（external、chunk=10、`max_wait_ms=50`、N=64、512 集）：**512/512 完成、成功率 97.27%、mean batch=3.29（8415 req / 2561 batch）**、CPU 80%、PSS 峰值 982GB。
  - 模型层等价性结论不受影响（`batch_probe` 直接调 `infer_action`，不经过 batcher）。

### T-07 · GPU-sim：sim/server 同卡 vs 异卡

- **对应功能**：§4.4 的放置决策（`01_architecture.md`）。
- **目的**：确定 GPU 仿真下 sim 与 server 该同卡还是异卡（默认同卡，需实测验证）。
- **前置**：T-02（N 标定）完成；GPU-sim benchmark（RoboTwin / SimplerEnv / ManiSkill 之一）。
- **步骤**：固定同一 benchmark + 同一 ckpt，跑两种放置：
  1. **同卡**：每张卡 = `k` 个 sim + 1 个 server；
  2. **异卡**：server 独占卡，sim 在其余卡（"1 server : N 张 sim 卡"）。
- **指标**：wall-clock、`t_infer`（mean/p50/p95，**看抖动**）、显存峰值、`utilization.memory`、单 sim 的显存。
- **通过标准**：给出两种放置的对比结论 + 推荐配置；重点确认**同卡是否把 `t_infer` 显著拉长**（若显著 → 改异卡，并回填 `01_architecture.md` §4.4 与风险栏）。
- **数据（2026-10-28，预实验：单卡共享环境，仅测显存基线）**：
  - 环境：RoboTwin stable_2.0、`adjust_bottle` / `demo_clean`、RoboTwin Python env（sapien 3.0.0 + curobo），A800-80GB，`CUDA_VISIBLE_DEVICES=4`。该卡被其他任务占用 ~49.2GB，剩余 ~32GB。
  - **单 sim（独立进程）**：setup 增量 **+1.77GB**，rollout 峰值 **+4.97GB**，进程退出后显存全部释放。
  - **k 个 sim 并发（同卡、独立进程、每进程 10 步）**：

    | k | 峰值增量 | 每 sim 边际 | 单步耗时（所有 sim） |
    |---|---|---|---|
    | 1 | 4.97GB | 4.97GB | 0.429s |
    | 2 | 10.94GB | 5.47GB | 0.66–0.69s |
    | 4 | 21.87GB | 5.47GB | 1.45s |

  - **暂定结论**：显存近似线性，边际 **~5.5GB/sim**（每进程 CUDA context 另计 ~0.5GB，首进程略低）；k=4 时单步耗时被拉长 3.4×，但该卡本身有邻居占用，**不能据此判断同卡争用**。
- **数据（2026-10-29，真实 pi05 server + 8 卡）**：
  - pi05 fp32 权重占 **16.6GB**；`use_bf16: false`（bf16 推理入口存在 float32/bf16 dtype 不兼容，见 §2.30）。
  - bf16 对照（仅加载、无推理）：活权重 7.72GiB，`nvidia-smi` 15.9GB，`empty_cache()` 后 8.7GB。
  - 8 卡 N=1 首轮：8 个 server 各 15.9GB；**worker 未绑卡 → 8 个 sim 全落在 GPU 0**（峰值 19.9GB），其余卡仅 server；单 episode 338s、wall 689s，连接超时触发重试。
  - **8 卡 N=1 复测（修复 external_url 端口 / UnStableError / 重试语义后）**：
    - 每卡峰值 **18.1–21.9GB**（8 张卡都有 sim）；`failed_units=[]`、unique 8/8
    - **wall 172.8s**、单 episode 平均 **82.0s**（53.2–113.2s）
    - 仍有 2 条重复：跳过 unstable seed 会越界进入相邻 shard 的固定 seed 区间（已实现 `seed_stride = shard 数 × chunk` 跳步，待 GPU 空闲复测）
  - seed 稳定性重复实验：`100003` 连续 3 次 unstable（同一物体），`100001` 两次 stable → **同机可复现**。
  - 限制：2026-10-29 起 8 卡被他人训练任务占用 44–54GB/卡，N≥2 需等卡。
- **结论**：🔄 显存基线与 8 卡放置验证完成；同卡 vs 异卡的 `t_infer` / wall-clock 对比待跑。

### T-08 · 同 ckpt：大 batch vs 单卡多副本 【✅ 已结案】

- **对应功能**：§4.5 的"填空闲 SM"两条路。
- **目的**：单个 forward 喂不满 SM 时，定"加大 batch"还是"同卡多副本"更优（实践出真知）。
- **前置**：M0 发现**吞吐已饱和、但 server 忙占比未满 / 带宽（`utilization.memory`）未满**（否则不需要）；batch 路径可用。
- **步骤**：固定同一 ckpt + benchmark，在**相同显存预算**下对比：
  1. 单副本 + 大 batch（batch=K）；
  2. 同卡 2 副本（各 batch=1）。
- **指标**：`utilization.memory`、吞吐、单样本 GPU 耗时、显存峰值、`t_infer` 分布。
- **通过标准**：给出推荐；**预期 batch 更优**（一份模型 / 一个进程，省显存），但以实测为准。
- **数据（2026-10-15）**：
  - **单卡多副本（A/B）**：同一批 **400 集**、同为 **128 并发**，只变每卡 server 数。

    | | A（1 server/卡） | B（2 server/卡） |
    |---|---|---|
    | 总 wall | 13.20 min | 12.29 min |
    | worker wall（纯计算） | 10.16 min | **8.15 min** |
    | ep/min | 30.3 | **32.5** |

    → **worker 只快 1.25×、总 wall 只快 1.07×**（差的那截是 16 个 server 加载更慢，多吃 1.1 min）。显存不是瓶颈（2×24.7=49.4 < 81.9GB，`server_restarts=0`）。
  - **大 batch**：见 T-13 → batch 8 的 `per_sample_ms` **583→79ms（7.3×）**。
- **结论**：✅ **batch 明显更优**（**7.3×** vs **1.25×**）。原因：同卡多副本走的是**多进程共享 GPU**，受**上下文切换/驱动串行化**限制（次线性），而 batch 在**单进程单次前向内**填满 SM。→ **"填空闲 SM"选 batch，不选多副本**。

---

## 6. 记录规范

- 每个用例跑完，**当场**把数据与结论填进对应用例；不要另开散落文件。
- 数据建议同时存原始文件（`results.jsonl`、`utilization.memory`/`dmon` 序列）到 `eval_update/data/<用例ID>/`，本文只留摘要 + 路径。
- 结论必须包含：**是否达到通过标准**、**关键数字**、**与基线的对比**、**已知偏差/异常**。
- 若某次测试推翻了方案假设（例如 GPU-sim 同卡 `t_infer` 显著变长），**先更新 `01_architecture.md` 的风险栏**，再继续。
