# LoongForge 评测编排改造 —— 开发进度 / 交接文档

> **本文用途**：交接。任何人 / agent 读完本文即可接手，无需回读对话历史。
> **配套文档**：`01_architecture.md`（做什么、怎么做）、`03_testing.md`（怎么验）。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `511aa24`），仓库 `/workspace/LoongForge-Github`。
> **最后更新**：2026-10-15。
> **一句话状态**：**M0+M1+M2 已实现并验证。** 静态 8 卡基线全量 94.2 min / 95.65%；**M2（单 coordinator 全局队列）全量 56.2 min / 96.45% → vs 基线 1.68×、vs 1卡1server vanilla 3.09×**。M3（batching）暂缓（GPU 未成瓶颈）。

---

## 0. 接手须知（先读这段）

1. **改动落点在编排层**：`loongforge/embodied/eval/orchestrator/`。**不要**动 runner、server、model、benchmark。
2. **代码仓库 / 分支**：改上游代码 / 提 PR 用 `/workspace/LoongForge-Github`（真正的 git 仓库，`origin`=个人 fork `yuehang-sun1998/LoongForge`，`upstream`=`baidu-baige/LoongForge`）；`/workspace/LoongForge` 等是旧克隆，仅供读代码。
   - **本改造的所有开发都在分支 `feat/eval-orchestration-parallel` 上进行**，基点 `upstream/master` @ `511aa24`。
   - 该分支**当前未设置 tracking**；首次推送用 `git push -u origin feat/eval-orchestration-parallel`（否则裸 `git push` 会报 "no upstream branch"）。注意别把它 track 到 `upstream/master`——那是公网上游组织，不是你的 fork。
   - 本目录（`loongforge/embodied/eval/eval_update/`）即该分支的文档目录，后续文档都在此更新。
3. **该环境访问 GitHub 需内网代理**（否则 `git fetch` 卡 ~130s 后超时）。代理地址与 `no_proxy` 白名单属**内网信息，不写入仓库**——具体值见团队内部说明。要点：走内网 egress；`no_proxy` 需含 `localhost,127.0.0.1` 与公司内网域名。
4. **未经要求不要 commit / 不要用 worktree**：说"建分支"= 主仓库 `checkout -b`；默认不 commit。

---

## 1. 状态总览

| 阶段 | 任务 | 状态 | 代码位置 | 验收 | 依赖 |
|---|---|---|---|---|---|
| — | 设计收敛（架构/标定/队列） | ✅ 完成（文档） | `01_architecture.md` | — | — |
| — | 代码触点核实 | ✅ 完成 | 见 `01_architecture.md` §10 | — | — |
| 0 | **N 扫描 + 报告 + agent 决策**（CPU-sim） | 🔄 扫描/报告/决策完成；GPU-sim 待做 | 探针 + `orchestrator/calibration_report.py` | `03_testing.md` T-02 | 需空闲 GPU |
| 0 | server 侧服务时间插桩 | ✅ 完成 | `transport/timing.py` + `rpc_server.py` | `03_testing.md` T-01 通过 | — |
| 0 | 标定报告生成器（三件套） | ✅ 完成 | `orchestrator/calibration_report.py` | 单测 3 passed | — |
| 0 | 配置校验 + 缓存 | ✅ 完成 | `orchestrator/calibration_config.py` | 单测 7 passed | — |
| 0 | **标定正式入口（扫 N）** | ✅ 完成 | `orchestrator/calibrate.py` | 单测 3 passed | 报告 + 校验/缓存 |
| 1 | external server 模式（+ 纯 client） | ✅ 完成 | `server_manager.py` + `config.py` | `03_testing.md` T-03 | — |
| 1 | **最小 coordinator（一行命令起 1 server + N worker）** | ✅ 完成 | `orchestrator/coordinator.py` | `03_testing.md` T-03 | external ✅ |
| 1 | **episode-chunk 分片（+ runner episode offset）** | ✅ 完成 | `coordinator.py` + `libero_runner.py` | `03_testing.md` T-03 | T1-5 |
| 1 | N 标定（报告 + agent 决策 + 配置缓存） | 🔄 报告/校验/缓存 ✅；扫描走手动探针 | `calibration_report.py` + `calibration_config.py` | T-02 | 插桩 ✅ + 最小 coordinator |
| 1 | GPU-sim 放置决策（同卡 vs 异卡） | ⏸ 暂缓（当前环境不支持 GPU-sim；待 CPU-sim 完成后统一做） | — | T-07 | 需 GPU-sim 环境 |
| 2 | shard/worker 契约 | ✅ 完成（由 `run.py` 统一分发吸收，**无需新 adapter 层**，见 §2.11） | `run.py` + 各 `*_runner` | `03_testing.md` T-04 | — |
| 2 | coordinator **动态化**（队列 + 按 ckpt 分片 + 按资源分池 + 看门狗） | ✅ 完成 + 全量对照（**全局队列 + 有界进程池 + 失败重试**；56.2 min / 3.09×，见 §2.11–§2.13） | `coordinator.py` | `03_testing.md` T-05 | 契约 |
| 2 | episode-chunk 粒度抢占 | ✅ 完成（chunk 细到 1 集，快槽可抢下一个 unit） | `coordinator.py` | `03_testing.md` T-05 | 队列 |
| 2 | **运行后覆盖校验 + 自动补跑** | ✅ 完成 | `validation.py` + `coordinator.py` | `03_testing.md` T-09 | — |
| 2 | **server 自愈（看门狗 + 连接重建）** | ✅ 完成（`57dcef9` + `3cc3a2d`） | `coordinator.py` + `libero_runner.py` | `03_testing.md` T-10 | §2.15 |
| 2 | **健壮性兜底**（unit 超时 / 收尾信号 / 竞态 / hung 探活） | ✅ 完成（`dc07c75`/`2e28ce0`/`e5f077c`/`cf9abb6`） | `coordinator.py` + `run.py` + `transport/client.py` | `03_testing.md` T-11 | §2.16 |
| 2 | **运维陷阱兜底**（output_dir 冲突 / 超时下限 / 崩溃循环） | ✅ 完成（`a01390b`/`2d43e73`）；GPU-sim 只写方案 | `coordinator.py` + `run.py` | `03_testing.md` T-11 | §2.17 |
| 2 | **分片：per-benchmark planner 表** | ✅ 完成 | `planning.py` + `coordinator.py` + `validation.py` | `03_testing.md` T-12 | §2.18 |
| 3 | **chunk 客户端化**（消 90% 请求） | ✅ 完成（请求量 9.83×；80/80 逐集一致） | 各 runner + `_common` | `03_testing.md` T-14 | §2.23 |
| 3 | server 侧 batcher（M3） | 🔄 **探针实测：`per_sample_ms` 583→79ms（batch 8，7.3×）→ 判定 GO**；**server 侧 `AsyncBatcher` 已实现（异步聚合 + cache-hit 过滤）**；**T-06 已过（bf16 max|d|=0.0054），真 batch 路径已接通** | `transport/batcher.py` + `batch_probe.py`（诊断） | `03_testing.md` T-06/T-13/T-14 | 下一步重扫 N |
| 3 | **单卡多 server vs batch**（T-08） | ✅ 已结案：**多副本只 1.25×、batch 7.3×** → 选 batch | —（A/B 用现有 coordinator 跑） | `03_testing.md` T-08 | — |

**图例**：✅ 完成 / 🔄 进行中 / ⬜ 未开始 / ⏸ 暂缓。

---

## 1.5 里程碑（M0–M3）

> 每个里程碑是一个**可验收的状态**；顺序有依赖，不要跳。**M0 是 go/no-go 门。**

### M0 · 试跑 / 标定（可行性门）⛔ go/no-go
- **内容**：server 侧插桩（`t_infer` + 队列深度）+ 1 server ↔ N sim 的 N-吞吐扫描。
- **交付**：插桩代码 + 标定报告（`report.json`/`report.md`/`env.json`）+ agent 决策出的**并行配置**（CPU-sim + GPU-sim 各一）。
- **验收**：吞吐曲线出现平台；N* 可复现。
- **门**：曲线不出平台 → 方案要重想，**不要往下做**。
- 对应任务 T0-1 / T0-2 / T1-3；测试 T-01 / T-02。

### M1 · 并行（1 server ↔ N sim）
- **内容**：external server 模式 + **最小 coordinator（一行命令起 1 server + N worker）** + GPU-sim 放置决策（同卡/异卡）。
- **交付**：`server_manager.py` external 模式、**`coordinator.py`（最小版）**、放置结论。
- **验收**：**一条命令** `orchestrator.run --config run.yaml` 起 1 server + N worker 跑完并聚合；server 不被误杀；CPU-sim 下 **server 忙占比逼近 100%**（或吞吐平台，**不用 `nvidia-smi util`**，见 `01` §1）。
- 对应任务 T1-1 / T1-2 / T1-4 / **T1-5**；测试 T-03 / T-07。

### M2 · 动态任务下发（把最小 coordinator 动态化）✅ 完成
- **内容**：shard/worker 契约（由 `run.py` 统一分发吸收）+ **coordinator 动态化**（**全局队列 + 有界进程池**，按资源分池、episode-chunk 粒度、失败重试一次、长尾抹平）。
- **交付**：`coordinator.py`（M2 版）+ `run.py` 分发（`coordinator`/`servers`/`pools` 任一存在即走编排）+ 单测 6 passed。已提交 `588eaf4`。
- **验收**：✅ **多卡 E2E 通过**（2 server / 4 并发 / 8 unit 全局队列，动态补位、无重叠无缺失、失败重试）；✅ **全量对照已量化**（静态 8 卡基线 94.2 min / 95.65%，逐 suite 2.7–4.3×、整机 1.85× vs 1卡1server；见 §2.12）。M2 全量对照（~3.0×）待跑。
- 对应任务 T2-1 / T2-2；测试 T-04 / T-05。

### M3 · batching（最后，条件触发）
- **内容**：server 侧 batcher + FastWAM batch 契约（`01_architecture.md` 附录 A）。
- **前置**：**GPU 成为绑定约束**（即 M1/M2 之后，`推理总时间/GPU 数 > 仿真总时间/并行度`）。
- **验收**：batch>1 出分一致 + GPU 吞吐提升。
- **说明**：batching 是"**提高 GPU 吞吐**"的手段，只在 GPU 吞吐成为瓶颈时兑现；改造前 GPU 空着，做它是负收益。
- 对应任务 T3-1；测试 T-06。

### 资源类差异一览（CPU-sim vs GPU-sim）

> 机制上 M1/M2 对两类**相同**；差异在参数与额外项。**M0 明确要求两类各做一次标定。**

| 里程碑 | CPU-sim（LIBERO / CALVIN） | GPU-sim（RoboTwin / SimplerEnv / ManiSkill） |
|---|---|---|
| **M0 标定** | 跑 N 扫描 | 同样跑；**额外测单 sim 显存 + 同卡 `t_infer` 被抬高多少**（T-02 / T-07） |
| **M1 并行** | external + 纯 client | 额外：**放置决策**（同卡 / 异卡，T-07） |
| **M2 下发** | worker 池按 **CPU 核数**装箱 | worker 池按 **显存**装箱；unit 带 vram |
| **M3 batch** | 纯吞吐优化（可选） | **与向量化耦合**（lockstep batch，§7.5） |
| **M0–M3 之外** | — | **向量化 env**（benchmark 侧，§7.5，不在本分支） |

### 横切门（不属某个 M，但必须过）
- **正确性门**：M1/M2 之后，结果必须与基线**逐位一致**（同 episode 集合、同分）——"只改快慢、不改结果"。**别只测速度不测分。**

---

## 2. 已完成

### 2.1 设计收敛（2026-10-12）

经过讨论收敛出以下设计决策（**接手者请勿重新纠结**）：

1. **改造收口在编排层**，不动 runner/server/model/benchmark。
2. **并行 = 进程级 fan-out（跨 shard）**，不是单环境向量化。因此"部分 benchmark 不支持并行"**不构成阻塞**——它们只需一个"跑 shard"的入口。
3. **server 侧几乎不用改**：`rpc_server.py` 已是 asyncio + 同步阻塞 `_route_message` → 天然串行排队。
4. **队列按 ckpt 分片**（server 按 ckpt 绑定），**按资源类型分池**（CPU-sim / GPU-sim）。
5. **N 是 (model, benchmark) 的函数**，用 §6 标定法确定，缓存成表。
6. **GPU 仿真的并行不是一一对应**，仍是 1 server : N sims；N 由显存定，且需决定 sim/server 同卡还是分卡。
7. **batching 属阶段 3，现在不做**（当前 CPU-bound，做了 GPU 更空）；真要做时位置在 **server 侧**。

### 2.2 代码触点核实（2026-10-12）

已逐处核实，结论写入 `01_architecture.md` §10。要点：

- `run.py` 确有 5 处 `ManagedServer(...)`（92/141/167/191/215）。
- `server_manager.py:30-48` 的 `ensure_running` 会杀 server 重启 → 共享场景的硬阻塞。
- `rpc_server.py:22/41-49/71` 串行排队成立；`loongforge_server.py:12/109` 的 HTTPServer 只服务 `/healthz`。
- `libero_runner.py:444` 已有 `completed_episode_keys` 续跑。
- `modeling_fastwam.py:349` 硬断言 `batch==1`（阶段 3 前置）。
- 仿真后端分类核实：LIBERO/CALVIN = MuJoCo CPU（CALVIN 用 osmesa 软件渲染）；RoboTwin/SimplerEnv/ManiSkill = SAPIEN GPU。

### 2.3 T0-1 实现 + 真评测验证（2026-10-12）

- **代码**：新增 `transport/timing.py`、`rpc_server.py` 接线、`unit_tests/test_server_timing.py`（**9 passed**，ruff clean）。**已提交 `af2d7ea`**。
- **验证**：真评测（LIBERO `libero_10` task0 × 2 集）跑通，`timing.json` 出数、与 `inference_latency_ms` 交叉验证一致（max 554.8ms 精确相同）；**开关关/开逐集结果完全一致**（非侵入）。详见 `03_testing.md` T-01。
- **跑评测踩到的环境坑（4 条，复现用）**：
  1. **`env.loongforge_root` 才是有效键**（`server.loongforge_root` 是 no-op；用户脚本靠 `export PYTHONPATH` 兜住）。
  2. **server 解释器要 `/usr/bin/python3`**（有 TransformerEngine）；**仿真侧用 venv `/workspace/venvs/libero-eval`**（有 robosuite/libero）。
  3. **`DIFFSYNTH_MODEL_BASE_PATH`**（= `/mnt/cfs/sunyuehang`）指向本地 Wan2.2，否则 VAE 会走 modelscope 联网超时。
  4. 小分片冒烟配置在 `/tmp/eval_smoke/t0_smoke.yaml`（2 集）。

### 2.4 标定报告生成器（2026-10-12）

- **代码**：`orchestrator/calibration_report.py`（`build_report` / `render_markdown` 纯函数 + CLI：`--sweep` / `--timing` / `--env` → `report.json` / `report.md` / `env.json`）+ 单测 `test_calibration_report.py`（**3 passed**，ruff clean）。**已提交 `9ac3021`**。
- **设计**：输入与扫描器**解耦**（只认 sweep/timing 的 JSON），任何 sweep harness 都能喂。
- **报告读法**（写进 `report.md`）：主判据 = **吞吐平台 + server 忙占比**；**不用 `nvidia-smi util`**。

### 2.5 T-02 标定扫描 + 报告 + agent 决策（CPU-sim，2026-10-12）

- **扫描**（手动探针，N=1..16）：吞吐 1.44→6.81 ep/min（次线性，未到平台）。
- **绑定资源 = 串行 server**（忙占比 18%→87% @ N=16）；**GPU / CPU 都不忙**（`util.gpu` 27%、`util.memory` 5%、CPU 8.9%）。
- **恒等式**：`吞吐 = server 忙占比 × 上限`，上限 ≈ **7.9 ep/min**。
- **agent 决策**：`n_sims_per_server = 16`；校验 PASS。产物：`report.json`/`report.md`/`env.json` + `parallel_config.yaml` + `validation.json`。
- **待做**：GPU-sim 一路；正式流程（需 T1-1 external server 模式）。

### 2.6 配置校验 + 缓存（2026-10-12）

- **代码**：`orchestrator/calibration_config.py`（`validate_parallel_config` 按 `N ≤ 核数/每sim核数`、显存、`timeout ≥ N×服务时间×safety` 做边界检查；`calibration_key` / `save_calibration` / `load_calibration` 做 `(model,benchmark,gpu)` 缓存）+ 单测（**7 passed**，ruff clean）。**已提交 `9ac3021`**。
- **实测**：对 T-02 的配置跑校验 → **PASS**（`timeout 60000ms ≥ 16×50.9×2 = 1628ms`）。

### 2.7 T1-1/T1-2 external server 模式（2026-10-12）

- **代码**：`config.py`（`server.external_url` → `external_url`，并从 URL 推 `host`/`port`）+ `server_manager.py`（`ManagedServer` external 模式下 `start`/`ensure_running`/`stop` 全 no-op）+ 单测 `test_server_manager_external.py`（5 passed）。**`run.py` 无需改动**。**已提交 `19f6ac7`**。
- **用法**：config 里加 `server.external_url: ws://host:port`；手起（或 coordinator 起）1 个 server，N 个 orchestrator 都指向它 → **1 server : N sims**。
- **E2E 验证**：✅ 起 1 共享 server + 1 纯 client orchestrator（`external_url`）→ `rc=0`、共享 server **未被误杀**（`alive after run: True`）。见 `03_testing.md` T-03。
- **待做**：N>1 多 client 并发（M2）；coordinator（起 server + 起 worker 池）。

### 2.8 T1-5 最小 coordinator（2026-10-12，已提交 `19f6ac7`）

- **代码**：`orchestrator/coordinator.py`（`split_round_robin` / `_shard_config` / `run`）+ `run.py` 分发（config 有 `coordinator:` → coordinator）+ 单测 `test_coordinator.py`（3 passed）。
- **用法**：config 加 `coordinator: {n_workers: N}`；`orchestrator.run --config run.yaml` **一条命令**。
- **E2E**：✅ 2 worker 各 1 task、rc=0、聚合 2 集、分片无重叠。
- **真任务烟测**（✅ 2026-10-12）：`libero_10` task 0-3、官方 `max_steps=700`、每任务 1 集、`n_workers=4` → 一条命令跑通：4 worker rc=0、**分片无重叠**（worker_i→task i）、聚合 **4 集 / 3 成功（0.75）**、`timing` `mean=51.1ms p95=495ms`（与既有量级一致）。
- 🔴 **发现的分片上限**（已在 T1-6 解决）：最小 coordinator 原按 task 分片 → `N ≤ 任务数`。见 **T1-6**。
- **不做**（M2/T2-2）：队列、分池、看门狗。

### 2.9 T1-6 episode-chunk 分片（2026-10-12）

- **代码**：`libero_runner` `--episode-offset` + config `benchmark.episode_offset`；`coordinator.build_shards`（episode-chunk 级）+ 单测更新（4 passed）。**已提交 `50fbfb6`**。
- **E2E**：✅ 2 task × 4 集 / `n_workers=4` → 4 分片、8 集恰好覆盖一次。

### 2.10 标定正式入口 `calibrate.py`（2026-10-12）

- **背景**：此前标定的"扫 N + 并行铺卡 + 收集"都是**手写临时脚本**（`/tmp/eval_smoke/*.py`），没进仓库 → **每次都要自己写**。用户指出后补上。
- **代码**：`orchestrator/calibrate.py`——`--config --n 1,2,4,8,16,32` **一条命令**：每个 N 起 coordinator（并行铺卡）→ 收集 wall/吞吐/server 忙占比 → `calibration_report` 报告 → **兜底 N**（`fallback_n` = ≥95% 最佳吞吐的最小 N）→ `calibration_config` 校验 + 缓存。单测 3 passed。
- **顺带**：`coordinator.py` 加 `elapsed_sec`（只计 worker 墙钟）+ 落盘 `coordinator_result.json`（供标定收集）。

### 2.11 M2 动态任务下发（2026-10-15，已提交 `588eaf4`）

- **背景**：T1-5/T1-6 的"最小 coordinator"是**静态一轮分片**（一次切 N 片、跑完即止）——`N ≤ 任务数`虽已解除，但**快槽跑完就空转**，长尾套件（`libero_10`）拖住 wall-clock。M2 把它升级为**动态调度**。
- **代码**（`orchestrator/coordinator.py` M2 版）：
  - **chunk 细化**：`chunk_size(total, workers, units_per_slot=4)` → 每槽约 4 个 unit（有富余，快槽可"偷"下一个）。
  - **全局队列 + 有界进程池**：`pending` 队列 + `free_slots` 槽位；每有槽空闲就从队列取下一个 unit，**任一 unit 跑完即补位**（不再按槽静态绑定分片）。
  - **并发度** = `len(gpus) × n_sims_per_server`；unit 按 `slot % len(urls)` 轮询绑到各 server。
  - **失败重试一次**（`MAX_ATTEMPTS=2`）：非零退出码的 unit 重新入队，第二次仍失败才记入 `failed_units`。
  - **多 server 端口**：`LOONGFORGE_COORD_PORT_BASE`（默认 14560）起基址，每 server `+PORT_STRIDE×i`；**关键修正**：policy server 的端口**只从 config 读**（`--port` 不是 `loongforge_server` 的 CLI 参数）→ coordinator 为每个 server **落一份独立 config**（`server_{i}.json`）再启动。
  - **套件差异化**：`benchmark.suite_overrides: {<suite>: {<key>: <val>}}`（如 LIBERO `libero_10: {max_steps: 700}`）→ 使**一条全局队列跨套件**成立（否则所有套件被迫同一 `max_steps`）。
  - **episode 级重试默认开**：`_shard_config` 默认注入 `run.max_retries=2`（显式值优先）。`episode_error`（含 `ConnectionClosedError` 这类 sim↔server 断连）本就在 runner 的重试集合里（`libero_runner.py:505`），但 `--max-retries` **默认 0** → 断连会被直接记成失败（基线就吃了 16 个，见 §2.13）。模型失败（`not_successful_within_max_steps`）**不重试**。
- **`run.py` 分发修正**：原判据 `config.get("coordinator")` 对**空映射 `coordinator: {}`** 为假 → 会误当单跑。改为**按 key 存在性**判断：`coordinator` / `servers` / `pools` 任一存在即走编排。
- **契约（T2-1）**：**不新增 adapter 层**——"统一入口"由 `run.py` 的 family 分发天然提供。unit 契约 = 一份普通 eval config（`benchmark.{suite,task_ids,episode_offset,episodes_per_task}` + `server.external_url` + `run.output_dir`）→ `results.jsonl`（含 `episode_id` key，可 resume）。**5 个 family 走同一条路**。
- **E2E（2026-10-15，2 卡 A800）**：config = `servers:[{gpus:[2,3]}]` + `pools.cpu_sim.n_sims_per_server=2`（并发 4）+ `libero_10` task[0,1] × 4 集（共 8 集 / chunk=1 → **8 unit**）。结果：**2 server 各占 1 卡**（端口 17000/17001、17020/17021）、**8 unit 全 rc=0、failed_units=[]**、聚合 **8 集**、**分片无重叠无缺失**（worker_i → task/eps 恰好一一对应）、`elapsed=105.3s`。
- **动态补位实证**：worker 0-3 于 `18:25:57` 起（第一波填满 4 槽）；worker 4-7 于 `18:26:47` 起（**第一波跑完、槽位释放后自动补位**）→ 队列确为全局动态，而非静态一轮。
- **单测**：`test_coordinator.py` **7 passed**（含 **retry 用例**：unit 首次失败→重入队→第二次成功→`failed_units=[]`；**suite_overrides 用例**；**max_retries 默认用例**）。全量 **69 passed / 1 skipped**，ruff clean。
- **不做**：`work/{pending,running,done}` **文件级**队列（用**进程内内存队列**代替；单机单进程够用，且避免 FS 原子性假设）；看门狗超时回收（留待有实际需要再加）。
- **待做**：GPU-sim 一路。（M2 全量对照已完成，见 §2.13。）

### 2.12 全量实测 + 逐 suite 加速（2026-10-15，**静态 8 卡基线**）

> 口径：8 卡、每卡 1 coordinator + 1 server + 16 worker，4 套件 × 500 集 = 2000 集；ckpt `lf_sft_pr240_fp32master_20k`。**这是 M2 之前的静态分配（每套件钉 2 卡），用作 M2 的对照基线。**

- **总 wall = 94.2 min**（17:28:26→19:02:40），8 卡全 rc=0；**1913/2000 = 95.65%**（10 91.20 / spatial 98.80 / object 95.80 / goal 96.80）。
- 每卡 worker wall（不含加载）：libero_10 **91.2**（c1，绑定卡）/ spatial 38.5 / object 42.5 / goal 44.5 min。
- **vanilla 参照 = 1 卡 1 server**（用户口径）：官方 harness `evaluate_results/pr240fp32_official_2000`（2026-10-06，4 卡 worker pool，同 ckpt）。per-task（50 集）libero_10 49.5 / spatial 33.1 / object 25.6 / goal 30.7 min；8 卡折合 ≈ **173.7 min**。
- **逐 suite 加速（每 50 集，log 实测，无估算）**：libero_10 **2.7×** / spatial **4.3×** / object **3.0×** / goal **3.5×**；**整机 94.2 vs 173.7 = 1.85×**。
- **缺口归因**：spatial/object/goal 在 ~45 min 跑完，后 **6 卡空转**到 libero_10 收尾（91 min）→ 逐 suite ~3.5× 被摊薄成整机 1.85×。**这正是 M2 要吃的**：空卡抢 libero_10 的 chunk → 整机 ≈ `45 + 254/(8×2.74) ≈ 57 min` → **~3.0×**。
- ⚠️ **分数未逐位一致**（object 95.80 vs vanilla 99.40 等）：vanilla 评测代码此后有更新，**待开发完 rebase 一版 vanilla 再重跑对齐**（用户 2026-10-15 决定先不追）。**速度对比也因此非严格 apples-to-apples**（episode 长度受协议项影响）。
- ⚠️ 注：**别用 `eval_ckpt_all_parallel.sh` 的 SPG=2 结果当基线**（10-13/10-14 的 followups 全量即 SPG=2）；口径不一致。

### 2.13 M2 全量对照（2026-10-15，已完成）

> 同一 ckpt / 协议 / 2000 集，**唯一变量 = 调度方式**：基线是"每套件钉 2 卡"（8 个独立 coordinator），M2 是"**单 coordinator + 跨套件全局队列**"（`servers:[{gpus:[0..7]}]` + `n_sims_per_server=16`）。

- **总 wall = 56.2 min**（19:33:21→20:29:30；worker 53.2 min）；**1929/2000 = 96.45%**；**520 unit 全 rc=0、`failed_units=[]`**、**0 个 `episode_error`**。
- **vs 静态 8 卡基线 94.2 min → 1.68×**；**vs 1卡1server vanilla 173.7 min → 3.09×**。
- **长尾被吃掉**：libero_10 的 500 集在基线里要 2 卡拖 91 min，M2 里用满 8 卡、在 56 min 的总盘子内跑完（后 6 卡不再空转）——正是 §2.12 预测的 ~3.0×。
- **分数对齐（逐集）**：与基线比对 2000 集，**仅 16 集不同**，全在 `libero_object/task=6/episode=16..31`，且基线里**全由同一 worker 连续产出**、`failure_reason=episode_error`/`ConnectionClosedError`（websocket keepalive 超时、`steps=0`、**根本没跑**）。**剔除这 16 个 spurious 失败后两边一致（96.42% vs 96.45%）→ 模型行为逐集相同**。
- **收获**：① 证实 M2 的全局队列确实抹平长尾（整机 1.68× / 对 vanilla 3.09×）；② 满负载下多 server 端口隔离、`suite_overrides`、unit 级重试均正常；③ 暴露"**episode 级重试默认关**"这个坑 → 已在 §2.11 修为默认 `run.max_retries=2`。
- **待做**：GPU-sim 一路；vanilla 代码 rebase 后重跑对齐绝对分数。

### 2.14 运行后覆盖校验 + 自动补跑（2026-10-15）

- **背景**：`_aggregate` 只做去重计数——**漏集 / 重复 / 多出**都会被静默吞掉（分数照出）。用户要求补一个校验机制。
- **代码**：新增 `orchestrator/validation.py`——`expected_keys(plan)`（计划应跑集合）、`key_of(record)`（取 `(suite,task,episode)`；缺字段回退解析 `episode_id`）、`read_records`、`validate`、`missing_keys`、`repair_shards`（缺失键**合并回连续区间**）、`format_report`；CLI `python -m ...validation --output-dir … --n-units N --plan plan.json`（默认**只报告**，`--strict` 才 exit 1）。coordinator 跑完把 `plan.json` 落盘，供离线复查。
- **闭环（不中断评测）**：主队列跑完 → **校验 → 有缺失就生成补跑分片（连续区间合并）→ 复用同一个有界池再跑 → 再校验**，最多 `MAX_REPAIR_ROUNDS=3` 轮。**只有 3 轮后仍不完整**才 `ok=false`（此时才值得人工介入）；**过程不抛异常、不中断**。
- **失败分类**：`infra_failed`（`episode_error`/超时/`server_unreachable`，**没真跑**）与**模型失败**分开；模型失败是正常结果，**不算不完整**。
- **验证**：
  - 单测 `test_validation.py` 5 passed（期望集合 / 键解析 / 区间合并 / 缺重多检测 / 失败分类）；全量 **74 passed / 1 skipped**。
  - **E2E 冒烟**（2 卡 / 8 unit）：`coordinator_result.json` 出 `validation` 块，`ok=true`、`repair_rounds=0`、模型失败 8 个被正确排除。
  - **对真实 M2 全量离线校验**（2000 集 / 520 unit）：**`ok=true`、missing=0、duplicates=0、unexpected=0、unkeyed=0、infra_failed=0**，`success_rate=0.9645`，失败全是 `not_successful_within_max_steps`（71）。

### 2.15 故障注入实测 + 自愈缺口（2026-10-15）

用户要求"真刀真枪试一下校验机制"。两个故障注入实验（各 2 卡 / 8 unit / 8 集，`max_steps=150`）：

- **实验 A（删记录 → 验补跑）✅**：跑到中途删掉一个已完成 unit 的 `results.jsonl`（1 集变缺失）→ **`repair_rounds=1`、`n_units_launched=9`（8 计划 + 1 补跑）、`validation.ok=true`、`missing=0`、`infra_failed=0`**。补跑分片 = 那 1 集合并成的连续区间，新 index、复用同一有界池。**校验闭环确实能自愈漏集。**
- **实验 B（kill server → 验鲁棒性）⚠️ 暴露 3 个缺口**：
  1. **没有重启**：unit 全 external，`ensure_running` 直接 return → server 死了没人拉起。
  2. **client 不重建 → 级联失败**：`ConnectionClosedError` 归为 `episode_error`，runner 只在 `server_unreachable` 时重建 client → 死连接继续失败（记录里 `attempt: 2`，重试 3 次全败）。**这正是基线那 16 连败的成因。**
  3. **unit 卡死 300s**：`PolicyClient._wait_for_server(timeout=300)` 对着死 server 等满 300s（`TimeoutError: Failed to connect to server within 300 seconds`）→ 一次 server 挂掉可把评测拖成几十分钟。

**结论**：覆盖校验解决"漏集"，但 **server 挂掉需要独立的"自愈"机制**（看门狗 + 连接重建 + 快速失败）——设计见 `01_architecture.md` §5.6，实现见 **T2-3**。

**修复后复测（2026-10-15）**：同一 kill-server 实验重跑 → **看门狗检测并重启**（`server_restarts=1`，日志 `server 0 died; restarting on port 18100` → `server 0 back up`）→ **`validation.ok=true`、`infra_failed=0`、`failed_units=[]`**、8 集全在（失败全是 `not_successful_within_max_steps`）、`elapsed=218s`。**自愈链打通**：server 挂 → 同端口重启 → unit 重建 client 重连 → episode 重试补齐。代价 = 重启要重载模型（~100s），期间命中的 unit 靠重试等待。

### 2.16 M2 健壮性加固（2026-10-15）

用户问"还有其他可能 fail 的 case 吗"，梳理出 4 类并逐一实现：

- **① unit 级 wall-clock 超时**（`dc07c75`）：把 `_drain_pool`（无限等）换成 `_await_slot`——超预算就 kill，走重试/补跑。防 native 层死锁把整轮挂死。预算 = `max episodes × per_episode_sec + connect + slack`，`run.unit_timeout_sec` 可覆盖，有效值落结果 JSON。
- **② 收尾信号**（`2e28ce0`）：coordinator 打印覆盖行 + 对 `infra_failed`/`ok=false` 出 stderr 告警；**`run.py` 在 `ok=false` 时 `exit(2)`**（"需要人工介入"）。**过程中仍不中断**（补跑照常）。
- **③ 看门狗竞态**（`e5f077c`）：`_restart` 前后都查 stop 标志（shutdown 期间启动的 server 自己 terminate）；restart 等待上限从 1800s 降到 **600s**；teardown 先 `join` 看门狗。防**孤儿 server 占卡**。
- **④ hung server 探活**（`cf9abb6`）：进程活着但不服务时 `poll()` 查不到、healthz（独立线程）照样 200 → 改为每分钟**真 ping**（`PolicyClient.ping` 新增 `timeout`，走 `recv(timeout=)`），连续 2 次失败重启；探活有界（3s/15s）且恢复被弹掉的代理环境变量。
- **测试**：新增 8 个用例（unit 超时 / 预算推导 / 退出码 / 竞态 / 探活）；全量 **87 passed / 1 skipped**，ruff clean。
- **仍未覆盖**：coordinator 无 supervisor（单点）；同 `output_dir` 无锁；`policy_call_ms` 未按 N 自动放宽。

### 2.17 运维陷阱兜底（2026-10-15）

用户对剩下 4 项的决定：**① supervisor 不做**；**③ GPU-sim 只写方案**；② ④ 做。

- **② `output_dir` 冲突**（`a01390b`）：启动时若已存在 `coordinator_result.json` → **拒绝启动**（`SystemExit`），除非 `run.resume: true`（runner 按 episode resume，续跑有意义）。防两份协调评测互相覆盖。
- **③ `policy_call_ms` 下限告警**（`a01390b`）：设了 `timeouts.service_ms`（= 标定报告的 `service_mean_ms`）时，按 `N × service_ms × 2` 算下限，低于就 **stderr 告警**——否则假 `policy_timeout` 会被误当模型失败。**GPU-sim 部分只写进 `01` §5.7，本次不开发**（它的 `t_service` 更长且与 sim 渲染争卡，必须用 GPU-sim 实测值）。
- **④ server 重启上限**（`2d43e73`）：`MAX_SERVER_RESTARTS=3`；超了**放弃该 server**（stderr 明说"需要人工介入"）并记入 `servers_given_up`；`run.py` 同样 **exit 2**。防崩溃循环慢速空转。
- **明确不做**：coordinator supervisor —— 它崩了靠 runner 的 **resume 手动重跑**（已完成集跳过），代价可接受。
- **测试**：新增 5 个用例（拒绝覆盖 / resume 放行 / 下限告警三态 / 重启上限放弃 / given_up → exit 2）；全量 **93 passed / 1 skipped**，ruff clean。
- **真实评测 E2E（2026-10-15，2 卡 / 8 unit / 8 集 / `max_steps=150`，逐个注入）**：
  - **unit 超时**（`run.unit_timeout_sec: 5`）：14 unit 被 kill → `repair_rounds=3`、`ok=False missing=8`、**exit 2**、打印 `coverage INCOMPLETE … needs a human`。
  - **hung server**（`SIGSTOP` server_0）：看门狗日志 `server 0 is hung; terminating it before restart` → `back up`；`server_restarts=1`、`repair_rounds=2`、**`ok=True missing=0 infra_failed=0`**、exit 0。
  - **崩溃循环**（反复 `SIGKILL` server_0）：3 次 restart 全失败 → `giving up … needs a human`；`servers_given_up=[0]`、**exit 2**；且 `ok=True missing=0`（剩余 server + 补跑兜住）。
  - **`output_dir` 冲突**：同目录重跑 → **exit 1** + `already exists -- refusing to overwrite`。
  - **`policy_call_ms` 下限**：`service_ms=500 / policy_call_ms=500 / N=16` → 启动即告警 `below the queue-wait floor 16000ms`。
- 🔴 **测试中发现的 bug（已修 `683b455`）**：看门狗重启 **hung** server 时**不先终止旧进程** → 旧进程仍占端口 → 重绑失败 → 永远恢复不了。修：`_restart` 先 `_terminate` 仍存活的进程。

### 2.18 分片重构：per-benchmark planner 表（2026-10-15）

- **起因**：用户问"这套 CPU-sim 都支持吗"。查出**分片契约只对 LIBERO 成立**——`episode_offset` 只有 `libero_runner` 有；`_task_ids` 的任务枚举是 libero-only；`task` 被硬编码成 `int`（而 simplerenv/robotwin/maniskill 用**字符串 task name**）。
- **拆法**：新增 **`orchestrator/planning.py`**——`benchmark_family()`（从 `run.py` 移来，两边共用）+ 5 个 planner（libero / episode_indexed×2 / robotwin / calvin）+ `PLANNERS` 表 + `plan_shards()`。
  - **shard 变成纯数据**：`{"id", "overrides": {config 段: 补丁}, "expected"}`。
  - `_shard_config()` 退化成"deepcopy + apply overrides + 去 coordinator-only 键"，**不认任何 benchmark**。
  - `_shards`/`build_shards`/`_suites`/`_task_ids` 从 coordinator 移除（进 planning）。
- **校验同步改成 benchmark-无关**（`validation.py` 重写）：不再展开 `(suite, task, episode)` → **按 shard 的 `expected` 计数 + `episode_id` 去重**；`duplicates` = **两个不同 shard 声明了同一集**（同 shard 重跑是预期重叠）；`short_shards()` 找"短了的 shard"，**补跑 = 重跑这些 shard**（新 worker 目录；libero/calvin 有 resume 只补缺的，其余靠去重）。
- **`run.py`**：`_benchmark_family` 改为从 planning 导入的别名（同一真相来源）。
- **测试**：新增 `test_planning.py` 7 个 + 重写 `test_validation.py` 7 个；全量 **99 passed / 1 skipped**，ruff clean。
- **仍未做**：runner 侧的细粒度支持 → 见待办 **T2-6**（`01` §5.8 有对照表）。

### 2.19 M3 判据修正 + batch 探针（2026-10-15）

- **起因**：用户质疑"batching 是不是可以做？至少 CPU-sim 显存没用满"。
- **我原来的推论有错**：我说过"GPU 大量空转 → 做 batching 只会让 GPU 更空"。**不完整**——batching 降低的不只是 GPU 耗时，还包括**每请求的固定开销**（Python 调度 / 数据搬运 / VAE encode·decode / launch）；而这里**绑定资源就是串行 server**，**server 每请求成本降下来 = 上限直接抬高**，跟 GPU 忙不忙无关。显存也确实是空的（单 server 24.7/81.9 GB）。
- **正确判据**：一次真实推理的 wall 里 **固定开销 vs 串行依赖链** 谁占主导。**没量过就不能下结论。**
- **探针**：新增 `orchestrator/batch_probe.py`（诊断入口，不动评测路径）：
  1. **拆分**：profile 真实 `predict_action` → `wall_ms / cuda_ms / host_ms / gpu_share` + top 5 kernels；
  2. **缩放**：抓真实 `FastWAM.infer_action` 调用（**内层已是 batch-general**，从张量读 `batch_size`），按 batch 展开重放 → `total_ms / per_sample_ms` vs batch 1/2/4/8。绕开 `predict_action` 的 `batch==1` 断言（`modeling_fastwam.py:349`，且它只取 `images[0]`）。
- **探针实测（2026-10-15，chaos 全量跑完后在空闲卡上跑）**：
  - **SPLIT**：`wall_ms=655.5`、**`cuda_ms=680.6`**、`host_ms=-25.1`、**`gpu_share≈1.04`**（多 stream 重叠 → 比值不精确，但**推理期间 GPU 饱和、host 开销 ≈ 0** 是确定的）。
  - **SCALING**：batch=1 得 `total_ms=593.4`；**batch 2/4/8 全报错** —— `input_image must have shape [1,3,H,W]`。
  - 🔴 **"固定开销主导"假设被证伪**；收益只能来自**大 batch 的 SM 利用率**。
  - ⛔ **batch 契约真的不存在**：batch=1 断言散在 **5 处**（`mot/fastwam.py::_encode_input_image_latents_tensor`、`wan/core.py` ×2、`mot/idm.py`、`mot/joint.py`），且编码器是 `input_image[0].unsqueeze(1)` + `vae.encode([image])` 逐样本 API。
- 🔑 **第二轮：临时放开契约后实测（跑完已还原，`git status` 干净）**：`total_ms` batch 1/2/4/8 = **583.4 / 584.4 / 595.7 / 635.0**，`per_sample_ms` = **583 / 292 / 149 / 79** → **7.3× @ batch 8**。插桩验证 batch 真进了去噪（`latents_action=(K,32,7)`）。
- 🔴 **正确性门**：T-13 只证明"跑得动"，**没证明"结果没变"** → M3 必须先过 **T-06**（batch=1 vs batch=K 逐样本比对，需 `infer_action` 接受 **per-row seeds**）——方案已写进 `03_testing.md` T-06。
- **结论**：✅ **M3 从"暂缓"改为 GO，且是量级最大的剩余杠杆**。前置 = 补 batch 契约（6 处入口断言全在推理侧 + 修返回路径 + 批内噪声逐行 seed + server 侧 batcher）。⚠️ 端到端收益受 CPU 侧约束（Amdahl），**要实测**，别把 7.3× 当端到端。

### 2.20 全量 chaos 实测：故障零扰动（2026-10-15）

用户要求"跑全量真实评测、中间捣乱"。8 卡 / 2000 集 / 单 coordinator，跑到中途连注 4 类故障：

| 注入 | 时刻 | 反应 |
|---|---|---|
| 删掉一个 unit 的 2 集记录 | +6 min | 变 missing → **覆盖补跑** |
| `SIGKILL` server_3 | +14 min | 看门狗 `died; restarting` → `back up` |
| `SIGSTOP` server_5（hung） | +24 min | `is hung; terminating it before restart` → `back up` |
| `SIGKILL` 一个在跑的 unit | +34 min | unit 重试/补跑 |

- **最终**：`exit 0`、`elapsed=57.3 min`（干净 M2 53.2 min → **+4.1 min / +7.7%**）、`server_restarts=2`、`servers_given_up=[]`、`failed_units=[]`、**`validation.ok=true`**（missing/duplicates/unexpected/unkeyed 全 0）、`infra_failed=0`、`1929/2000=96.45%`。
- 🔑 **逐集比对干净 M2 全量：2000 集 0 处不同** → **故障零扰动**（不是"看起来对"，是逐位一致）。
- **代价有界**：2 次 server 重启各 ~100s 重载 + 补跑/重试 ≈ +4.1 min。
- **顺带**：这轮之前我中断一次工具调用，暴露并修掉了 `watchdog.join()` 未 start 就调用 → **server 泄漏**的 bug（`ed7a7b6`）。

### 2.21 单卡多 server vs batch 的 A/B（2026-10-15）

- **背景**：用户提出"单卡多 server 是不是更简单"（不用写 batcher）。用**现有 coordinator** 直接跑 A/B 回答。
- **设计**：同一批 **400 集**（`libero_10` 10 tasks × 40 集、`max_steps=150`）、**同为 128 并发**，只变每卡 server 数：
  - **A**：`gpus:[0..7]`，8 server × 16 sim（1 server/卡）
  - **B**：`gpus:[0,0,1,1,...]`，16 server × 8 sim（2 server/卡）—— 现有代码直接支持（同卡列两次即可）
- **结果**：A 30.3 ep/min（wall 13.20 min）vs B **32.5 ep/min**（wall 12.29 min）→ **worker wall 只快 1.25×**（10.16→8.15 min），总 wall 只快 1.07×。显存不是瓶颈（2×24.7=49.4 < 81.9GB，`server_restarts=0`）。
- **结论**：**强次线性**——多进程共享 GPU 受**上下文切换/驱动串行化**限制（不是 SM 不够）。**"单卡多 server 就够了"不成立**（~1.25×），而 batching 有独立且大得多的空间（7.3×）→ **M3 值得做，且不能靠多开 server 替代**。同时**结案 T-08**（"大 batch vs 单卡多副本" → 选 batch）。
- **caveat**：单 workload 的结论；换 workload / 试 3 server 一卡可能有出入，但 1.25× 这个量级已足以否掉"多 server 替代 batching"。

### 2.22 优化链设计：chunk 客户端化 → batching → 提 N（2026-10-15，仅文档）

- **起因**：讨论 cache hit 的优化空间时厘清——**chunk 现在存在 server 端**（`_chunk_cache`），**评测端每步都发请求**（还带 300KB 图像），命中路径 server 只花 7.7µs、但端到端 ~0.9ms 全是传输。
- **结论（写进 `01` §7.6）**：
  1. **chunk 客户端化**：传 `return_action_chunk=True`（开关已有）+ 评测端加 action 队列（队列空才发请求）→ **请求量 10→1**、图像传输 10→1、**结果逐位一致**；本身几乎不加速，价值是**消掉 90% 无效请求**。
  2. **batching**：chunk 化后**队列天然只装真推理**（hit 不出 sim 进程）→ batcher 无需特判。但 **batch 不会因此变大**：`K ≈ N × t_infer / (10 × t_step)` → `N=16` 时 **K≈5**（实收 ~4×，对照 T-13）。
  3. **提 N**：`N_max ≈ K × 10 × t_step / t_infer` → K=4 时 ~12、K=8 时 ~25（每 server）。**新瓶颈是内存**（N=64×8 ≈ 1.1TB / 1.5TB），CPU 还有余量。
  4. **理想下界** ≈ `max(仿真 121s, 推理 219s) ≈ 3.6 min`（现状 56min）；现实 8–12 min。
- **耦合提醒**：`t_step` 与 `N_max` 耦合 → 换渲染后端（egl）要重标定 N。
- **适用性**：LIBERO/CALVIN/ManiSkill 直接套；SimplerEnv 有 action ensemble、RoboTwin 在外部脚本里，需单独处理。
- **本步只更新文档**（用户正在用 GPU 跑其他任务）。

### 2.23 M3 前两步落地：chunk 客户端化 + server 侧 batcher（2026-10-20）

- **代码**：`return_action_chunk=True` + `_common.ChunkConsumer`；server 在 client-owned chunk 模式下**绕过 `_chunk_cache`**（否则下次请求会回放旧 chunk）。新增 `transport/batcher.py::AsyncBatcher`（`max_wait_ms` 聚合、`max_batch_size`、cache-hit 过滤、每 batch 一个 `Future`），`server.batching` 默认关闭。
- **T-14 实测**（真实 FastWAM + LIBERO）：
  - `libero_10/task0` 10 集 + 随机 `goal/spatial/object` 7 task × 10 集：**80/80 集 success/steps 与旧 server-cache run 完全一致**。
  - 请求量：`task0` 10 集 **302 requests / 2969 env steps = 9.83×**，逐集严格 `ceil(steps/10)`。
- **batcher E2E**：4 个 sim 共用 1 个 server（`max_wait_ms=50`）→ 51 请求 / 46 batch，mean 1.11、max 2；无 server 错误。
- **T-06 数值等价（2026-10-20）**：`infer_action` 支持 batch + per-row seeds；真实 bf16 模型 `batch_probe --equivalence`：`out_shape=[4,32,7]`，逐行 `max|d|≈0.0039–0.0054`，跨 seed 差 0.236（行独立，无结构错位）。生产 batch 路径已接通（`predict_action` batch + `predict_action_batch`），4 sim E2E 无回退，3/4 集 steps 与顺序基线一致、1 集差 1 步（bf16 噪声）。
- **未做**：真 batch 的 GPU 加速实测（T-13 的 7.3× 需在 batch 路径上复测）；重扫 N。

---

## 3. 待办（按依赖排序）

> 每条格式：目标 / 改动 / 验收 / 产出。

### T0-1 server 侧服务时间插桩 【✅ 已完成】

- **交付**：`transport/timing.py`（`LatencyRecorder`，env 开关、mean/p50/p95/p99、原子落盘）+ `rpc_server.py::_predict_action_timed` + `unit_tests/test_server_timing.py`（8 条，9 passed）。
- **验证**：真评测（LIBERO `libero_10` task0 × 2 集）跑通，`timing.json` 出数、与 `inference_latency_ms` 交叉验证一致（max 554.8ms 精确相同）；**开关关/开逐集结果完全一致**（非侵入）。详见 `03_testing.md` T-01。
- **关键发现**：服务时间**双峰**（cache hit ~7.7µs vs 真推理 ~490ms）→ `N_min` 用**平均每请求 ~49ms**，非 `avg_inference_latency_ms` 的 ~492ms。
- **未做**：server 侧"队列深度"——同步阻塞下数不到个数。改用等待时间 `e2e - t_infer`；平均深度 Little's Law `L ≈ λ×W`；瞬时深度等 M3。

### T0-2 N-吞吐验证实验 【依赖 T0-1】

- **目标**：把"串行下 GPU 接近 100%"从推演变实测。
- **步骤**：1 server + N sim，N=1/2/3/5/8，采 `utilization.memory` 时间序列 + 吞吐曲线 + server 忙占比。
- **必须按类各跑一个**：CPU-sim 用 LIBERO；GPU-sim 用 RoboTwin/SimplerEnv/ManiSkill 之一。
- **验收**：能画出 N-吞吐曲线并给出拐点（`03_testing.md` T-02）。

### T1-1 external server 模式 【✅ 已完成】

- **目标**：server 可被多个 orchestrator 共享，且不被误杀。
- **改动**：`config.py` 加 `server.external_url`（→ `external_url`，并从 URL 推 `host`/`port`）；`server_manager.py` 的 `ManagedServer` 在 external 模式下 `start`/`ensure_running`/`stop` 全 no-op。
- **验收**：✅ E2E 通过（1 共享 server + 1 纯 client orchestrator，server 未被误杀）。见 `03_testing.md` T-03。

### T1-2 纯 client 启动 【✅ 已并入 T1-1，无需单独做】

- 原计划改 `run.py` 5 处 `_run_*_once`；实际**不需要**——external 逻辑放进 `ManagedServer`，`run.py` **一行未改**。

### T1-3 N 标定：报告 + agent 决策 + 配置缓存 【依赖 T0-1 + T1-1】

- **目标**：扫 N 产出**标定报告**，由 agent 决策出**并行配置**，缓存 `(model, benchmark, gpu)`。
- **方法**：`01_architecture.md` §6.3–§6.5（报告规范、agent 决策、校验/兜底）+ §5.4（配置 schema）。
- **验收**：报告三件套（`report.json`/`report.md`/`env.json`）+ agent 产出**合法**配置（过 schema + 边界校验）；至少覆盖 CPU-sim 一个 + GPU-sim 一个（`03_testing.md` T-02）。

### T1-4 GPU-sim 放置决策（同卡 vs 异卡）【⏸ 暂缓】

- **暂缓原因**：**当前环境不支持 GPU-sim**（RoboTwin / SimplerEnv / ManiSkill 跑不起来）。**统一等 CPU-sim 完成后一起做**。

- **目标**：定下 GPU 仿真下 sim 与 server 同卡还是异卡（默认同卡，见 `01_architecture.md` §4.4）。
- **方法**：同一 GPU-sim benchmark 分别跑同卡/异卡两种放置，比 wall-clock、`t_infer`（含抖动）、显存峰值（`03_testing.md` T-07）。
- **验收**：给出推荐配置；若同卡显著拉长 `t_infer` → 改异卡，并回填 §4.4 与风险栏。

### T1-5 最小 coordinator（一行命令）【✅ 已完成】

- **目标**：**一条命令**起 1 server + N worker、跑完并聚合，**不再手起 server**。
- **改动**：新增 `orchestrator/coordinator.py`（起 server → 按 task 轮询切 N 片 → 起 N 个 worker 子进程，每个 = `orchestrator.run --config <shard.json>` 带 `external_url` → 聚合）；`run.py` 加"**编排型 config（有 `coordinator:`）→ 分发到 coordinator**"。
- **不做**（留 M2/T2-2）：队列、按 ckpt 分片、按资源分池、看门狗、episode-chunk 抢占。
- **验收**：✅ 一条命令 `orchestrator.run --config run.yaml` 跑通 1 server + 2 worker（各 1 task、rc=0、聚合 2 集、分片无重叠）。见 `03_testing.md` T-03。

### T1-6 episode-chunk 分片（+ runner episode offset）【✅ 已完成】

- **目标**：分片细到 **episode-chunk**，使 `N` **不再受"任务数"限制**——全量每 server 需 N≈16，而 libero_10 只有 10 个 task。
- **改动**：① `libero_runner` 加 `--episode-offset`（+ config `benchmark.episode_offset`）；② `coordinator.py` 分片从 `task` 改成 `(task, episode_offset, episode_count)`（`build_shards`，chunk 大小按目标 `n_workers` 推）。
- **验收**：✅ 2 个 task × 4 集 / `n_workers=4` → **4 个 episode-chunk 分片**（N=4 > 2 任务），**8 集恰好覆盖一次**（worker_i 各领 `(task, eps[0,1])`/`(task, eps[2,3])`）。见 `03_testing.md` T-03。

### T2-1 shard/worker 契约 【✅ 已完成，见 §2.11】

- **目标**：统一 5 个非统一 runner 的入口。
- **结论**：**不新增 adapter 层**——`run.py` 已按 `benchmark.name` 分发到 family runner，故"统一入口"= **一份普通 eval config**：`benchmark.{suite,task_ids,episode_offset,episodes_per_task}` + `server.external_url` + `run.output_dir`（+ `run.timestamped_output=false`）→ `results.jsonl`（含 `episode_id`，可 resume）。coordinator 的每个 unit 就是一个这样的 `orchestrator.run` 子进程。
- **验收**：✅ 同一条契约驱动 coordinator 的多 server 分片（T-05 E2E）。多 family 交叉验证待有第二个 CPU-sim family 时补（GPU-sim 环境不支持）。

### T2-2 coordinator 动态化 【✅ 已完成，见 §2.11】

- **目标**：把 **T1-5 的最小 coordinator** 升级为动态调度（抹平长尾，卡不空转）。
- **改动**：`coordinator.py` 用**全局队列 + 有界进程池**（`pending` + `free_slots`，跑完即补位）；chunk 细到 1 集；失败**重试一次**；每 server 落独立 config 定端口。
- **粒度**：**episode-chunk 级**（不是 task 级）。
- **验收**：✅ 多卡 E2E（2 server / 4 并发 / 8 unit）动态补位、无重叠无缺失、失败重试；✅ 全量对照已量化（静态基线 §2.12 + **M2 全量 §2.13：56.2 min / 96.45%，vs 基线 1.68×、vs 1卡1server 3.09×**）。

### T2-3 server 自愈（看门狗 + 连接重建）【✅ 已完成，`57dcef9` + `3cc3a2d`】

- **目标**：server 挂掉后自动恢复，不让评测中断、也不被 300s 连接等待拖尾。
- **改动**：① coordinator 加看门狗线程（查进程 + `/healthz`，死了**原地重启**）；② runner 把 websocket 连接类异常（`ConnectionClosed*`）归为 `server_unreachable`（触发 client 重建 + `ensure_running`）；③ client 的 `_wait_for_server` 300s 对 unit 级缩短 / 或让 unit 快速失败。
- **验收**：✅ kill 掉一个 server → 看门狗**同端口重启**（`server_restarts=1`）、**`infra_failed=0`**、8 集全补齐、218s 跑完（不再 300s 卡死）。见 §2.15 复测。
- **依据**：§2.15 的故障注入实测；设计见 `01_architecture.md` §5.6。

### T2-4 健壮性兜底（unit 超时 / 收尾信号 / 竞态 / hung 探活 / 运维陷阱）【✅ 已完成，见 §2.16 + §2.17】

- **依据**：§2.16 / §2.17 的失败模式梳理。
- **明确不做**：coordinator supervisor（靠 resume 手动重跑）。
- **只写方案**：GPU-sim 的 `policy_call_ms` 推导（`01` §5.7 末）。

### T2-5 分片 planner 表（让所有 benchmark 进得来）【✅ 已完成，见 §2.18】

- **仍未做**：各 runner 的细粒度分片支持（CALVIN offset / SimplerEnv·ManiSkill N 集循环 / RoboTwin 正式参数）。

### T2-6 runner 侧细粒度分片支持【⬜ 待做，无环境验证】

- **背景**：§2.18 的 planner 表让 5 个 family 都能被切，但**粒度**受 runner 限制。
- **三项**（对照表见 `01_architecture.md` §5.8）：
  1. `calvin_runner` 加 `--sequence-offset`（现在 `eval_sequences[:n]` 只能取前 N 条 → **整个 CALVIN 只能 1 片**）；
  2. `simplerenv_runner` / `maniskill_runner` 支持"跑 N 集"（现在 **1 集/进程** → 每集重建 SAPIEN scene）；
  3. `robotwin_runner` 别用**正则 patch 源码**设 `st_seed`/`test_num`（现在静默易失效）。
- **为什么先不做**：环境里**只有 LIBERO 能跑**（CALVIN 依赖未装 / RoboTwin 缺 EGL 驱动 / SimplerEnv·ManiSkill 依赖未装）→ **改了没法验证**。
- **边界**：这三项**要动 runner**，超出"编排层"约定 → 建议**单独一轮、按 family 分开提交**。

### T3-1 server 侧 batcher 【阶段 3，暂缓】

- **前置**：阶段 1/2 完成 + FastWAM 补 batch 契约（`modeling_fastwam.py:349`、`mot/fastwam.py::infer_action` 4 处）+ 批内噪声语义定义。
- **改动**：`_route_message` 与 `policy` 之间插 batcher（入队 + `Future` + `max_wait_ms` 聚合）。

---

## 4. 关键约束与坑（接手前必读）

1. **一个 server 只服务同一 ckpt**。不同 ckpt / 不同 action dim（LIBERO 7D vs RoboTwin 14D）不可共享 server，队列必须按 ckpt 分片。
2. **`t_gpu≈0.5s` 是 CPU-sim 下量的**。GPU-sim 同卡时 sim 渲染会拉长 `t_infer` → 按 0.5s 算的 N 系统性偏大。GPU-sim 必须重测。
3. **`N`（每 server 几个 sim）和 `G`（几张 GPU）是两个旋钮**，日志里分开记。
4. **长尾套件（libero_10，单集 ~60s）要在代表性分片上标定**，否则 N 被拉偏。
5. **N 过大 → CPU 超订**（128 核打满），单 sim wall-clock 被拉长，吞吐反而降。找拐点，别一味加大。
6. **结果文件并发写**：worker 各自独立文件，聚合按 `(suite,task,episode)` 去重。
7. **排队等待撞客户端超时**：串行 server → 第 N 个 sim 每步等 `≈(N-1)×t_infer`；客户端 `--policy-call-timeout-ms` **默认 5000ms**（`libero_runner.py:588`）→ N>10 直接爆，GPU-sim 同卡更早。**超时须按 N 放宽**。
8. **server 默认不安全**：`PolicyServer` 默认 `host="0.0.0.0"` + `max_size=None` + 无认证（`rpc_server.py:26/46`）。共享长驻后须收紧（绑 localhost/内网 + 帧上限）。
9. **队列前提是单机单 FS**：`os.rename` 原子性只在同一文件系统成立，跨节点 / NFS 失效（Ray Serve 已评估、因依赖重不选）。
10. **公开仓文档规范**（若本文档要进开源仓）：注释用英文、内部路径用 `/path/to` 占位符、真实值放 gitignored 的 `*.internal.*`、提交前跑 `sensitive_scan`。本目录现已随分支入库（`loongforge/embodied/eval/eval_update/`），提交前需按此规范处理。

---

## 5. 复现 / 环境速查

- 评测入口：`python -m loongforge.embodied.eval.orchestrator.run --config <yaml>`。
- 单次评测内部：`run.py:_run_once` → `_run_<family>_once` → `ManagedServer.start()` → `<family>_runner.run_batch/run_evaluation`。
- LIBERO 配置目录由 `ensure_libero_config` 生成（`server_manager.py:87`），`LIBERO_ROOT` 默认 `/workspace/LIBERO`。
- server 启动命令见 `server_manager.py:120 start_server()`；`server_kind` 支持 `loongforge`/`pi05`/`mock`。
- 现有全量评测脚本：`runs/release_and_eval.sh`（静态分片 + 波次补跑）。

---

## 6. 立即下一步（Next Action）

**从 T0-1 开始**：给 `transport/rpc_server.py::_route_message` 加 `t_infer` + 队列深度插桩，然后用它跑 T-01（vanilla 1:1 标定），拿到 `t_sim`/`t_infer` 后按 §6 算 `N_min`，再进 T-02 小扫。

> 不先做这一步的原因没有——阶段 1 的所有参数（N、是否同卡）都依赖它，跳过就是盲搜。
