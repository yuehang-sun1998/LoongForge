# LoongForge 具身评测编排改造 —— 架构方案

> **范围**：LoongForge 具身评测框架（`loongforge/embodied/eval`）的**评测编排加速**。本方案与模型、benchmark 均无关，对所有被测模型与所有基准（LIBERO / CALVIN / RoboTwin / SimplerEnv / ManiSkill）生效；文中出现的 FastWAM / LIBERO 只是**实测样本**。
> **分支**：`feat/eval-orchestration-parallel`（base `upstream/master` @ `7847a83`，已 rebase），仓库 `/workspace/LoongForge-Github`。
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
- **聚合**：merge `results/worker_*.jsonl`，按 `(suite, task, episode)` 去重。
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

> **两种都要实测对比**（见 `03_testing.md` T-07）：同一 GPU-sim benchmark，分别跑「同卡」与「异卡」两种放置，比 wall-clock、`t_infer`（含抖动）、显存峰值。**已有单 sim 显存基线（~5.5GB/sim），同卡/异卡的 `t_infer` 对比待跑；默认按"同卡"起步。**

> **实现（2026-10-29）**：coordinator 已支持 `pools.gpu_sim.placement`（`co_locate` / `separate`），并为每个 worker 注入 `run.cuda_visible_devices`，绑定到 server 所在卡（`co_locate`）或 `sim_gpus`（`separate`）；`separate` 要求 `sim_gpus` 与 server 卡不相交。显存上限由 `resources.gpu_sim` 做 fail-fast 校验。见 `02_progress.md` §2.30。

**并行关系不是一一对应**：仍是 1 server : N sims（N>1）。理由：单 sim 流水 `obs→推理→step→obs` 严格串行有气泡，N>1 才能让 server 在 A 步进时给 B 推理。差别在 N 的约束：CPU 仿真由**核数**定，GPU 仿真由**显存**定。

### 4.5 一张卡放几个 server（同 ckpt 多副本）

**先分清两个量**：**"喂满"指算力（SM），不是显存**——server 可以只占 10% 显存却把算力吃满，也可能占一半显存但算力只用 50%。显存是**容量**约束（能放多少），算力是**吞吐**约束（跑多快）。

**为什么默认 1 server/卡 就够喂满算力**：server 串行，只要 sims 够多、请求背靠背，forward 一个接一个 → 算力饱和，**与模型占多少显存无关**。

**反例（需要同卡并发）**：若**单个 forward 本身喂不满 SM**（模型小 / kernel 计算量低 / 访存受限），则一个串行 server 喂不满算力（带宽 `utilization.memory` 会卡在低位）。这时有两条路：

1. **加大 batch**（M3，一次 forward 处理 K 个）——**一份模型、一个进程**；
2. **同卡第二副本**（两个进程各一份模型、并发跑 forward）——两份模型。

两者都是"填空闲 SM"。**若只是要填空闲算力，batch 通常更优**（省显存、省管理）——**现已实测（2026-10-15）**：

- **同卡 2 副本只给 1.25×**（同一批 400 集、同为 128 并发：worker wall 10.16→8.15 min，总 wall 13.20→12.29 min；差的 1.1 min 是 16 个 server 加载更慢）。**强次线性** —— 多进程共享 GPU 有**上下文切换/驱动串行化**开销，**不是**显存问题（2×24.7=49.4 < 81.9GB，跑得很干净）。
- **大 batch 在单次前向内给 7.3×**（`per_sample_ms` 583→79ms @ batch 8，T-13）。

→ **"填空闲 SM"该走 batch，不该靠多开副本**；`03_testing.md` T-08 据此结案。

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
    gpu_sim: { n_sims_per_server: 3, placement: co_locate }   # separate 时再加 sim_gpus: [8,9]
  timeouts: { policy_call_ms: 8000 }        # 按 N 放宽
  rationale: "..."

# ── 预留（先 disabled，避免以后改 schema）──
batching:      { enabled: false, max_batch_size: 8, max_wait_ms: 5 }   # M3
vectorization: { num_envs: 1 }                                          # §7.5
```

两条设计原则：

1. **按 `(ckpt) × (resource_class)` 两条轴组织**，别用扁平字段——否则多 ckpt / 两类混跑会立刻不够用。
2. **`batching` / `vectorization` 现在就预留块**（`enabled: false` / `num_envs: 1`），保证 schema 前向兼容。

> **已实现子集（M1/M2，当前代码，`coordinator.py`）**：`run.output_dir` / `run.timestamped_output` / `run.max_retries`（coordinator 默认注入 `2`：episode 级 infra 失败——`episode_error`/超时——重试，模型失败不重试）、`servers[].gpus`（=**G**，一张卡一个 server）、`pools.cpu_sim.n_sims_per_server`（=**N**）、`benchmark.{suite|suites, task_ids, episodes_per_task, episode_offset, episodes_per_chunk, max_steps, suite_overrides, return_action_chunk}`、`server.external_url`、`timeouts.policy_call_ms`；编排开关 = config 里有 `coordinator` / `servers` / `pools` 任一 key（`run.py` 按 key 存在性分发）。
> **GPU-sim（2026-10-29 已接线）**：`pools.gpu_sim.{n_sims_per_server, placement, sim_gpus}` 决定 sim 的卡；worker 通过 `run.cuda_visible_devices` 绑定到 server 卡（`co_locate`）或 `sim_gpus`（`separate`，要求与 server 卡不相交）；`resources.gpu_sim.{vram_total_mb, vram_per_sim_mb, vram_model_mb}` 做 `N*vram_per_sim + vram_model <= vram_total` 的 fail-fast 校验。
> **unit 级细节**：`_shard_config` 从 `server.external_url` 同步 `server.host/port`（部分 runner 只读 port）；`_unit_completed()` 在 unit 已写出全部预期 episode 时不再因进程返回码非 0 而重试。RoboTwin 分片额外注入 `seed_stride = shard 数 × chunk`，unstable seed 按整轮跳步，避免与相邻 shard 的 seed 区间重叠。
> **端口**：coordinator 基址默认 **6000**（`LOONGFORGE_COORD_PORT_BASE` 可覆盖），不要落在 10000–61000 的内核临时端口段。
> 其中 **`benchmark.return_action_chunk`**（LIBERO 默认 true；CALVIN / SimplerEnv / ManiSkill 需显式开启）false 时走旧 server-side chunk cache（每步请求），用于 T-14 的旧路径 A/B。执行细节见 §7.6 (2) 的 `EpisodePolicyStepper`。
> **`benchmark.skip_intra_chunk_obs`（RoboTwin，默认 false，2026-10-30）**：chunk 由客户端持有时，非 replan 步不再调用 `get_obs()`（不渲染 3 路相机），只构造 `joint_action` 供 decoder 锚定；`ModelClient.needs_obs()` 决定当前步是否需要观测。**仅在 `return_action_chunk: true` + `chunk_execute_steps > 0` 时有效**（server-cache 模式每步都请求，恒为 True）；闭环模型自动恒 True。实测渲染占单步 ≈66%，端到端 2.96×（`03_testing.md` T-07）。
> **`benchmark.episodes_per_chunk`**（默认 0 = 自动）显式指定“一个 worker 进程连续跑几集”，用于摊薄每集一次 torch/robosuite import；`per_task` 下仍强制一 task 一 shard（忽略该值）。
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

### 5.6 server 自愈：看门狗 + 连接重建【✅ 已实现】

覆盖校验（§5.5）解决"漏集"，但 **server 挂掉**是另一类故障。实测（`03_testing.md` T-10）暴露三个缺口：

1. **无人重启**：unit 一律 external 模式，`ManagedServer.ensure_running` 直接 `return`（`server_manager.py:44`）→ **server 死了没人拉起**。
2. **连接不重建 → 级联失败**：`ConnectionClosedError` 被 `_classify_exception` 归为 `episode_error`，而 runner 只在 `server_unreachable` 时重建 client（`libero_runner.py:513`）→ 同一条死连接继续失败。
3. **连接等待 300s**：一旦重建，`PolicyClient._wait_for_server(timeout=300)` 对着死 server 等满 300s → 每次尝试 300s × 重试 × 补跑轮 = 分钟级到几十分钟级拖尾。

**实现（三件，`57dcef9` + `3cc3a2d`）**：

| # | 位置 | 改动 |
|---|---|---|
| 1 | **coordinator（server 所有者）** | 看门狗线程：周期查每个 server 的进程存活 + `/healthz`；死则**原地重启**（同端口 → unit 的 `external_url` 不变）。**只有所有者能真正重启 server**，unit 不拥有它。 |
| 2 | **runner** | `_classify_exception` 把 websocket 连接类异常（`ConnectionClosed*`）归为 `server_unreachable` → 触发 client 重建 + `ensure_running`。 |
| 3 | **client / unit** | `_wait_for_server` 的 300s 对 unit 级过长 → 缩短/可配；或 unit 连接失败**快速失败**，把恢复交给 coordinator（unit 重试 + §5.5 补跑）。 |

**自愈链**：server 挂 → coordinator 看门狗重启（**同端口**）→ unit 重建 client 重连 → episode 级重试 + §5.5 补跑补齐覆盖。

**实测（2026-10-15，T-10）**：跑到中途 `kill -9` 掉一个 server → 看门狗日志 `server 0 died; restarting on port 18100` → `server 0 back up`；结果 **`server_restarts=1`、`validation.ok=true`、`infra_failed=0`、`failed_units=[]`**，218s 跑完（修复前是无限 300s 卡死）。**代价**：重启要重新加载模型（~100s），期间命中的 unit 靠重试/补跑等它起来——**可接受，远好于不恢复**。

### 5.7 健壮性兜底（M2 加固，2026-10-15）【✅ 已实现】

除了 server 自愈（§5.6），还有四类失败模式，已一并兜住：

| 失败模式 | 后果 | 兜底 |
|---|---|---|
| **unit 卡死**（native 层死锁，SIGALRM 穿不透） | 等待单元永久阻塞 → **整轮挂死** | 每个 unit 有 wall-clock 预算（`max episodes × per_episode_sec + connect + slack`，`run.unit_timeout_sec` 可覆盖）；超预算 kill → 走重试/补跑。有效值落 `coordinator_result.json` |
| **`infra_failed > 0` 却看着正常** | 分数被压低却被当成功 | coordinator 打印覆盖行 + 对 `infra_failed`/`ok=false` 出 **stderr 告警**；**`ok=false` 时 `run.py` exit 2**（"需要人工介入"）。过程中仍不中断 |
| **看门狗 vs 关闭竞态** | restart 期间 shutdown → **孤儿 server 占卡** | `_restart` 前后都查 stop 标志（期间启动的进程自己收尾）；restart 等待上限 600s（不用 1800s）；teardown 先 `join` 看门狗 |
| **hung server**（进程活但不服务） | 看门狗查不到（`poll()` 正常，且 healthz 是**独立线程**照样 200） | 每分钟对每个活 server 做**真 ping 探活**（`PolicyClient.ping(timeout=)`），连续 2 次失败 → **先 terminate 旧进程（它仍占端口）再重启**；探活有界（3s 连接 / 15s pong），并**恢复被 `PolicyClient` 弹掉的代理环境变量** |
| **两个 coordinator 共用 `output_dir`** | 互相覆盖 `worker_*/` 与结果 | 启动时发现 `coordinator_result.json` 就**拒绝启动**（除非 `run.resume: true`） |
| **`policy_call_ms` 低于排队等待** | 假 `policy_timeout`，看着像模型失败 | 设了 `timeouts.service_ms`（来自标定报告的 `service_mean_ms`）时，按 `N × service_ms × 2` 算下限，**低于就 stderr 告警** |
| **server 崩溃循环** | 每 15s 重启一次、每次重载 ~100s → 慢速空转 | 每个 server 重启上限 `MAX_SERVER_RESTARTS=3`；超了**放弃**并记入 `servers_given_up`，`run.py` 同样 **exit 2** |

**明确不做 / 只写方案（用户 2026-10-15 决定）**：

- **coordinator supervisor：不做。** coordinator 仍是单点（它崩了没人拉起），但**不引入外部 supervisor**——靠 runner 的 **resume 手动重跑**即可（已完成集跳过，代价可接受）。
- **GPU-sim 的 `policy_call_ms` 推导：只写方案，本次不开发。** GPU-sim 的 `t_service` 更长、且与 sim 渲染争同一张卡，下限必须用**实测的 GPU-sim 服务时间**（见 §4.4 / `03_testing.md` T-07），不能拿 CPU-sim 的数外推。

### 5.8 分片：per-benchmark planner 表【✅ 已实现】

**问题**：原 `_shards()` 把两件事混在一起——**"这个 benchmark 有哪些工作单元"（benchmark 相关）** 和 **"怎么把单元变成 worker"（benchmark 无关）**。前者被写死成 LIBERO 的 `(suite, task, episode)` 网格 → 其他 benchmark 进不来。

**拆法**：shard 变成**纯数据记录**，planner 每族一个，按 `benchmark_family()`（= `run.py` 选 runner 用的**同一个函数**，两边共用一份）查表。

```python
Shard = {"id": str, "overrides": {<config 段>: {<键>: <值>}}, "expected": int}
```

| family | planner | 工作单元 | 分片轴 |
|---|---|---|---|
| libero | `plan_libero` | `(suite, task, episode)` | suite × task(int) × episode(+offset)，chunk 细到快槽能抢；`env_seed_mode=per_task` 时默认退化为 **task 粒度**，启用 RNG 快照后恢复 episode 粒度（见 §5.8.1） |
| simplerenv / maniskill | `plan_episode_indexed` | **单集** | task(str) × episode |
| robotwin | `plan_robotwin` | `(task, config, seed 区间)` | task(str) × config × `start_seed` + `test_num` |
| calvin | `plan_calvin` | `(task_suite, sequence)` | suite × `num_sequences`（**runner 无 offset → 目前只能 1 片**） |

**通用侧不变**：`_shard_config()` 退化成"deepcopy + apply overrides + 去 coordinator-only 键"，不认任何 benchmark；派发（全局队列 / 重试 / 看门狗）不变。

**校验也跟着改成 benchmark-无关**：不再展开 `(suite, task, episode)`，改成**按 shard 的 `expected` 计数 + `episode_id` 去重**；"重复"定义为**两个不同 shard 声明了同一集**（同一 shard 的重跑是预期重叠，靠 `episode_id` 去重吸收）；**补跑 = 重跑"短了的 shard"**（落到新 worker 目录）。

**后续 TODO（要拿细粒度必须改 runner；环境无法验证，先留档）**：

| # | runner | 现状 | 改成 | 买到 |
|---|---|---|---|---|
| 1 | `calvin_runner` | `eval_sequences[:num_sequences]` —— **只能从头取 N 条** | 加 `--sequence-offset` → `eval_sequences[off:off+n]` | CALVIN 从**1 片**变 N 片，能进全局队列 |
| 2 | `simplerenv_runner` / `maniskill_runner` | 一次进程**只跑 1 集**（`--episode-idx`） | 支持"跑 N 集"（`--episodes-per-task` + offset，内部循环） | 一片 = N 集，env/scene 只建一次，摊薄启动开销；planner 能按 chunk 切 |
| 3 | `robotwin_runner` | 靠**正则 patch RoboTwin 源码**设 `st_seed`/`test_num`（`text.replace(...)`） | 走正式入参；**至少校验 patch 命中，不命中就报错** | 分片不再静默失效 |

> **边界提醒**：本方案的约定是"改动落点在**编排层**，不动 runner"。以上三项**要动 runner** → 建议**单独一轮、按 family 分开提交**，别和编排层混。
> **为什么现在不做**：环境里**只有 LIBERO 能跑**（CALVIN 依赖未装、RoboTwin 缺 EGL 驱动、SimplerEnv/ManiSkill 依赖未装）→ **写了也没法验证**。

#### 5.8.1 `per_task` 下的 episode 级分片：RNG 快照方案（已实现，6 集 GPU 逐位验证通过）

**问题**：LIBERO 的 `per_task`（官方 FastWAM 语义）把 env 的随机状态绑在"进程内第几次 reset"上，而 episode-chunk 分片会把同一 task 拆到多个进程 → 每个进程都从第一个场景重放，静默改变评测集。

**代码事实（`/workspace/LIBERO` + robosuite 1.4.0）**：

- `bddl_base_domain.seed(seed)` 只做 `np.random.seed(seed)`；
- `_reset_internal()` 用 placement sampler（`np.random.uniform`，含拒绝采样）采样物体/fixture 摆放，并把 **fixture 的 `body_pos/body_quat` 写进 model**；
- runner 每集调用 `env.reset()` + `set_init_state(initial_states[k])`，而 `set_init_state` 只恢复 **sim state（qpos/qvel）** → **fixture 摆放不受它约束，仍取决于 reset 时的 RNG**；
- `step()` / `manipulation_env.py` 中**没有** `np.random` 调用 → 随机消耗集中在 reset。

**方案**：把"第 k 集开始前的 RNG 状态"预先算出来并持久化，分片按集恢复，彻底解耦"进程内 reset 次数"。

1. **预计算**（每 `(suite, task, seed)` 一次，只 reset、不跑 episode）：

   ```python
   env = OffScreenRenderEnv(...)
   env.seed(seed)
   for k in range(episodes_per_task):
       snapshots[k] = np.random.get_state()   # 第 k 次 reset 之前
       env.reset()                            # 真跑，复现拒绝采样次数
   ```

   产物 `scene_snapshots/<suite>__task<id>__seed<seed>.npz`：E 份状态（约 2.5KB/份，E=50 约 125KB/task）。

2. **分片执行**：`per_task` 下每集开始前 `np.random.set_state(snapshots[k])`，再 `env.reset()` + `set_init_state(initial_states[k])`。

**例子**（E=6、chunk=2、3 个进程）：

| 进程 | episode | 恢复的快照 | 实际场景 |
|---|---|---|---|
| shard 0 | 0,1 | S0,S1 | A,B |
| shard 1 | 2,3 | S2,S3 | C,D |
| shard 2 | 4,5 | S4,S5 | E,F |

不恢复快照时，每个进程 `seed` 后都从 S0 起步 → 三个分片实际都只看到 A、B。

**实现要点**：

- **每集都恢复**，不只片头跳一次。片头跳一次在"连续 + 不重试 + 不 resume + step 不消耗 RNG"时才成立；而 retry 会多 reset 一次、resume 会从中间开始，都会静默错位。`set_state` 是微秒级，相对单集几十秒不可见。
- **预计算与 worker 的 env 创建顺序一致**：建 env → `seed` → 恢复快照 → reset。
- **manifest 记录**快照路径 + sha256；缺失/指纹不符直接报错，不静默退回。
- **回退**：未启用快照时保持 §5.5.4 的 task 级分片 + 拒绝兜底（正确但并发 ≤ task 数）。
- **planner**：启用快照后 `per_task` 恢复 episode 级切片，`N` 不再受 task 数限制。

**成本（E=50、40 task 全量）**：

| 方案 | 并发上限 | 额外 reset | 备注 |
|---|---|---|---|
| task 级（现状） | 40 / 128 槽 | 0 | 长尾 = 最慢 task 的 50 集 |
| reset 快进（每片重放前缀） | 128 | ~600 次/task（c=2，O(E²/2c)） | 每片墙钟最多等 ~(E-c)×t_reset |
| **RNG 快照** | **128** | **50 次/task，一次性** | 任意 offset O(1) |

**验证**（见 `03_testing.md` T-17）：串行 vs 分片的逐集首帧哈希比对，外加"故意恢复错集快照"的反证。

**实现（2026-10-22）**：

- 新模块 `orchestrator/scene_snapshots.py`：dump/load/apply + CLI；
- 生成（每 task 一次，先于评测）：

  ```bash
  python -m loongforge.embodied.eval.orchestrator.scene_snapshots \
    --suite libero_10 --task -1 --seed 42 --episodes 50 --out <snapshot_dir>
  ```

  产物 `<snapshot_dir>/<suite>__task<id>__seed<seed>.pkl`，含每集 reset 前的 `np.random` 与 `random` 状态，并返回 sha256。
- 配置：`benchmark.env_seed_mode: per_task` + `benchmark.scene_snapshot_dir: <snapshot_dir>`；
- planner：配置了快照目录时，`per_task` 恢复 **episode 级切片**，并跳过“禁止 task 内切分”的兜底；
- runner：每集 `env.reset()` 前恢复 `snapshots[episode_idx]`，episode record 记录 `scene_snapshot` 路径；
- 单测：`test_scene_snapshots.py`（状态往返复现场景序列、越界检测、版本校验）+ planner 2 例；
- **GPU 验证（2026-10-22）**：libero_10/task0/seed42 6 集，串行 vs 快照恢复的 sim state 与首帧哈希 6/6 全等；分片 steps 与串行一致。
- **全量对齐（2026-10-22）**：2000/2000、0 重启、97.15% vs 参照 97.00%；逐集 steps 一致 91.3%。残余差异已证明为模型代码数值漂移（task3/spatial task4 前 10 集场景哈希新旧代码 10/10 相同，steps 却已分叉）。
- **未做**：coordinator 尚未自动生成快照（当前需要先跑上面的 CLI）。


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

- 🔴 **判据已修正（2026-10-15）**。旧判据"GPU 大量空转 → 做 batching 只会让 GPU 更空"**不完整**：batching 降低的不只是"单请求 GPU 耗时"，还包括**每请求的固定开销**（Python 调度 / 数据搬运 / VAE encode·decode / kernel launch）。而这里**绑定资源就是那个串行 server**（§6.2 实测 N=16 时 server 忙占比 87%、GPU 仅 ~27%）——**server 每请求成本降下来 = 吞吐上限直接抬高**，与"GPU 忙不忙"无关。所以 **batching 是候选，不是默认否决**。
- **正确判据**：**一次真实推理的 wall 花在哪**？① 固定开销占主导 → batching K 个请求把固定开销只付一次 → 收益大；② 串行依赖链占主导（逐步去噪 / 无法批的 VAE decode）→ 收益有限。
- **怎么量（go/no-go 探针）**：`orchestrator/batch_probe.py`（诊断入口，**不动评测路径**）—— ① profile 真实 `predict_action` → `wall / cuda / host / gpu_share` + top kernels；② 抓真实 `FastWAM.infer_action` 调用、按 batch 展开重放 → `per_sample_ms` vs batch。
- 🟡 **实测结论（2026-10-15，`03_testing.md` T-13）**：① **`gpu_share ≈ 1.04`**（`cuda_ms 680.6 > wall_ms 655.5`，host ≈ 0）→ **一次真实推理是 GPU 饱和的**，所以上面"固定开销占主导"那条**被证伪**；batching 的收益只可能来自**大 batch 下 SM 利用率更高**（batch-1 的 GEMM 效率）。② **缩放测不出来**：batch=1 假设**散在 5 处**（`mot/fastwam.py` / `wan/core.py` ×2 / `mot/idm.py` / `mot/joint.py`），编码器还是逐样本 list API → **batch 契约是真的不存在**。
- 🔑 **实测收益（2026-10-15，T-13 第二轮：临时放开契约后测，已还原）**：**batch 8 的 `total_ms` 与 batch 1 几乎相同**（583→635ms，+9%），即 **`per_sample_ms` 583→79ms，7.3×**（batch 2/4 分别 2.0×/3.9×）。正确性已验证（编码器实测收到 K 张、`latents_action=(K,32,7)`）→ **batch 真的进了去噪**。
- **因此 M3 的结论改为 GO，且是量级最大的剩余杠杆**（server 是绑定资源；把它每请求成本降 ~7× 直接抬上限）。**做的前置 = 补 batch 契约**：放开 6 处入口断言（`mot/fastwam.py`×2 / `wan/core.py`×2 / `mot/idm.py` / `mot/joint.py`，**全在推理侧**；训练 bs=16 已证明 DiT 能批、VAE `encode(list)` 会 stack）+ 修返回路径（现在返回仍压掉 batch）+ **批内噪声按各请求 seed 逐行设** + server 侧 batcher（`max_wait_ms` 窗口聚批，sim 不同步 → 批可能不满）。
- ⚠️ **端到端上限受 CPU 侧约束**（Amdahl）：server 提速 ~7× 后 CPU 可能成为新绑定资源。当前 CPU 仅 8.9%（N=16，§6.2）→ 有余量，但**最终收益要实测**，不能把 7.3× 直接当端到端加速。
- 🔴 **正确性门先行**：T-13 只证明"跑得动、batch 流进去了"，**没证明"结果没变"**。M3 必须先过 **`03_testing.md` T-06**（batch=1 vs batch=K 逐样本比对）——为此 `infer_action` 要接受 **per-row seeds**（既是 batcher 需要的接口，也是让比对可精确归因的前提）。
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

### 7.6 优化链：chunk 客户端化 → batching → 提 N【M3 设计依据】

> **状态（2026-10-20）**：第 (2) 步已实现并验证——`return_action_chunk` + `ChunkConsumer`，80/80 集与旧 run 逐位一致，请求量 9.83×；第 (3) 步 `AsyncBatcher` + 真 batch 路径已接通（T-06：同 seed 受控下 batch 行逐位相同、与 bs1 的 `max|d|≈4.9e-3` 为 bf16 归约差；per-row seeds 行独立）；第 (4) 步重扫 N 是下一步。

三件事**必须按序做**，且**互相耦合**。

#### (1) 现状（2026-10-15 核实）

- **chunk 存在 server 端**：`loongforge_policy.py:42` 的 `_chunk_cache[episode_id] = (index, chunk[32])`；真推理算 32 步，只执行前 `chunk_execute_steps=10` 步。
- **评测端每步都发请求**（`libero_runner.py:359`），且**每次带完整 payload**（2×224×224×3 ≈ 300KB 图像），**即使这次只是取 cache 里的下一个动作**。
- 结果：**每 10 步 = 1 次真推理 + 9 次 cache hit**；命中路径 `_route_message` 只花 **7.7µs**，但端到端 **~0.9ms**（全是打包/传输/解包一帧用不上的图像）。
- **`return_action_chunk` 开关已存在**（`loongforge_policy.py:95`，一次返回整段），但 **runner 的调用点从没传过**（恒 `False`），且**评测端没有"持 chunk 逐步消费"的代码**。

#### (2) chunk 客户端化：开关 + 队列

| # | 改动 | 位置 |
|---|---|---|
| 1 | 传 `return_action_chunk=True` | runner 调用点 |
| 2 | **评测端新增 action 队列**：队列空才发请求，一次填满整段，逐步 `pop` | `_common.ChunkConsumer`（供各 runner 复用） |
| 3 | 语义对齐：`episode_step` 只在实际发请求时推进（trace 区分"请求步/环境步"）；`payload_builder.update_from_response` 只在发请求时调；`client.reset` 时清队列 | 各 runner |
| 4 | **遵守 `disable_action_cache`**（闭环 chunk 模型）：该开关为真时队列长度=1，行为等价于现状 | 各 runner |

- **收益**：**请求量 10 → 1**、图像传输 10 → 1、server 忙占比从尖峰降到 ~10%；**结果应逐位一致**（同一份 chunk、同一顺序），可直接与现有全量结果比对。
- **注意**：这一步**本身几乎不加速**（省的 RPC 相对 2s/10步 很小），它的价值是 **消掉 90% 的无效请求**，为 batcher 铺路。

**落地（2026-10-26）**：把"何时请求 / 如何缓冲 / 如何切动作"收敛到 `_common.EpisodePolicyStepper`，四个 in-process runner（LIBERO / CALVIN / SimplerEnv / ManiSkill）都走同一个组件：

- runner 只提供 `request_fn(request_step) -> (response["data"], e2e_ms)`（内含 adapter / payload / RPC / 超时）；stepper 负责 chunk 游标、`disable_action_cache`、`payload_builder.update_from_response` 和 trace 用的 `request_step` / `request_id` / latency。
- `return_action_chunk=False` 时 server 每次只回 1 行，chunk 每步耗尽 → 请求节奏与旧的逐 step 循环完全一致，所以 CALVIN / SimplerEnv / ManiSkill 默认仍走旧路径，新增 `--return-action-chunk` 显式开启；LIBERO 默认开启（已验证）。
- **两类必须 opt-out**：`disable_action_cache` 模型（DreamZero / LingBot-VA，闭环 chunk）由 stepper 自动每步请求、只用第一个动作；action ensemble（SimplerEnv `--action-ensemble`）需要每步新预测，传 `chunk_steps=1`，但保留 `PolicyStep.raw_actions` 供 ensemble 使用整段。

#### (3) batching：队列里只剩真推理

**chunk 化之后，batcher 的队列天然只装真推理**（hit 请求根本不出 sim 进程），无需任何特判。

**但 batch 大小不会因此变大**——真实推理的到达率没变（`N / (10 × t_step)`）。它由**供需平衡**决定：

```
需求率 λ = N / (10 × t_step)
容量   C = K / t_infer          # batch K 的服务时间几乎不变（T-13：8× 工作量 +9%）
稳态   λ ≈ C  →  K ≈ N × t_infer / (10 × t_step)
```

实测代入（`t_infer≈0.65s`、`t_step≈0.2s`、`N=16`）→ **K ≈ 5**（对照 T-13：batch 4 = 3.9×、batch 8 = 7.3× → **实收 ~4×**）。

**正反馈**：batching 让推理变快 → sim 不再等 → 需求率上升 → 队列保持饱和 → batch 维持（不会自己饿死）。

> **batching 不提供逐位可复现性**：batch 里装哪几个请求由到达时序决定，bf16 归约顺序随组成变化。实测配置相同的两次全量（batching 均开启、仅分片粒度不同）只有 **46.3%** 的集 steps 相同；即使 **batching 关闭**，两次完全相同的 run 也只有 **93.8%**（`off16` vs `off16b`）。所以验收看 **success rate**，不看逐集 steps。数据与结论见 `03_testing.md` T-19。

#### (4) 提 N：两者做完后的真正杠杆

**server 能支撑的 sim 数**（chunk 化之后请求 100% 是真推理）：

```
N_max ≈ K × (10 × t_step) / t_infer
```
| batch K | N_max（每 server） | 8 卡总量 | 备注 |
|---|---|---|---|
| 1 | ≈ 5 | 40 | **现状 N=16 是靠 90% 的 µs 级 hit 在"过载"跑** |
| 4 | ≈ 12 | 96 | |
| 8 | ≈ 25 | 200 | |

> **2026-10-22 实测校正（chunk 已下沉 + batching 接通）**：
> - **口径**：历史基线（vanilla / 静态 94.2 / M2 56.2）都是 **`chunk_execute_steps=10`**；N 必须在这个 cadence 下标定。
> - **第一轮扫描（chunk=16、固定 200 集/点）作废**：固定 workload 只有 200 个 unit，N≥25 时实际并发被 unit 数封顶，`N / mean(episode_wall)` 会高估吞吐（各 N 的 elapsed 实际都是 564–583s）。详见 `02_progress.md` §2.25。
> - **内存实测**（N=64、200 workers）：峰值 467GB、`MemAvailable` 最低 1040GB、单 sim PSS **~2.0GB**、无 OOM → `N_mem≈75`，N=64 安全。
> - **第二轮改用 external 模式**：8 server 常驻、每 client 1 集、总并发 = 8N；`chunk=10`、`max_wait_ms=50`，同时采集实际 mean batch size、timing、内存与 CPU。
> - 上表的 `N_max ≈ K × 10 × t_step / t_infer` 只算 **server 容量**；实际约束是 **CPU/内存 + 吞吐平台**。`N*` 不能跨 cadence 搬运（10 的请求率是 16 的 1.6×）。
> - **T-16 实测（2026-10-23）**：8 卡同 N=16 的 per-server 吞吐 8.84 eps/min，单卡独跑 17.6 → **shared-load penalty ≈ 0.50**；8 卡并行不同 N 时，同 N=16 的 server 只有 5.8（-34%）。**正式标定必须 8 卡同 N**，或用 0.50 修正单卡结果；明细见 `03_testing.md` T-16。

> **2026-10-30 GPU-sim 实测（RoboTwin + pi05，N=4，8 卡）**：batching 端到端**无收益**（3.57 → 3.42 ep/min）。
> - 不是 batch 契约的问题（模型侧 k=8 时单样本快 4.1×），而是**到达率**：每 sim 每 ~74s 一次真推理，N=4 时每 server 18.4s 一个请求，`max_wait_ms=50` 内几乎不可能聚合（理论 mean batch ≈ 1.003）；要到 mean batch≈2 需要 ~370 sim/卡。
> - 推理只占单集 wall 的 3~4%；GPU 被 sim 渲染占满（`utilization.gpu` 100%），N=8 吞吐反而下降。
> - 因此 **“N 不够大”不是解法**，真正的杠杆是 sim 侧渲染成本（见 `/workspace/markdowns/robotwin_render_every_step.md`）。
> - 成功率侧：同配置复跑（batching 均关）也有 11/32 集翻转，单轮 A/B 无法判断成功率变化，需多轮均值。
> - 数值侧：pi05 batch vs 串行 `max=2.9e-2`、`mean=3.3e-3`（~0.4% 相对），与 FastWAM bf16 同属归约噪声，无结构性差异（`03_testing.md` T-06）。
> - **2026-10-30 之后 cadence 已变**：开启 `skip_intra_chunk_obs` 后单步 2.30s → 0.77s（渲染占 66%，端到端 2.96×），每 server 请求间隔从 18.4s 降到 4.6s，server busy 预计 40–50%。**上一条“batching 无收益”只对旧 cadence 成立**；batching 与 N 需要在 skip_obs 配置下重测（N=8 复测进行中）。


> ⚠️ **`N` 是 per-server，CPU/内存是整机共享**：并行在 8 张卡上跑不同 N 时，每个点的 `ΣN` 和背景负载不同，per-server 吞吐/膝点会被污染。正式标定用“8 卡同 N”，或用 `03_testing.md` T-16 测出的 shared-load penalty 修正单卡扫描结果。显存/内存是**硬上限**，不是“效率最高”的判据。

**新的瓶颈顺序（N 加大后）**：

| 资源 | 现状（N=16×8） | N=64×8 | 结论 |
|---|---|---|---|
| CPU | 8.9%（128 核） | ~35% | 有余量 |
| **内存** | 2.2GB/sim × 128 ≈ 281GB | ≈ 1.1TB（上限 1.5TB） | ⚠️ **最先撞墙** |
| 仿真 | 被推理掩盖 | 成为瓶颈 | 见下 |

```text
wall = max(仿真总时间 / 总 sim 数, 推理总时间 / (K × 卡数))
     = max(62000 CPU·s / 512, 14000 GPU·s / 64) ≈ max(121s, 219s) ≈ 3.6 min   ← 理想下界
```
对现状 56 min → 理论上限 ~15×；计入加载/长尾/争用，**现实估计 8–12 min**。

#### (5) 顺序与耦合（不要跳步）

1. **chunk 客户端化**（简单、结果逐位一致、为 batcher 铺路）
2. **batcher**（队列只装真推理；按目标 K 反推 `max_wait_ms`）
3. **重扫 N-吞吐曲线**（chunk 化之后才是真实曲线，`calibrate.py`）
4. **提 N，先看内存**；再往上需要降 per-sim 占用（如 `MUJOCO_GL=egl` 省渲染 buffer）
5. ⚠️ **`t_step` 与 `N_max` 耦合**：换渲染后端（osmesa→egl）会改 `t_step` → **N 必须重标定**
6. GPU-sim 另算（SAPIEN 向量化与 batch 天然耦合，见 §7.5）

**per-benchmark 适用性**：LIBERO / CALVIN / ManiSkill 可**直接套**（都是"每步请求、只取第一条"）；**SimplerEnv** 有 action ensemble（依赖跨步 `action_history`，要保证更新节奏不变）；**RoboTwin** 的交互在外部脚本里（要单独改）。

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
| B10 聚合 | `results/worker_*.jsonl` | 按 `(suite, task, episode)` 去重的最终指标 |

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

8. ~~**server 挂掉无自愈**~~ **【已修 `57dcef9` + `3cc3a2d`】**（2026-10-15 实测，T-10）：coordinator 路径上 unit 全是 external 模式 → 没人重启 server；`ConnectionClosedError` 不触发 client 重建 → 死连接级联失败；client 重建又会对着死 server 等满 300s。**已按 §5.6 修**（看门狗同端口重启 + 连接异常归类 + 连接超时 60s）；复测 `server_restarts=1`、`infra_failed=0`。残留代价：重启需重载模型 ~100s。

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
