---
doc_id: fusion-osagent/architecture
product: fusion-osagent
class: app
entry: developer
doc_type: architecture
title: { zh: "fusion-osagent 架构", en: "fusion-osagent architecture" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - os_agent/api.py
  - os_agent/perception.py
---

# 架构 / Architecture

## 分层

```
                   DesktopAgent  (os_agent.api)
                        |
              Perception (os_agent.perception)
              /         |          \
   ExecutorAdapter  MlxAdapter  BrowserAdapter   AgentStudioAdapter
        |              |              |                 |
  fusion-executor  fusion-mlx   fusion-browser    fusion-agent-studio
  (AXUI/CGEvent)   (inference)  (Web AXTree/CDP)  (orchestration)
        |
  fusion-core (HTTP/LLM client, config, logging)
```

`DesktopAgent` 是唯一编程入口；`Perception` 做双轨定位（AX 搜索 → 视觉 grounding）；四个适配器把兄弟包包成统一 `Adapter` 协议（`adapters/base.py`：`Locator` / `Screenshot` / `Adapter`）。

## 五道屏障

1. **AX + 视觉双轨感知**（`perception.py`）— AXUI 树优先，视觉回退。
2. **Set-of-Mark 标注**（`som.py`）— AX 边界 + 编号标记叠加截图。
3. **Fast/Slow 双核推理**（`reasoning.py`）— 小 VL 提议，大 VL 验证/规划。
4. **动作后帧断言 + 自愈**（`action.py` / `healer.py`）— 像素 diff + VLM 验证；多定位器自愈。
5. **拟人轨迹**（`trajectory.py`）— Bezier 鼠标路径 + 键盘抖动。

## 支撑模块

| 模块 | 职责 |
|------|------|
| `ax_tree.py` | 统一 AX 树解析（find / collect / guess_role / 敏感标签剥离） |
| `image_cache.py` | 解码图像 LRU 共享缓存（mask / SOM / diff 每帧只解码一次） |
| `vlm_cache.py` | VLM 结果缓存（TTL + LRU，key = model+prompt+image hash） |
| `mask.py` | VLM 推理前敏感区域遮罩 |
| `crop_zoom.py` | 细粒度 grounding 的局部裁剪放大 |
| `planner.py` | FSM 规划器 + State Guard |
| `recorder.py` / `translator.py` / `replayer.py` | 录制 → 语义化翻译 → 断言回放 |
| `loops/code_debug.py` / `loops/autotest.py` | fusion-code 视觉调试回路；fusion-autotest 验收回路 |

## 兄弟复用（不重建）

| 兄弟包 | 角色 | 契约 |
|--------|------|------|
| fusion-core | HTTP/LLM 客户端、配置、日志 | `get_async_client` / `get_logger` |
| fusion-executor | GUI 执行 | `gui_action(dict) -> GuiResult`，18 种 |
| fusion-mlx | VL 推理 | `FusionMLXClient`（11434） |
| fusion-browser | Web AX 树 + CDP | UDS JSON-RPC |
| fusion-agent-studio | 编排 | HTTP API |
| fusion-autotest / fusion-code | 断言 / 编码回路 | 见 `loops/` |

上游 API 缺口一律以 issue 提出（README「Upstream dependencies」列出的 E1/E2/B2/C1/AT1），不在本仓修补兄弟仓库。

## 稳定性机制

- AX 遍历超时（`OSA_INSPECT_TIMEOUT_MS`）、MLX 熔断器（`OSA_BREAKER_*`，半开探测）、图像/VLM 双层缓存、审计日志轮转、报告根目录白名单。
- 集群模式：`OSA_AGENT_ID` 命名节点，`close()` 时注销；stub 模式跳过集群接线。
- 可靠性目标与测量见 [SLO.md](SLO.md)。
