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
    agent = DesktopAgent(OsaConfig(stub_mode=True))   # 真实模式：stub_mode=False
    shot = await agent.screenshot()
    await agent.click(100.0, 200.0)
    res = await agent.click_by("OK")        # 双轨：先 AX，视觉兜底
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
points_to_pixels(100.0, 200.0, 2.0)   # (200.0, 400.0)
pixels_to_points(200.0, 400.0, 2.0)   # (100.0, 200.0)
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

## 上游依赖

硬阻塞缺口以 issue 上报（不在本仓内补同源仓）：

- **E1** —— `fusion-executor` [#43](https://github.com/dahai80/fusion-executor/issues/43)：暴露 `scale_factor` / 能力查询。
- **E2** —— `fusion-executor` [#44](https://github.com/dahai80/fusion-executor/issues/44)：批量鼠标移动（waypoint 路径）GuiAction。
- **B2** —— `fusion-browser` [#12](https://github.com/dahai80/fusion-browser/issues/12)：为 UDS JSON-RPC API 提供 Python 客户端。
- **C1** —— `fusion-code` [#217](https://github.com/dahai80/fusion-code/issues/217)：稳定的视觉回喂协议（osagent 发 JSON，code 消费自动修复）。在此之前 `code_debug` 写本地报告 + 可选 `--visual-feedback` CLI 钩子。
- **AT1** —— `fusion-autotest` [#11](https://github.com/dahai80/fusion-autotest/issues/11)：单需求 VLM 断言模式（当前 `vlm` 断言整份 PRD；osagent 解析 `vlm_result.json` 缺陷）。

## 许可证

Fusion 本地优先 Apple Silicon 生态的一部分。见仓库根目录。
