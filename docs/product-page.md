---
doc_id: fusion-osagent/product-page
product: fusion-osagent
class: app
entry: developer
doc_type: product-page
title: { zh: "fusion-osagent 产品页", en: "fusion-osagent product page" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - pyproject.toml
  - os_agent/api.py
  - os_agent/cli.py
---

# fusion-osagent

桌面具身 AI 屏障层（barrier layer），Apple Silicon 原生。与驱动体素级物理机器人的 fusion-robot 相对，fusion-osagent 驱动像素级桌面 GUI 自动化：复用兄弟 fusion 包作为原语，在其上叠加五道屏障，把原始 GUI 访问变成可靠的 agent 能力。

## 产品能力

| 能力 | 说明 | 你不再需要做的事 | 约束 |
|------|------|------------------|------|
| AX + 视觉双轨感知 | AXUI 树优先，视觉 grounding 回退（`perception.py`） | 不必在 AX 查询与截图识别之间手写切换逻辑 | AX 遍历受 `OSA_INSPECT_TIMEOUT_MS` 约束（默认 15s） |
| Computer-Use 对齐 API | `screenshot` / `click` / `type` / `key` / `scroll` / `drag` / `wait`，扩展 `assert` / `heal` / `som_view` / `replay` | 不必自行适配 Claude Computer Use 工具协议 | 单一入口 `DesktopAgent`（`os_agent/api.py`） |
| Set-of-Mark 标注 | 截图 + AX 树叠加编号标记（`som.py`） | 不必自绘元素标注层 | 依赖 AX 边界数据 |
| Fast/Slow 双核推理 | 小 VL 模型提议、大 VL 模型验证/规划（`reasoning.py`） | 不必自建双模型调度策略 | 推理经 fusion-mlx（默认 11434） |
| 动作后断言 + 自愈 | 像素 diff + VLM 验证状态变更；多定位器自愈（ax-label → ax-role → visual） | 不必为每次点击手写验证与重试 | 自愈上限 `OSA_PLANNER_MAX_HEAL_CYCLES`（默认 4） |
| 拟人轨迹 | Bezier 鼠标路径 + 键盘抖动（`trajectory.py`） | 不必手写拟人化轨迹生成 | `OSA_TRAJECTORY_SEED` 默认 None（按目标随机） |
| 录制 → 翻译 → 回放 | 事件 + 截图上下文 → 语义化 Steps → 带断言回放（`recorder` / `translator` / `replayer`） | 不必从固定坐标脚本手工泛化 | 回放每步带帧断言 + 视觉守卫 |
| 敏感区域遮罩 | VLM 推理前遮盖敏感区域（`mask.py`） | 不必为隐私自建图像脱敏管线 | 作用域为发送给 VLM 的图像 |
| 兄弟包复用 | executor（18 种 GuiAction）/ mlx / browser（UDS JSON-RPC）/ agent-studio 适配器 | 不必重建 GUI 执行、推理或浏览器树能力 | 上游缺口以 issue 提出，不在本仓修补 |
| 运维面 | `preflight` 自检、`health` 聚合探活、`serve-metrics` stdlib HTTP `/metrics` | 不必自写探活与指标端点 | serve-metrics 默认 `127.0.0.1:11571`（issue #6） |

## 约束与边界

- 真实模式（`stub_mode=False`）需要 fusion-executor 可用；stub 模式（`OSA_STUB_MODE=1`）完全离线，截图为 1x1 占位图。
- 集成测试需要 Accessibility TCC 授权 + 已加载 VL 模型的 fusion-mlx（如 `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`），由 `OSA_RUN_INTEGRATION=1` 显式开启。
- API key 解析单点委托 fusion-core（`FUSION_CLIENT_KEY` → `FUSION_MLX_API_KEY` → `MLX_API_KEY`；prod 缺 key 直接 RuntimeError）。
- 审计日志带轮转（`OSA_AUDIT_ROTATE_MAX_BYTES` 默认 50 MB）与保留策略；报告写入限 `OSA_REPORT_ROOT` 白名单根目录内（防路径穿越）。

## 安装

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
pip install -e fusion-osagent
```

## 文档索引

- 快速上手：[get-started.md](get-started.md)
- API 参考：[api-reference.md](api-reference.md)
- 配置参考：[config-reference.md](config-reference.md)
- 架构说明：[architecture.md](architecture.md)
- 故障排查：[troubleshooting.md](troubleshooting.md)
- SLO 定义：[SLO.md](SLO.md)
- 变更记录：[CHANGELOG.md](../CHANGELOG.md)
