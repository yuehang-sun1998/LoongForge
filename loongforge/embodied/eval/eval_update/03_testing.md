# LoongForge 评测编排改造 —— 测试文档

> **本文用途**：规定"每实现一个功能 → 对应一次性能测试 → 留数据与结论"的流程，并作为测试数据的**唯一归档处**。
> **配套**：`01_architecture.md`（方案）、`02_progress.md`（进度）。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `511aa24`），仓库 `/workspace/LoongForge-Github`。
> **最后更新**：2026-10-12。
> **当前状态**：**尚无任何功能实现，故尚无新测试数据**；§4 列出已有基线，§5 列出待执行用例。

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
| T-04 | shard/worker 契约 | 2 | 至少两个 family | ⬜ 待执行 |
| T-05 | 动态队列 + 全量 wall-clock | 2 | 全 benchmark | ⬜ 待执行 |
| T-06 | 数值一致性（batch>1 vs batch=1） | 3（暂缓） | 任一 | ⏸ 暂缓 |
| T-07 | GPU-sim：sim/server 同卡 vs 异卡 | 1 | GPU-sim | ⬜ 待执行 |
| T-08 | 同 ckpt：大 batch vs 单卡多副本 | 3（暂缓） | 任一 | ⏸ 暂缓 |

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
- **数据**：_待填_
- **结论**：_待填_

### T-05 · 动态队列 + 全量 wall-clock

- **对应功能**：T2-2。
- **目的**：验证长尾被抹平、wall-clock 达目标。
- **前置**：T2-2 已实现；N* 已标定。
- **步骤**：按 `G × N*` 起 worker 池，跑全量；记录 wall-clock、各 worker 空转时长、失败重跑次数。
- **通过标准**：wall-clock 落进目标区间（对齐 `01_architecture.md` §7.1 的 29~32min 理想值，实际按标定 N 修正）；无 worker 长时间空转；**无因等待超时导致的失败重跑**。
- **数据**：_待填_
- **结论**：_待填_

### T-06 · 数值一致性（batch>1 vs batch=1） 【阶段 3，暂缓】

- **对应功能**：T3-1 + FastWAM batch 契约。
- **目的**：确认 batching 不改出分。
- **通过标准**：batch>1 与 batch=1 在同一分片上出分一致（或差异在噪声范围内），批内噪声语义明确。
- **数据**：_待填_
- **结论**：_待填_

### T-07 · GPU-sim：sim/server 同卡 vs 异卡

- **对应功能**：§4.4 的放置决策（`01_architecture.md`）。
- **目的**：确定 GPU 仿真下 sim 与 server 该同卡还是异卡（默认同卡，需实测验证）。
- **前置**：T-02（N 标定）完成；GPU-sim benchmark（RoboTwin / SimplerEnv / ManiSkill 之一）。
- **步骤**：固定同一 benchmark + 同一 ckpt，跑两种放置：
  1. **同卡**：每张卡 = `k` 个 sim + 1 个 server；
  2. **异卡**：server 独占卡，sim 在其余卡（"1 server : N 张 sim 卡"）。
- **指标**：wall-clock、`t_infer`（mean/p50/p95，**看抖动**）、显存峰值、`utilization.memory`、单 sim 的显存。
- **通过标准**：给出两种放置的对比结论 + 推荐配置；重点确认**同卡是否把 `t_infer` 显著拉长**（若显著 → 改异卡，并回填 `01_architecture.md` §4.4 与风险栏）。
- **数据**：_待填_（两种放置各一行）
- **结论**：_待填_

### T-08 · 同 ckpt：大 batch vs 单卡多副本 【阶段 3，暂缓】

- **对应功能**：§4.5 的"填空闲 SM"两条路。
- **目的**：单个 forward 喂不满 SM 时，定"加大 batch"还是"同卡多副本"更优（实践出真知）。
- **前置**：M0 发现**吞吐已饱和、但 server 忙占比未满 / 带宽（`utilization.memory`）未满**（否则不需要）；batch 路径可用。
- **步骤**：固定同一 ckpt + benchmark，在**相同显存预算**下对比：
  1. 单副本 + 大 batch（batch=K）；
  2. 同卡 2 副本（各 batch=1）。
- **指标**：`utilization.memory`、吞吐、单样本 GPU 耗时、显存峰值、`t_infer` 分布。
- **通过标准**：给出推荐；**预期 batch 更优**（一份模型 / 一个进程，省显存），但以实测为准。
- **数据**：_待填_
- **结论**：_待填_

---

## 6. 记录规范

- 每个用例跑完，**当场**把数据与结论填进对应用例；不要另开散落文件。
- 数据建议同时存原始文件（`results.jsonl`、`utilization.memory`/`dmon` 序列）到 `eval_update/data/<用例ID>/`，本文只留摘要 + 路径。
- 结论必须包含：**是否达到通过标准**、**关键数字**、**与基线的对比**、**已知偏差/异常**。
- 若某次测试推翻了方案假设（例如 GPU-sim 同卡 `t_infer` 显著变长），**先更新 `01_architecture.md` 的风险栏**，再继续。
