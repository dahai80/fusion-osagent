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
| **3** | 录制/回放、类人轨迹执行、敏感打码接入 | 计划中 |

Phase 0 验收：stub `api.screenshot()` + `api.click(x,y)` 走通双轨调度；
`ruff check .` 与 `pytest tests/` 绿。✅

## 上游依赖

硬阻塞缺口以 issue 上报（不在本仓内补同源仓）：

- **E1** —— `fusion-executor`：暴露 `scale_factor` / 能力查询。
- **B2** —— `fusion-browser`：为 UDS JSON-RPC API 提供 Python 客户端。
- **C1** —— `fusion-code`：稳定的视觉回喂协议（osagent 发 JSON，code 消费自动修复）。在此之前 `code_debug` 写本地报告 + 可选 `--visual-feedback` CLI 钩子。
- **AT1** —— `fusion-autotest`：单需求 VLM 断言模式（当前 `vlm` 断言整份 PRD；osagent 解析 `vlm_result.json` 缺陷）。

## 许可证

Fusion 本地优先 Apple Silicon 生态的一部分。见仓库根目录。
