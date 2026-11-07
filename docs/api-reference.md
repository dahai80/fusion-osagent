---
doc_id: fusion-osagent/api-reference
product: fusion-osagent
class: app
entry: developer
doc_type: api-reference
title: { zh: "fusion-osagent API 参考", en: "fusion-osagent API reference" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - os_agent/api.py
  - os_agent/cli.py
---

# API 参考 / API Reference

## CLI（`fusion-osagent`）

| 命令 | 说明 |
|------|------|
| `preflight` | 离线软件自检（无需兄弟服务在线；额外实测 fusion-mlx health ping，停机的引擎会显式报 fail） |
| `screenshot --out frame.png` | 捕获一帧并写 PNG（stub 模式写 1x1 占位图；真实模式需 fusion-executor） |
| `click X Y` | 在逻辑坐标 (x, y) 点击 |
| `health` | 聚合探活：fusion-mlx + executor + browser |
| `serve-metrics --host 127.0.0.1 --port 11571` | 零依赖 stdlib HTTP 服务，暴露 `GET /metrics`（默认端口 11571，issue #6） |
| `code_debug` | fusion-code 视觉调试回路：patch → verify → feedback；写本地报告，可选 `--visual-feedback` CLI 钩子 |

全局开关：`--stub` 强制 stub 模式（等价 `OSA_STUB_MODE=1` / `OsaConfig(stub_mode=True)`）。

## `DesktopAgent`（`os_agent.api`）

Computer-Use 对齐动作（全部 async）：

| 方法 | 说明 |
|------|------|
| `screenshot()` | 捕获当前帧 |
| `click(x, y)` | 逻辑坐标点击 |
| `click_by("OK")` | 双轨定位点击：AX 搜索优先，视觉 grounding 回退 |
| `type_text("hello")` | 键入文本 |
| `key("Return", modifiers=["command"])` | 按键（可带修饰键） |
| `scroll(...)` / `drag(...)` / `wait(...)` | 滚动 / 拖拽 / 等待 |
| `assert_state(...)` | 动作后帧断言（像素 diff + VLM 验证） |
| `heal(...)` | 多定位器自愈：ax-label → ax-role → visual |
| `som_view(...)` | Set-of-Mark 编号标注视图 |
| `replay(script)` | 逐步回放语义化脚本，每步带帧断言 + 视觉守卫 |
| `close()` | 释放资源；如注册过集群则注销 |

## 周边模块（编程接口）

| 模块 | 接口 |
|------|------|
| `os_agent.config.OsaConfig` | env 驱动配置；点↔像素换算 |
| `os_agent.ax_tree` | `parse` / `find_by_label` / `find_by_role` / `collect_interactive` / `collect_sensitive` / `guess_role` / `strip_sensitive_labels` |
| `os_agent.recorder` / `translator` / `replayer` | 事件录制（`ManualEventSource` / `CGEventTapSource`）→ 语义化脚本 → 断言回放 |
| `os_agent.loops.code_debug` / `loops.autotest` | fusion-code 视觉调试回路；fusion-autotest 验收回路（osagent 执行、autotest 断言） |

## 适配器契约（兄弟包复用）

| 适配器 | 上游 | 契约 |
|--------|------|------|
| `adapters/executor.py` | fusion-executor | `gui_action(dict) -> GuiResult`，18 种 GuiAction |
| `adapters/mlx.py` | fusion-mlx | 多模态 chat（`localhost:11434`） |
| `adapters/browser.py` | fusion-browser | UDS JSON-RPC |
| `adapters/agent_studio.py` | fusion-agent-studio | HTTP API |

`/metrics` 端点与 SLO 的对应关系见 [SLO.md](SLO.md)。
