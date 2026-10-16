# LoongForge 具身评测编排改造 —— 架构方案

> **范围**：LoongForge 具身评测框架（`loongforge/embodied/eval`）的**评测编排加速**。本方案与模型、benchmark 均无关，对所有被测模型与所有基准（LIBERO / CALVIN / RoboTwin / SimplerEnv / ManiSkill）生效；文中出现的 FastWAM / LIBERO 只是**实测样本**。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `511aa24`），仓库 `/workspace/LoongForge-Github`。
> **状态**：设计已收敛，**代码尚未开始**。日期：2026-10-12。

---

## 1. 目标与判据

**目标函数是 wall-clock 最短**，不是"GPU 利用率高"：

```
T_wall = max( 仿真总时间 / 仿真并行度 , 推理总时间 / GPU 吞吐 )
```

**手段**：让评测端（sim）排队等待，使**绑定资源**不再空转——即把**绑定资源的空闲**从关键路径上摘掉（**GPU 只是候选之一**，见 §6.1 通用方法论）。这样 wall-clock 就由 `max_r(资源时间_r / 容量_r)` 里真正大的那一项决定。

**判据**：
- **主判据**：**吞吐曲线出现平台**（见 §6）+ **server 忙占比** = `请求数 × 平均服务时间 / wall`（从 `timing.json` 直接算）→ 逼近 100% 即 server 成为瓶颈。
- **不要用 `nvidia-smi utilization.gpu` 当饱和度**：它报的是"过去采样周期里**有 ≥1 个 kernel 在跑的时间占比**"（NVML 定义），**不反映有多少 SM 被占用**——100% ≠ 用满，低 ≠ 空闲。GPU 侧旁证用 **`utilization.memory`**（带宽）或 **DCGM**（`DRAM_ACTIVE` / `PIPE_TENSOR_ACTIVE` / SM occupancy）/ `nvidia-smi dmon`。
- 附加：**sim 侧无因等待超时而失败**——串行排队会让后面的 sim 每步多等 `≈(N-1)×t_infer`，可能撞客户端超时（见 §9 风险 7）。

**收益的边界（Amdahl）**：只有降低**绑定资源**的耗时才有收益；一旦它不再是 max，继续优化它收益为零，必须转攻新的 max。**绑定资源是哪个要量出来**（§6.1），**不要预设是 GPU**。§7.1 的推演建立在"GPU 是 max"这个假设上；实测示例（§6.1）显示该 workload 实际是 **server 墙钟 + sim CPU** 绑定——所以 §7.1 只是**推演**，落地前以标定为准。

---

## 2. 现状（代码事实，均已核实）

### 2.1 编排层是"1 config = 1 卡 = 1 server = 1 sim"

- `orchestrator/run.py:_run_once`：一个 config → `_run_<family>_once()` → `ManagedServer` 起自己的 server → 调一个 family runner。**config 之间没有任何调度**，并行完全靠外部 `launch_*.sh` 静态铺卡。
- `orchestrator/run.py` 有 **5 处** `ManagedServer(...)`：行 92 / 141 / 167 / 191 / 215。
- `orchestrator/server_manager.py:30-48`：`ManagedServer.start()` / `ensure_running()` **无条件起自己的 server**；`ensure_running` 健康检查失败会**杀掉自己的 server 重启** → 共享场景会误杀别人的 server。**这是阶段 1 的唯一硬阻塞点。**

**当前架构图**：

```
  launch_*.sh  ── 静态：手工把每个 config 映射到一张卡（+ 波次补跑）
       │
       │  每个 config = 一个独立进程，彼此不知晓
       ▼
 ┌─────────────────────────────── GPU k ───────────────────────────────┐
 │  python -m loongforge.embodied.eval.orchestrator.run --config <cfg_k>│
 │                                                                     │
 │  run.py:_run_once                                                   │
 │    ├─ ManagedServer.start()      → 起「自己的」policy server         │
 │    │                               （占 GPU k，不可被共享）          │
 │    └─ <family>_runner.run_batch() → 1 个 sim（占 CPU ~1.5 核）       │
 └─────────────────────────────────────────────────────────────────────┘

 映射： 1 config = 1 卡 = 1 server + 1 sim
 问题： · 无队列 / 无动态调度 → 分片跑完该卡就空转，不接管别的任务
        · server 与 sim 绑在同一进程组，无法跨 config 复用
        · 各 config 时长不均（spatial 9703s vs goal 3005s）→ 长尾卡长时间空转
```

### 2.2 runner 层接口非统一

| family | 入口 | 返回 |
|---|---|---|
| libero | `libero_runner.run_batch(args)` | dict |
| calvin | `calvin_runner.run_batch(args)` | dict |
| simplerenv | `simplerenv_runner.run_evaluation(args)` | dict |
| robotwin | `robotwin_runner.run_evaluation(args)` | returncode(int) |
| maniskill | `maniskill_runner.run_evaluation(args)` | dict |

→ 需要一个 **adapter/契约层**把这些差异吸收掉（见 §5.3）。

### 2.3 关键事实修正：server 已经能排队，**server 侧几乎不用改**

- 推理路径是 `transport/rpc_server.py:22` 的 `PolicyServer`：**asyncio + websockets**，`websockets.asyncio.server.serve`（`:41-49`）**天然接受 N 个并发连接**。
- `_route_message`（`:71`）是**同步阻塞**调用 `policy.predict_action`，直接跑在 event loop 里 → **多客户端请求被天然串行化排队**。这既是"评测端等待"的现成实现，也顺带避免了"模型 forward 非线程安全"的坑。
- `servers/loongforge_server.py:12` 的 `HTTPServer` **只服务 `/healthz`**（`:109`），与推理无关。

**推论**：单请求 GPU 时间 ~0.5s，event loop 开销仅 ms 级 → 串行执行下 server 不再空转（请求背靠背）。**并行改造的落点不在 server，在编排层。**（注：这里说的是"server 不再等请求"，**不等于** GPU 被用满——见 §1 的 util 说明。）

### 2.4 仿真的两类资源画像

| 类别 | benchmark | 后端 | 渲染 | 并行化要点 |
|---|---|---|---|---|
| **CPU 仿真** | LIBERO | robosuite / MuJoCo（`MUJOCO_GL=egl`） | GPU（EGL 离屏） | 仿真吃 CPU 核，GPU 基本只跑推理 |
| **CPU 仿真** | CALVIN | MuJoCo（`MUJOCO_GL=osmesa`） | **软件光栅化，全 CPU** | 同上，渲染更吃 CPU |
| **GPU 仿真** | RoboTwin | SAPIEN（`CUDA_VISIBLE_DEVICES`） | GPU | 仿真与 server **抢同一张卡**（算力+显存） |
| **GPU 仿真** | SimplerEnv / ManiSkill | SAPIEN + Vulkan | GPU | 同上；且 SAPIEN 原生支持 `num_envs` 向量化 |

资源盘点（128 核 / 1.5 核每 sim / 上限 ~80 sim）与总账（仿真 62000 CPU·s vs 推理 14000 GPU·s，见 §7.1）**都只用 LIBERO 量过**，对 GPU 仿真不成立——"GPU 空闲"这个前提直接消失。**资源盘点必须按类各做一份。**

---

## 3. 设计原则

1. **改造收口在编排层**：并行 + 动态下发加在 `run.py` 之上，**不动 runner、不动 server、不动模型、不动 benchmark**。这正是"方案与模型/benchmark 无关"的落地形态。
2. **并行 = 进程级 fan-out（跨 shard），不是单环境向量化。**
   - fan-out：起 N 个独立 worker 进程，各跑不相交的 episode 子集，共享一个 server。对 benchmark 的要求最低——**只要能"只跑一个子集"**，不要求它自己支持并行。
   - 单环境向量化（`env.step(K 场景)`）在 benchmark 自己的 rollout 循环里，**编排层不实现**；但编排层**支持调用**——通过契约里的 `num_envs` 字段透传给 benchmark（默认 1 = 不向量化，见 §5.3 / §7.5）。**本分支只定义字段、打通调用，不实现向量化本身。**
   - **GPU-sim 例外（重要）**：对 SAPIEN 系（RoboTwin / SimplerEnv / ManiSkill），最高杠杆**不是**进程 fan-out（N 个进程各一份 scene → 显存重复、无共享 GPU scene），而是**向量化 env（`num_envs`）+ 批量推理**。这条路活在 benchmark/sim 引擎层，**编排层覆盖不到**，但必须在 roadmap 单列（见 §7.5）。本方案的 fan-out 对 GPU-sim 是**保底方案**，不是最优。
   - **推论**：因"部分 benchmark 不支持并行"而不阻塞本方案——它们只需要一个"跑 shard"的入口。
3. **队列按 ckpt 分片，按资源类型分池。** server 是**按 ckpt 绑定**的：共享一个 server 的 N 个 sim 必须同模型同 ckpt；多个 benchmark/模型同时跑时不是一条全局队列。
4. **N 是可标定量，不是拍脑袋。** 见 §6。

---

## 4. 目标架构

### 4.1 目标架构图（对比 §2.1 当前架构）

```
                    coordinator（新增：编排层）
 ┌────────────────────────────────────────────────────────────────────────┐
 │  work 单元 = (ckpt, benchmark, shard)，带两个「正交」标签：              │
 │    · ckpt      → 路由：连哪个 server（server 按 ckpt 分，都在 GPU 上）   │
 │    · benchmark → 放置：sim 是 CPU-sim 还是 GPU-sim → 进哪个 worker 池    │
 │  work/{pending,running,done}/   原子 os.rename 抢占（无锁）             │
 │                                                                         │
 │     worker 池按「资源类」分（与 ckpt 无关，两池都可指向任意 server）     │
 │     ┌── CPU-sim 池 ──┐          ┌── GPU-sim 池 ──┐                       │
 │     └────────┬───────┘          └────────┬───────┘                       │
 └──────────────┼───────────────────────────┼──────────────────────────────┘
                │ worker = 纯 client：连「自己 work 单元 ckpt」的 server
                ▼                           ▼
        ┌───────────────┐           ┌───────────────┐
        │ server(ckpt A)│           │ server(ckpt B)│  ← 每个 ckpt 一个 server，
        └───────┬───────┘           └───────┬───────┘     常驻 GPU（推理永远在 GPU）
                │ ws://host:port            │
        ┌───────┴────────┐          ┌───────┴────────┐
        │ sim sim sim …×N│          │ sim sim sim …×N│  ← sim 的 CPU/GPU 由 benchmark 决定
        └────────────────┘          └────────────────┘
```

> **两条轴正交**：**ckpt（路由轴）**决定连哪个 server（server 恒在 GPU）；**benchmark（放置轴）**决定 sim 是 CPU-sim 还是 GPU-sim、进哪个 worker 池。同一 ckpt 下可**同时**有 CPU-sim 与 GPU-sim 的 shard，它们连同一个 server、但属不同 worker 池——所以 work 单元必须同时带 ckpt 与资源类两个标签。

### 4.2 三类角色

- **server（托管方）**：由 coordinator 起，长驻；一个 server 服务同一 ckpt 的 N 个 sim。
- **worker（纯 client）**：一个 worker = 一个 family runner 进程，跑一个 shard，**不自己起 server**，只 `wait_healthz` + 连 `ws://host:port`。
- **coordinator**：起 worker 池、维护队列、按资源画像（`utilization.memory` / server 忙占比等）决定并发数、worker 死则回收其 `running/` 单元。

### 4.3 队列与抢占（动态任务下发）

> **分两步落地**：**最小版**（起 server + 起 N worker + 聚合，**M1/T1-5 ✅ 已实现**）→ **动态版**（队列 / 分池 / 看门狗 / episode-chunk，**M2/T2-2**）。下面描述的是**目标形态（M2）**；最小版只做"一行命令"（`coordinator.py` + config 里 `coordinator: {n_workers: N}`）。
> ⚠️ **最小版分片**：原按 task 分片 → `N ≤ 任务数`（libero_10 只有 10）。**已由 T1-6 解决**：`build_shards` 改成 **episode-chunk 级**（`(task, episode_offset, count)`，+ runner `--episode-offset`），`N` 不再受任务数限制。

**工作单元**：`unit = (ckpt, benchmark, suite, task_id, episode_range)`，例 `libero_10__task3__eps0-9`。粒度取 **episode-chunk（~10 集）**——task 级太粗（libero_10 单集 60s 长尾会失衡），单集级太碎（协调开销 + resume 粒度太细）。

**组件与数据流**：

```
                            coordinator
     ┌──────────────────────────────────────────────────────┐
     │ ①枚举 unit  ②起 server  ③起 worker 池  ④看门狗  ⑤收尾  │
     └──────┬─────────────────────────────────┬─────────────┘
            │ 灌 unit                          │ 起 worker
            ▼                                  ▼
   ┌────────────────────────┐        ┌───────────────────────────┐
   │ work/（本地磁盘队列）    │        │ worker 池                  │
   │  pending → running → done│◄─claim─┤ (ckpt × resource_class)   │
   └────────────────────────┘ os.rename└─────────────┬─────────────┘
                                                    │ 纯 client
                                                    ▼
                                         ┌────────────────────┐
                                         │ server(该 unit 的   │
                                         │  ckpt)，常驻 GPU    │
                                         └─────────┬──────────┘
                                                   │ ws://host:port
                                         ┌─────────▼──────────┐
                                         │ sim 跑 shard        │
                                         └─────────┬──────────┘
                                                   │ append
                                                   ▼
                                     results/worker_<id>.jsonl
                                                   │ 聚合
                                                   ▼
                                     按 (suite,task,episode) 去重 → 指标
```

**一个 unit 的生命周期**：

```
worker                    work/                       server/sim            结果
  │ ① 扫 pending（按 resource_class 过滤）              │                    │
  │ ② os.rename pending→running（原子抢占；抢不到换下一个）│                    │
  │ ③ 连 server，跑 shard ────────────────────────────► │                    │
  │ ◄──────────────────── episode 结果 ──────────────── │                    │
  │ ④ append ─────────────────────────────────────────────────────────────► │
  │ ⑤ os.rename running→done                            │                    │
  │ ⑥ 失败 → os.rename running→pending（回队）           │                    │
```

**要点**：

- **无锁**：`os.rename` 同 FS 原子 → 恰好一个 worker 抢到。
- **worker 是纯 client**：只 `wait_healthz` + 连 `ws://`，不自己起 server（依赖 M1 的 external 模式）。
- **二维分组**：worker 池 = `ckpt × resource_class`；worker 只领同类 unit。
- **看门狗**：worker 死 → 其 `running/` 单元回 `pending/`。
- **断点复用**：`--resume` + `completed_episode_keys`（`libero_runner.py:444`）→ 即使 chunk 跑到一半崩，新 worker 跳过已完成的 episode。
- **聚合 + 校验**：merge `results/worker_*.jsonl`，按 `(suite, task, episode)` 去重；**再与"计划应跑集合"比对**（漏 / 重 / 多出），**漏的自动补跑**（见 §5.5）。
- **离线前提**：work 全部事先已知 → coordinator 启动时**一次性枚举**；"动态"只体现在分配，**不需要任何外部服务**（本地进程 + 本地 `work/` 目录）。
- **前提：单机单 FS**。`os.rename` 的原子性只在**同一文件系统**内成立——跨节点 / NFS 不保证。本方案假定所有 worker 与 `work/` 在同一台机器。上多机时这个队列是死路，需换真正的调度器（**Ray Serve 已评估**：能现成解决路由 / autoscale / 容错，但依赖重，与"编排层收口、少依赖"冲突，故不选）。

**代码改动**：新增 `coordinator.py` + worker 入口（薄 wrapper：claim → 拼 shard args 调现有 runner → 标 done）；runner 需接受 shard spec（现成可复用 `--task-suite-name` / `--task-ids` / `--episodes-per-task` / `--resume`，见 `libero_runner.py:575-587`；**缺 task 内 episode offset**，需小改）；worker 跳过 `ensure_running()`（`libero_runner.py:470-472`）。详见 §5.3 与 `02_progress.md` T2-1 / T2-2。

### 4.4 GPU 仿真的放置决策：sim 与 server 同卡还是异卡

对 GPU 仿真，sim 与 server 争同一张卡，问题变成"每卡放 `k` 个 sim + `s` 个 server"。

**默认：同卡（co-locate）**，前提是 sim 渲染相对推理很轻（单帧 ms 级 vs 0.5s forward，VLA 基本都这样）。理由：

1. **推理必须摊到所有卡上**：总推理 ≈ 14000 GPU·s，要落在 ~29min 就得 8 张卡都在跑推理（14000/8 ≈ 1750s）。若把卡分成"仿真卡 + server 卡"，推理被挤到少数卡上 → 那些卡立刻成瓶颈，wall-clock 反而变长。所以**每张卡都该挂一个 server**。
2. **同卡并不省搬运**：观测路径是 `GPU(sim) → CPU(numpy/msgpack) → GPU(server)`，无论同卡异卡都要过一次 CPU，没有 GPU→GPU 直传。所以同卡的**唯一成本是争用**，**唯一收益是让推理摊在跑仿真的同一批卡上**（不为 server 额外占卡）。
3. **真正的约束是显存**：同卡要求 `k × vram_sim + vram_model + kv ≤ 卡显存`；模型大或 k 大时被迫降 k 或分卡。

**例外：异卡（sim 主导）**：当单个 sim 的渲染就能吃满一张卡（高分辨率 + 多相机，典型 RoboTwin 3 路），再塞 server 只是抢 SM → server 挪到独立卡，退化成"1 server : N 张 sim 卡"。

**判据**（实测）：量单 sim 的 GPU 占用（显存 / ms/帧，**别用 `nvidia-smi util`**）与推理占用——
- `sim_share × k + infer_share` 能共同饱和该卡、互不饿死 → **同卡**；
- sim 自己就接近 100% → **异卡**。

**同卡的坑**：SAPIEN/Vulkan 渲染与 PyTorch 推理同卡分时，会**拉长 `t_infer` 并带来抖动**（无 MPS 时更明显）→ 同卡部署后必须**重测 `t_infer`**，别拿 CPU-sim 的 0.5s 算 N；并留显存余量防 OOM。

> **两种都要实测对比**（见 `03_testing.md` T-07）：同一 GPU-sim benchmark，分别跑「同卡」与「异卡」两种放置，比 wall-clock、`t_infer`（含抖动）、显存峰值。**当前无实测数据，默认按"同卡"起步。**

**并行关系不是一一对应**：仍是 1 server : N sims（N>1）。理由：单 sim 流水 `obs→推理→step→obs` 严格串行有气泡，N>1 才能让 server 在 A 步进时给 B 推理。差别在 N 的约束：CPU 仿真由**核数**定，GPU 仿真由**显存**定。

### 4.5 一张卡放几个 server（同 ckpt 多副本）

**先分清两个量**：**"喂满"指算力（SM），不是显存**——server 可以只占 10% 显存却把算力吃满，也可能占一半显存但算力只用 50%。显存是**容量**约束（能放多少），算力是**吞吐**约束（跑多快）。

**为什么默认 1 server/卡 就够喂满算力**：server 串行，只要 sims 够多、请求背靠背，forward 一个接一个 → 算力饱和，**与模型占多少显存无关**。

**反例（需要同卡并发）**：若**单个 forward 本身喂不满 SM**（模型小 / kernel 计算量低 / 访存受限），则一个串行 server 喂不满算力（带宽 `utilization.memory` 会卡在低位）。这时有两条路：

1. **加大 batch**（M3，一次 forward 处理 K 个）——**一份模型、一个进程**；
2. **同卡第二副本**（两个进程各一份模型、并发跑 forward）——两份模型。

两者都是"填空闲 SM"。**若只是要填空闲算力，batch 通常更优**（省显存、省管理）——但**以实测为准**（见 `03_testing.md` T-08）。

**判据**：M0 扫 N 时的 **吞吐平台 + server 忙占比**（**不要用 `nvidia-smi util`**，见 §1）。server 忙占比逼近 100% 即 1 server/卡 足够；若忙占比高但吞吐不涨 → 单个 forward 喂不满，需 batch / 多副本。

**显存富余 ≠ 要开第二个副本**：显存富余的正当用途是 GPU-sim 加 sim、batch 的激活、放别的 ckpt 的 server。**同 ckpt 同卡多副本只在"单个 forward 喂不满 SM"时才有意义。**

---

## 5. 关键接口

### 5.1 server 侧：external 模式（阶段 1）【✅ 已实现】

**配置**：`server.external_url: ws://host:port`（命中即 external 模式）。

**改动**（比原计划更小——**`run.py` 不用动**）：

- `config.py`：`server.external_url` → `external_url`，并在 `apply_config` 里**从 URL 推出 `host`/`port`**（client 连接用）。
- `server_manager.py`：`ManagedServer` 在 external 模式下 `start()` / `ensure_running()` / `stop()` **全部 no-op**——不起、不重启、不杀共享 server。因 `run.py` 只是 `ManagedServer(...).start()`，**编排层无需改动**。
- 单测：`unit_tests/test_server_manager_external.py`；E2E：起 1 个 server + 用真 orchestrator（带 `external_url`）跑 shard。

**安全前提（server 从"自起自销"变"共享长驻"后必须显式化）**：`PolicyServer` 默认 `host="0.0.0.0"`（`transport/rpc_server.py:26`，`servers/loongforge_server.py:110` 未传 host）、`max_size=None`（`:46`）、**无认证**。改造后应收紧：

- 绑 `localhost` 或内网地址，**不要 `0.0.0.0`**；
- 给 websocket 设**帧大小上限**（`max_size`）；
- "无认证"作为**已知前提**写明（评测机共享 / 跨机时尤其要标出）。

### 5.2 调参旋钮与放置单位

- `N` = 每 server 挂几个 sim（§6 标定）。
- `G` = 分配的**卡数**（= 总 server 副本数，跨所有 ckpt）；单个 ckpt 的副本数 = `G / ckpt 数`（默认每卡 1 个副本）。
- 两者**分开**，不要混成一个旋钮。

**放置单位是「卡」**：每卡放 `s` 个 server（按 ckpt）+ `k` 个 sim。默认 `s = 1`；`s > 1`（同 ckpt 多副本）仅在"单个 forward 喂不满 SM"时才有意义，见 §4.5。

### 5.3 统一的 shard/worker 契约（阶段 2 的核心交付物）

每个 family 只需实现这一个契约，差异全被 adapter 吸收：

- **入参**：`shard = (suite, task, episode_range)` + `server_url` + `output_path` + **`num_envs`**（默认 1，见 §7.5）。
- **出参**：`results.jsonl`，每行带稳定 key `(suite, task, episode)`（供去重 / 续跑）。
- **resume**：读 `completed_episode_keys`，跳过已完成 episode。
- **禁止**：对 GPU / 端口 / peer 数做任何假设，全部由 coordinator 注入。
- **`num_envs`（向量化，opt-in）**：默认 1 = 不向量化。仅当 benchmark 声明支持时才有意义（`metadata.max_num_envs`）；`num_envs > max_num_envs` → **fail-fast，不静默忽略**。`num_envs > 1` 时**必须同时开启 server 侧 batching**（否则 lockstep 白搭）。**本分支只定义字段**，实现归 GPU-sim 工作流（§7.5）。

现有 5 个 runner 的入口不统一（§2.2），需要一层薄 adapter 包成上述契约。

### 5.4 并行配置 schema

分**静态配置**（人写）与**标定产物**（agent 决策，见 §6.5），按 **`(ckpt) × (resource_class)` 两条轴**组织：

```yaml
run:      { output_dir: ..., resume: true }

# ── 静态（人写）──
servers:
  - { ckpt: /path/to/ckptA, model: fastwam, action_dim: 7 }
resources:
  cpu_sim: { cores_per_sim: 1.5, mem_per_sim_gb: 2.2 }
  gpu_sim: { vram_per_sim_gb: 12, vram_model_gb: 20, kv_gb: 4 }
queue:     { unit_granularity: episode_chunk, chunk_size: 10, work_dir: ... }
timeouts:  { policy_call_ms: 8000, per_episode_sec: 900 }

# ── 标定产物（agent 决策，可缓存复用）──
calibrated:
  servers:
    - { ckpt: /path/to/ckptA, replicas: 8, gpus: [0,1,2,3,4,5,6,7] }
  pools:
    cpu_sim: { n_sims_per_server: 6 }
    gpu_sim: { n_sims_per_server: 3, placement: co_located }
  timeouts: { policy_call_ms: 8000 }        # 按 N 放宽
  rationale: "..."

# ── 预留（先 disabled，避免以后改 schema）──
batching:      { enabled: false, max_batch_size: 8, max_wait_ms: 5 }   # M3
vectorization: { num_envs: 1 }                                          # §7.5
```

两条设计原则：

1. **按 `(ckpt) × (resource_class)` 两条轴组织**，别用扁平字段——否则多 ckpt / 两类混跑会立刻不够用。
2. **`batching` / `vectorization` 现在就预留块**（`enabled: false` / `num_envs: 1`），保证 schema 前向兼容。

> **已实现子集（M1/M2，当前代码，`coordinator.py`）**：`run.output_dir` / `run.timestamped_output` / `run.max_retries`（coordinator 默认注入 `2`：episode 级 infra 失败——`episode_error`/超时——重试，模型失败不重试）、`servers[].gpus`（=**G**，一张卡一个 server）、`pools.cpu_sim.n_sims_per_server`（=**N**）、`benchmark.{suite|suites, task_ids, episodes_per_task, episode_offset, max_steps, suite_overrides}`、`server.external_url`、`timeouts.policy_call_ms`；编排开关 = config 里有 `coordinator` / `servers` / `pools` 任一 key（`run.py` 按 key 存在性分发）。
> 其中 **`benchmark.suite_overrides: {<suite>: {<key>: <val>}}`** 用于**套件差异化**（如 LIBERO 官方 `max_steps`：`libero_10` 700 / 其余 400），使**一条全局队列跨套件**成立。
> `resources` / `queue` / `calibrated` / `batching` / `vectorization` 仍是**目标 schema**，尚未接线（M2 的队列用进程内内存实现，非 `work/` 文件队列，见 `02_progress.md` §2.11）。

---

### 5.5 运行后覆盖校验 + 自动补跑

跑完 ≠ 跑对：`_aggregate` 只做去重计数，**漏集 / 重复 / 多出**都会被静默吞掉（分数照出）。所以聚合后必须与**计划应跑集合**比对：

- **期望集合**：由分片计划推出——`(suite, task, offset..offset+count-1)` → `{(suite, task, episode)}`。
- **实跑集合**：逐 worker `results.jsonl` 取 `(task_suite, task_id, episode_idx)`（缺字段时回退解析 `episode_id`）。
- **判据**：`missing`（漏）/ `duplicates`（重）/ `unexpected`（多出）/ `unkeyed` 全为 0 → `ok`。
- **失败分类**：`infra_failed`（`episode_error` / 超时 / `server_unreachable`——**没真跑**）与**模型失败**分开统计；模型失败是正常结果，**不算"不完整"**。
- **闭环（不中断评测）**：`missing` 非空 → 把缺失键**合并回连续区间**成补跑分片 → 复用同一个有界池再跑 → 再校验，最多 `MAX_REPAIR_ROUNDS=3` 轮。**只有 3 轮后仍不完整**才 `ok=false`（此时才值得人工介入）；**过程中不抛异常、不中断**。
- **实现**：`orchestrator/validation.py`（`expected_keys` / `key_of` / `read_records` / `validate` / `missing_keys` / `repair_shards` / `format_report` + CLI）；报告随 `coordinator_result.json` 落盘。

## 6. 标定方法论

> **本方案的核心方法**：对任意 `(model, benchmark, gpu, config)`，先**找出绑定资源**，再按该资源选杠杆；**不要预设**哪个资源是瓶颈——量出来。N 的标定（§6.2 起）只是其中一个环节。

### 6.1 通用方法论（5 步 + 2 陷阱）

**目标函数**（泛化，**不写死 GPU**）：
```
T_wall = max_r ( 资源时间_r / 容量_r )   r ∈ { sim-CPU, sim-GPU, server-wall, server-GPU-busy, ... }
```
优化 **argmax_r**（绑定资源）。"把 X 从关键路径摘掉"里的 **X 是绑定资源**，不是固定的 GPU。

**Step 1 · 分解"单请求成本"**（一次 N=1 小分片）
- `t_sim`（仿真侧）、`t_server_wall`（server 每请求墙钟 = `timing.json` 的 mean）；
- **把 `t_server_wall` 再拆**：GPU-busy vs **non-GPU**（`utilization.memory` / DCGM 采样，或 nsys/ncu）。
- 得到三个每请求成本：**sim / server-wall / server-GPU-busy**。

**Step 2 · 算资源总量与容量**：总量 = 请求数 × 各每请求成本（CPU·s / server-wall·s / GPU-busy·s）；容量 = 核数、卡数、每卡 SM / 带宽。

**每资源的吞吐上限**（取 `min` 即总上限；`argmin` = 绑定资源）：
```
上限_r = 容量_r / (每请求成本_r × R)        R = 每集请求数
   server : (1 / t_service) / R
   sim-CPU: (核数 / 每步核数) / R
```
> 例：`t_service=50.9ms`、`R=150` → `1/0.0509/150 ≈ 7.9 ep/min`；且恒等式 **`吞吐 = 忙占比 × 上限`**（实测 N=16：`0.87 × 7.9 ≈ 6.8` ✓）。
> **`t_service` 取 `timing.json` 的 `mean`**——不是 max/p95，也不是 `avg_inference_latency_ms`（后者只对"真推理"平均，会高估 ~10×）。

**Step 3 · 扫 N，测各资源占用率**：主判据 = **吞吐曲线**；各资源占用 = **server 忙占比**（`timing` 算）、**CPU 占用**、**GPU 带宽占用**（`utilization.memory`）、**GPU SM 占用**。

**Step 4 · 找绑定资源**：随 N 增大，**哪个资源先逼近 100%** 就是瓶颈（没到平台就继续加 N）。⚠️ **同步 client 下，串行资源会靠"排队等待"先绑定**——不必等到 100% 利用率；判据用**等待时间增长**（`e2e − 服务时间`）+ **恒等式 `吞吐 = 忙占比 × 上限`**。
> **队列是闭环的、有界的**：client 同步（发一个等一个）→ 在途请求 ≤ N → 队列深度 ≤ N−1 → **最长等待 ≈ (N−1) × t_service**。所以不会无界增长；但 N 大时会撞**客户端超时**（§10 风险 7），超时阈值要按 N 放宽。

**Step 5 · 按绑定资源选杠杆**：

| 绑定资源 | 杠杆 |
|---|---|
| **server 墙钟** | 降每请求 server 成本（batch / 更快 forward / chunk 复用）；加 server 副本或卡 |
| **server GPU-busy** | batch（摊薄 kernel 开销）；加卡 |
| **sim CPU** | 降 sim 成本；加核；换更快的 sim 后端 |
| **sim GPU** | 减每卡 sim 数；向量化（§7.5） |

**三个杠杆**（不是两个——别把"喂饱"和"扩容"混了）：

| 杠杆 | 作用 | 吃谁的资源 | 何时有用 |
|---|---|---|---|
| **N**（每 server 挂几个 sim） | **喂饱**（提高利用率） | **sim 侧**（CPU；GPU-sim 吃显存） | server 没喂满时 |
| **batch** | 提高**单卡吞吐**（降每请求成本） | **server 显存**（激活 / KV 随 batch 涨） | server 已成绑定资源时 |
| **加卡 / server 副本** | 提高**总容量** | 更多卡 | 单卡容量到顶时 |

> **正交性**：**资源维度基本正交**（N 吃 sim 侧、batch 吃 server 显存，两套资源互不挤）；**效果维度不正交**（batch 的收益依赖 N 够大——server 被喂饱才攒得出成批请求）。故 **M3(batch) 排在 M1/M2 之后**：先喂饱，再扩容。
> 注：**"scale out"严格说指加卡**（第三行）；N 只是"加负载/喂饱"，**不增加任何容量**。

**两个通用陷阱**（实测踩到，必查）：

1. **`nvidia-smi utilization.gpu` ≠ 饱和度**——它报的是"过去采样周期里有 ≥1 个 kernel 在跑的时间占比"，**不反映 SM 占用**。改用 **server 忙占比 / `utilization.memory` / DCGM**。
2. **`t_server_wall` ≠ GPU 时间**——forward 可能大部分墙钟**不在 GPU 上**（实测：GPU 只占 forward 墙钟的 ~1/3）。不拆的话，会把"**server 慢**"误判成"**GPU 忙**"，进而选错杠杆。

> **示例（不是结论）**：FastWAM × LIBERO 实测（N=1..16）——GPU 与 CPU **都不忙**（`util.gpu` 5.6→26.9%，`util.memory` 1→5%，CPU 2.6→8.9% / 128 核），**绑定资源是串行 server**（忙占比 18%→87%，且满足恒等式 `吞吐 = 忙占比 × 上限`，上限 ≈ 7.9 ep/min）。**只作方法论的回归用例**，不是普适结论。

### 6.2 下界与资源上界（一次 1:1 标定即可算出）

`N*` 是 **(model, benchmark)** 的函数：benchmark 决定 sim 侧（`t_sim`、每 sim 核数/显存），model 决定 server 侧（`t_infer`、显存）。

卡要饱和，server 不能等请求：

```
请求到达率 = N / t_sim_per_request      服务率 = 1 / t_infer
饱和条件 →  N ≥ t_sim_per_request / t_infer          （= N_min）
```

资源上界：
```
CPU-sim:  N ≤ cores / cores_per_sim            （留 headroom，因 t_sim 会随 N 膨胀）
GPU-sim:  N ≤ (card_vram - vram_model - kv) / vram_sim
```

> **`N_min` 是 model + suite 相关的**：以 31s 样本算得 `N_min≈4.2`；而 libero_10 单集 ~60s（每请求 `t_sim≈4.2s`）对应 `N_min≈8.4`。所以"每卡 5~8 sim"对 libero_10 是**欠喂**——N 应按套件取，或统一取上界。
> **`N_min` 只作"起扫种子"，不是目标值**：N 增大时 `t_sim`（CPU 争用）与 `t_infer`（GPU-sim 同卡抖动）会**同时膨胀**，真实饱和点不是这个比值。主判据只认吞吐曲线拐点（§6.3）。

### 6.3 标定流程（~4 次小 run，替代盲搜；**每次只跑代表性小分片，不是全量**）

1. **扫 N**（~4 次）：`N ∈ {1, 2, 4, 8}`（或按 §6.2 的 `N_min` 取附近点）。
   - **N=1 那次就是"vanilla"**：既是无争用基线（拿 `t_sim`、`t_infer` 算 `N_min`），又是曲线的起点——**不用单独再跑一次 vanilla**。
   - N>1 的几次用来观测队列深度、争用、`t_sim` 膨胀。
   - ⚡ **不同 N 并行跑**：每个 N 一个 server（各占一张卡），N 个值**同时**跑。**别串行——卡都空着**。串行只省显存、费时间；并行后墙钟 ≈ 最慢那档。
2. **取拐点**（0 次）：`N*` = 达到 ≥95% 平台吞吐的最小 N（避免超订）。**可自适应**——先跑 `1` 与 `2·N_min`，见平台即收敛。
3. **缓存**（0 次）：存成 `(model, benchmark, gpu)` 表，下次直接查。

> **每次 run 的规模**：只跑一个**代表性小分片**（几十~上百集），目的是**测吞吐/延迟**，不是出分。所以是"小探针"，**不是"多次全量评测"**。
> **不能只查一次 vanilla 的 logging 代替扫描**：N>1 的队列深度 / 争用 / 拐点，只有真的在 N>1 跑才有。
> **正式入口已实现**：`orchestrator/calibrate.py --config run.yaml --n 1,2,4,8,16,32`——**一条命令**跑完：每个 N 起一个 coordinator（**并行铺卡**）→ 收集吞吐 / server 忙占比 → `calibration_report` 报告 → **兜底 N**（≥95% 最佳吞吐的最小 N）→ `calibration_config` 校验 + 缓存。

### 6.4 所需指标与现状（修订：大部分已有）

| 指标 | 来源 | 现状 |
|---|---|---|
| `t_infer`（纯模型调用） | `servers/loongforge_policy.py:129` → `inference_latency_ms` → runner 逐请求接住 → `metrics/results.py:100` 聚合 | **已有** |
| `e2e` 延迟（含排队 + 传输） | `orchestrator/runners/libero_runner.py:293/307` → `e2e_latency_ms` | **已有** |
| `t_sim` | `results.jsonl` 的 `episode_time_sec` / `reset_time_sec` | **已有** |
| **队列等待（时间）** | `e2e - t_infer`（**不需要 server 改动**） | 可推导 |
| **平均队列深度** | Little's Law：`L ≈ λ × W`（λ = 请求到达率，W = 上面的等待时间） | 可推导（平均） |
| 瞬时队列深度 | 需 **recv→队列→worker** 架构（= M3 batcher），现有串行 server 测不到 | M3 |
| **延迟分布 p50/p95/p99** | 现只有 `avg`（`metrics/results.py`） | **待加** |
| **server 侧服务时间 + 导出** | 无（`inference_latency_ms` 在 chunk-cache hit 时为 `None`） | **待加**（T0-1） |
| GPU 旁证（带宽 / 显存） | `utilization.memory` / `nvidia-smi dmon` / DCGM | 待采集（外部） |

> **"server 侧队列深度"测不到**：`_route_message` 同步阻塞 event loop，其它连接的 handler 进不来 → 数不到"待处理请求个数"。能拿到的是**等待时间**（`e2e - t_infer`），**平均深度**再由 **Little's Law** `L ≈ λ × W` 推导；**瞬时深度**要等 M3 的队列架构。
> **服务时间是双峰，别用错数（T-01 实测）**：cache hit ~7.7µs vs 真推理 ~490ms（LIBERO 上 94/920 ≈ 10% 是真推理）。`avg_inference_latency_ms`（~492ms）**只反映真推理那一小撮**；server 是逐请求串行的，**`N_min` 要用平均每请求服务时间（实测 ~49ms）**——用 492ms 会把 N 算小约 10×。
> **判饱和，等待时间比深度更直接**；**主判据**是**吞吐曲线平台 + server 忙占比**（见 §1）。**不要用 `nvidia-smi utilization.gpu`**——那是"有 kernel 在跑的时间占比"，不是饱和度。

### 6.5 标定产物：报告 + agent 决策

标定的产物不是"一个 `N*` 数字"，而是 **报告 → agent 决策 → 配置** 三步（配合"都是 AI agent"的用法）：

**① 生成报告**（`calibration_report/`）：

```
report.json    # service_time_ms + 每 N 一条：吞吐、server 忙占比、utilization.memory、CPU busy、wall
report.md      # 表格 + 读法（含"不要用 nvidia-smi util"的提示）
env.json       # 资源事实 + 硬约束：核数、每 sim 核/内存、卡数/显存、每 sim 显存、模型+KV 显存
```

> **生成器已实现**：`orchestrator/calibration_report.py`（`--sweep` / `--timing` / `--env` → 三件套；纯函数 `build_report` / `render_markdown`，含单测）。输入与扫描器解耦（只认 sweep/timing 的 JSON）。

**② agent 决策**：agent 读报告，产出并行配置（schema 见 §5.4）。拐点往往不是干净平台，需要权衡（server 忙占比 88% 够不够、加 N 会不会顶起 `t_sim`、会不会撞超时），固定规则判不了。

**③ 校验 + 兜底**（防 agent 编）：

- 报告给**数据 + 硬约束**，别塞"推荐 N"让它复述；
- agent 输出的配置过 **schema + 边界检查**（`N ≤ 核数/每sim核数`、显存不超、`timeout ≥ N × 服务时间 × safety`），非法则拒；
- 兜底：agent 挂 / 输出非法 → 回落"≥95% 平台吞吐的最小 N"；`rationale` 存档可审。

> **校验 + 缓存已实现**：`orchestrator/calibration_config.py`（`validate_parallel_config` / `calibration_key` / `save_calibration` / `load_calibration`，含单测；CLI：`--config --env --timing` → PASS/FAIL）。

**缓存**：`(model, benchmark, gpu)` → 存**报告 + 配置**；下次直接查配置。

---

## 7. 收益与阶段划分

### 7.1 收益推演（理想装箱下界）

总账（4 套件 × 500 集）：仿真 ≈ **62000 CPU·s**，推理 ≈ **14000 GPU·s**。

配置：8 GPU × 1 server，每 server 挂 5~8 个 sim（共 40~64 sim，占 60~96 核）：

- CPU 侧：62000/40 ≈ 1550s；62000/64 ≈ 970s
- GPU 侧：14000/8 = 1750s ≈ **29min** ← 成为瓶颈

**结论：4 套件全量 wall-clock ≈ 29~32min（现 ~2.7h，约 5.5×），系统正好落在 GPU/CPU 平衡点。全程不需要改模型、不需要 batching。**

> ⚠️ 这是**理想装箱下界**——假设分片完美均衡、无冷启动/长尾；**且假设 GPU 侧是 max（未必，见 §6.1 实测）**。静态映射（阶段 1）达不到，需阶段 1+2 共同兑现。且上述是 **CPU-sim（LIBERO）**口径，GPU-sim 不适用。
>
> **实测对照（2026-10-15）**：静态 8 卡（每套件钉 2 卡）全量 = **94.2 min**；vs 1 卡 1 server vanilla（官方 harness，8 卡折合 ~173.7 min）→ **逐 suite 2.7–4.3×、整机 1.85×**。离 29min 下界仍差很远，三个原因：① **每套件钉 2 卡** → 长尾期 6 卡空转（spatial/object/goal ~45min 跑完，libero_10 拖到 91min）→ 由 **M2 动态下发**吃（推算 ~57min / ~3.0×）；② 单卡 N=16 只到 server 忙占比 87%（§6.2），**未喂满**；③ 下界假设 GPU 是绑定资源，而实测**绑定资源是串行 server**（§6.1）。详见 `02_progress.md` §2.12。

### 7.2 阶段 1：1 server ↔ N sim（唯一有量级收益的一步）

- 前置阻塞：`server_manager.py:30-48` 的 server 托管逻辑（§5.1）。
- 交付：external server 模式（✅ 已实现）+ **最小 coordinator（一行命令起 1 server + N worker，T1-5）** + N 标定（§6）。
- 预期：把**绑定资源**从关键路径摘掉。**注意**：§7.1 的"5.5×/29min"是**理想装箱下界**（且假设 GPU 是 max），静态映射达不到，需阶段 1+2 共同兑现。

### 7.3 阶段 2：动态任务分配（把最小 coordinator 动态化）

- 交付：§5.3 的 shard 契约 + §4.3 的**动态队列**（按 ckpt 分片 / 按资源分池 / 看门狗 / episode-chunk 粒度）。
- 解决：套件/分片间时长不均（spatial 9703s vs goal 3005s）导致的卡空转。

### 7.4 阶段 3：Batch inference（**排在 M1/M2 之后**）

- **为什么不是现在做**：batching 降低的是**单请求 GPU 耗时**；改造前（未并行）GPU 大量空转、瓶颈在 CPU 仿真，做了只会让 GPU 更空。**只有当 GPU 吞吐成为绑定约束时才有收益**（判据见 §1 的 Amdahl 边界；目标态 §7.1 里 GPU 侧略大于 CPU 侧，所以届时仍有收益）。
- 真要做的位置是 **server 侧**（`_route_message` 与 `policy` 之间插 batcher：请求入队挂 `Future`，在 `max_wait_ms` 窗口内聚合 `max_batch_size` 个请求）。fan-out 的 sim 会漂移出相位，所以要等窗口、batch 可能不满；env 向量化的 lockstep 天然满 batch。
- 前置：先补齐 FastWAM 的 batch 契约（`modeling_fastwam.py:349` 硬断言 `batch==1`，`mot/fastwam.py::infer_action` 另有 4 处 batch=1），并重新定义批内噪声语义（固定 seed 的 per-sample 定义）、验证 batch>1 出分不漂移。逐处改动见**附录 A**。
- **对 GPU-sim 的耦合**：向量化 env 天然产出 lockstep 的 batched obs → 直接喂 batch 路径。所以对 GPU-sim，**batch 与向量化强耦合**，不是"GPU 成瓶颈后才做"（见 §7.5）。

### 7.5 GPU-sim 的额外路径：向量化 env（编排层之外）

对 SAPIEN 系（RoboTwin / SimplerEnv / ManiSkill），引擎原生支持 `num_envs` 向量化（ManiSkill `gym.make_vec`）。一个向量化 PhysX scene 比"N 个独立进程各一份 scene"省显存、共享 GPU scene，收益是**引擎级**的。

- **归属**：这条路在 benchmark 的 rollout loop 里，**编排层给不了**——属 benchmark/sim 引擎侧（如迁移到 ManiSkill3）。
- **与 batch 的关系**：向量化 env 产出 lockstep 的 batched obs，直接喂 server 的 batch 路径 → **batch 与向量化强耦合**。
- **本方案定位**：§3.2 的进程 fan-out 对 GPU-sim 是**保底**；向量化是 benchmark 侧的独立工作流，不在本分支范围，但 roadmap 要认领。

**配置入口（本分支只定义契约，不实现）**：

- config：`benchmark.num_envs`（默认 1）→ adapter 透传 → env 构造。
- 能力声明：benchmark 用 `metadata.max_num_envs` 声明支持上限（照抄已有的 `metadata.num_camera_views` / `state_dim` 模式，见 `servers/loongforge_server.py::_warmup_model`）；coordinator 校验 `num_envs > max_num_envs` → **报错**，不静默忽略。
- 与 batch 绑定：`num_envs > 1` **必须同时开启 server 侧 batching**，否则 K 个请求仍会漂移、拿不到收益。
- 现在只把字段写进契约（§5.3）保证前向兼容，**实现留给 GPU-sim 工作流**——不要在 config 里先放一个没人实现、也不生效的旋钮。

---

## 8. 完整流程（升级后）

> 输入/输出视角的端到端流程。**阶段 A 标定**（一次性，可缓存）→ **阶段 B 正式评测**。相关：§6（标定）、§5.4（配置 schema）、§4.3（队列）。

### 8.1 阶段 A · 标定（一次性；按 `(model, benchmark, gpu)` 缓存）

| 步骤 | 输入 | 输出 |
|---|---|---|
| A1 提交标定配置 | 标定 YAML（model + benchmark，不跑全量） | 标定任务 |
| A2 扫 N（小分片） | `N ∈ {1,2,4,8}`、代表性小分片、1 server（+ N sim） | 原始指标：插桩 logging（`t_infer`/延迟分布）+ `results.jsonl`（`t_sim`）+ `utilization.memory`/`dmon` 序列 |
| A3 生成报告 | A2 的原始指标 | `report.json` / `report.md` / `env.json` |
| A4 agent 决策 | 报告三件套 | 并行配置（`calibrated` 段）+ `rationale` |
| A5 校验 + 兜底 | agent 配置 + `env.json` 硬约束 | 合法配置（过 schema + 边界）；非法 → 回落"≥95% 平台吞吐的最小 N" |
| A6 缓存 | 合法配置 + 报告 | `(model, benchmark, gpu) → 配置 + 报告` 表项 |

> **A2 的 N=1 那次同时就是 T-01 的 vanilla**，不重跑。

### 8.2 阶段 B · 正式评测

| 步骤 | 输入 | 输出 |
|---|---|---|
| B1 提交评测配置 | run YAML（静态段 + 查到的 `calibrated` 段） | 完整配置 |
| B2 枚举 unit | benchmark 定义（suite×task×episode）+ `chunk_size` | `work/pending/*.json`（每 unit 带 `ckpt/benchmark/suite/task/ep_range/resource_class`） |
| B3 起 server | `servers` 段（ckpt/replicas/gpus）+ `placement` | 常驻 server 进程（`ws://host:port` + `/healthz`） |
| B4 起 worker 池 | `calibrated.pools` + 资源画像 | worker 进程池（按 `ckpt × resource_class` 分组） |
| B5 认领 unit | `pending/` + 本 worker 的 `resource_class` | `running/<unit>.<wid>.json`（原子抢占，抢不到换下一个） |
| B6 跑 shard | unit + 该 unit ckpt 的 `server_url` | 每 episode 结果 → append `results/worker_<id>.jsonl` |
| B7 标 done | 完成的 unit | `done/<unit>.json` |
| B8 看门狗（与 B5–B7 并发） | worker 心跳 / PID + `running/` | 死 worker 的 unit 回 `pending/` |
| B9 收尾 | `pending/` 与 `running/` 皆空 | 停 server |
| B10 聚合 + 校验 | `results/worker_*.jsonl` + 计划应跑集合 | 去重指标 + **覆盖校验**（漏→自动补跑，最多 3 轮；仍缺才 `ok=false`） |

### 8.3 两类差异（只影响 B3 / B4）

- **CPU-sim**：B4 按**核数**装箱（每卡 1 server + N sim）；B3 无 placement。
- **GPU-sim**：B4 按**显存**装箱（每卡 1 server + k sim）；B3 先定 `placement`（同卡 / 异卡，§4.4）。

---

## 9. 风险

1. **CPU 超订**：N 过大 → 128 核打满 → 每个 sim 的 wall-clock 被拉长。必须扫 N-吞吐曲线找拐点。
2. **GPU 仿真同卡争用**：sim 渲染与推理分时，**`t_infer` 会被拉长**（0.5s 是 CPU-sim 下量的）→ 按它算的 N 会系统性偏大。GPU 仿真必须重测同卡下的 `t_infer`。
3. **失败隔离**：某 sim 崩溃只重跑其 episode，不影响同 server 其他 sim（队列天然支持）。
4. **结果写入并发**：worker 必须各自独立结果文件，聚合时按 `(suite, task, episode)` 去重。
5. **server 长时运行**：一个 server 服务多任务时显存/KV cache 是否需定期刷新（待观察）。
6. **批内噪声 / 数值一致性**（仅阶段 3）：见 §7.4。
7. **排队等待撞客户端超时**：server 串行处理 → 第 N 个 sim 每步要等 `≈(N-1)×t_infer`。客户端 per-call 超时 `--policy-call-timeout-ms` **默认 5000ms**（`libero_runner.py:588`，`:294` 用 SIGALRM）→ N=8、`t_infer`=0.5s 就 ~3.5s（贴着 5s），**N>10 直接爆**；GPU-sim 同卡 `t_infer` 变长会更早爆。**超时必须按 N 放宽**（≥ `N × t_infer` + margin）。

---

## 10. 代码触点速查

| 文件 | 行 | 用途 |
|---|---|---|
| `orchestrator/run.py` | 92/141/167/191/215 | 5 处 `ManagedServer(...)`，阶段 1 改造点 |
| `orchestrator/server_manager.py` | 30-48 | `start()` / `ensure_running()`，external 模式改造点 |
| `orchestrator/runners/libero_runner.py` | 444 | `completed_episode_keys`，续跑现成实现 |
| `transport/rpc_server.py` | 22 / 41-49 / 71 | `PolicyServer`、串行排队、阶段 3 batcher 插入点 |
| `servers/loongforge_server.py` | 12 / 109 | `HTTPServer` 仅 `/healthz` |
| `model/fastwam/modeling_fastwam.py` | 349 | FastWAM `batch==1` 硬断言（阶段 3） |
| `model/fastwam/mot/fastwam.py` | infer_action | 另有 4 处 batch=1（阶段 3） |

---

## 附录 A：Batch inference 改动审计（阶段 3 前置调研）

> 结论：**底层 forward 无需改动**，只需补齐 FastWAM 的 batch 契约——它是当前唯一违约的模型。

### A.1 Model `modeling_fastwam.py::predict_action`（346-459）

| 行 | 现状 | 改为 |
|---|---|---|
| 349-350 | `if batch != 1: raise` | 删除断言 |
| 356-365 | `views = images[0]` | 遍历 B 个样本各自预处理，`stack → input_image [B,C,H,W]` |
| 382-385 | `state.reshape(1,-1)` | `reshape(batch,-1)` → `[B,D]` |
| 387-414 | noise `[1,H,D]` 固定 seed | `[B,H,D]`；**需定义批内噪声策略** |
| 422-431 | `prompt=instructions[0]` | `prompt=instructions`（Sequence）|
| 443-444 | `chunk.reshape(-1,dim)` | 批量反归一化 → `reshape(B,H,D)` |
| 459 | `return denorm[None,:,:]` | `return denorm.reshape(B,H,D)` |

### A.2 Core `mot/fastwam.py::infer_action`（979-1123）

**关键区分：限制在"推理侧 wrapper"，不在共享 forward。** 共享 forward 一定 batch-capable——训练 `training_loss`（`mot/fastwam.py:508`）就是 GBS 128 在跑。

**明确的 batch=1（4 处，已逐处确认）**：

| 行 | 代码 |
|---|---|
| 1004 | `input_image.shape[0] != 1: raise` |
| 1018 | `proprio.shape[0] == 1` 才通过 |
| 1027 | `torch.randn((1, action_horizon, dim))` |
| 1122 | `return {"action": latents_action[0]}` ← 只返回第 0 个样本 |

**已是 batch 通用（前期调研结论，尚未逐处复核）**：`timestep_video`(1068)、context 校验(1047-1060)、`encode_prompt`(230，收 `Sequence[str]`)、`_append_proprio_to_context`(248)、`_build_mot_attention_mask`（`[S,S]` 与 batch 无关）、`prefill_video_cache`(`mot/model.py:336`，docstring `[B,Sv,D]`）、`forward_action_with_video_cache`(`mot/model.py:422`，`cat(...,dim=1)` 保留 batch 维）、`infer_action_scheduler.step`（标量/逐元素）。

**结论：去噪循环 + KV-cache 路径是按 `[B,...,D]` 设计的，无隐藏 batch=1。支持 batch 只需改 `infer_action` 4 行 + `predict_action` ~3 行，底层 forward 一行不用动。**

### A.3 Server 侧动态批处理

阶段 1 下 `PolicyServer` 串行处理即可。阶段 3 若要动态批处理，方案是在 `_route_message` 与 `policy` 之间插入 batcher：请求入队并挂 `Future`，batcher 在 `max_wait_ms` 窗口内聚合 `max_batch_size` 个请求后一次性调用 `predict_action`，再按索引分发结果。**一个 server 只服务同一 ckpt**（LIBERO 7D / RoboTwin 14D 不可同批）；**同批还要求输入形状一致**——图像分辨率、相机路数不同也不能同批（RoboTwin 3 路 vs LIBERO 单路）。

### A.4 框架契约：batch 是既有约定，FastWAM 是唯一违约方

`eval/servers/predict_action_interface.py` 文件头明确：*"Accept batched eval images and instructions."*；`call_predict_action`(:102) 原样透传。

| 模型 | batch 支持 |
|---|---|
| groot_n1_6 (`modeling_groot_n1_6.py:1019`) | ✅ |
| groot_n1_7 (`:648`) | ✅ |
| pi05 (`:1033`) | ✅ |
| lingbot_va (`modeling_lingbot_va_infer.py:717`) | ✅ |
| xvla (`modeling_xvla.py:408`) | ✅ |
| dreamzero (`modeling_dreamzero_infer.py:141`) | ✅（收 batch dict）|
| cosmos3 (`modeling_cosmos3.py:466`) | ✅（内部按 batch_size 循环）|
| **fastwam (`modeling_fastwam.py:349`)** | ❌ **硬断言 `batch==1`** |

（该表为前期调研结论，除 fastwam 外尚未逐个复核。）

即 FastWAM 侧改动本质是"补齐契约"；server 端批处理是模型无关的框架改动，加上后其余 7 个模型都受益——但**这是阶段 3 的事，收益要到 GPU 成为瓶颈之后才兑现**。
