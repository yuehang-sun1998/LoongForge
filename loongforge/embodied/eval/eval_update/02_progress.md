# LoongForge 评测编排改造 —— 开发进度 / 交接文档

> **本文用途**：交接。任何人 / agent 读完本文即可接手，无需回读对话历史。
> **配套文档**：`01_architecture.md`（做什么、怎么做）、`03_testing.md`（怎么验）。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `7847a83`，已 rebase），仓库 `/workspace/LoongForge-Github`。
> **最后更新**：2026-10-15。
> **一句话状态**：**M0+M1+M2 已实现并验证。** 静态 8 卡基线全量 94.2 min / 95.65%；**M2（单 coordinator 全局队列）全量 56.2 min / 96.45% → vs 基线 1.68×、vs 1卡1server vanilla 3.09×**。M3（batching）暂缓（GPU 未成瓶颈）。

---

## 0. 接手须知（先读这段）

1. **改动落点在编排层**：`loongforge/embodied/eval/orchestrator/`。**不要**动 runner、server、model、benchmark。
2. **代码仓库 / 分支**：改上游代码 / 提 PR 用 `/workspace/LoongForge-Github`（真正的 git 仓库，`origin`=个人 fork `yuehang-sun1998/LoongForge`，`upstream`=`baidu-baige/LoongForge`）；`/workspace/LoongForge` 等是旧克隆，仅供读代码。
   - **本改造的所有开发都在分支 `feat/eval-orchestration-parallel` 上进行**，基点 `upstream/master` @ `7847a83`（已 rebase）。
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
| 0 | **N 扫描 + 报告 + agent 决策**（CPU-sim） | 🔄 CPU-sim ✅；GPU-sim 显存基线 ✅，N 扫描待做（见 §2.30） | 探针 + `orchestrator/calibration_report.py` | `03_testing.md` T-02 | 需空闲 GPU |
| 0 | server 侧服务时间插桩 | ✅ 完成 | `transport/timing.py` + `rpc_server.py` | `03_testing.md` T-01 通过 | — |
| 0 | 标定报告生成器（三件套） | ✅ 完成 | `orchestrator/calibration_report.py` | 单测 3 passed | — |
| 0 | 配置校验 + 缓存 | ✅ 完成 | `orchestrator/calibration_config.py` | 单测 7 passed | — |
| 0 | **标定正式入口（扫 N）** | ✅ 完成 | `orchestrator/calibrate.py` | 单测 3 passed | 报告 + 校验/缓存 |
| 1 | external server 模式（+ 纯 client） | ✅ 完成 | `server_manager.py` + `config.py` | `03_testing.md` T-03 | — |
| 1 | **最小 coordinator（一行命令起 1 server + N worker）** | ✅ 完成 | `orchestrator/coordinator.py` | `03_testing.md` T-03 | external ✅ |
| 1 | **episode-chunk 分片（+ runner episode offset）** | ✅ 完成 | `coordinator.py` + `libero_runner.py` | `03_testing.md` T-03 | T1-5 |
| 1 | N 标定（报告 + agent 决策 + 配置缓存） | 🔄 报告/校验/缓存 ✅；扫描走手动探针 | `calibration_report.py` + `calibration_config.py` | T-02 | 插桩 ✅ + 最小 coordinator |
| 1 | GPU-sim 放置决策（同卡 vs 异卡） | 🔄 环境已具备、显存基线完成；同卡/异卡 `t_infer` 对比待跑（A2–A4 已实现，见 §2.30） | `coordinator.py` + `calibrate.py` | T1-4 / T-07 | GPU-sim 环境 ✅ |
| 2 | shard/worker 契约 | ✅ 完成（由 `run.py` 统一分发吸收，**无需新 adapter 层**，见 §2.11） | `run.py` + 各 `*_runner` | `03_testing.md` T-04 | — |
| 2 | coordinator **动态化**（队列 + 按 ckpt 分片 + 按资源分池 + 看门狗） | ✅ 完成 + 全量对照（**全局队列 + 有界进程池 + 失败重试**；56.2 min / 3.09×，见 §2.11–§2.13） | `coordinator.py` | `03_testing.md` T-05 | 契约 |
| 2 | episode-chunk 粒度抢占 | ✅ 完成（chunk 细到 1 集，快槽可抢下一个 unit） | `coordinator.py` | `03_testing.md` T-05 | 队列 |
| 2 | **运行后覆盖校验 + 自动补跑** | ✅ 完成 | `validation.py` + `coordinator.py` | `03_testing.md` T-09 | — |
| 2 | **server 自愈（看门狗 + 连接重建）** | ✅ 完成（`57dcef9` + `3cc3a2d`） | `coordinator.py` + `libero_runner.py` | `03_testing.md` T-10 | §2.15 |
| 2 | **健壮性兜底**（unit 超时 / 收尾信号 / 竞态 / hung 探活） | ✅ 完成（`dc07c75`/`2e28ce0`/`e5f077c`/`cf9abb6`） | `coordinator.py` + `run.py` + `transport/client.py` | `03_testing.md` T-11 | §2.16 |
| 2 | **运维陷阱兜底**（output_dir 冲突 / 超时下限 / 崩溃循环） | ✅ 完成（`a01390b`/`2d43e73`）；GPU-sim 只写方案 | `coordinator.py` + `run.py` | `03_testing.md` T-11 | §2.17 |
| 2 | **分片：per-benchmark planner 表** | ✅ 完成 | `planning.py` + `coordinator.py` + `validation.py` | `03_testing.md` T-12 | §2.18 |
| 2 | **GPU-sim 编排**（worker 绑卡 / `pools.gpu_sim` / `placement` / 显存校验） | ✅ 代码完成 + 8 卡 N=1 验证（每卡 server+sim，wall 172.8s）；N 扫描待跑 | `coordinator.py` + `calibrate.py` | `03_testing.md` T-02/T-07 | §2.30 |
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
- **T-06 数值等价（2026-10-20）**：`infer_action` 支持 batch + per-row seeds；真实 bf16 模型：
  - **同 seed 受控（主判据）**：4 次 bs1 与一次 bs4、seed 42 → 参考之间/批内行之间均逐位相同（spread=0），逐行 `max|d|=0.00488`（纯 bf16 归约差）。
  - **per-row seeds（补充）**：`--seeds 1,2,3,4` → `out_shape=[4,32,7]`，逐行 `max|d|≈0.0039–0.0054`，跨 seed 差 0.236（行独立，无假 batch）。
  - 生产 batch 路径已接通（`predict_action` batch + `predict_action_batch`），4 sim E2E 无回退。
- **未做**：真 batch 的 GPU 加速实测（T-13 的 7.3× 需在 batch 路径上复测）；重扫 N；**多卡共享 CPU/内存对 N 标定的影响（T-16）**——N 是 per-server，CPU/内存整机共享，并行异 N 扫描会混淆，正式标定需 8 卡同 N 或加修正。

### 2.24 rebase 后回归验证（2026-10-21/23，已完成）

> rebase 到 `upstream/master @ 7847a83` 后，对分支自有功能重新实测；upstream 已合入的 F1–F12 不重测。

- **CPU**：`loongforge/embodied/eval/unit_tests` **129 passed / 1 skipped**，ruff clean。
- **集成冒烟**（1 server + N=2，4 集）：4/4 成功，`validation.ok=true`、missing/duplicates/unexpected=0、`infra_failed=0`，elapsed 161.9s；manifest 正确记录实际 `env_seed_mode`。
- **chunk 生效**：254 steps / 16 requests、346/22，约 `steps/16`。
- **batch 数值等价**（真实 FastWAM + 本 ckpt）：
  - fp32 同 seed bs4 vs 4×bs1：`max|d|=7.0e-4`，参考/批内 spread 均为 0 → **结构性正确**。
  - bf16：相对误差 ~1.7%（per-row 逐行 0.008–0.031，跨 seed 差 1.84）→ 绝对值随动作幅度放大，仍非行错位。
- **batch 缩放**：591.8 → 88.8 ms/sample（batch 8，**6.7×**；与 rebase 前 7.3× 同量级）。
- **batching ON vs OFF**（N=8/卡，2 卡，80 集）：**逐集 0 差异**（success/steps/failure_reason），请求数与步数完全相同；elapsed 415.2 vs 418.2s（`max_wait_ms=5` 下几乎不形成 batch，端到端无可测收益，符合预期）。
- **故障注入**：
  - **kill server**：watchdog 发现进程退出 → 重启，10/10 集完成、`rc=0`；`server_restarts=1`。
  - **删记录 + resume**：删掉 1 条记录（该 worker 只有 1 集）→ 二次运行带 `run.resume: true` → 最终 10/10、`missing=0`、`duplicates=0`、`infra_failed=0`。
  - **SIGSTOP 挂起 server**：发现**真实缺口**——`PolicyClient._wait_for_server` 里 metadata 的 `conn.recv()` 没有 timeout，server 被 SIGSTOP 后 TCP 仍可连接，watchdog 线程永久阻塞在 connect 阶段，9 分钟未重启（只有 SIGCONT 后才恢复）。
  - **修复**：metadata `recv` 加 `timeout=remaining`（超时关闭连接），`open_timeout=min(30, remaining)`；新增单测 `test_client_bounds_the_metadata_wait`（`test_mock_server_conformance.py`，2 passed / ruff clean）。
  - **复测**：SIGSTOP `21:38:25` → 重启完成 `21:40:53`，共 **2m28s**（≈ 2×probe 间隔 + 3s 超时），符合设计。
- **历史复现（关键）**：`libero_10/task0` 前 10 集按 **`per_task` + `prefer_controller_ee=true` + `controller_force_update=false`** 重跑，与 2026-10-03 accel_c16 历史记录 **10/10 逐集一致**（success/steps/failure_reason）。此前两轮不一致的原因分别是 `per_episode`（场景不同）和 `controller_force_update=true`（首帧观测不同）。
- **待做**：8 卡 N 扫描、2000 集全量；`per_episode` 的逐集参照（M2 原始记录未找到，必要时用旧分支小网格自建）。

### 2.25 N 扫描与 chunk 频率口径（2026-10-22/23，第一轮作废、第二轮完成）

**先钉口径**：所有历史基线——vanilla 官方 harness、静态 8 卡 94.2 min、M2 56.2 min——**都是 `chunk_execute_steps=10`**（config 写 `0` → 默认 10）。`accel_c16/c5`（2026-10-03）只是早期调参，不作为基线，后续全部按 10。

#### (1) 第一轮扫描（coordinator，chunk=16）——结论作废

条件：8 卡同 N、4 suite × 5 task × 10 集 = **固定 200 集/点**、`chunk=16`、`max_wait_ms=5`。

- **cadence 错**：与基线（10）不一致，请求率少 1.6×。
- **并发被 unit 数封顶**：200 集 × chunk=1 → 只有 200 个 unit；N≥25 时 `concurrency = 8N` 已超过 200，实际只有 **200 个 worker** 在跑。
  - 吞吐公式 `N / mean(episode_wall)` 假设每 server 有 N 个 sim；N>25 后这个假设不成立 → **高估吞吐**。
  - 佐证：N=32/40/48/64 的 `elapsed` 都在 **564–583s**，基本不变；所谓 88→175 eps/min 主要是公式口径错误。
- 该轮数据仅作历史记录，**不能用来选 N\***。

#### (2) 内存实测（N=64，coordinator，200 workers）

| 指标 | 实测 |
|---|---|
| 峰值已用内存 | **467GB**（`MemTotal - min(MemAvailable)`） |
| 最低 `MemAvailable` | 1040GB |
| PSS 峰值 | 429GB |
| 单 sim PSS | **~2.0GB** |
| OOM | 无 |

按单 sim 2.0GB 重算上限：`N_mem = 0.8 × 1507 / (8 × 2.0) ≈ 75` → **N=64 安全，75 以上需要监控**。

#### (3) 第二轮：external 模式（N=16/32/48/64/80 完成）

- 8 个 server 常驻（每卡一个），全程只加载一次；
- `chunk_execute_steps=0` → **10**；`max_wait_ms=50`、`max_batch_size=8`；
- **每个 client 跑 1 集**，N per server → 总 episode = 8N，**真实并发 = 8N**；
- 采集：吞吐、单集 wall、server stats 增量（requests/batches/mean_batch）、timing、`MemAvailable`/PSS、CPU。

**跑通前修掉的三个问题**：

1. **batching 一直没开**：`rpc_server.py` 只认 `batching.enabled`，而配置只写了 `max_batch_size/max_wait_ms` → **batcher 从未启动**。这解释了此前 ON/OFF 结果与耗时完全一致、stats 恒为 0。已改为“非空 `batching` 块默认启用，只有显式 `enabled: false` 才关闭”。
2. **高并发握手失败**：512 个 client 同时连接，`websocket handshake` 超时（307/512），client 直接退出、无 results。已在 `PolicyClient._wait_for_server` 里对握手超时做预算内重试，并让驱动按 10ms 错峰启动。
3. **端口冲突根因**：本机 `ip_local_port_range = 10000–61000`，之前用的 14560/16000/22600/24600 都可能被 client 的源端口/TIME_WAIT 占用；改用 **6000–6140**，并用真实 `bind` 而不是 `connect` 探测。

**N=32 / N=64 结果（均 0 失败 client）**：

| N | 完成 | 成功率 | 单集 wall | 容量 | mean batch | CPU | PSS 峰值 |
|---|---|---|---|---|---|---|---|
| 16* | 128/128 | 95.31% | 170.0s | 45.2 eps/min | — | 35% | 286GB |
| 32 | 256/256 | 94.92% | 196.1s | **78.3 eps/min** | 2.85 | 70% | 526GB |
| 64 | 512/512 | 97.27% | 339.0s | **90.6 eps/min** | 3.29 | **80%** | 982GB |

\* N=16 是在 batching 修复前跑的（batcher 未启动），只作参考。

- N=32 → N=64：容量 +15.7%，CPU 70%→80%，内存 PSS 526→982GB；**收益递减**（后续 N=48/80 补充扫描确认平台在 48）。
- **补充扫描（N=48/80，pr240、chunk=10、batching on）**：

| N | 整机容量 | CPU | PSS | MemAvailable 最低 |
|---|---|---|---|---|
| 24 | 77.8 | 59% | 409GB | 1060GB |
| 48 | **81.8** | 74% | 772GB | 690GB |
| 80 | **81.9** | 85% | 1266GB | **197GB** |

N=48→80 吞吐持平，内存却从 772GB 涨到 1266GB → **平台在 N≈48，推荐 N*=48**（与 N=64 吞吐相同但更省资源）。
- ~~全量 2000 集外推：`2000/90.6 ≈ 22 min` + 启动 ≈ 28–31 min → 5.5–6.5×~~ **（已由实测推翻，见下）**。

**2000 集全量实测（pr240 fp32master，chunk=10，N=64，batching on）**：

| 指标 | 数值 |
|---|---|
| 完成 | **2000/2000**，`validation.ok=true`，infra/重试/重启全 0 |
| 成功率 | **96.85%**（1937/2000） |
| worker wall（coordinator 口径） | **43.6 min** |
| 总 wall（含模型加载） | **约 51 min** |
| 实际吞吐 | **45.9 eps/min** |
| 平均单集 | 426.9s（libero_10 774s、spatial 322s、object 336s、goal 275s） |

| 参照 | wall | 提升 |
|---|---|---|
| vanilla 官方（8 卡折合） | 173.7 min | **3.4×** |
| 静态 8 卡基线 | 94.2 min | 1.85× |
| M2（N=16） | 56.2 min | **1.10×** |

- 与 external 试跑的 90 eps/min 差距主要来自：全量换了 pr240 ckpt、请求错峰导致 batch 变小、以及 libero_10 长尾在收尾阶段空转；进程 import 开销按上限估算只占 ~4%。
- **同 ckpt external 对照（2026-10-22，pr240，N=64，512 集）**：512/512、0 失败、成功率 96.09%、单集 334.5s、**稳态容量 91.8 eps/min**、mean batch 3.41、CPU 83.5%、PSS 897GB。
  - 同 ckpt 下 external 容量 91.8 vs coordinator 71.9（由 mean ep 426.9s 反推）→ **coordinator 每集慢约 28%**；
  - 2000 集端到端：coordinator 43.6 min（worker）vs external 按 91.8 eps/min 外推 21.8 min → **调度/进程模型带来约 2× 的墙钟差距**；
  - 归因方向：每集一个 worker 进程（2000 次 import）、持续补位导致请求错峰/batch 变小、以及尾部空槽。**不是 ckpt 或模型问题**。
- **优化项（已验证）**：`benchmark.episodes_per_chunk=4`（一个 worker 连续跑 4 集，保留动态补位；`per_task` 仍强制一 task 一 shard）。2000 集实测：
  - **520 unit、2000/2000、成功率 96.80%、validation ok、0 失败/重试/重启**；
  - worker wall **36.1 min**（chunk=1 为 43.6 min，**-17%**）；
  - 吞吐 **55.4 eps/min**（chunk=1 为 45.9，**+21%**）；
  - 单集 wall 332.0s（chunk=1 为 426.9s）。
  - 仍低于 external 的 91.8 eps/min：剩余差距来自尾部空槽（520 unit 略多于 512 槽）与进程 import，而不是模型。

**当前性能结论（2000 集，pr240，chunk=10）**：

| 配置 | 总 wall | worker wall | 吞吐 | vs vanilla | vs M2 |
|---|---|---|---|---|---|
| chunk=1（每集一进程） | ~51 min | 43.6 min | 45.9 eps/min | 3.4× | 1.10× |
| **chunk=4（每 worker 4 集）** | **~45 min** | **36.1 min** | **55.4 eps/min** | **3.9×** | **1.25×** |

- 对照：vanilla 173.7（8 卡折合）、静态 94.2、M2 56.2 → 当前分别为 **3.9× / 2.1× / 1.25×**。
- external 稳态容量 91.8 eps/min 仍高于 coordinator，剩余差距来自尾部空槽与进程 import；继续优化的边际收益有限。
- 成功率口径不参与性能对比（per_episode vs vanilla per_task，场景集不同）。
- **结论修正**：当前最佳全量实测 **vs vanilla 3.9×**（chunk=4，总 wall ~45 min）；chunk=1 为 3.4×。不是 5.5–6.5×。
- **同 ckpt external 对照已完成**（见上），确认差距来自调度/进程模型而非 ckpt。
- 单请求推理耗时 0.9–1.9s（含 batch 与排队）；`max_wait_ms=50` 下 mean batch 2.85–3.29，batching 确实生效。
- 内存：N=64 时 `MemAvailable` 最低 577GB、PSS 982GB，无 OOM。
- **复现验证（第二次 N=64）**：512/512、0 失败、成功率 96.48%、单集 341.6s、**89.9 eps/min**、mean batch 3.45 —— 与首轮 90.6 eps/min 偏差 <1%，容量可复现。

**batching ON / OFF 平行 A/B**（4+4 卡同时跑、N=32、128 集/臂、整机负载相同）：

| 臂 | 完成 | 成功率 | 单集 wall | 容量 | mean batch | CPU |
|---|---|---|---|---|---|---|
| **ON** | 128/128 | 95.31% | **154.2s** | **49.8 eps/min** | 3.22 | 69% |
| OFF | 115/128 | 94.78% | 250.2s | 30.7 eps/min | — | 47% |

- **batching 带来约 1.6× 吞吐**，单集 wall 也降到 1/1.6；CPU 利用率从 47% 提到 69%（sim 不再空等）。
- OFF 臂有 13 个 client 握手超时（60s 重试预算耗尽），说明关闭 batching 时 server 更容易饱和；这使对比略微偏向 ON，但单集 wall 差异足够大，结论稳健。
- 4 server × N=32 的 ON 容量 49.8 eps/min → 按 8 server 外推 ≈ 99.6 eps/min，与 8 卡 N=64 实测 90.6 一致。

**确定性验证（batching 是唯一非确定源）**：

- **batching OFF**：
  - 同配置重复两轮（8 server × N=16，128 集）：`success/steps/failure_reason` **0 差异**；
  - 跨并发对比（4 server × N=32 vs 8 server × N=16，115 个共同 episode）：**0 差异**。
  - 结论：batch=1 时评测逐集确定，且与并发/server 数无关。
- **batching ON**：两次 N=64（512 集）→ **325 集步数不同（多为 ±1–3 步）**、**4 集成功翻转**（498/512 vs 494/512，差 0.8%，在二项噪声内）。来源是 batch 组成随到达时序变化 + bf16 batched-GEMM 归约顺序。
- **取舍**：要逐位可复现 → 关闭 batching（吞吐约降 1.6×）；要最高吞吐 → 开 batching，报告多轮均值/置信区间。

**定性结论仍然成立**：N 是并发数不是单集加速；单集 wall 随并发上升（N=16 170s → N=64 339s），容量看整机吞吐。

---

### 2.26 T-16 共享 CPU/内存实验（2026-10-23，已完成）

- **方法**：external 模式，7 组串行（避免互相污染），chunk=10、wait=50、max batch=8；每组 1 集/client。
- **结果**：

  | 组 | G | N | 总吞吐 | per-server | mean ep | CPU | PSS |
  |---|---|---|---|---|---|---|---|
  | 单卡 | 1 | 16 | 17.6 | **17.6** | 54.6s | 9% | 37GB |
  | 单卡 | 1 | 24 | 27.4 | 27.4 | 52.5s | 12% | 52GB |
  | 2 卡 | 2 | 16 | 36.2 | 18.1 | 53.0s | 16% | 73GB |
  | 4 卡 | 4 | 16 | 58.4 | 14.6 | 65.8s | 31% | 144GB |
  | 8 卡同 N | 8 | 16 | 70.7 | **8.84** | 108.6s | 51% | 286GB |
  | 8 卡同 N | 8 | 24 | 77.8 | 9.72 | 148.1s | 59% | 409GB |
  | 8 卡异 N | 8 | 12–48 | 78.7 | 4.3–16.1 | 167.8s | 61% | 462GB |

- **结论**：
  - **shared-load penalty ≈ 0.50**（8 卡同 N 的 per-server 吞吐只有单卡一半）→ 单卡标定不能外推，必须 8 卡同 N；
  - 异 N 并行会让同 N 的点偏低（N=16 的 server 5.8 vs 均匀 8.84，**-34%**），验证了“正式标定必须同 N”；
  - N 从 16→24 在 8 卡下只 +10%（单卡 +56%），共享资源吃掉大部分收益；
  - 内存/CPU 随总负载近似线性（PSS 37→462GB，CPU 9%→61%）。

---

### 2.27 补充验证（2026-10-23，已完成）

- **N=48/80 补点**：48 → 81.8 eps/min、80 → 81.9 eps/min（平台），内存 772→1266GB → **N*=48**。
- **T-14 旧 server-cache 路径**：新增 `benchmark.return_action_chunk`（默认 true；false = 旧 server cache，每步请求），已实现并通过单测。A/B（80 集、batching off、pr240）：**success 翻转 0，79/80 集 steps 一致**（1 集 239 vs 240，约 9.8× 请求差）。同卡串行复测该集：client 与 cache **均为 240 步** → **两条路径等价**，A/B 的 1 步差异来自跨进程/GPU 的数值抖动（pr240 在 batching off 下也不是逐位确定）。
- **per_episode 新旧代码对照**：旧 clone `d1b8c9f` vs 当前代码，task0 10 集、per_episode、同 ckpt/协议 → **10/10 steps 完全一致**（含 1 个 700 步失败集），说明 rebase 未改变 per_episode 行为。旧 clone 首次运行在 ep1 出现 `ConnectionClosedError`（server 边界断开且无重试），加 `max_retries=2` 后完成，当前代码健壮性更好。

---

### 2.28 chunk 消费逻辑下沉到 runner 公共层（2026-10-26，单测 + 全量 2000 已过）

- **动机**：T-14 的"持有 chunk、空才请求"原本只在 LIBERO 里实现（`ChunkConsumer` + 手写游标），其余 runner 每步都发完整 observation，`benchmark.return_action_chunk` 对它们等于无效。
- **改动**：`_common.EpisodePolicyStepper` 统一"何时请求 / 如何缓冲 / 如何切动作 / 何时回填 payload builder"；runner 只提供 `request_fn(request_step) -> (data, e2e_ms)`。
  - LIBERO 改为使用 stepper（行为不变，`inference_requests` 改读 `stepper.requests`）。
  - CALVIN / SimplerEnv / ManiSkill 接入 stepper，并新增 `--return-action-chunk`；默认 false → server 每步只回 1 行 → 请求节奏与改造前完全一致。
  - 三个新接入的 runner 同时把 `PayloadBuilder.disable_action_cache`（DreamZero / LingBot-VA 等闭环 chunk 模型）并入开关：这类模型即使没开 chunk 也必须每步真推理（旧行为会被 server cache 回放旧动作）。
  - SimplerEnv 的 action ensemble 传 `chunk_steps=1` 强制每步新预测，`PolicyStep.raw_actions` 保留整段供 ensemble 使用。
- **验证**：
  - `test_chunk_consumer.py` 新增 5 条（请求数 = ceil(steps/N)、与旧 server-cache 路径动作逐位一致、`disable_action_cache` 每步请求、ensemble opt-out、`begin_episode` 清 chunk）；全套 eval 单测 **144 passed / 1 skipped**，见 `03_testing.md` T-18。
  - LIBERO 单集冒烟（pr240 fp32、per_episode、task0 ep12）：client 与 cache 两条路径均 **steps=240 / success=1**，与改造前参照完全一致；请求数 **24 vs 240**。
  - **全量 2000 集**（pr240、per_task + 快照、8 卡、N=64、batching on）：**1933/2000 = 96.65%** vs 参照 **1927/2000 = 96.35%**（+0.3pt）；逐 suite：libero_10 +3、goal +3、object 0、spatial 0；success 翻转 24 上 / 18 下；`validation.ok=true`、0 重启。中途 568 集时会话中断，`run.resume=true` 续跑补齐。
  - **速度**：同为 512 并发，单集耗时中位数 **249.7s → 248.2s**、均值 323.6s → 302.7s，`steps/request` 9.74 vs 9.74 → **没有可归因于本次改动的速度差异**。端到端 wall 不可直接比（新 run 分两段、两次模型加载）：旧一次跑完 2000 集 2129s，续跑段 1432 集 1657s（含加载，且只有 358 个 unit 在跑）。
- **未覆盖**：CALVIN / SimplerEnv / ManiSkill 无可用仿真环境，只做了单测与静态检查；RoboTwin 走官方 evaluator 的 bridge（`bridges/robotwin_policy.py`），未接入，仍是每步请求。

---

### 2.29 运行间可复现性（2026-10-26）

- **结论**：这套 pipeline **不保证逐位复现**。两次完全相同的 run（batching off、仅 output_dir 不同，N=16、1973 集）只有 **93.8%** 的集 steps 相同，success 1917 vs 1918（11 个翻转）→ 跨进程/GPU 的 bf16 数值抖动本身就存在。
- **batching 再叠加一层**：batch 组成由请求到达时序决定，bf16 归约顺序随组成变化。配置相同的两次全量（仅 `episodes_per_chunk` 不同）steps 只有 46.3% 相同；80 集小规模 batch on/off A/B 恰好 100%，不能外推。
- **验收口径**：代码改动看 **success rate / 逐 suite 成功率**（± 运行间噪声），逐集 steps 只作参考。数据见 `03_testing.md` T-19。

---

### 2.30 GPU-sim 环境打通与首轮实测（2026-10-29）

**环境**（`03_testing.md` T-07 预实验）

- RoboTwin stable_2.0 可运行：`script/eval_policy.py` + `task_config/` 布局匹配，assets 接好，curobo 可用；补齐 NVIDIA 用户态 Vulkan 库后 SAPIEN 可渲染。
- 真实 pi05 + RoboTwin 端到端首次跑通（`returncode=0`，20 步，单次真推理 285ms）。三个兼容性结论：
  1. **transformers 必须用仓库 pin 的 5.3.0**；5.12 会在推理时报 `create_causal_mask() got an unexpected keyword argument 'cache_position'`。
  2. 视觉塔 key 命名与 transformers 版本绑定：5.3 下**原始 checkpoint**（`vision_tower.vision_model.*`）正确；5.12 下才需要重命名。
  3. `server.use_bf16` 目前必须为 **false**：推理入口把 embedding/state 固定为 float32，bf16 权重会触发 `mat1 and mat2 must have the same dtype`。
- NFS 冷启动影响很大：权重与 venv 放本地后 pi05 加载 **135s**，NFS 上 >10 分钟。

**显存基线（RoboTwin `adjust_bottle`，独立进程）**

| k（同卡） | 峰值增量 | 每 sim 边际 |
|---|---|---|
| 1 | 4.97GB | 4.97GB |
| 2 | 10.94GB | 5.47GB |
| 4 | 21.87GB | 5.47GB |

→ 近似线性，**边际 ~5.5GB/sim**；进程退出全部释放。k=4 的单步耗时被拉长 3.4×，但测试卡有邻居占用，不能作为同卡争用结论。

**pi05 加载显存**

| 口径 | 数值 |
|---|---|
| fp32 权重（当前可用） | **16.6GB** |
| bf16 活权重 / `nvidia-smi` | 7.72GiB / **15.9GB**（allocator 缓存） |
| `empty_cache()` 后 | **8.7GB** |
| fp32 + `empty_cache` | 基本无收益（最终权重 16.6GB 已复用中间态缓存） |

bf16 的回收收益已由 `fix/eval-server-release-load-cache`（`f093865`）实现，尚未进入本分支。

**8 卡 N=1 首轮（暴露问题）**

- 8 个 server 全部 healthy；**端口基址必须避开临时端口范围**：默认 14560 会让 3/8 个 server 因 `address already in use` 退出，改用 6000 段后正常。
- **worker 没有绑卡**：coordinator 只给 server 设了 `CUDA_VISIBLE_DEVICES`，worker 继承父进程环境 → 8 个 sim 全部落在 GPU 0。结果单 episode 338s、wall 689s，首批 client 连接超时触发重试（16 条记录、去重后 8 集）。
- server 占卡 15.9GB（fp32），GPU 0 峰值 19.9GB（server + 8 sim），其余卡只有 server。

**8 卡 N=1 复测（修复后，2026-10-29）**

又发现并修复了两个问题：

1. **`external_url` 与 `server.port` 不一致**：coordinator 给 unit 注入 `server.external_url=ws://...:6140`，但 RoboTwin runner 从 `server.port`（仍是 base 6125）建 client → 连到不存在的端口，300s 超时。修复：`_shard_config` 从 `external_url` 同时推导 `server.host` / `server.port`。
2. **不稳定 seed 只在 official 的 `expert_check` 分支被捕获**：`disable_expert_check=true` 时正式评测循环无 try/except → 整集崩溃、coordinator 重试同一确定性 seed。修复：worker 副本在评测循环里复刻 `UnStableError → now_seed += 1 → continue`。
3. **重试语义**：新增 `_unit_completed()`——unit 已写出计划要求的全部 episode 时，即使进程返回非 0（模型失败）也不再重跑。修复前 `failed_units` 全满且记录翻倍。

复测结果：

```
failed_units = []          # 不再重试
elapsed      = 172.8s      # 首轮 689s → 332s → 172.8s
unique        = 8/8
records       = 10（2 条重复）
```

- 每卡峰值：GPU 0/2/3/6/7 **21.4–21.9GB**，GPU 1/4/5 **18.1GB**（对应 seed 提前结束）→ **8 张卡都跑上了 sim**
- 单 episode 平均 **82.0s**（53.2–113.2s），首轮全挤 GPU 0 时是 338s
- 2 条重复的来源：跳过 unstable seed 后 `now_seed += 1` 越界进入相邻 shard 的固定 seed 区间（seed 100002 / 100006）

**seed 稳定性重复实验**

- seed `100003` 连续 3 次 `UnStableError`，且都是 `001_bottle`；seed `100001` 两次稳定（另一次是无关 OOM）
- 结论：**同一机器上稳定 seed 可复现**，可以做“预扫描稳定 seed 列表 + 按列表分片”（见待做）；跨机器/驱动仍需复测

**已实现（A2–A4 + GPU-sim 修复，本分支未提交）**

- worker 绑卡：`_shard_config` / `_launch_unit` 注入 `run.cuda_visible_devices`，仅 GPU-sim 生效。
- `pools.gpu_sim.{n_sims_per_server, placement, sim_gpus}`；`placement=co_locate` 时 sim 跟 server 同卡，`separate` 时 sim 用独立卡且拒绝与 server 卡重叠。
- 显存校验 `N*vram_per_sim + vram_model <= vram_total`（`resources.gpu_sim`）。
- coordinator 默认端口基址改 **6000**；`coordinator_result.json` 增加 `sim_gpus` / `placement`。
- `external_url → server.host/port` 同步；`_unit_completed()` 避免“已跑完但返回非 0”时的重复执行。
- `calibrate.py` 对 GPU-sim 产出 `pools.gpu_sim` fallback + `env.json`。
- **GPU-sim 标定入口**：`calibrate.py --n ... --gpus 0..7` 自动识别 GPU-sim family；每个 N 用全部 GPU（每卡 1 server + N sim）、点间串行；`episodes_per_task` 自动抬到 `G×N`；每 server 独立 timing 文件并按请求数加权聚合；采每卡显存峰值；产物 `report.json/report.md/env.json/cache.json`。
- RoboTwin worker 补丁：评测循环捕获 `UnStableError` 并跳过该 seed。
- **seed 跳步**：`plan_robotwin` 注入 `seed_stride = shard 数 × chunk`（≈ 总 episode 数向上取整到 chunk 的倍数），worker 的 `now_seed += 1` 改为 `+= seed_stride`；跳过 unstable 时进入下一轮自己的 seed 区间，不再与相邻 shard 重叠。E2E：`duplicates=0`，8 个计划 seed + 5 个替补（100010/100013/100016/100021/100024），wall 235.6s。
- 单测：`test_coordinator.py` + `test_robotwin_adapter.py` **48 passed**；全量 eval 单测 **154 passed / 1 skipped**。

**待做**

- N=2/4/8 扫描；同卡 vs 异卡 `t_infer` 对比。入口已就绪（`calibrate.py --gpus 0..7`）；2026-10-29 曾因他人 DeepSeek-V3-8MoE 预训练占用 44–54GB/卡而推迟，训练结束后可跑。
- **稳定 seed 预扫描 + 分片**（可选）：`seed_stride` 已消除越界重复；若要跨机器保证稳定 seed 集合，仍可扫描并缓存；或改用全局 seed 游标。
- bf16 eval 支持（autocast + 保留 `_FP32_PARAM_SELECTORS`），可省 ~8GB；当前按配置问题处理，`use_bf16: false` 即可。
- 官方 evaluator 的 `Fail!` 退出码已由 `_unit_completed` 兜住；`unexpected` 已改为允许的替补 episode。

### 2.31 pi05 推理端 batching：契约补齐与实测（2026-10-30）

**背景**：GPU-sim 下 sim 与 server 同卡，4 sim/卡时 `t_infer` 被拉长到 ~2.9s。先确认“batch 一次前向”能把多少固定开销摊薄，再决定是否值得端到端接入。

**补齐的三处契约**（本分支，未提交）

1. `pi05_factory` metadata 增加 `supports_batching: True` 与 `noise_seed`（来自 `Pi05ModelConfig.noise_seed`）。
2. `modeling_pi05.predict_action(..., seeds=[...])`：每行用独立 `torch.Generator(manual_seed=seed)` 生成 `(chunk, max_action_dim)` 噪声，再 `torch.stack` 成 batch 噪声传给 `sample_actions`，保证**每行噪声与 batch=1 时逐位相同**。
3. `GenericPredictActionPolicy._predict_action_batch`：
   - 要求同一 batch 内 cache 模式一致，否则回退串行；
   - 模型输出是 padded `max_action_dim`，**必须裁到 `metadata.action_dim`**（此前缺失，k≥2 直接 reshape 失败）；
   - 成功后按行写回 `_chunk_cache`（legacy 模式）。

**单次前向缩放（GPU 0，单请求、fp32、horizon=32、`max_action_dim=32`）**

| k | 串行总时延 | batch 总时延 | 串行/样本 | batch/样本 | 加速 |
|---|---|---|---|---|---|
| 1 | 287ms | 276ms | 287ms | 276ms | 1.0× |
| 2 | 554ms | 326ms | 277ms | 163ms | 1.7× |
| 4 | 1111ms | 381ms | 278ms | 95ms | 2.9× |
| 8 | 2212ms | 539ms | 277ms | 67ms | 4.1× |

→ 固定开销占比很高：batch=8 的 wall 只有 batch=1 的 1.9×，**每样本成本降到 1/4**。

**数值性**

- 串行自身、同组成 batch 重复调用均 **逐位一致**（diff=0）。
- ⚠️ **2026-10-30 更正**：此前“batch 与串行差 2~3%（`max_abs≈0.46`）”的结论**来自探针 payload 写错**——探针把 `instructions` 写成了字符串 `"adjust bottle"`，串行路径把它原样传给模型后 `instructions[0]` 取到的是首字符 `"a"`，而 batch 路径会转成单元素列表拿到完整指令。两条路径的 prompt 文本不同，所以差异与 batching 无关。已改为 `["adjust bottle"]` 重测。
- 副产品：`pi05.predict_action` 对 `instructions` 为字符串时**静默按字符索引**、不报错，建议加类型校验（生产路径 bridge 传的是 list，不影响评测）。
- 视觉塔的 batch 差异是真实的：同一张图 `embed_image` 在 B=1/B=2 下 `max=1.99e-2`、`mean=3.03e-4`（cuDNN 卷积按 batch 选算法）。
- **修正后的真实差异（2026-10-30，payload 改为 `["adjust bottle"]`）**：串行 vs k=2 batch `max=2.90e-2`、`mean=3.32e-3`（未归一化动作，量纲 scale≈0.9 → 约 0.4% 相对差），与 T-06 的“仅归约顺序噪声”结论一致，**没有结构性差异**。
- 无效的尝试：`torch.use_deterministic_algorithms(True)` 结果完全相同；batch 路径“视觉塔逐样本前向”反而略差（`max=4.41e-2`），不再采用。
- 副产品：`pi05.predict_action` 对 `instructions` 为字符串时会静默按字符索引（`instructions[0] == "a"`），建议加类型校验。
- 验收口径与 T-19 一致：**看 success rate，不看逐位可复现**。

**单测**：`test_server_batcher.py` 新增 3 条（padded 输出裁剪、batch 行写入缓存、混合 cache 模式回退）；全量 eval 单测 **169 passed / 1 skipped**。

**端到端 A/B（RoboTwin `adjust_bottle`，N=4，8 卡 = 8 server × 4 sim，max_steps=200，32 集）**

| 臂 | elapsed | wall（含加载） | success | 吞吐 |
|---|---|---|---|---|
| batching off | 537.0s | 827s | 8/32 = 25.0% | 3.57 ep/min |
| batching on（`max_batch_size=8`、`max_wait_ms=50`） | 561.2s | 843s | 3/32 = 9.4% | 3.42 ep/min |
| **off 复跑（off2）** | — | 835s | **7/32 = 21.9%** | — |

逐 seed 翻转：**off vs on = 11/32**，但 **off vs off2 = 11/32**（同一份 batching-off 配置跑两遍）。

→ 两条结论：
1. **batching 对吞吐无收益**（3.57 → 3.42 ep/min）。原因：请求稀疏（每 sim 每 ~74s 一次真推理，N=4 时每 server 18.4s 一个请求），`max_wait_ms=50` 内几乎不可能聚合；且推理只占单集 wall 的 3~4%，收益上限本就 ~2%。
2. **成功率翻转不可归因于 batching**：RoboTwin 自身 run-to-run 就是 11/32 集翻转（同 seed、同配置、同分片）。所以 A/B 只能得出“吞吐不变”，成功率需要多轮均值 + 置信区间，不能用单轮比较。

**数值不对齐的根因（已定位）**

- 串行与同组成 batch 均逐位可复现；关 TF32 无变化（`0.459` → `0.453`）；同一 batch 内相同 payload 两行逐位相等（无跨行污染）。
- 分段探针发现差异**从视觉塔就存在**：同一张图，`embed_image` 在 B=1 与 B=2 下输出 `max=1.99e-2`、`mean=3.03e-4`（cuDNN 卷积按 batch 选不同算法），随后 prompt embedding / 去噪逐步放大到归一化后 ~2~3%。
- 结论：不是我们代码里的 batch 分支（推理路径无任何 `batch_size` 判断），而是 **视觉塔的卷积 kernel 选择**；10 步 Euler 复合放大。
- 待验证的修复：batch 路径里**逐样本前向视觉塔**（`torch.cat([embed_image(x[i:i+1]) ...])`），语言塔/action expert 仍批处理。若最终 diff 回到 1e-6 量级即证明可做到 batch 不变性。
  - **已验证（2026-10-30）**：无效。逐样本后 `max` 从 `2.90e-2` 变成 `4.41e-2`（略差），说明差异主要来自语言塔/去噪阶段的形状相关 kernel，而不是视觉塔卷积；**结论：接受 batch 数值噪声（≤1%），不做逐样本视觉塔**。

**与 RoboTwin 上游的对照（2026-09-24 `main`）**
- 上游未修复“每步 `get_obs()` 渲染”（详见 `/workspace/markdowns/robotwin_render_every_step.md`），也无 `skip_get_obs_within_replan` 概念；我们写的该键**无任何消费者**。
- 上游近期修的是可复现性（#509 clutter bounds 原地累加 + instruction 全局 RNG 就地 shuffle；#490 跨文件系统 model 选择）。`demo_clean` 下 `cluttered_table: false`，clutter 那条不影响我们；instruction 那条只在“每 worker 多集”时才会咬到。

### 2.32 合入 eval server 加载后释放 allocator 缓存（原 PR #316，2026-10-30）

**来源**：`baidu-baige/LoongForge#316`（已关闭），不再单独合入，随本分支一起提交。

**改动**

- `servers/loongforge_server.py`：新增 `_release_loader_cached_memory()`，在 `build_model_spec()` 之后、`_warmup_model()` 之前调用一次 `torch.cuda.empty_cache()`；只释放 allocator 里的空闲块，不动活权重；有释放时打一行 `reserved X GiB -> Y GiB`。
- 新增 `unit_tests/test_server_memory_release.py`（CUDA / 非 CUDA 两条路径）。

**动机**：pi05 / xvla / GR00T / DreamZero 等工厂先以 fp32 构建再转 bf16，瞬态 fp32 副本与 staging 张量留在 caching allocator 里，`nvidia-smi` 看到的是加载高水位而不是活权重（pi0.5 bf16：15.9GB → 8.7GB，活权重 7.72GiB 不变）。同卡部署的 sim 进程看不到这块内存，会直接影响 `pools.gpu_sim` 的显存预算。

**验证**：新增单测 2 条；全量 eval 单测 **171 passed / 1 skipped**。GPU 侧未复测（沿用 PR 内的 bf16 数据）。

### 2.33 RoboTwin 新入口迁移（`fix/skip-intra-chunk-render`，2026-10-30）

**背景**：RoboTwin 上游已迁移到 `RoboTwin-Platform/RoboTwin`，本地方 `/workspace/RoboTwin` 现位于分支 `fix/skip-intra-chunk-render`（`a9d2f5e`），新增 `skip_intra_chunk_obs`：chunk 内跳过 `get_obs()/update_obs()`，用 `get_proprio_observation()` 只读关节/末端位姿。默认关，`eval_video_log` 打开时自动忽略。

**已核实**

- 该分支**已删除 `script/eval_policy.py`**，只剩 `scripts/` 新入口（`eval_policy.sh` → `eval_policy_xpolicylab.py` / `eval_policy_server.py` / `eval_policy_multitask.py`）。我们的 `robotwin_runner` 硬编码旧路径 + 文本注入，切分支后不可用。
- `XPolicyLab/` 子模块原本为空；已 `git submodule update --init --recursive`（pin `fa431ec`）。
- 新入口已内建 `UnStableError` 捕获（`scripts/eval_policy_xpolicylab.py:637/659/819/840`）与 `max_seed_attempts` 游标 → 我们的 `seed_stride` / UnstableError 补丁可退役。
- `skip_get_obs_within_replan` 是我们自己写的**死键**（RoboTwin 与 bridge 均不读），迁移时删除或由 `skip_intra_chunk_obs` 取代。

**迁移方案（三选一，推荐 B）**

| 方案 | 做法 | 取舍 |
|---|---|---|
| A | adapter 进程内建模型 | 改动小；每 GPU 只能 1 个 server，丢掉 batching/chunk cache/timing |
| **B** | adapter 做 ws 客户端连现有 LoongForge server | **复用全部已有 server 能力**；多一层 hop |
| C | 把 `skip_intra_chunk_obs` 回植到 stable_2.0 的旧入口 | 最快见效，但偏离“用新分支”的决定 |

**B 的工作项**

1. `XPolicyLab/policy/<LoongForgePi05>/{model.py, deploy.yml}`：实现 `ModelTemplate`（`update_obs` / `get_action` / `reset`），内部用 `transport/client.py` 调我们的 server。
2. `robotwin_runner` 改为启动 `scripts/eval_policy_xpolicylab.py`（`--policy_name/--host/--port/--protocol ws/--task_name/--task_config/--test_num/--skip_intra_chunk_obs`）。
3. 语义对齐：seed 从“固定区间 + stride”改为 `seed + max_seed_attempts` 游标；结果文件（`_result.txt`）格式重新确认；worker GPU 绑定与端口分配沿用 coordinator。
4. 先做 **1 GPU / 1 sim / 1 集** 的 spike，再上 8 卡与 A/B。

### 2.35 GPU-sim 吞吐的资源分配模型（2026-11-02）

> ⚠️ **本节所有数字均来自旧权重** `pi0.5_robotwin2` + `max_steps=200/chunk 24` 的旧口径，仅用于建立资源分配模型。新权重 `steps_190000_eval` 的正式基线见 **§2.36**。

**问题**：8 卡 GPU-sim 上，`co_locate`（每卡 1 server + N sim）和 `separate`（server 独占部分卡）到底怎么选。

**模型**：本质是"8 张卡在渲染和推理之间怎么分"，两条约束：

1. **推理需求 ≤ server 容量**
   - 单 sim 每 `chunk_execute_steps` 步请求一次；单步 0.8–1.5s → 每 sim 每 40–75s 一个请求；
   - 单 server 不批时 ~2–3 req/s（0.3–0.5s/次），开 batching 后更高（实测 mean_batch 5.76，排队延迟 8.2s → 0.5s）；
   - `co_locate N=4`：32 sim × 1/38.5s ≈ **0.83 req/s**，摊到 8 个 server → 每个 **0.1 req/s（大量空闲）**；
   - `separate 56 sim`：≈ **0.9 req/s** 全压到 1 个 server → **必须靠 batching**，否则像 sep1 一样排队到 8s。
2. **sim 侧不饱和**：每卡 8 sim 之后吞吐只涨 8%（渲染/CPU 到顶），继续堆收益很小；显存 5.5GB/sim，12/卡仍在预算内。

**判据**：同 sim 密度下，**谁把推理挤到最少的卡上，谁就赢**；batching 是这个挤压成立的前提。

**实测（官方 400 步、chunk 50、skip on、同一策略）**

| 配置 | 并发 sim | 吞吐 | 单步 | 成功 |
|---|---|---|---|---|
| co_locate N=4（8 server + 4/卡） | 32 | 10.80 ep/min | 0.77s | 24/50 |
| **separate 1 server + 8/卡（batching 200ms）** | 56 | **12.21 ep/min** | 1.21s | **3/50** |
| co_locate N=4 + batching 50ms（200 步旧口径） | 32 | 11.50（≈关闭） | 0.76s | — |
| co_locate N=4 + batching 2000ms（200 步旧口径） | 32 | 10.74（−5%） | 0.83s | — |

- **吞吐上 separate 胜**（+13%），且在成功率更低（集更长）的情况下仍然更快；
- **但成功率从 24/50 掉到 3/50**，差异远超 run-to-run 噪声，必须先解释再采用。头号嫌疑：batch 路径固定了 `noise_seed`（`_predict_action_batch` 传 `seeds=[noise_seed]`），而单请求走全局 RNG——两条路径的采样噪声不同。

**待办扫描**（同一口径，排队中）：co_locate N=6/N=8；separate 10/卡、12/卡。

### 2.36 新权重基线：steps_190000_eval（2026-11-02）

> 本节**只包含新权重**数据；旧权重 `pi0.5_robotwin2` 的结果见 §2.35（不可与本节混用）。

**权重与口径**（来自 checkpoint 自带 `config.json`，不是我们猜的）

| 项 | 值 |
|---|---|
| 权重 | `/raid0/workspace/20261026_pi05/pi05_sft_final_chkpts/RoboTwin/steps_190000_eval` |
| 统计 | 同目录 `dataset_statistics.json` |
| action_horizon | **30**（旧跑用的是工厂默认 50，是错的） |
| chunk_execute_steps | 30（执行完整 chunk） |
| eval_image_normalize_mode | **siglip** |
| normalization | q99；3 相机；`resize_with_pad`；fp32 |
| 步数 | 官方 `_eval_step_limit.yml` 逐任务 |
| 参考分数 | README：50 任务 × 100 集、start_seed=100000 → **77.56%**；`adjust_bottle` **96%** |

**链路校验**

| 实验 | 结果 | 结论 |
|---|---|---|
| `adjust_bottle × 100`（start_seed 100000） | **91/100 = 91%** vs 参考 96% | ✅ 链路正确（n=100 的 95% CI 约 ±6pt） |
| `grab_roller × N`（参考 100%） | 各臂均 **100%** | ✅ 选作功能测试任务 |

→ 之前旧权重 12–48% 的低分，主要来自**旧权重 + horizon 50 与该 ckpt 的 30 不匹配**。

**并发扫描（grab_roller，官方 400 步，chunk 30，skip on）**

口径：`steady = sims × 60 / 平均单集耗时`（wall 吞吐会被启动成本放大，仅作参考）。

| 臂 | 模式 | sims | 成功率 | wall | steady | 每 sim 卡 |
|---|---|---|---|---|---|---|
| cl4 | co_locate | 32 | 32/32 | 165s | 12.8 | 1.60 |
| cl6 | co_locate | 48 | 48/48 | 211s | 14.8 | 1.85 |
| **cl8** | co_locate | 64 | 64/64 | 333s | **16.2** | **2.03** |
| sp8 | separate + batching 200ms | 56 | 56/56 | 242s | 14.8 | 2.11 |
| sp10 | separate + batching 200ms | 70 | 70/70 | 541s | **24.8**（见下方异常） | 3.55 |
| sp12 / cl10 | — | 84 / 80 | 待跑 | — | — | — |

要点：
- co_locate 在 N=8 仍**未饱和**（12.8 → 14.8 → 16.2，环比 +16% / +9.5%），已追加 N=10；
- 同密度（8/卡）下 separate 每卡只快 **4%**（2.11 vs 2.03），但少一张 sim 卡 → **总量更低**；
- 当前负载下"推理与渲染抢 SM"已不是主要矛盾（单次推理仅 ~0.4s，请求稀疏）。
- ⚠️ **sp10 数据异常**：10/卡（s/step 1.89）反而比 8/卡（s/step 2.54）快 34%，与"密度越高越慢"矛盾，且两次推理延迟接近（393ms vs 447ms）。最可能是 sp8 运行时机器上有其他负载/降频。**需要在同一时段背靠背重跑 sp8 与 sp10 才能确认**，暂不据此下结论。

### 2.38 计时口径：把"建场景 + I/O"从吞吐里拆出去（2026-11-02）

**问题**：`results.jsonl` 的 `episode_time_sec` 是整集 wall（建场景 + 资产 I/O + rollout + teardown）。短任务（grab_roller ~90 步）里建场景占 90%+，此前所有 grab_roller 的吞吐对比其实在测 I/O 抖动。

**工具**：`orchestrator/step_timing.py`（CLI：`python -m ...step_timing <out_dir> ...`）。用 `trace.json` 每步的 `timestamp_sec` 拆出：

- `compute` = 首末步时间差（rollout）；
- `setup` = episode wall − compute；
- `inference` = Σ `inference_latency_ms`；
- `render` = compute − inference；
- 三种吞吐：`wall / compute / render`（ep/min）。

**实测（新权重、grab_roller、100% 成功）**

| 臂 | sims | wall | setup 占比 | compute | render | 中位单步 |
|---|---|---|---|---|---|---|
| cl4 | 32 | 150s | **91.7%** | 12s | 9s | 71ms |
| cl6 | 48 | 194s | 92.7% | 14s | 10s | 66ms |
| cl8 | 64 | 238s | 93.3% | 16s | 11s | 68ms |
| cl10 | 80 | 197s | 93.4% | 13s | 10s | 68ms |
| sp8 | 56 | 227s | 94.8% | 12s | 10s | 66ms |
| sp10 | 70 | 169s | 93.4% | 11s | 10s | 68ms |
| sp12 | 84 | 226s | 95.1% | 11s | 10s | 69ms |

（对照：blocks_ranking_rgb 1200 步的任务，setup 占 47%，中位单步 82ms。）

**三个结论**
1. **中位单步在 66–69ms 之间，与 sim 密度（4→12 每卡）无关** → GPU/CPU 都没有被渲染打满；
2. **吞吐完全由建场景支配**（占 92–95%），之前的"密度曲线"测的是并发建场景时的 I/O 竞争；
3. 资产在 CFS（`/workspace/robotwin_env/assets`，16GB）已拷到本地 `/root/local/robotwin_assets` 并切换软链，**实测 setup 只从 215s 降到 195s（−9%）** → 建场景**不是 I/O 瓶颈**。

**setup 的真正构成：官方 `check_stable()`**

> ⚠️ **2026-11-05 更正（见 §2.45）**：本段"2500 步 × 60–70ms ≈ 195s"是**错的**——60–70ms 是**带渲染的 env step**，而 `check_stable` 只做纯物理 `scene.step()`（实测 **~29µs/步，整段 0.073s**）。setup 的真实大头是**每个 worker 进程首次初始化（cuRobo/SAPIEN/URDF/RT）约 29–45s**，之后同进程约 1s。以下结论仍成立（2500 步是官方协议、不能跳过），但**它并不昂贵**。

`envs/_base_task.py:161` 在每个 `setup_demo` 里跑 **2000 步物理沉降 + 500 步稳定性观测**（共 2500 步，逐步记录所有 actor 位姿）。按 ~60–70ms/步算 ≈ 150–175s，与实测 195s 吻合。

- 这 2500 步**是官方协议的一部分**，且 2000 步沉降直接决定 episode 的初始状态 → **不能跳过、也不能当免费优化**；
- 结论：**性能测试必须保持官方口径（含稳定性检查）**，不接受"关掉预检换吞吐"的做法；
- **最终指标 = 端到端时间（含建场景）**：建场景是评测的一部分，不能从对外口径里剔除。`step_timing` 的 setup/compute/render 拆分**只作诊断用**，用来解释差异来源，不用来排名配置；
- 因此短任务（grab_roller 90 步）的吞吐上限由这 ~200s 固定成本决定，配置差异只能在长任务（如 blocks_ranking_rgb 1200 步，setup 占 47%）上体现。

### 2.37 派发顺序：task / interleave / LPT（2026-11-02）

**背景**：`_run_batch` 是"全局 FIFO 队列 + 空闲槽补位"，而 `plan_robotwin` 产出的顺序是**任务优先**（一个任务的所有 seed 块连续）。因此 10 个 sim、10 个任务时，前 10 个槽会全部落在同一个任务上——形成"任务波次"，长任务（1700 步）会把整波 sim 一起拖住。

**改动**：`benchmark.plan_order`

| 值 | 行为 |
|---|---|
| `task`（默认） | 历史行为：按任务成波 |
| `interleave` | 每波混合所有任务（任务间 round-robin） |
| `lpt` | 按官方 step limit **降序**排任务，再交错派发 |

只改派发顺序，不改分片覆盖；三种顺序是同一组 shard 的排列（单测校验）。

**为什么用 LPT**：这是并行机调度里的经典启发式（Graham, 1969, *Bounds on multiprocessing timing anomalies*）——**最长任务优先**。makespan 由最后完成的任务决定，把长任务排到后面会让它在尾部"孤零零"地跑。LPT 对 makespan 有 **4/3 近似保证**。

示例（8 槽、8 个 5min 快任务 + 2 个 60min 慢任务）：

| 顺序 | makespan |
|---|---|
| 先快后慢 | 65 min（慢任务等第一波结束才开始） |
| LPT | 60 min |

**估值方式**：用台账 `_eval_step_limit.yml` 的步数上限作为耗时代理（单步耗时近似恒定）；成功提前结束会让实际耗时小于上限，所以这是近似排序。

**Dry-run**（adjust_bottle 400 + blocks_ranking_rgb 1200，各 6 集，8 槽）

```
task       first wave: A A A A A A B B      # 4 个慢任务排到下一波
interleave first wave: A B A B A B A B
lpt        first wave: B A B A B A B A      # 慢任务全部第一波开始
```

**端到端对比**（已排队，`ordtask` vs `ordlpt`）：两任务各 6 集、N=1（8 槽），预期 makespan 1280s → 960–1050s，差约 4–5 分钟。

### 2.34 RoboTwin 多任务分片与官方 step limit（2026-11-02）

**动机**：性能实验一直只用 `adjust_bottle` 单任务（200 步），不能代表 50 个任务的真实工作负载。要在一个 run 内覆盖全部任务，原 planner 不够用。

**改动**

- `planning.py::plan_robotwin` 支持多任务：
  - `benchmark.tasks: [..]` 显式列表，或 `"all"` / `"*"`（从 `<robotwin_root>/task_config/_eval_step_limit.yml` 读全量任务名，需 `env.robotwin_root`）；
  - 每个任务独立 seed 区间与 `seed_stride`（unstable 跳步仍留在本任务自己的区间内）；
  - `benchmark.episodes_per_chunk` 显式指定每 worker 连续跑几集（不设则按 worker 数自动切分）。
- `robotwin_runner` 新增 `benchmark.use_official_step_limits`：为真时**不**把 `max_steps` 写进 `step_limit_override`，让官方 `_eval_step_limit.yml`（400–1700 步）逐任务生效；同时把 `max_steps` 同步成该任务官方值，保证 adapter context / trace / 记录口径一致。

**Dry-run（50 任务 × 5 集，`episodes_per_chunk` 不设）**

- 250 个 shard，250 个 episode；50 个任务各 5 个 seed（如 `adjust_bottle: 100001–100005`）；`seed_stride = 5`。
- 官方步数统计：50 个任务 min 400 / median 500 / mean 620 / max 1700。

**单测**：`test_plan_robotwin_fans_out_over_tasks`、`test_plan_robotwin_tasks_all_reads_the_official_task_set`、`test_plan_robotwin_rejects_tasks_all_without_a_root`、`test_official_step_limit_lookup`；全量 **178 passed / 1 skipped**。

**状态（2026-10-30）**

- **旧入口（stable_2.0）已实现同等能力，无需改 RoboTwin**：`_worker_eval_script` 在 `benchmark.skip_intra_chunk_obs: true` 时把评测循环改成
  `observation = TASK_ENV.get_obs() if model.needs_obs() else _vla_proprio_observation(TASK_ENV)`；
  `ModelClient.needs_obs()` 依据本地 chunk 游标判断（chunk 有剩余 → 不取观测）；`step()` 只在真正发请求时才构建 `canonical_obs`，其余步只读 `joint_action.vector`。
  - **生效前提**：必须 `benchmark.return_action_chunk: true` + `chunk_execute_steps > 0`。server-cache 模式下每步都会发请求，`needs_obs()` 恒为 True，等于没开。
  - 闭环模型（`disable_action_cache` / chunk=1）自动恒 True，不会喂旧帧。
  - 单测：`test_needs_obs_follows_the_buffered_chunk`、`test_worker_script_skips_intra_chunk_render`；全量 **173 passed / 1 skipped**。
  - 旧的死键 `skip_get_obs_within_replan` 已删除，统一为 `skip_intra_chunk_obs`。
  - **batcher 心跳（2026-10-30）**：`AsyncBatcher` 新增 `stats_interval_s`（默认 30s，配置 `server.batching.stats_interval_s`），周期打印 `requests/batches/mean_batch/max_batch/pending`。此前 server 被 SIGTERM 杀掉、`finally` 不执行，`Batcher stats` 永远打不出来，只能靠耗时反推。单测 `test_batcher_logs_periodic_stats`。
  - **实测（2026-10-30，N=4、8 卡、200 步、32 集，两臂均 `return_action_chunk + chunk_execute_steps=24`）**：

    | 臂 | 单步 | 单集 | 吞吐 | 成功率 |
    |---|---|---|---|---|
    | skip off | 2.297s | 459.4s | 3.82 ep/min | 5/32 |
    | skip on | **0.773s** | **154.6s** | **11.29 ep/min** | 4/32 |

    → 渲染占单步 **≈66%**，端到端 **2.96×**；成功率差异在 RoboTwin run-to-run 噪声内。
    → **副作用**：单步变快后请求到达率提高 ~3×（每 sim 每 24 步一次 → 18.5s；N=4 时每 server 4.6s 一个请求），server busy 从 ~13% 升到 ~40–50%，**batching 与 N 的标定需要在这个新 cadence 下重做**。

  - **N=8 复测（2026-10-30，已完成）**：8 sim/server、64 worker、200 步、64 集 → elapsed **315.2s**、**12.18 ep/min**、单步 **1.58s**、成功 6/64。
    对比 N=4 的 11.29 ep/min：**仅 +7.9%**，但 sim 数翻倍、单步慢 2.05×、显存 38GB→~60GB → **N=4 仍是拐点**，N=8 不划算。
  - **新 cadence 下的 batching 调参（2026-10-30，N=4 + skip_obs，32 集/200 步）**：

    | 臂 | elapsed | 吞吐 | 单步 | mean_batch（心跳） |
    |---|---|---|---|---|
    | batching off | 170.0s | 11.29 ep/min | 0.773s | — |
    | on，`max_wait_ms=50` | 166.9s | 11.50 ep/min | 0.763s | 未采集（旧代码） |
    | on，`max_wait_ms=2000` | 178.8s | **10.74 ep/min** | 0.830s | **3.27（max 4）** |

    → `max_wait=50ms` 与关闭无差别（到达间隔 4.6s，50ms 内凑不出批）；`2000ms` 确实凑出 mean_batch≈3.3，但**等待代价大于并行收益**（-5%）。**结论：RoboTwin 这条链保持 batching 关闭**，除非将来出现 lockstep/向量化。
  - **下一步**：用 N=4 + skip_obs 跑成功率验证（多轮均值，单轮不可比）。
- **新入口（XPolicyLab）迁移**：仍未开工，等待确认拓扑。

### 2.39 多集/进程摊销 + separate vs co_locate 终局对比（2026-11-03）

**背景**：§2.35–2.38 定了资源模型、LPT 派发与计时口径后，用新权重 `steps_190000_eval` + 官方协议 + 资产本地化，做两件事：(1) 验证 `episodes_per_chunk`（一个 worker 进程连续跑多集，摊销 torch/SAPIEN import ~100s）本身就是加速手段；(2) 在 3-wave（`episodes_per_chunk=3`）口径下把 separate+batching 与 co_locate 拉通比一次，给出最终推荐。

**`episodes_per_chunk` 加速（separate 10/card，blocks_ranking_rgb）**：单集/进程 6.58 ep/min → `chunk=3` **8.21 ep/min（+25%）**。短任务收益更大（import 占比更高）。

**A/B（blocks_ranking_rgb，1200 步，新权重/官方协议/资产本地化，`episodes_per_chunk=3`）**

| 臂 | 布局 | slots | end-to-end | steady | mean ep | 成功率 | repair |
|---|---|---|---|---|---|---|---|
| bsp10_c3 | separate 70 sim + batched server(200ms) | 70 | 8.21 ep/min | **18.28** | 229.7s | 72/211 (34.1%) | 1 轮 / launched 74 |
| bcl8_c3 | co_locate 8 sim/card | 64 | **10.63 ep/min** | 12.70 | 302.4s | 56/192 (29.2%) | 0 |

**读数**：
- **单集 separate 快 24%**（230s vs 302s）——server 独占卡，不和 8 个 sim 的渲染抢 GPU；这是结构性优势，体现在 **steady +44%**（18.28 vs 12.70）。
- **end-to-end 这轮 co_locate 反超**（10.63 vs 8.21），纯粹因为 separate 踩了 1 个 repair round：4 个单元尾部串行重跑，大部分 slot 空等（效率仅 8.21/18.28≈45%；co_locate 10.63/12.70≈84%）。这是尾部 straggler，不是吞吐天花板。

**推荐**：
- **吞吐优先（全量多任务）→ separate+batching 10/card + `plan_order=lpt` + `episodes_per_chunk=3`**。单集 -24% 的优势在 50 任务上累积；LPT + 多任务调度空间让零散重跑回填进空闲 slot，不再像单任务 210 集那样把尾巴暴露出来。
- **稳定/省心 → co_locate N=8**：不拆 server/sim 卡、不调 batching，0 重启 0 重跑；end-to-end 也不差。
- 两者都保留 `episodes_per_chunk=3`。RoboTwin 链 batching 收益有限（§2.34），separate 侧开 batching 主要是因为 server 独占卡后请求汇聚到单卡，需要聚批喂满；co_locate 每卡自带 server，关 batching 即可。

**全量验证（50 clean 任务 × 5 集 = 250 集，官方 step limit，`plan_order=lpt`，`episodes_per_chunk=3`，2026-11-03）**：两组各占满 8 卡串行跑；separate 另跑一遍（从 t=0 采显存峰值 + 复核方差）。三轮均 `validation.ok=true`、`server_restarts=0`、`infra_failed=0`、`unexpected=0`、`duplicates=0`（补跑重复行被按 `episode_id` 去重）。

| 组 | 并发 sim | 成功率 | worker wall | ep/min | repair | 每卡显存峰值 |
|---|---|---|---|---|---|---|
| separate 70sim + batched server(200ms) | 70 | 152/250 = **60.8%** | 1519s | 9.88 | 1 轮 (launched 127) | sim 卡 ~56GB / server 卡 ~14GB |
| separate（复跑） | 70 | 149/250 = **59.6%** | 1986s | 7.55 | 1 轮 (launched 127) | 同上（实测 gpu1–7 42–56GB，gpu0 14GB） |
| **co_locate N=8** | 64 | 150/250 = **60.0%** | 1502s | 9.99 | 1 轮 (launched 118) | **~57GB/卡**（48–57，实测） |

**结论**：
- **端到端打平、略偏 co_locate**：co_locate 1502s 比 separate 两轮（1519s / 1986s，均值 ~1753s）**更快且更稳**。separate 单集快 24% 的结构优势（§2.39 A/B 的 steady +44%），在全量 50 任务上被 batched-server 瓶颈 + repair 尾巴抵消——两轮 separate wall 差 31%（1519 vs 1986，取决于补跑轮里落进的长任务），方差明显大于 co_locate。
- **成功率三轮一致 ~60%**（60.8 / 59.6 / 60.0）。⚠️ **注意：这三轮都是 `disable_expert_check=true`（expert-OFF），不是官方协议，不能与 README 的 77.56% 对比**——官方 expert_check 会先用脚本专家筛掉不可解种子、只在可解场景上评测（见 §2.40）；关闭后评测集变成原始连续种子，且部分任务 `check_success` 丢了 `play_once` 的簿记上下文 → 双向假阳/假阴。此处 ~60% 仅用于"三轮口径一致性"的内部对照，**不是该 checkpoint 的官方成功率**。官方口径见 §2.40。
- **全量规模下 co_locate 也会补跑**：COLO launched 118（18 个补跑单元），之前单任务 A/B 的"0 repair"只是样本小，任务一多就有 straggler/不稳定种子。
- **显存**：co_locate N=8 ~57GB/卡（server+8sim 同卡）；separate 的 sim 卡（10 sim）也到 ~56GB，server 卡仅 14GB——两种布局的 sim 卡峰值接近（都是 ~8–10 个 sim 的量级），80GB 卡均留 ~23GB headroom。

**最终推荐：co_locate N=8**（+ `plan_order=lpt` + `episodes_per_chunk=3`，batching 关）。理由：端到端同样快甚至更快、方差更小；运维最简单——不拆 server/sim 卡、不维护 batched server、不调 batching 窗口；显存同量级。separate+batching 仅在"server 能独占整卡且 sim 卡能堆到远超 co_locate 密度"时才有净收益，当前 8 卡对称场景下不成立。

### 2.40 官方协议成功率对齐：根因诊断链（2026-11-03）

**背景**：§2.39 的 ~60% 成功率是 `disable_expert_check=true`（expert-OFF）口径，**不是官方协议，不能与 README 对比**。为对齐 checkpoint README（`steps_200000_eval`，clean 50 任务 **77.56%**）逐层排查。README 参照来自 `examples/embodied/pi05/RoboTwin/README.md`（PR #313 产出）；权重 `robotwin_pi05_h30_100k_q99_s1000/checkpoints/steps_{190000,200000}_eval`、RoboTwin `/raid0/workspace/perf_test/RoboTwin`，均在本 pod。

**判别实验（挑偏差最大的 5 任务 × 50，expert-on）**

| 任务 | expert-off(5集) | 190k官方limit | 200k官方limit | s300(max300) | README |
|---|---|---|---|---|---|
| lift_pot | 0–2/5 | 96% | 94% | 98% | 97% |
| place_empty_cup | 1/5 | 98% | 98% | 100% | 99% |
| open_microwave | 4/5 | 78% | 88% | **8%** | 16% |
| put_bottles_dustbin | 4/5 | 62% | 58% | **0%** | 0% |
| handover_block | 0/5 | 2% | 14% | 4% | 72% |

**逐层结论（逐一排除）**

1. **expert_check（主因之一）**：§2.39 低分部分源于 `disable_expert_check=true`。开启后 `lift_pot` 2%→96%、`place_empty_cup` 1%→98%，对上 README。expert_check 是官方协议的**种子筛选闸门**（`eval_policy_client.py:391-421`：脚本专家 `play_once` 先验证场景可解才录取该 seed），**不是 `check_success` 能替代的**——两者角色不同（准入门 vs 判分）。
2. **checkpoint 190k vs 200k**：**排除**。3 个背离任务换 200k 仍同向偏离（microwave 88%、dustbin 58%、handover 14%）。
3. **eval 管线 vs 上游 PR#313**（已合入 `upstream/master 7ea02e4`）：**排除**。`action_decoders/joint.py`（`pi05_aloha_robotwin` decoder）逐字节相同；`payload_builders/pi05.py` 仅差 LIBERO `libero_ee8`（与 RoboTwin 无关）；`modeling_pi05.predict_action` 对本权重语义等价（siglip + q99，与 config.json 一致）。偏差是任务特异的（50 个里仅 3 个），也排除任何全局管线差异。
4. **RoboTwin 版本/判据**：**排除**。README 作者用的 `/raid0/workspace/perf_test/RoboTwin`，这 3 个任务的 `check_success` 与我们 `13c3c47` 逐字相同。
5. **step-limit 协议差（根因）**：README 用固定 `max_steps: 300`（官方 pi05 模板），我们用 `use_official_step_limits`（`_eval_step_limit.yml`，每任务 400–1700）。按 README 的 300 重跑：`open_microwave` 88%→**8%**（README 16%）、`put_bottles_dustbin` 58%→**0%**（README 0%）——塌到 README 水平，**坐实**。这俩是长程任务，300 步做不完→README 低，我们给 5× 步数→高。

**结论**：50 任务里 **47 个对齐协议后与 README 一致**；`open_microwave`/`put_bottles_dustbin` 是 **step-limit 口径差**（我们给 5× 步数，非错误）；**`handover_block` 是唯一真·单点异常**——我们 2–14% vs README 72%，**反向**（我们步数更多反而更低），`max_steps=300` 对它无效（仍 4%），与步数无关，待单独查（instruction / bimanual 交接执行 / 初始位姿）。

**口径铁律**：要和 README 逐任务对比，必须 `max_steps: 300` 固定（关 `use_official_step_limits`）；要测模型真实能力上限才用官方 per-task limit。**两套口径不可混用**。

### 2.41 handover_block 单点异常（未解决，2026-11-03）

**现象**：`handover_block` 我们 2–14%（官方 limit）/ 0%（max 300），README 72%——**反向**（我们低）、`max_steps` 改不动。

**已排除的假设**：
- **chunk / 开环 cadence**：曾怀疑我们用 30 步开环 chunk + `skip_intra_chunk_obs` 破坏了双臂交接。但按 **README 精确口径重跑**（`return_action_chunk=false` → server 端 30-replay cache、关 skip、每步渲染、`max_steps=300`、expert-on、co_locate N=8，`/tmp/ab_t8_ho.json`）仍 **0/42**。而 README 同口径 72%。→ **cadence 排除**（且已确认 README 本身也是 server-cache 30-replay，不是每步重推理；见 §server `loongforge_policy.py:108-148`）。
- **checkpoint / eval 管线 / RoboTwin 判据**：均已在 §2.40 排除。

**关键线索**：expert_check 开着仍能录取到 42 个 seed → **专家 `play_once` 在我们环境里能完成这个 handover、场景可解、`check_success` 正常**；但**策略 42 集全败**。专家能做、策略全挂，且与 README 同 checkpoint/同口径却 0 vs 72。

**收窄到**：策略实际吃到的**输入**与 README 不同——最可能是 **instruction 文本/`instruction_type`**（handover 是语言条件的双臂交接，指令不对就不会做），其次是 proprio/state 某处差异。**下一步**（未做）：调 `handover_block` 的 `trace.json`，看实际喂进去的 instruction + state，与官方 deploy 对比；必要时跑真·闭环（`disable_action_cache=true`）排除最后的 cadence 可能。

**状态**：handover 是 50 任务里唯一未收口的单点；其余 47 一致、microwave/dustbin 是 step-limit 口径差（§2.40）。

### 2.42 调度 spread 修复 + websocket keepalive 修复（2026-11-04）

**调度 spread（§4 坑 #16 的修复落地）**：`coordinator._build_slots` 由"按 GPU 分组"改为 **sim-layer major 交错**（每层每 GPU 一槽）。配合 `free_slots.pop()`（LIFO）→ 前 `len(urls)` 次 dispatch 落在不同 GPU。实测（adjust_bottle×50、chunk=5 → 10 单元 < 64 槽，pre/post 同配置对照）：

| | 单元 GPU 分布 | 用卡数 | worker-elapsed | 成功率 |
|---|---|---|---|---|
| pre-fix | GPU7×8 + GPU6×2 | 2 | 458s | 100% |
| post-fix | 8 卡均摊（1–2/卡） | 8 | **228s** | 98% |

→ **2.0×**，且仅对"单元 < 槽位"的作业生效；满载（单元≥槽位）槽集合不变、零影响。单测 `test_build_slots_dispatch_order_spreads_across_gpus`。

**websocket keepalive（§4 坑 #17）**：sim 客户端长阻塞（check_stable ~195s / 长渲染 / 开环 chunk）回不了 server 的 20s keepalive ping → server 1011 断连（重连自愈但噪声+开销+偶发丢集）。修复 `ping_interval=None`。修复后验证 run（sched_fix）：**1011 = 0**。

**scene-cache / restore 探索结论（不采用）**：曾实现"快照 check_stable 沉降态、restore 跳过第二次 setup"（注入 monkeypatch、RoboTwin 源码不动、`scene_cache_dir` 开关），**等价性验证失败**——restore（`set_pose`+置零速度）没有重建静止接触，物理一 step 物体微动 → adjust_bottle 从 96% 塌到 **25%**（失败全 `not_successful`、无 infra，确认是场景不等价而非通信）。**已整体撤掉**（runner 注入/helper 删除）。教训：`set_pose` 不等于静止平衡；要跳过第二次 `check_stable` 只能走 **"restore + 短重沉降"并验证等价** 或只跳**阶段 B 的 500 步稳定性判定**（该 seed 已在第一次 setup 验证过、确定性 → 无逻辑风险，约省一次 check_stable 的 20%），均未做、留待后续。

### 2.43 check_stable 减步数实验：均正确但无吞吐收益（结论：步数不是瓶颈，2026-11-04）

为实现过两个"减少 check_stable 步数"的注入式加速（`benchmark.skip_second_check_phase_b` / `benchmark.settle_early_stop`，均默认关、RoboTwin 源码不动）。**两者都验证为正确，但端到端几乎不提速**——这否定了"2000 步沉降是 setup 瓶颈"的旧假设（§2.38 曾按 2500 步 × ~70ms/步 推算，是猜的）。

| 臂（adjust_bottle×50, expert-on, 官方 limit, co_locate N=8） | 成功率 | worker-elapsed |
|---|---|---|
| base（原版，无注入） | 96–98% | **228s** |
| phase-B skip（第二次 setup 只跑 2000 步沉降、跳 500 步判定） | 98% | 228s（差异在噪声内） |
| settle early-stop（每 100 步查位姿，静即停） | 96% | 228s（同上） |

**关键实测**：
- **instrumentation 证明 2000 步足够**：所有稳定 seed 的 `drift(step2000→2500)=0.000000m`；早停在**绝大多数场景 100 步**（最小窗口）即判定静止，极少数 200 步；个别慢沉降 seed（如 100000，`drift=9.65mm@2000`）不早停、落回官方判定并被**正确拒为 unstable**——兜底逻辑工作正常。
- 但 **WALL 559/550/543s 三者几乎相同**：把沉降从 2000 步砍到 100 步，端到端只省 ~2%。**说明 setup 的 211s 不在"物理步数"上**，而在每步本身的成本（64 sim 并发下的 CPU 争抢/物理求解）或 setup 的其它环节（场景构建/URDF/专家 rollout）。§2.38 对 setup 的归因不准确，需按"每步成本 × 并发"重估。
- 采样阶段还修掉两个注入 bug：`_poses`/`_settled_at` 是实例方法，注入文本里漏传 `self` → 每次 check_stable 抛 TypeError 被 eval 循环吞成 `error occurs !`、worker 空转。回归测试已加断言扫所有 `_poses(` 调用必须带 `self`。

**处置**：两个开关保留（正确、默认关、有单测），文档标注"**已验证正确但无实测收益，不建议作为加速手段**"。~~真正的 setup 大头仍未归因（一次 8 卡计时实验因计时文件路径写错而失败，未重做）。~~ → **已于 §2.45 归因**：`check_stable` 仅 0.073s；setup 大头是每 worker 首次初始化（cuRobo/SAPIEN）约 29–45s。

### 2.44 expert_check on/off 实测加速（短任务 / 长任务，2026-11-05）

**口径**：co_locate N=8、官方 per-task step limit、`chunk=1`、`episodes_per_episode=50`、seed 100000 起；**唯一变量 `disable_expert_check`**；两个 check_stable 实验开关均关（原版 2500 步 check_stable）。

| 任务 | expert | worker-elapsed | 吞吐 | 加速 | 成功率(仅参考) |
|---|---|---|---|---|---|
| adjust_bottle（400 步，短） | on | 258.6s | 11.60 ep/min | — | 49/50 (98%) |
| | **off** | 214.9s | 13.96 ep/min | **1.20×** | 49/50 (98%) |
| open_microwave（1500 步，长） | on | 540.3s | 5.55 ep/min | — | 38/50 (76%) |
| | **off** | 427.9s | 7.01 ep/min | **1.26×** | 41/50 (82%) |

（含 8-server 模型加载的 WALL：短 549→495s、长 813→697s。）

**结论**：
- 关 expert_check 约**提速 1.20–1.26×（省 17–21%）**，是确定性的开销；**不是**早前按"单集串行成本"估的 1.4–1.6×——**并行吞吐下专家的额外 setup + rollout 被调度摊薄**，只有 ~1/5 落在关键路径上。
- **长任务不比短任务更快**（1.26× vs 1.20×）：专家 rollout 在长任务上同样更长，占比没有明显变小。
- 成功率：短任务 on/off 都是 98%（adjust_bottle 场景基本都可解）；长任务 off 略高（82% vs 76%）属不同 seed 集合的抖动。**注意 expert-off 是不同评测口径**（不筛不可解场景），不能与 README 对比，仅作"换口径"的加速参考。

### 2.45 setup 真实归因：瓶颈是每 worker 首次预热，不是 check_stable 步数（2026-11-05）

用注入式计时（`benchmark.time_setup`，打到 worker 自己的 eval 日志）在**真实 8 卡配置**（adjust_bottle×50、expert-on、官方 limit、chunk=1、co_locate N=8）下测三段耗时：

| 指标 | n | mean | median | max |
|---|---:|---:|---:|---:|
| `init_task_env` **每个 worker 第 1 次** | 50 | **28.7s** | **38.3s** | 45.6s |
| `init_task_env` 后续（同进程） | 52 | **1.06s** | 0.94s | 2.35s |
| `check_stable`（2500 步物理） | 125 | **0.073s** | 0.06s | 0.14s |

**结论（修正 §2.38 / §2.43）**：
- **`check_stable` 只有 0.073s**——§2.38 的"check_stable ≈195s"是把**带渲染的 env step（60–70ms）**误当成**纯物理 `scene.step()`**（~29µs）。**围绕"减少 check_stable 步数"的所有优化注定无收益**（phase-B skip、早停、restore 全落在这里）。
- **setup 的真实大头是每个 worker 进程的首次初始化 ~29–45s**，之后同进程约 1s（cuRobo/SAPIEN/URDF/首次 RT 渲染的一次性预热）。
- **推论：`episodes_per_chunk=1` 等于每集付一次 ~29s 预热**。把 chunk 调大（配合 §4.16 的 spread 修复）可把这笔摊销。
- 次要因素：并发启动时 **CPU 线程超订**会放大该预热（每进程 torch/OMP 线程数应显式限制，而不是跟随宿主核数）。

### 2.46 离线种子缓存（admission cache）：成立前提与适用性（2026-11-05，结论）

**是什么**：expert_check 的录取结果只取决于 `(task, suite, seed)` 与场景/专家栈，**与 checkpoint、策略无关**。所以可以**离线**把每个 task 的"专家有效 seed + `episode_info`"扫一遍存盘，评测时**跳过专家预跑**（只做一次 `setup_demo` + 策略 rollout）。

**收益的账**：预计算要完整跑一遍专家预跑，所以取决于复用次数 K（同一批 seed 被评多少次）：

| K（评几个 checkpoint / 跑几次） | 官方（每次现场预跑） | 离线缓存 | 净省 |
|---|---|---|---|
| 1（单权重、只评一次） | 每个录取 seed 2 次 setup + 1 次专家 rollout | 预计算同样要付这些 | **0** |
| 2 | 4 次 setup | 3 次 | 1 |
| K | 2K | K+1 | K−1 |

→ **单权重一次性评测不划算；K 越大越划算**，且离线后评测端收益 ≈ expert-off 的实测值 **1.20–1.26×**（§2.44），语义不变（仍只在专家验证过的可解场景上评）。

**成立前提（环境绑定，缺一不可）**：

| 依赖 | 说明 |
|---|---|
| **RoboTwin 代码** | commit + **我们的本地补丁**（`open_laptop`/`place_object_scale`/`put_object_cabinet` 的 `arm_tag/origin_z` 移动） |
| **task_config** | `demo_clean` 等（随机化开关、摆放范围） |
| **物理** | SAPIEN/PhysX 版本（默认 **CPU** 物理；多线程求解归约顺序可能带来微小非确定性） |
| **专家规划** | cuRobo / mplib 版本与参数 ± **GPU 型号 + 驱动 + CUDA**——`play_once` 的规划（`planner: curobo`）**跑在 GPU 上** |
| **资产** | 物体/embodiment URDF、本地资产软链指向的内容 |

**不受影响的**：策略模型、推理 kernel、batching——它们**不改变"哪些 seed 有效"**（专家不用策略）；但**会改变每集结果**（不同 GPU kernel/batch 组合有 1–3 步抖动、偶发成功翻转）。**冻结种子 ≠ 逐位可复现**，只保证"考的是同一批题"。

**工程要求**：
1. 缓存**绑定环境摘要**（上述所有项的版本/提交/资产哈希 + GPU 型号/驱动），文件名或元数据里带摘要；
2. 评测启动时**校验摘要**，不匹配 **fail-loud**（报错要求重扫，而不是静默用错种子）；
3. 环境变更（升级依赖 / 换机换卡 / 改任务代码或资产）→ **重扫**，或至少抽样几十个 seed 复核有效性；
4. 注意**边界 seed**：`check_stable` 是"最后 200 步 <3°"的容差判定、cuRobo 也有边缘可行性，跨环境可能翻转少数 seed。

**适用性结论**：**只要我们的仿真环境长期固定**（同一台机、同一套 pin 死的版本/驱动/资产），离线种子缓存就成立——一次性扫描（5000 个有效 seed，64 并发、每 seed ~7–10s，约 **10–15 分钟**量级）换后续每次评测稳定省 ~20%，同时让评测输入固定、可复现。若环境会漂移，则需把"重扫/复核"计入流程。

### 2.47 `offline_expert_check` 实现与实测（2026-11-05）

**目标**：把 expert_check 的"筛选合格 seed"从**每次评测在线做**，变成**离线做一次、评测时直接复用**。RoboTwin 源码零改动，全部在 `robotwin_runner` 生成的 worker 脚本里注入（与 `skip_intra_chunk_obs` 同一机制）。

**两个开关**

| 开关 | 作用 |
|---|---|
| `benchmark.offline_expert_check_scan: <dir>` | 只跑官方专家分支（`setup_demo` + `play_once` + 官方录取判定），把每个合格 `(seed, episode_info["info"])` 写入 `<dir>/<task>.pkl`；**不跑策略**（跳过 `get_model`），且**不启动 policy server**（`server.disabled`） |
| `benchmark.offline_expert_check_scan_shards: <k>` | 扫描分片数（默认 1）。k>1 时每个任务切成 k 个**残差类**（`start+i`、`seed_stride=k`、每个收 `ceil(N/k)` 个），各写 `<task>.part<i>.pkl`；顶层 run 结束后自动 **merge**（去重、升序、截断到 N）生成 `<task>.pkl` |
| `benchmark.offline_expert_check: <dir>` | 消费 `<dir>/<task>.pkl`：`expert_check=False`，按**索引**取 seed 与 `episode_info`，每个 seed 只做 **1 次 `setup_demo` + 策略 rollout** |
| `benchmark.offline_expert_check_offset: <i>` | 消费模式下该 unit 的**缓存起始下标**。由 planner 自动写入（`0, c, 2c, …`），**不要手填**。`start_seed` 始终只表示 seed 值，消费模式下不参与索引（2026-11-05 拆分，见下） |

**`start_seed` 的两套语义已拆开（2026-11-05）**

- 旧实现里消费模式把 `start_seed` 当**缓存下标**用（`_S_OFFSET = start_seed`），与它在在线模式里"seed 值"的语义冲突。实测踩坑：配置写 `start_seed: 100000` → 第 15 个 unit 去取 `cache[100014]`，立即 `cache exhausted`。
- 现在 planner 在消费模式下把 range 起点固定为 0，并在每个 unit 的 override 里写 `offline_expert_check_offset`（0, c, 2c…）；runner 优先用它，只有缺这个 key 的旧 plan 才回退到 `start_seed`。
- 因此消费模式下 `start_seed` 可以继续写真实 seed（如 100000），**不会再影响索引**。单测：`test_plan_robotwin_consume_uses_cache_index_offset`、`test_worker_script_offline_expert_check_modes`。

**与在线流程的差别**（每个合格 seed）

| | 在线 | offline_expert_check |
|---|---|---|
| `setup_demo`（含 check_stable） | 2 次 | **1 次** |
| 专家 rollout | 1 次 | **0 次** |
| 被拒候选 seed 的浪费 | 每次评测都有 | **0**（扫描时一次性承担） |
| 种子集 | 每次现算 | **固定，可复现** |

**分片与失败语义（方案 A：不跳过）**
- 缓存里的 seed 是**已验证稳定且可解**的；同环境下 `check_stable` 是确定性的，正常情况下不会再抛 `UnStableError`；
- 消费端**纯索引分配**（`now_seed = cache[episode_offset + succ_seed]`），各分片互不重叠，**没有任何"跳过"**；
- 若缓存 seed 真的抛 `UnStableError` → 打印 `CACHE STALE` 并 `SystemExit(3)` → 该 unit 失败、coordinator 按既有语义重试；重试仍失败则 `validation.ok=false`。**不静默换题**（跳过会跨分片撞 seed，且会悄悄改变评测口径）；
- 缓存不足（`offset + test_num > len(seeds)`）显式报错，不 IndexError。

**实测（adjust_bottle / open_microwave 各 50 集，co_locate N=8，官方 step limit，chunk=1）**

| 任务 | 在线（expert_check 每集现场跑） | offline_expert_check | 加速 | 成功率 |
|---|---|---|---|---|
| adjust_bottle（400 步） | 260s / 11.55 ep/min | **204s / 14.71 ep/min**（复测 204s，两次一致） | **1.27×** | 98% → 100% |
| open_microwave（1500 步） | 540s / 5.55 ep/min | **476s / 6.30 ep/min**（复测 **456s / 6.58 ep/min**） | **1.14–1.18×** | 76% → 78% / **82%**（复测） |

> 复测（2026-11-05，简化消费逻辑后）：短 204s / 50-50 / ok=true；长 456s / 41-50（82%）/ ok=true。两次结果与首次一致，说明消费端"纯索引分配 + 异常即失败"的简化没有引入回归。

- 与"直接关 expert_check"的 A/B（§2.44：1.20× / 1.26×）同量级 → 省下的正是专家预跑，无额外失真；
- 一次性扫描：2 任务 × 50 seed ≈ 18 min（含模型加载）；全量 50 任务评测约 **2 次即可回本**。

**已知限制**
1. ~~扫描不能分片~~ **已支持（2026-11-05）**：`offline_expert_check_scan_shards: k` 把每个任务的候选 seed 空间按**残差类**切给 k 个 worker（`start+i`、stride=k），各写 `<task>.part<i>.pkl`；顶层 run 结束后自动 merge（去重、排序、截断到 N）。实测 2 任务 × 10 seed × 4 分片：`ok=true`、24/24 占位记录、merge 产出 `adjust_bottle`/`open_microwave` 各 10 个 seed、**0 个 policy server**、WALL 263s。注意**收益随 N 增长**：N 很小时每个分片的进程启动/warmup（~30–60s）会盖过并行收益；N=50 以上才明显（单分片 ~400s → 4 分片 ~200s）。
2. **环境摘要未落地**：缓存尚未记录 commit/版本/GPU/驱动/资产哈希，也**未在启动时校验**。这是当前最大的正确性缺口（详见 §2.46 的前提），建议下一步补 `env_digest` + fail-loud 校验。
3. ~~扫描仍会启动 policy server~~ **已修复（2026-11-05）**：不仅顶层 coordinator 不起 server，**unit 子进程也不再起**（`_shard_config` 在无 `external_url` 且处于 scan 模式时设 `server.disabled=true`，`_run_robotwin_once` 据此跳过 `ManagedServer`）。—— 之前只修了顶层，unit 仍会各自加载一份模型（全量 50 任务扫描会直接 OOM）。实测：**0 个 `policy_server*.log`**、GPU 无模型占用。注意 scan 判断只在**顶层 config**（带 `pools`）生效，避免 unit 递归编排。

**k=1 与分片的关系（2026-11-05 统一）**

- **不分片 = k=1 的特例**：planner 只要看到 `offline_expert_check_scan` 就走残差类分支（k 默认 1 → `start+0`、`stride=1`、`part=0`），因此 k=1 也会先写 `<task>.part0.pkl`、再由 merge 生成 `<task>.pkl` —— 与 k>1 **完全同一条代码路径**（此前 k=1 走老分支，`seed_stride = chunk = N`，修好"记录后按 stride 前进"后会变成跳着扫，属于同一处的隐患，已一并消除）。
- 实测（2 任务 × 10 seed）：**k=1 → ok=true、20/20、WALL 236s**；**k=4 → ok=true、24/24、WALL 263s**。小 N 时 k=1 反而更快（每个分片都要付一次进程启动/warmup）。
- **同一配置内 seed 不重复**：每个分片固定在各自残差类内前进（`start+i`，步长 k），merge 另有去重兜底。实测 k=4 的 4 个 part 共 12 条 → 去重 12 条、重复 **0**；merge 按排序截断到 N（12 → 10）。
- **不同 k 之间 seed 集合可能不同（属预期，非 bug）**：每分片配额 `ceil(N/k)`，若某残差类有效 seed 偏多，其第 4 个会被截断，而别的类里数值更大的 seed 反而进入。实测 k=1 vs k=4：10 个里 **9 个相同**（adjust_bottle 差 `100016`/`100011`；open_microwave 差 `100008`/`100012`），`episode_info` 逐条一致。
- **实践建议**：缓存生成后**固定 k**，不要在两次评测之间改变 k（否则选中的 seed 集合会变、可比性下降）。k 只影响**扫描耗时**与**选中哪 N 个有效 seed**，不影响评测语义（都是专家验证过的场景）。

### 2.48 `episodes_per_chunk` 的取值公式 + N 试跑口径（2026-11-05）

**问题**：`episodes_per_chunk`（下称 `c`）此前只有经验值（单任务 50 集用 1、全量 50 任务×5 集用 3），没有可复用的判据。

**模型**：设 `S = G × N`（并发 slot 数）、`E` 总集数、`A` 每个 worker 进程的一次性成本（torch/SAPIEN import + 首次 env 初始化，实测 ~100–130 s，§2.45）、`t̄` 单集耗时。import 按**波**付、不按集付：

```
T(c) ≈ (U / S) × (A + c × t̄) = (E / S) × (A / c) + (E / S) × t̄
U = ceil(E / c)，且取 U ≥ S
```

> 用**流体比** `U / S` 而不是 `ceil(U / S)`：最后一个不满的波里，空闲 slot 会在别的 unit 结束时立即接下一个，实际 makespan 接近平均值，ceil 会系统性高估（bsp10 场景下两者差 1.48×）。

**取最优**：`c* = max(1, floor(E / S))` —— 让 unit 数 ≈ slot 数，一波跑完、`A` 只付一次且没有空闲 slot。`c` 再大 → `U < S`，有 slot 闲置；`c` 再小 → 多波，每波重复付 `A`。

**数据点核对**

| 场景 | E | S | 公式 `c*` | 实际采用 | 备注 |
|---|---|---|---|---|---|
| 全量 50 任务 × 5 集 | 250 | 64 | 3（3.9 → 3/4 等价） | **3** | §2.39 全量验证 |
| 单任务 × 50 集 @ N=8 | 50 | 64 | 1（E/S < 1） | **1** | §2.44 / §2.47 |
| bsp10 A/B（c=1 vs c=3） | 211 | 70 | 3 | 1 vs 3 | 流体比预测 `3.01(A+t̄) / 1.01(A+3t̄) = 1.24`，**实测 1.25×**（6.58 → 8.21 ep/min） |

第三个点是唯一有 A/B 对照的：模型在「多波 → 少波」区间量得准。（注意必须用流体比；用 ceil 会得到 0.84，方向都反了。）

**三个修正**

1. **异质任务留余量**：`c = floor(E / (2S))`，让 `U ≥ 2S`，LPT 才有回填空间；同构单任务不需要。
2. **repair 粒度**：`c` 越大，一个 unit 失败重跑的成本越高（250 集全量：separate launched 127 vs co_locate 118）。
3. **整除**：`c` 最后取成能整除 `episodes_per_task` 的值，避免多出一个短 unit（无害，但会让 `U` 比预期多 1）。

> 推论：全量 50×5 场景 `c=4` 值得试（`U=63 ≤ 64`，理论上比 3 少半波），本次不动。

**应用：N 试跑口径（Arm A 收尾后执行）**

已定项**不再重扫**：`placement=co_locate`（§2.39，250 集终局对比 1502 s vs separate 1519/1986 s）、batching 关、`episodes_per_chunk` 按上式、`plan_order=lpt`、`sim_cpu_threads=1`、`chunk_execute_steps=30` + `skip_intra_chunk_obs`、fp32、`offline_expert_check` 开。**唯一变量：`N ∈ {6, 8, 10}`**。

- 去掉 N=2/4：历史 cl4 → cl6 → cl8 吞吐 12.8 → 14.8 → 16.2 ep/min **仍在上升**，膝点只可能在 8 之上；真正要回答的是「N=8 是否已过膝点」
- 口径：`E = 16N`（96 / 128 / 160 集），`c = floor(E / S) = 2` → `U = S`，一波跑完
- **丢弃每个 slot 的第 1 集**（含首次 init，§2.45 首个进程 28.7 s）；用第 2 集的 `episode_time_sec` 求 `t̄_ep`，吞吐 ≈ `N / t̄_ep`（每卡）
- **不要用 wall 算 ep/min**：2 集/slot 时 wall ≈ 最慢那个 slot，方差大；wall 只做交叉校验
- N=10 资源（**2026-11-05 实测**）：峰值 **~58–60 GB/卡**（server 9.7 GB + 10 × ~4.8 GB/sim，`adjust_bottle`）。原估 69 GB 偏保守 ~15%：server 按 14 GB（旧口径含 fp32 瞬态副本，实际常驻 9.7 GB）、sim 按 5.5 GB（`blocks_ranking_rgb` 这类更重场景的边际）。80 GB 余 ~20 GB，**N=12 显存也够**（~67 GB），真正的墙会先到 CPU（每 sim 仅 1.6 个逻辑核，16 逻辑核/卡）
- 成本：3 点 ≈ **27–30 min**（每点加载 ~290 s + 运行 3–5 min）；常驻复用见 T2-8（可降到 ~18 min），本轮先不动
- 判定：N=10 与 N=8 差异 <5% → 膝点在 8，维持 N=8；N=10 明显更优 → 再用长任务抽验一次（短任务对 CPU 争用不敏感，可能高估 N 的收益）

**N 扫描的两种口径（2026-11-05 实测，adjust_bottle）**

用 worker `trace.json` 里每步的绝对时间戳拆「启动」与「rollout」：`rollout` = 最后一个 episode 首末步的时间差；`startup` = 最后 episode 首步时间 − unit 起点 − 该 rollout（含进程 import、SAPIEN/首次 env 初始化，以及前一个完整 episode）。

| N | unit 墙钟（c=2） | 启动 | rollout / 集 | 含启动（每卡） | **去启动（每卡）** |
|---|---|---|---|---|---|
| 6 | 204.5 s | 167 s（±15） | 17.0 s（±3.2） | 3.52 ep/min | **21.1 ep/min** |
| 8 | 266.6 s | 221 s（±21） | 20.8 s（±6.4） | 3.60 ep/min | **23.1 ep/min** |
| 10 | 323.8 s | 275 s | 22.0 s | 3.71 ep/min | **27.3 ep/min** |
| 12 | 389.0 s | 341 s | 21.9 s（sd 6.8） | 3.70 ep/min | **32.9 ep/min** |

**配对比较（同一批 48 个 seed，按均值比而非逐 seed 比值）**

| 对比 | rollout 均值比 | 稳态吞吐增益 |
|---|---|---|
| N=6 → 8 | 17.0 / 20.6 = 0.825 | **+10.0%** |
| N=8 → 10 | 20.6 / 22.1 = 0.932 | **+16.5%** |
| N=6 → 10 | 17.0 / 22.1 = 0.769 | **+28.2%** |
| N=10 → 12（36 个共同 seed 配对） | 22.1 / 21.8 = 0.987 | **+21.5%** |

**N=12 资源实测（2026-11-06）**：峰值 **74.7 GB**（gpu0），其余卡 56–70 GB；server 9.7 GB + 12 × ~5.4 GB/sim。**N=14 不可行**（按此边际需 ~85 GB，超过 80 GB），N=16 更不可能。N=12 本身只剩 ~5 GB headroom，全量 50 任务（场景更杂）有 OOM 风险。

- **N=12 用了重新扫的 256-seed 缓存（stride 32）**，与 N=6/8/10 的 160-seed 缓存（stride 16）不是同一批；上表的 10→12 是取 **36 个共同 seed** 做的配对，结论一致（每集耗时基本不变 22.1 → 21.8 s，吞吐提升全部来自 N）

- 校验：`startup + 2 × rollout` 与实测 unit 墙钟一致（N=6：201 vs 204.5；N=8：263 vs 266.6）
- **两种口径结论不同**：含启动 N=6→8 只有 +2.3%、6→10 +5.4%；去掉启动后 6→10 **+20%**。原因是 c=2 的 unit 里启动占 80–85%，把 N 的差异稀释了
- **膝点**：稳态口径下到 N=10 **仍未饱和**（6→8 +10%、8→10 +16.5%、6→10 +28%）；含启动口径下 N=8→10 只快 3%
- **口径怎么选**：单任务 50 集这种「slot 数 ≥ 集数」的场景，启动无法避免（一波跑完，每个 unit 只付一次），按**含启动**口径；全量 50 任务 × 50 集（每个 slot 连续跑几十集）启动被摊薄，按**去启动**口径
- 去启动口径**略偏乐观**：rollout 只算 episode 内的步进，不含每集 `setup_demo`（§2.45 实测后续集 ~1.06 s，量级 ~5%）
- **推荐**：单任务评测维持 N=8（N=10 只快 3%，还要多占 slot）；全量多任务可考虑 **N=10**（显存 60 GB，稳态 +16.5% vs N=8）。**N=12 稳态再 +21.5%，但峰值 74.7 GB、仅剩 5 GB，不建议直接用于全量**；N=14/16 被显存封死
- 推论：当前 `episodes_per_chunk=2` 的扫描本质上是「启动争用」曲线；要单独测稳态吞吐，需要让每个 unit 跑更多集（$c \ge 10$），见 T2-8 阶段 2 的 worker 常驻方案

**Arm B 预期（修正）**：含 8-server 加载（~290 s），短任务 ~494 s（worker 204 s + 加载）、长任务 ~746 s（456 s + 加载）。§2.44 / §2.47 的 **1.27× / 1.18× 是同一份加速代码上 offline vs online 的比，不是对 Arm A（944c5ca）的比**；对 Arm A 的倍数必须等 Arm A 长任务收尾后按同口径计算，**不能用 1.18× 外推**。

### 2.49 Arm A / Arm B 端到端对照（2026-11-05/06，已完成）

**口径**：8 × A800，co_locate，官方 per-task step limit，`adjust_bottle` / `open_microwave` 各 50 集，专家检查开（Arm B 走 offline cache）。Arm A = `944c5ca`（8 个独立进程 × 6–7 集，每卡 1 server）；Arm B = `4ccbe0c`（coordinator + chunk 执行，N=8）。

| 臂 | 任务 | 墙钟 | 加载 | episode 阶段 | 加速 | 成功率 |
|---|---|---|---|---|---|---|
| A | adjust_bottle（400 步） | 1040 s | ~341 s | 699 s | — | 48/50 = 96% |
| **B** | adjust_bottle | **513 s** | 308 s | **199 s** | **2.03×**（episode 阶段 3.5×） | 50/50 = 100% |
| A | open_microwave（1500 步） | 4984 s | ~283 s | 4701 s | — | 32/50 = 64% |
| **B** | open_microwave | **756 s** | 303 s | **453 s** | **6.59×**（episode 阶段 10.4×） | 38/50 = 76% |
| **合计** | | 6024 s | | | **4.75×** | |

- **有效并发只有 ~6.25 sim/卡**：50 集、`c=1` → 50 个 unit，`N=8` 的 64 个 slot 只用了 50 个（GPU 0/1 各 7、其余各 6）。配置里的 `N=8` 只是上限，**N 在这个规模下不起作用**
- **两臂 seed 集不同**：Arm A 分 8 个 block（100000/101000/…/107000），Arm B 用缓存里的连续有效 seed（adjust_bottle 100100–100699、open_microwave 100000–100549）。**墙钟对比有效，成功率只作参考**，不能逐集对齐
- 长任务倍数更高：Arm A 每步渲染 + 每集专家 rollout；Arm B 每 30 步才渲染一次、专家 rollout 被缓存掉，episode 越长省得越多
- 总加速被 8-server 加载摊薄（Arm B 侧加载占 59%），所以 2.0× / 6.6× 低于 episode 阶段的 3.5× / 10.4×

### 2.50 环境移交快照（2026-11-05 20:30）

**已完成**
- `offline_expert_check` 消费模式的 `start_seed` 双语义拆分（新增 `benchmark.offline_expert_check_offset`，planner 自动写入；旧 plan 回退兼容）；单测 86 passed
- N 扫描 N=6/8/10（8 卡、`E=16N`、`c=2`、adjust_bottle、seed 100000 起）：unit 墙钟 204.5 / 266.6 / 323.8 s，去启动稳态 21.1 / 23.1 / 27.3 ep/min/卡，成功率 95/96、124/128、159/160
- 两种口径已写入 §2.48（含启动 vs 去启动），并注明全量场景应看去启动口径
- 显存实测：N=10 峰值 ~58–60 GB（server 9.7 GB + ~4.8 GB/sim），N=12 显存可行（~67 GB）

**未完成 / 下次接手**
1. **N=12**：✅ 已完成（2026-11-06，重新扫了 256-seed 缓存，WALL 763 s，192 集，187/192）；稳态 32.9 ep/min/卡，峰值显存 74.7 GB。**N=14/16 因显存不可行**（需 ~85 / ~95 GB），N 的上限到 12 为止
2. **Arm B（`4ccbe0c`）短/长任务 50 集**：✅ 已完成（2026-11-06，短 513 s / 2.03×、长 756 s / 6.59×，见 §2.49）
3. **5 任务 × 100 集 @ N=12**：✅ 已完成（2026-11-06，1956 s / 500 集 / 80.6%，见 §2.51）
4. **加速前 baseline 的 5 任务 × 100 集对照**：未跑（外推 ~5.8 h）；倍数分解见 §2.51，实测可校正外推误差
5. 长任务成功率差异（Arm A 64% vs Arm B 76%，不同 seed 集）尚未逐集核对

**环境状态（移交时）**
- 所有评测进程已停止，8 张卡 `nvidia-smi` 均为 0 MiB / 0%，无残留 server/worker
- 分支 `feat/eval-orchestration-parallel`（`7157bbe` + 本次提交），未 push
- 本次改动只涉及 5 个文件：`planning.py`、`robotwin_runner.py`、两个单测、本文件

### 2.51 5 任务 × 100 集 @ N=12：端到端实测与倍数分解（2026-11-06）

**口径**：8 × A800，co_locate **N=12**（96 slot），`c=5`（500 集 → 100 unit，约 1.04 波），官方 step limit，offline_expert_check 开，任务：`adjust_bottle`(400) / `grab_roller`(400) / `open_microwave`(1500) / `blocks_ranking_rgb`(1200) / `stack_bowls_three`(1200)，各 100 集。

| 指标 | 值 |
|---|---|
| 总墙钟 | **1956 s（32.6 min）** |
| 加载 / worker 阶段 | 309 s / **1647 s** |
| 集数 / 校验 | 500/500，`validation.ok=true`，`infra_failed=0`，**0 轮补跑、无 OOM** |
| 成功率 | **403/500 = 80.6%** |
| 吞吐（worker 阶段） | **18.2 集/min 聚合 ≈ 2.28 集/min/卡**（含加载 15.3 集/min） |
| 显存峰值 | **~60.7 GB/卡** |

| 任务 | unit 墙钟（5 集） | rollout/集 | 成功率 |
|---|---|---|---|
| adjust_bottle | 616.7 s | 34.1 s | 98/100 |
| grab_roller | 501.8 s | 23.8 s | 100/100 |
| open_microwave | 1067.2 s | 135.9 s | 91/100 |
| blocks_ranking_rgb | 1210.4 s | 120.7 s | 61/100 |
| stack_bowls_three | 1274.1 s | 136.0 s | 53/100 |

**相对加速前（baseline `944c5ca`，外推 ~21,000 s / 5.8 h）**

| 口径 | 倍数 |
|---|---|
| 仅评测（1956 s，外推 baseline） | **~10.7×**（worker 阶段 ~11.2×） |
| **含 offline 缓存扫描**（+~750 s → 2706 s） | **~7.8×** |
| 缓存复用（同任务/seed 集评测 k 次） | k=2 → ~9.4×；k=10 → **~10.5×** |

- **倍数分解**：并发度 ×12（96 vs 8 slot）× 单集效率 ~3.3（chunk 执行 + 边界渲染 + 离线专家）≈ 39×，实际只有 ~11× —— 差额损失在 **启动**（每个 unit 341 s × 100）和 **长任务尾部**（后半段 60 个长 unit 只占 96 个 slot 中的 60 个，36 个空转）
- **baseline 是外推值，未实测**：`blocks_ranking_rgb` / `stack_bowls_three` 的 baseline 单集耗时按步数从 `open_microwave` 外推，误差可能 ±30%
- **倍数强依赖 workload 规模**：50 集的 A/B 只有 2.03× / 6.59×（填不满 slot）；500 集才把并发优势兑现出来。对外表述必须带口径
- 成功率：`blocks_ranking_rgb` 61% / `stack_bowls_three` 53% 与历史 expert-off 口径一致，属任务难度，非基础设施问题

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

### T2-7 `per_task` 的 episode 级分片（RNG 快照）【🟡 已实现（CPU 单测），待 GPU 逐集验证】

- **问题**：`env_seed_mode=per_task` 的场景绑定"进程内第几次 reset"，现行 planner 为保证正确性退化为 task 粒度 → 并发上限 = task 数（单 suite 10 / 全量 40），E=50 时空转与长尾明显。
- **方案**（`01_architecture.md` §5.8.1）：预计算每集 reset 前的 RNG 状态，分片逐集恢复 → 任意 offset 都 O(1)，可恢复 episode 级切片。
- **已实现（2026-10-22）**：
  - `orchestrator/scene_snapshots.py`：dump/load/apply + CLI（`--suite/--task/--seed/--episodes/--out`），产物 `<suite>__task<id>__seed<seed>.pkl` + sha256；
  - runner：`--scene-snapshot-dir`，每集 reset 前恢复状态，record 写 `scene_snapshot`；
  - config：`benchmark.scene_snapshot_dir`；planner 在配置快照时允许 `per_task` 下 episode 级切片，并跳过拒绝兜底；
  - 单测：`test_scene_snapshots.py` 3 例 + planner 2 例；全量 **138 passed / 1 skipped**。
- **GPU 验证（2026-10-22）**：libero_10/task0/seed42 6 集，串行 vs 快照恢复的 `sim state` 与 `agentview_image` 首帧哈希 **6/6 全等**，steps/success 也 6/6 一致；分片（chunk=2）结果与串行一致。
- **全量 2000 对齐（2026-10-22，followups ckpt、per_task + 快照、batching off、N=16）**：2000/2000、`validation.ok=true`、**server_restarts=0**、repair=0、infra_failed=3；成功率 **97.15%**，参照 followups 静态 **97.00%**（libero_10 94.2 vs 94.2、goal 97.2 vs 97.2、spatial 97.8 vs 97.0、object 99.4 vs 99.6）。逐集 steps 一致率 **91.3%**，差异集中在 libero_10 task3/8/9 与 spatial task4，且均为“前几集一致、之后分叉”。**已证明是模型代码数值漂移**：对 task3、spatial task4 的前 10 集，旧 clone `d1b8c9f` 与当前代码的 `sim state` + 首帧哈希 **10/10 完全相同**，而 steps 已在 ep2 / ep9 分叉 → 场景一致、策略数值不同，与快照无关。worker wall **63.1 min**（batching off）。
- **pr240 官方语义全量（2026-10-23，per_task + 快照、chunk=10、N=64、batching on）**：2000/2000、0 重启、0 修复、infra=0；成功率 **96.35%**，vanilla 参照 **96.65%**（差 -0.30%，在 batching ±0.8% 抖动内）；逐 suite：10 92.80 vs 93.20、spatial 97.00 vs 97.60、object 98.40 vs 99.40、goal 97.20 vs 96.40。worker wall **35.5 min**（vs vanilla 173.7 min，约 **4.9×**）。
- **retry / resume（2026-10-23）**：kill server 重试、kill 后 resume 均通过，6 集 steps 与串行基线完全一致；期间修了 coordinator 在 resume 时未创建新 worker 目录的问题（`_run_unit` 现在先 mkdir）。
- **未做**：coordinator 尚未自动生成快照（需先跑 CLI）。
- **验证**：`03_testing.md` T-17（串行 vs 分片逐集首帧哈希、错集反证、retry/resume）。

### T2-8 标定/试跑的 server 常驻复用【⬜ 待做】

- **问题**：`calibrate.py` 的 GPU sweep 是**串行**的，每个 N 点自己起停一次 8-server 池 → 模型加载 ~290 s/点。N=6/8/10 三点里近 15 min 花在重复加载上，占整轮标定的一半。
- **目标**：一次起 8 个 server，多个 N 点的 coordinator 依次复用（走 `external_url`），点间只重启 coordinator。
- **改动**：
  - `calibrate.py::_run_gpu_sweep` 增加 `--reuse-servers`：先起一池 server（每卡一个），`_point_config` 写 `external_url` 而不是 `servers`，跑完只停 coordinator；
  - 顶层 pool 支持 external urls（unit 侧 `_shard_config(external_url=...)` 已支持，顶层 `coordinator()` 需同样接收）；
  - 端口/健康检查/收尾清理在 sweep 层统一管理。
- **验收**：同一配置跑 N=6/8/10，成功率与吞吐与逐点起停一致（差异在噪声内）；总 wall 从 ~30 min 降到 ~18 min；无残留 server 进程、端口不冲突。
- **风险**：模型侧若有 per-client 状态（`loongforge_policy.py`），复用会串味，需先确认无状态；显存不随 N 变化（server 固定，slot 数是 coordinator 侧配置），风险可控。

**阶段 2（同一任务的另一半）：sim worker 常驻 + 动态扩容【⬜ 待做，暂缓实现】**

> 2026-11-05：方案已记录，用户决定**暂不实现**，等阶段 1 落地并验证后再评估。

- **动机**：当前是「一个 unit 一个进程」，每个点 8N 个进程全部重新 import（~100 s）+ 首次 env init（~30 s）。N 从 6 → 8 时，前 6 个/卡的 sim 其实完全可以不销毁，只补 2 个。
- **目标**：一个常驻 worker 进程池，unit 通过控制通道下发；N 变化时只 **扩容/缩容**（6/卡 → 8/卡 = 每卡新增 2 个，旧的不动）。
- **改动**（比 server 常驻大得多，是调度路径改动）：
  - runner 侧新增 worker daemon：一次 import + 一次 `Env` 创建，循环接收 `(task, seeds, episode_info)` 并调用现有 eval 逻辑；unit 之间做 env 状态复位（RoboTwin 本身支持一进程多集，基线就是 8 进程 × 6–7 集）
  - coordinator 侧：pool 从「每 unit 起进程」改为「常驻 + 派发」，支持运行时 `scale(n)`；超时、hung、失败重试、崩溃重启语义要重新定义
  - 观测：每 worker 的 episode 计数、内存增长（SAPIEN/cuRobo 跨集是否有泄漏）、单 worker 最长连续集数（需要回收阈值）
- **收益**：每点省一次 import/init（wall 上约 100–130 s，三点约 5–6 min；叠加 server 常驻后整轮标定 ~10 min）；更重要的是消除「冷启动争用」——N=10 时 80 个进程同时 import 会放大高 N 的劣势，常驻后曲线更接近稳态
- **风险**：① 跨集状态泄漏/显存增长，需要回收阈值与长跑验证；② 改变了测量口径（冷启动从每点付一次变成只付一次），历史 N 数据不能直接对比；③ 与「一 unit 一进程」的失败隔离相比，常驻进程崩一次会影响后续 unit，需要重启+重派逻辑
- **验收**：同一配置下常驻池与逐 unit 起进程的 success/steps 逐集一致；N=6→8→10 原地扩容无残留、无 OOM；worker 连续跑 ≥50 集后内存增长在阈值内
- **建议顺序**：先做 T2-8 阶段 1（server 常驻，改动小、收益确定），阶段 2 单独一个 PR，只在标定路径启用，生产评测保持「一 unit 一进程」的隔离语义。

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
11. **高并发 client 握手会丢集**：512 个 client 同时连接时，server 在 30s `open_timeout` 内可能完不成 websocket upgrade；旧客户端不重试 → 进程退出、无 `results.jsonl`（N=64 时丢了 307/512）。已让 `PolicyClient` 在 connect 预算内重试，并在驱动里按 10ms 错峰启动；单 server 同时握手建议 ≤32。
12. **端口不要落在临时端口范围**：本机 `ip_local_port_range = 10000–61000`，选 14560/16000/22600/24600 等会被 client 源端口或 TIME_WAIT 占用 → server bind 失败。评估用 **6000–6140**，并**用真实 `bind` 而不是 `connect` 探测空闲**。
13. **`batching.enabled` 必须显式或非空块**：旧实现只认 `enabled: true`，只写 `max_batch_size/max_wait_ms` 时 batcher **不会启动**（历史“ON/OFF 没区别”即由此而来）。现改为非空块默认启用，显式 `enabled: false` 才关闭；跑完用 server `stats` 的 `mean_batch_size` 确认真的生效。
14. **batching 会引入 run-to-run 非确定性**：batch 组成随请求到达时序变化，bf16 batched-GEMM 归约顺序随之改变 → 同一配置两次 N=64 有 325/512 集步数抖动（±1–3 步）、4 集成功翻转（约 0.8%）。关闭 batching 在 **accel ckpt** 上逐集完全确定（同配置重复、跨并发均 0 差异）；但 **pr240** 在 batching off 下仍观察到 1 集 1 步的跨进程差异（GPU kernel 非确定归约）。要严格逐位复现，优先关闭 batching，必要时再固定 GPU/改用 fp32。
15. **无 batching 时推理会阻塞 event loop → watchdog 误杀**：`_route_message` 同步调用 `predict_action`，N=16 时 ping 排在十几次推理后超过 15s，watchdog 判 hung 并重启（实测单次 2000 集触发 24 次重启、27 集缺失）。已把无 batching 的推理也放进 `run_in_executor`，让 loop 能及时回 ping；单测 `test_ping_is_responsive_while_inference_runs` 覆盖。

16. **`episodes_per_chunk` 要按"集数 / 槽位"选，否则 unit 少时只用到少数几张卡**：coordinator 把 unit **按整卡 8 个一组打包**下发（不是每卡轮流撒 1 个），所以 **unit 数越少、实际用到的 GPU 越少**。实测（2026-11-03）：handover+lift_pot 共 100 集、`chunk=5` → 仅 20 unit → 全挤在 **3/8 张卡**上，5 张空转、38min 才跑 27 集。选择公式：
    - `E = tasks × episodes_per_task`（总集数），`S = n_sims_per_server × n_servers`（总槽位）；
    - **`chunk = clamp(ceil(E / (k·S)), 1, episodes_per_task)`**：`k=1` 求极致吞吐（import 每槽只摊一次、波数最少，但长短任务悬殊时 LPT 无法细粒度均衡 → 拖尾）；**`k=2~3` 让 unit 数到 S 的 2–3 倍、LPT 能把长任务摊开抗拖尾**，代价是多付 import。
    - `E ≤ S` 时取 `chunk=1`（一波并行，import 并发只付 ~1 次 wall-clock）。注意 `chunk` 是**按 task** 切的，公式在各 task 长度相近、`tasks×episodes` 能整除 `S` 时最准。
    - 反例：`chunk` 过大（如 `E/S` 向下取整）→ 每槽一个大 unit → 长任务槽拖尾；`chunk` 过小且 E≫S → import 成本吃满 CPU（50 个冷 import 把 load 打到 60+）。
    - **调度 spread 修复（2026-11-04）**：`_build_slots` 改为 **sim-layer major 交错排列**（每层每 GPU 一个槽，不再按 GPU 分组），配合 `free_slots.pop()`（LIFO 从末尾取）→ 前 `len(urls)` 次 dispatch 落在**不同 GPU** 上 → 单元 < 槽位时也铺满所有卡。实测（10 unit, adjust_bottle×50）：**pre-fix 挤 GPU7×8+GPU6×2（2 卡）458s → post-fix 均摊 8 卡 228s，2.0×**；成功率等价。**满载（单元≥槽位）槽集合不变、零影响**。

17. **sim 客户端长阻塞 → server 误判死连接（1011 keepalive ping timeout）**：`websockets` 服务端默认每 20s keepalive ping、20s 无 pong 判超时。RoboTwin worker（sim）会长时间阻塞在同步 C 代码（`check_stable` ~195s、长渲染、chunk 开环执行）里不理 socket → server 发 1011 关连接。多数情况下客户端下次推理会重连自愈，但带来日志噪声、重连开销、偶发丢集。**修复（2026-11-04）**：`rpc_server.py` 的 `serve(...)` 加 `ping_interval=None`（关掉 websocket 层 keepalive；连接存活由评测流程 + coordinator watchdog 负责）。注意与坑 #15 方向相反：#15 是 server 自身 event loop 被推理阻塞回不了 client 的 ping；本条是 client 被 sim 阻塞回不了 server 的 ping。

---

## 5. 复现 / 环境速查

- 评测入口：`python -m loongforge.embodied.eval.orchestrator.run --config <yaml>`。
- 单次评测内部：`run.py:_run_once` → `_run_<family>_once` → `ManagedServer.start()` → `<family>_runner.run_batch/run_evaluation`。
- LIBERO 配置目录由 `ensure_libero_config` 生成（`server_manager.py:87`），`LIBERO_ROOT` 默认 `/workspace/LIBERO`。
- server 启动命令见 `server_manager.py:120 start_server()`；`server_kind` 支持 `loongforge`/`pi05`/`mock`。
- 现有全量评测脚本：`runs/release_and_eval.sh`（静态分片 + 波次补跑）。

---

---

## 5.5 与 `paper_review.md`（§7 核对）的对应关系

> 依据：`/workspace/markdowns/loongforge/paper_review.md`（2026-10-19，对 `upstream/master` @ `8cbdcea` 的 §7 逐句核对）。
> **2026-10-20 更新**：本分支已 rebase 到 `upstream/master` @ `7847a83`；F1/F2/F3/F4/F10/F12 已由 **PR #303** 合入 upstream。下面区分"upstream 已实现"与"本分支编排层额外提供"。

### 5.5.1 已实现

| ID | paper_review 的要求 | 实现 | 覆盖度 |
|---|---|---|---|
| **F1** | infra 失败不算"已完成"/不进成功率分母 | upstream `metrics/results.py`（`INFRA_FAILURE_REASONS`、`completed_episode_keys` 过滤、`infra_failed`）+ runner retry/resume；本分支 `validation.py` 复用同一常量 | ✅ 单 runner + 编排层 |
| **F2** | 完整性有独立期望集合、非零退出 | upstream：`expected_counts` + `min_episodes_per_task` + exit 2；本分支：`validation.py` 的 **shard 级** `short_shards`/`validate` + 自动补跑（≤3 轮） | ✅ 两层互补：episode 级 + shard 级 |
| **F3** | 真实 metadata | upstream：`repro_metadata.py` + `run_manifest.json` + `resolved_config.yaml` + `effective_config.json`；本分支 coordinator 额外落 `plan.json`/`coordinator_result.json` | ✅ |
| **F4** | resume 默认 / 目录 / ckpt 校验 / 复用保护 | upstream：`run.resume` 默认 False、目录须存在、ckpt path+sha256、输出目录 guard、manifest 缺失补写；本分支 coordinator 额外有顶层 `resume` 防覆盖 | ✅ |
| **F10** | 统一入口出报告 | upstream：`run.generate_report` 映射 + `report/` | ✅ |
| **F12** | CALVIN repro compare | upstream：benchmark-aware key（`sequence_idx`） | ✅（CALVIN live 环境未验证） |
| **本分支额外** | — | 动态多卡下发、覆盖校验 + 自动补跑、server watchdog、chunk 客户端化、batcher | ✅ |

### 5.5.2 原"计划实现"的处置

原 1–9 项（F1/F2/F3/F4/F10/F12）**已全部由 upstream PR #303 完成**，本分支 rebase 后直接复用，不再重复实现。唯一需要本分支持续维护的是 `validation.py` 与 `metrics/results.py` 的 `INFRA_FAILURE_REASONS` 一致性——现已改为**从 `metrics/results.py` 导入**。

### 5.5.3 不归本分支 / 已关闭

| ID | 处置 | 说明 |
|---|---|---|
| F5 / F6 / F7 / F9 / F11 / F13 | **论文措辞或 docs** | 与本分支的编排改造无关 |
| F8、C1–C4 | **不处理** | paper_review 已判定原句成立或属协议 |

### 5.5.4 rebase 后仍需处理的语义冲突（paper_review §4.3）【✅ 已解决】

> `per_episode`（默认）：分片**不改场景** ✅。
> `per_task`（官方 FastWAM）：场景 = `(run.seed, 该 task 进程内第几次 reset)`，**task 内切分会静默改变场景**。
>
> **处置（方案①：按 task 粒度分片 + 拒绝兜底）**：
> - `plan_libero`：`env_seed_mode=per_task` 时忽略 worker 数，强制 `chunk = episodes_per_task` → **一个 task 恰好一个 shard**（`episode_offset=0`）；`suite_overrides` 里的 `env_seed_mode` 同样生效（逐 suite 判断）。
> - `plan_shards`：LIBERO family 分片后做兜底校验 `_assert_per_task_not_split`——若 `per_task` 下出现 `episode_offset > 0`，或同一 `(suite, task)` 落进多个 shard，直接 `ValueError`（防手写/未来 planner 回归）。
> - **实际模式可追溯**：`env_seed_mode` 同时写进 runner 的 `effective_config.json` 与 episode record metadata（`libero_runner._config_metadata`）。
> - **单测**：`test_planning.py` 新增 5 例（per_task 一 task 一 shard、per_episode 仍切分、per-suite override、offset 切分拒绝、重复 task 拒绝）；全量 `129 passed / 1 skipped`，ruff clean。
>
> **代价与后续**：该方案下 `per_task` 的并发上限 = task 数（单 suite 10、全量 4 suite 40），而 8 卡 × N≈16 有 128 个槽；E=50 时 task 级的长尾和空转会很明显。**episode 级分片 + 官方语义**的方案（预计算 RNG 快照，逐集恢复）已在 `01_architecture.md` **§5.8.1** 定稿，当前**未实现**，验证用例见 `03_testing.md` **T-17**；未启用前保持 task 级分片作为安全回退。
>
> **协议口径提醒（2026-10-21 修正）**：仓库里的 `examples/embodied/fastwam/eval/configs/libero/*.yaml` 和 `/mnt/cfs/sunyuehang/runs/eval_configs_accel_*` 都写着 `env_seed_mode: per_task`，但历史数据的实际语义分两套：
> - **accel_c16 / c5 / nis\***（2026-10-03）跑的是 `/workspace/LoongForge` @ `d1b8c9f`，该 clone **已接线** → 实际 `per_task`（50 集记录 `episode_seed` 全 42）；已验证复现口径 = `per_task` + `prefer_controller_ee: true` + `controller_force_update: false`，`task 0` 前 10 集与历史 **10/10 逐集一致**。
> - **静态 94.2 / M2 56.2 min**（2026-10-15）跑的是**本分支 rebase 前**代码，映射缺失 → 实际 `per_episode`。
> - **对比规则**：对 accel 历史用 `per_task` 组合；对本分支 M2/静态基线显式 pin `per_episode`。两套参照不可混用。
> - 跑完以 `effective_config.json` / `run_manifest.json` 记录的实际模式为准，不要只看源 config。

## 6. 当前计划（Next Action）

**已完成（2026-10-22）**：CPU 单测 130 passed；`per_task` 分片保护；client 握手/超时修复；batching 开关修复；N 扫描（N=64）；batching A/B（1.62×）；确定性验证（OFF 0 差异）；2000 集全量 chunk=1（~51 min / 3.4×）与 chunk=4（~45 min / **3.9×**）；同 ckpt external 对照（91.8 eps/min）。

**待做，按优先级**：

1. **提交代码与文档**：`rpc_server.py`（batching 默认开启）、`client.py`（握手重试）、`planning.py`（`episodes_per_chunk` + per_task 保护）、对应单测、三份 docs。
2. **分数对齐（可选）**：若要与 vanilla 逐集比分数，跑 `per_task + batching off` 的 2000 集（2–3 h，并发 ≤40）；或实现 T-17 RNG 快照，兼顾场景对齐与并行。
3. **T-16 共享 CPU/内存实验**（约 1 h）：A 单 server 独跑 / B 8 卡同 N / C 异 N / D 固定 N 改总负载。
4. **N=48/80 补点**（可选）：确认膝点；N=80 需盯 CPU/内存。
5. **`per_episode` 逐集参照**：用旧分支小网格自建，回答 rebase 是否改变 per_episode 行为（M2 原始记录未找到）。
6. **GPU-sim / CALVIN**：环境具备后再补标定与评测。
7. **GPU-sim 新 cadence 标定**：`skip_intra_chunk_obs` 下重扫 N（**6/8/10**）→ 重做 batching A/B → 成功率验证。口径见 §2.48（`c=2`、每 slot 2 集丢第 1 集、`E=16N`）；placement 与 batching 已由 §2.39 定案，不再重扫。server 常驻复用见 T2-8。
8. **RoboTwin 新入口迁移**（§2.33）：`fix/skip-intra-chunk-render` × XPolicyLab adapter（方案 B：adapter 做 ws 客户端）；先 1 GPU/1 sim/1 集 spike，再上 8 卡。
