# LoongForge 评测编排改造 —— 开发进度 / 交接文档

> **本文用途**：交接。任何人 / agent 读完本文即可接手，无需回读对话历史。
> **配套文档**：`01_architecture.md`（做什么、怎么做）、`03_testing.md`（怎么验）。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `511aa24`），仓库 `/workspace/LoongForge-Github`。
> **最后更新**：2026-10-12。
> **一句话状态**：**设计已收敛，代码 0 行，测试 0 项。** 当前唯一产出是文档与代码核实结论。

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
| 0 | **N-吞吐验证实验**（1 server + N sim 扫 N-吞吐） | ⬜ 未开始 | — | `03_testing.md` T-01/T-02 | 需空闲 GPU |
| 0 | server 侧插桩（`t_infer` + 队列深度） | ⬜ 未开始 | `transport/rpc_server.py::_route_message` | T-01 能出数 | — |
| 1 | external server 模式 | ⬜ 未开始 | `server_manager.py:30-48` | T-03 | — |
| 1 | 纯 client 启动 | ⬜ 未开始 | `run.py` 5 处 `_run_*_once` | T-03 | external 模式 |
| 1 | N 标定（报告 + agent 决策 + 配置缓存） | ⬜ 未开始 | — | T-02 | 插桩 + external |
| 1 | GPU-sim 放置决策（同卡 vs 异卡） | ⬜ 未开始 | — | T-07 | 需 GPU-sim 环境 |
| 2 | shard/worker 契约 | ⬜ 未开始 | 新增 adapter 层 | T-04 | 阶段 1 |
| 2 | 动态队列（按 ckpt 分片 + 按资源分池） | ⬜ 未开始 | 新增 coordinator | T-05 | 契约 |
| 2 | episode-chunk 粒度抢占 | ⬜ 未开始 | 新增 | T-05 | 队列 |
| 3 | server 侧 batcher | ⬜ 未开始（**暂不做**） | `rpc_server.py` | — | 阶段 1/2 + FastWAM batch 契约 |

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
- **内容**：external server 模式 + 纯 client 启动 + GPU-sim 放置决策（同卡/异卡）。
- **交付**：`server_manager.py` external 模式、`run.py` 纯 client、放置结论。
- **验收**：N 个 orchestrator 共享一个 server 不冲突不误杀；CPU-sim 下 GPU util 稳定 >90%。
- 对应任务 T1-1 / T1-2 / T1-4；测试 T-03 / T-07。

### M2 · 动态任务下发
- **内容**：shard/worker 契约（adapter 层）+ coordinator + 队列（按 ckpt 分片、按资源分池、episode-chunk 粒度）。
- **交付**：契约 + coordinator + 队列。
- **验收**：全量 wall-clock 落进目标区间（长尾被抹平）；失败隔离 + 续跑可用。
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

---

## 3. 待办（按依赖排序）

> 每条格式：目标 / 改动 / 验收 / 产出。

### T0-1 加 server 侧插桩 【最先做，无依赖】

- **目标**：让"卡有没有喂饱"可观测。
- **改动**：`transport/rpc_server.py::_route_message` 加：每次 `t_infer`、请求到达时间戳、到达时队列深度；聚合输出 mean/p50/p95。
- **验收**：vanilla 1:1 跑一个分片能导出上述指标（`03_testing.md` T-01）。
- **产出**：可复用的 timing 日志。

### T0-2 N-吞吐验证实验 【依赖 T0-1】

- **目标**：把"串行下 GPU 接近 100%"从推演变实测。
- **步骤**：1 server + N sim，N=1/2/3/5/8，采 `nvidia-smi` 时间序列 + 吞吐曲线。
- **必须按类各跑一个**：CPU-sim 用 LIBERO；GPU-sim 用 RoboTwin/SimplerEnv/ManiSkill 之一。
- **验收**：能画出 N-吞吐曲线并给出拐点（`03_testing.md` T-02）。

### T1-1 external server 模式 【阶段 1】

- **目标**：server 可被多个 orchestrator 共享，且不被误杀。
- **改动**：`server_manager.py` 加 `server.external_url`（或 `--reuse-server`）：命中则跳过 `start()`，只 `wait_healthz`，并**禁用 `ensure_running` 重启分支**。
- **验收**：N 个 orchestrator 指向同一 `ws://host:port` 不冲突、不误杀（`03_testing.md` T-03）。

### T1-2 纯 client 启动 【依赖 T1-1】

- **目标**：orchestrator 可只当 client。
- **改动**：`run.py` 5 处 `_run_*_once` 支持不起 server、只连外部 URL。
- **验收**：同 T1-1。

### T1-3 N 标定：报告 + agent 决策 + 配置缓存 【依赖 T0-1 + T1-1】

- **目标**：扫 N 产出**标定报告**，由 agent 决策出**并行配置**，缓存 `(model, benchmark, gpu)`。
- **方法**：`01_architecture.md` §6.2–§6.4（报告规范、agent 决策、校验/兜底）+ §5.4（配置 schema）。
- **验收**：报告三件套（`report.json`/`report.md`/`env.json`）+ agent 产出**合法**配置（过 schema + 边界校验）；至少覆盖 CPU-sim 一个 + GPU-sim 一个（`03_testing.md` T-02）。

### T1-4 GPU-sim 放置决策（同卡 vs 异卡）【依赖 T0-1，需 GPU-sim 环境】

- **目标**：定下 GPU 仿真下 sim 与 server 同卡还是异卡（默认同卡，见 `01_architecture.md` §4.4）。
- **方法**：同一 GPU-sim benchmark 分别跑同卡/异卡两种放置，比 wall-clock、`t_infer`（含抖动）、显存峰值（`03_testing.md` T-07）。
- **验收**：给出推荐配置；若同卡显著拉长 `t_infer` → 改异卡，并回填 §4.4 与风险栏。

### T2-1 shard/worker 契约 【阶段 2】

- **目标**：统一 5 个非统一 runner 的入口。
- **改动**：新增薄 adapter 层（`libero.run_batch` vs 其余 `run_evaluation` 等差异在此吸收）；入参 `(shard, server_url, output_path)`，出参 `results.jsonl` 带 `(suite,task,episode)` key，支持 resume。
- **验收**：任选两个 family 能走同一契约跑通（`03_testing.md` T-04）。

### T2-2 动态队列 + coordinator 【依赖 T2-1】

- **目标**：抹平长尾，卡不空转。
- **改动**：新增 coordinator；`work/{pending,running,done}/` 原子 `os.rename` 抢占；按 ckpt 分片、按资源分池；worker 死则回收 `running/`。
- **粒度**：**episode-chunk 级**（不是 task 级）。
- **验收**：全量 wall-clock 落进目标区间（`03_testing.md` T-05）。

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
