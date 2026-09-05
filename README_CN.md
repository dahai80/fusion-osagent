# fusion-osagent

> 桌面具身智能 —— Apple Silicon 上的像素级 OS Agent 壁垒层。

`fusion-osagent` 是 `fusion-robot` 的桌面具身对应物：`fusion-robot` 驱动体素级物理机器人，
`fusion-osagent` 驱动像素级桌面 GUI 自动化。它复用同源 fusion 包作为原语而非重建，
并加上把原始 GUI 访问变成可靠 Agent 的五道壁垒：

1. **AX + 视觉双轨感知** —— 先 AXUI 树，视觉定位兜底。
2. **Set-of-Mark (SOM) 标注** —— 在截图 + AX 树上叠加编号标记。
3. **快/慢双核推理** —— 小 VL 模型提议，大 VL 模型校验/规划。
4. **动作后帧断言 + 自愈** —— 断言状态变化，定位失败则重定位。
5. **类人轨迹** —— 贝塞尔鼠标路径、敏感区域遮罩、录制/回放。

API 对齐 Claude Computer Use 的 `computer` 工具
（`screenshot` / `click` / `type` / `key` / `scroll` / `drag` / `wait`），
并扩展 `assert` / `heal` / `som_view` / `replay`。

## 架构

```
                   DesktopAgent  (os_agent.api)
                        |
              Perception (os_agent.perception)
              /         |          \
   ExecutorAdapter  MlxAdapter  BrowserAdapter   AgentStudioAdapter
        |              |              |                 |
  fusion-executor  fusion-mlx   fusion-browser    fusion-agent-studio
  (AXUI/CGEvent)   (推理)       (Web AXTree/CDP)  (编排)
        |
  fusion-core (HTTP/LLM 客户端, 配置, 日志)
```

- `os_agent/config.py` —— 环境变量驱动的 `OsaConfig`，点↔像素转换。
- `os_agent/ax_tree.py` —— 统一 AX 树解析器（parse / find_by_label / find_by_role / collect_interactive / collect_sensitive / guess_role / strip_sensitive_labels）。
- `os_agent/image_cache.py` —— 共享解码图像 LRU 缓存（mask / SOM / diff 复用同一帧的单次解码）。
- `os_agent/vlm_cache.py` —— VLM 结果缓存（TTL+LRU，键为 model+prompt+图像哈希；相同输入跳过重复推理）。
- `os_agent/adapters/base.py` —— `Locator`、`Screenshot`、`Adapter` 协议。
- `os_agent/adapters/executor.py` —— 封装 `fusion-executor` 的 `gui_action`（18 种 GuiAction）。
- `os_agent/adapters/mlx.py` —— 封装 `fusion-mlx` 多模态对话。
- `os_agent/adapters/browser.py` —— 经 UDS JSON-RPC 连接 `fusion-browser`。
- `os_agent/adapters/agent_studio.py` —— 经 HTTP 连接 `fusion-agent-studio`。
- `os_agent/perception.py` —— 双轨定位（AX 搜索 → 视觉定位）。
- `os_agent/api.py` —— `DesktopAgent`，唯一入口。
- `os_agent/som.py` —— Set-of-Mark 标注（AX 边界、编号标记）。
- `os_agent/action.py` —— 动作后帧断言（像素差分 + VLM 校验）。
- `os_agent/healer.py` —— 多定位自愈（ax-label → ax-role → 视觉）。
- `os_agent/planner.py` —— FSM 规划器 + State Guard。
- `os_agent/mask.py` —— 推理前敏感区域打码。
- `os_agent/reasoning.py` —— 快/慢双核调度（Phase 2.1）。
- `os_agent/trajectory.py` —— 类人 Bezier 鼠标轨迹 + 按键抖动（Phase 2.2）。
- `os_agent/crop_zoom.py` —— Patch 级裁剪放大，精细定位（Phase 2.3）。
- `os_agent/loops/code_debug.py` —— fusion-code 视觉调试闭环：改码→验证→回喂（F5.2）。
- `os_agent/loops/autotest.py` —— fusion-autotest 验收闭环：osagent 执行，autotest 断言（F5.3）。
- `os_agent/recorder.py` —— GUI 轨迹录制：事件+截图上下文→结构化 Step（F4.1）。
- `os_agent/translator.py` —— 固定坐标 Step→自然语言脚本+Semantic Guard（F4.2）。
- `os_agent/replayer.py` —— 经 DesktopAgent 逐步回放，每步过帧断言（F4.3）。
- `os_agent/cli.py` —— `fusion-osagent` CLI（preflight / screenshot / click / health）。

## 同源复用（不重建）

| 同源包 | 角色 | 契约 |
|--------|------|------|
| `fusion-core` | HTTP/LLM 客户端、配置、日志 | `get_async_client`、`get_logger` |
| `fusion-executor` | GUI 执行 | `FusionSandboxExecutor.gui_action(dict) -> GuiResult`，18 种 |
| `fusion-mlx` | VL 推理 | `FusionMLXClient`，`localhost:11434` |
| `fusion-browser` | 网页 AX 树 + CDP | UDS JSON-RPC（暂无 Python 客户端） |
| `fusion-agent-studio` | 编排 | HTTP API，37 内置工具 / 11 NodeType |
| `fusion-autotest` | 断言 | 动作后帧断言（Phase 1） |
| `fusion-code` | 编码闭环 | 软件工程任务的视觉调试（Phase 2） |

同源 API 的缺口以 issue 形式上报，不在本仓内修补
（见 `architecture/fusion-osagent-prd-0904.md`）。

## 安装

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
pip install -e fusion-osagent         # 运行时
pip install -e "fusion-osagent[test]" # 带测试依赖
```

## 用法

```bash
# 离线软件自检（无需启动同源服务）
fusion-osagent preflight

# 抓一帧（stub 模式写 1x1 占位图）
fusion-osagent --stub screenshot --out frame.png
fusion-osagent screenshot --out frame.png   # 真实模式需 fusion-executor

# 在逻辑点 (x, y) 点击
fusion-osagent --stub click 100 200

# 探测 fusion-mlx
fusion-osagent --stub health
```

编程式调用：

```python
import asyncio
from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig


async def main():
    agent = DesktopAgent(OsaConfig(stub_mode=True))  # 真实模式：stub_mode=False
    shot = await agent.screenshot()
    await agent.click(100.0, 200.0)
    res = await agent.click_by("OK")  # 双轨：先 AX，视觉兜底
    await agent.type_text("hello")
    await agent.key("Return", modifiers=["command"])
    await agent.close()


asyncio.run(main())
```

## 测试

```bash
pytest tests/ -v          # 全部单测/stub 测试
pytest tests/test_api.py::test_click_logs_and_returns_ok -v   # 单个用例
ruff check .              # lint
ruff format .             # 格式化
```

- `asyncio_mode=auto`，`testpaths=["tests"]`。
- 默认测试完全离线，跑进程内 stub 适配器
  （`OSA_STUB_MODE=1` / `OsaConfig(stub_mode=True)`）。
- 标记 `integration` 隔离需启动同源服务的用例：
  `OSA_RUN_INTEGRATION=1 pytest -m integration`（需 fusion-mlx 加载 VL 模型，
  如 `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`）。

## 坐标空间

API 暴露**单一逻辑点空间**（Apple "points"）。
适配器经 `scale_factor`（默认 `2.0` Retina）换算成物理像素。

```python
from os_agent.config import points_to_pixels, pixels_to_points

points_to_pixels(100.0, 200.0, 2.0)  # (200.0, 400.0)
pixels_to_points(200.0, 400.0, 2.0)  # (100.0, 200.0)
```

`scale_factor` 自动探测待 executor 暴露能力查询接口（issue E1）；
在此之前默认 `2.0`。

## 路线图 / 阶段

| 阶段 | 目标 | 状态 |
|------|------|------|
| **0** | 骨架 + stub 适配器、双轨感知、ruff/pytest 绿 | ✅ 完成 |
| **1** | SOM 叠加、帧断言、自愈、FSM 规划器、敏感打码、真实 VL 端到端 | ✅ 完成 |
| **2** | 快/慢双核推理、Bezier 轨迹、crop/zoom、code-debug + autotest 闭环 | ✅ 完成 |
| **3** | 录制/回放+泛化转译、类人轨迹执行、敏感打码接入 | ✅ 完成 |

Phase 0 验收：stub `api.screenshot()` + `api.click(x,y)` 走通双轨调度；
`ruff check .` 与 `pytest tests/` 绿。✅

Phase 3 验收：录制→转译→回放闭环跑通，每步过帧断言；Semantic Guard 按描述
重定位而非固定坐标；Bezier 轨迹驱动类人点击；每次 VLM 调用前敏感区域打码。✅

## 加固（审计 0904）

一次完整对抗审计（`audit/fusion-osagent-0904.md`）推动了 P0–P3 全部发现的加固修复。要点：

- **P0 致命** —— executor 路径改为真实 `asyncio.to_thread`（不再假异步阻塞）；browser 适配器复用单一 UDS socket + 原子 RPC id 计数；敏感打码 fail-closed（无 AX 树 → 整帧模糊，绝不把原始像素送 VLM）；VLM 坐标输出消歧（归一化 vs 像素），`chat_json` fail-loud（返回 `None`，绝不返回 `{}`）；Recorder `CGEventTapSource` 在独立线程跑 tap + 线程安全队列；帧断言按直方图统计变化像素数（非 bbox 面积）；文件句柄全部 `with` 关闭。
- **P1 逻辑** —— AX 定位采用分级置信度（精确 0.95 → 前缀 0.85 → 子串 0.7 → 角色 0.75）且设最小查询长度；planner 愈合重试有界并 `exc_info` 记录；translator guard 无 describer 时降级为 `point`；replayer 拖拽派发一次真实 `drag`；`code_debug` 传 `expected=None`（不做无意义语义匹配）并在点击后采集真实 `before` 帧（B3）。
- **P2 架构** —— 统一 `os_agent/ax_tree.py` 解析器取代三套重复递归遍历（`parse`、`find_by_label` 精确/前缀/子串三模式、`find_by_role`、`collect_interactive`、`collect_sensitive`、`guess_role`、`strip_sensitive_labels`）；坐标携带每帧 `scale_factor` 而非全局假设。
- **P3 性能** —— 共享解码图像缓存（`os_agent/image_cache.py`），mask / SOM / diff 复用同一帧单次解码；帧 diff 先降采样到 256px 缩略图再做直方图；VLM 结果缓存（`os_agent/vlm_cache.py`，TTL+LRU，键为 model+prompt+图像哈希）在 `decide` 两次输入未变时跳过重复推理。
- **可维护性** —— 顶层 import（无内联 `import`）；关键异常路径用 `log.exception` 输出 traceback；报告写入限定在 env 可配白名单根（`OSA_REPORT_ROOT`）并拒绝路径穿越。

D14（真实端到端跑通）受环境门控：7 个集成测试在 `OSA_RUN_INTEGRATION=1` 后端，需 Accessibility TCC 授权 + 已加载 VL 模型。TCC 授权属宿主环境步骤，非代码缺陷。

## 加固（审计 0905）

第二次对抗审计（`audit/fusion-osagent-audit-result-0905.md`）推动了 P0–P3 加固，聚焦并发、阻塞与分辨率无关安全。该审计结论：**尚不可企业级生产商用发布**；修复后可达"受控内部 Beta"。要点：

- **P0 致命** —— `BrowserAdapter._rpc_sync` 全程 `threading.Lock` 串行化 send/recv（并发 RPC 不再在共享 UDS socket 上交错长度前缀帧），并修复真实模式构造崩溃的漏 `import threading`；VLM `chat_vision` 以 `asyncio.wait_for(timeout=cfg.vlm_timeout)` 限界，mlx 推理卡住不再永久挂起 Agent loop；`assert_changed` 缺 `before` 帧时拒绝执行，不再背靠背捕获恒报"无变化"。
- **P1 高危** —— `_extract_json` 优先 ```json``` 代码块，7B fast 口语前缀不再每次强制 escalate slow；mask 模糊半径按帧长边自适应（`max(16, long_edge//32)`），4K Retina fail-closed 打码不再可辨认；planner 愈合成功重置 retry 预算；`CGEventTapSource` 接收真实显示器 scale 而非硬编码 2.0（多 DPI 安全）；`strip_sensitive_labels`/`_to_dict` 改迭代 + 深度上限，深 AX 树不再 RecursionError 崩溃 fail-closed 路径。
- **P2/P3** —— perception 死代码重复 return 块删除；`image_cache` 加锁（`asyncio.to_thread` 下线程安全）；`vlm_cache` prompt 改 sha1 哈希（dict key 不再存 KB 级字符串）；executor `_run` 真重试一次（兑现 docstring）并在 clamp 时告警；`inspect_tree` 独立超时（`OSA_INSPECT_TIMEOUT_MS` 默认 15s），AX 遍历不再被误判为点击超时；`collect_sensitive` 早停。
- 每项修复均有回归测试在 `tests/test_audit0905.py`。

### 审计 0905 —— v2 补充修复

后续一轮关闭了审计剩余项（E4、E6）及邻近运行时缺陷（N5、N9、N10），并收紧两处早期修复：

- **E4（P2）** —— `Reasoner` 与 `Perception` 现共享同一个 `SensitiveMasker`（在 `DesktopAgent` 构造一次）。此前两个独立 masker 的 `masked_count` 计数与 LRU 缓存各自分裂，fast→slow 升级对同一帧重复打码且无缓存复用，打码区域计数也不准。
- **E6（P3）** —— `Translator` 新增 `translate_async`/`_describe_async`，将阻塞的模型 `describer` 通过 `asyncio.to_thread` 下放到工作线程，翻译录制不再按步逐次 VLM 阻塞事件循环且无法取消。同步 `translate()` 路径不变，供离线测试。
- **N5** —— Fast 核心在帧无 AX 树时直接跳到 Slow。fail-closed 打码会把无 AX 整帧模糊到不可辨认，7B Fast 几乎总返回 `"none"` 随即升级 —— 但白白浪费一次 VLM 往返。直接跳过 Fast 移除该无效推理。
- **N9** —— `click_humanlike` 现从已跟踪光标位置（`DesktopAgent._cursor_pos`）起笔，而非固定 `(0,0)`。每次点击从角落瞬移到目标既突兀又是机器人指纹；点击后更新位置。
- **N10** —— `bezier_path` 无显式 seed 时按起止坐标派生每目标 seed（同目标可复现、跨目标变化），而非固定 `seed=7` 使每次点击抖动形状完全一致——强机器人指纹。`OSA_TRAJECTORY_SEED` 默认 `None`（每目标）；严格回放可设固定整数。
- **A2（收紧）** —— `Planner` 以 `max_heal_cycles`（默认 4，`OSA_PLANNER_MAX_HEAL_CYCLES`）限界总愈合次数，flapping 步骤（执行失败→愈合成功→执行失败…）不再死循环，尽管每次愈合成功仍重置单步 retry 预算。
- **R4（收紧）** —— `assert_changed` 阈值改取 `cfg.assert_diff_threshold`（默认 0.002，`OSA_ASSERT_DIFF_THRESHOLD`），光标闪烁误报与小高亮漏报可按场景调参，不再硬编码。

上游阻塞不变：E1（executor scale_factor/能力查询）、E2（executor 批量 move）、B2（browser Python 客户端）、C1（code 视觉回喂协议）、AT1（autotest 单需求模式）仍以 issue 上报，不在本仓内修补。

### 审计 0905 —— 企业发布缺口

审计将五处架构层缺口列为 osagent"不可企业级生产商用发布"的原因。本轮全部关闭，agent 达到商用机队部署标准。

- **指标与可观测性（E5）** —— `os_agent/metrics.py` 是零依赖、线程安全的指标核心：计数器、直方图（独立延迟桶 `LATENCY_BUCKETS_MS`）、缓存命中/未命中统计。`DesktopAgent` 持有按 agent 隔离的 `MetricsRegistry`（多节点机队按 agent 隔离计数，而非共享单例）。`Reasoner.decide` 记录 fast-accept / 升级计数与 `decide_latency_ms`；`_act`/`_act_raw` 记录每动作 total/ok/fail 计数与延迟。`metrics_snapshot()` 一次导出完整视图（计数器+直方图+打码总数+vlm 缓存统计）为 JSON-safe dict，供外部 Prometheus exporter 或 fusion-core monitor 抓取。
- **审计日志聚合（缺口 4）** —— `os_agent/audit_log.py` 是结构化 append-only JSONL 审计轨迹：每次 `decide`/`action`/`assert`/`heal`/`replay` 记录 `AuditEntry`（ts、agent_id、kind、detail）。线程安全；坏路径 fail-open（禁用持久化，保留内存缓冲）。`OSA_AUDIT_PATH` 选 JSONL 文件；空（默认）= 仅内存，离线测试无副作用。`query(kind, since)`/`count()` 支持聚合。
- **熔断 + 限流（缺口 3）** —— `os_agent/circuit_breaker.py` 守护每次 `chat_vision`。连续失败达 `breaker_failure_threshold` 或滑动窗口 `breaker_window_s` 内失败率超 `breaker_failure_rate`（最少 `breaker_min_calls_for_rate` 次调用）即开路，`breaker_cooldown_s` 内快速失败抛 `CircuitOpenError`，随后半开探测。全部旋钮可环境变量调（`OSA_BREAKER_*`）。mlx 集群宕机不再每个并发槽位钉满超时。
- **幂等回放事务（缺口 5）** —— `os_agent/replay_ledger.py` 按幂等键将已完成步骤序列持久化到 JSONL 账本。`replay(script, idempotency_key=...)` 跳过已标记完成的步骤，崩溃回放**恢复**而非重复执行变更步骤（无双击/双输/重复提交）。`ReplayLedger` 幂等（对已完成 seq 的 `mark_done` 为空操作），按键隔离进度。不传键则原非幂等行为（向后兼容）。
- **多节点编排（缺口 2）** —— `os_agent/coordination.py` 为共享同一 mlx 集群的 N 个 osagent 节点提供本地优先协调面（无 Redis/etcd——单机 Apple Silicon 机队）：`NodeRegistry` 向 flock 保护的 `nodes.json` 注册/心跳/注销，超 `heartbeat_ttl_s` 的僵尸节点被回收；`ClusterHealth` 跨所有节点聚合 mlx 失败为共享信号，任一节点在**集群**病态时即开路自身熔断，而非仅自身达标才开（4 节点 × 各 5 次自有失败不再 = 集群 20 次命中才有任一开路）。`DesktopAgent` 打 `mlx.node_id`，构造时注册，每周期心跳，`close()` 时注销。`OSA_AGENT_ID` 命名节点；`OSA_CLUSTER_DIR` 重定位状态目录。stub 模式跳过集群接线。

五项缺口的回归测试在 `tests/test_audit0905.py`（指标快照、线程安全注册表、直方图分桶、审计持久化+fail-open、熔断 开路/半开/按速率、幂等回放无双击、账本持久化+键隔离、节点注册表 注册/注销/僵尸回收、集群聚合失败开路、成功裁剪、文件锁非重入）。

### 审计 0905 —— 产品就绪性（六维评审）

产品 + 架构双视角六维评审（功能完整性、架构稳定性、安全风险、性能瓶颈、异常容错、运维配套）判定 osagent **具备受控企业内部生产商用发布条件，暂不具备无约束企业商用 GA 发布条件**。完整报告：`audit/fusion-osagent-audit-result-product-0905.md`。

本轮关闭 1 个 P0 安全旁路与全部新发现 P1/P2/P3：

- **SEC-1（P0）fail-closed 掩码旁路** —— `_mask_impl` 按字符串真值判定空树，`"{}"` / 空子树 JSON 为 truthy → Canvas/Electron 空树场景泄露原始像素（可能含密码框）。改为解析态判定：`tree is None or not tree.children`。
- **SEC-2（P1）** `som_view` 可视化前先掩码。
- **SEC-3（P1）** `_FileLock` 重写为 per-path `threading.Lock` + `fcntl.flock`，非重入（闭环跨线程读改写交错丢更新）。
- **SEC-4（P1）** `NodeRegistry.live_nodes` 过滤已知字段，peer 写额外键不再 TypeError 崩溃全部 reader。
- **SEC-5（P1）** `ReplayLedger.claim(seq)` 执行前原子占步，闭环 `is_done→execute→mark_done` 并发双执行竞态。
- **SEC-6（P2）** `audit_log.record` 降为 debug（不泄露 detail）；新增 `query_disk(kind, since)` 读持久化 JSONL。
- **SEC-7（P2）** `agent_studio.run_graph` 守卫上游返回结构。
- **SEC-8（P2）** `browser._send_recv` 响应上限 16 MiB + 退避。
- **ARCH-1（P1）** `code_debug` 在点击前捕获 `before`，`assert_changed` 比对真实点击前/后帧。
- **ARCH-2（P2）** `autotest.verify` 容错 VLM 超时。
- **ARCH-3（P3）** planner guard 失败历史记录 `action_ok`。
- **PERF-1/2（P0）** `masker.mask` / `som.annotate` / `api.som_view` / `mlx.cluster_health` 经 `asyncio.to_thread` 下放，PIL 与文件锁不再阻塞事件循环。
- **PERF-3（P1）** `image_cache` 解码像素上限 192 MiB（按条目数 + 字节双重 LRU 驱逐），长会话不致 OOM。
- **PERF-4（P1）** `trajectory.DEFAULT_STEPS` 24 → 6（减少每次点击 RPC）。
- **PERF-5（P1）** `executor` 超时退避重试。
- **PERF-6（P2）** 三个缓存均用完整 sha1 hexdigest（原截断 → 脏结果碰撞风险）。
- **PERF-7（P2）** `vlm_cache.put` 不再缓存 `None`（解析失败）。
- **OPS-1（P0）** CLI 运维面：`metrics`、`audit query`、`cluster nodes`、`cluster health` 子命令；SIGTERM → 优雅 `close()`（注销节点 + 关闭适配器）。

**判定**：无 P0 阻断；屏障层安全前提成立；容错有界可恢复。剩余到达商用 GA 的缺口为运维级（Prometheus 端点、审计日志轮转、正式 SLO）——列为后续立项，非可用性/安全性硬阻塞。建议路径：当前版本标记 `v1.0-controlled` 面向受控内部生产；运维补齐后发 `v1.1-ga`。

### 审计 0905 —— GA 运维补齐（v1.1-ga）

六维评审的 4 项运维级缺口现已全部闭合：

- **GA-1 Prometheus 采集端点** —— 新增 `os_agent/prometheus.py`，将 `metrics_snapshot()` 转为 Prometheus 0.0.4 文本格式（计数器、直方图 `_bucket`/`_sum`/`_count`、缓存 gauge、熔断/集群状态 gauge）。零依赖 stdlib HTTP 服务。CLI：`fusion-osagent serve-metrics --host 127.0.0.1 --port 9100` 暴露 `GET /metrics`。指标名清洗（`.`/`-` → `_`）以符合 `[a-zA-Z_:][a-zA-Z0-9_:]*` 规则。
- **GA-2 审计日志轮转 + 留存** —— `AuditLog` 在活动 JSONL 超过 `rotate_max_bytes` 时轮转为 `.{时间戳}` 归档，并按文件数（`retention_files`）和天数（`retention_days`）裁剪归档。配置项：`OSA_AUDIT_ROTATE_MAX_BYTES`（默认 50 MB）、`OSA_AUDIT_RETENTION_FILES`（默认 10）、`OSA_AUDIT_RETENTION_DAYS`（默认 30）。轮转为尽力而为、fail-open。`0` 禁用对应约束。
- **GA-3 正式 SLO + 容量基线** —— `docs/SLO.md`：各阶段延迟目标（p50/p95/p99 + 硬上限）、错误预算（30 天 1%）、可用性信号、单节点容量基线（2 并发 VLM 推理、32 图像缓存条目、审计磁盘边界）、多节点扩缩上限、Prometheus 告警规则、运维手册。仅对已声明模型（MLX 上 Qwen2.5-VL-7B/32B 4-bit）有效。
- **GA-4 replay_recording 迁移至 claim** —— `replay_recording` 改用 `ReplayLedger.claim(seq)`（执行前原子抢占），与 `replay_script` 对齐。关闭 `is_done→execute→mark_done` 窗口下相同幂等键的并发重放重复执行变更型固定坐标步骤（重复点击/提交）的竞态。

四项均由 `tests/test_audit0905.py` 回归测试覆盖。

## 上游依赖

硬阻塞缺口以 issue 上报（不在本仓内补同源仓）：

- **E1** —— `fusion-executor` [#43](https://github.com/dahai80/fusion-executor/issues/43)：暴露 `scale_factor` / 能力查询。
- **E2** —— `fusion-executor` [#44](https://github.com/dahai80/fusion-executor/issues/44)：批量鼠标移动（waypoint 路径）GuiAction。
- **B2** —— `fusion-browser` [#12](https://github.com/dahai80/fusion-browser/issues/12)：为 UDS JSON-RPC API 提供 Python 客户端。
- **C1** —— `fusion-code` [#217](https://github.com/dahai80/fusion-code/issues/217)：稳定的视觉回喂协议（osagent 发 JSON，code 消费自动修复）。在此之前 `code_debug` 写本地报告 + 可选 `--visual-feedback` CLI 钩子。
- **AT1** —— `fusion-autotest` [#11](https://github.com/dahai80/fusion-autotest/issues/11)：单需求 VLM 断言模式（当前 `vlm` 断言整份 PRD；osagent 解析 `vlm_result.json` 缺陷）。

## 许可证

Fusion 本地优先 Apple Silicon 生态的一部分。见仓库根目录。
