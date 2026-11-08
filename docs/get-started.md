---
doc_id: fusion-osagent/get-started
product: fusion-osagent
class: app
entry: developer
doc_type: get-started
title: { zh: "fusion-osagent 快速上手", en: "fusion-osagent get started" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - os_agent/cli.py
---

# 快速上手 / Get Started

## 1. 安装（macOS，Python 3.12）

```bash
cd /Users/dahai/fusion
source .venv/bin/activate
pip install -e fusion-osagent
```

## 2. 离线自检（stub 模式，无需任何兄弟服务）

```bash
fusion-osagent preflight
```

输出逐项检查结果（ok / fail 计数）。stub 模式下截图为 1x1 占位图，用于验证软件链路。

## 3. 首次调用

```bash
# capture one frame (stub mode)
fusion-osagent --stub screenshot --out frame.png

# click at logical point (x, y)
fusion-osagent --stub click 100 200

# ping fusion-mlx
fusion-osagent --stub health
```

## 4. 编程接口

```python
import asyncio
from os_agent.api import DesktopAgent
from os_agent.config import OsaConfig


async def main():
    agent = DesktopAgent(OsaConfig(stub_mode=True))  # real mode: stub_mode=False
    shot = await agent.screenshot()
    await agent.click(100.0, 200.0)
    res = await agent.click_by("OK")  # dual-track: AX first, visual fallback
    await agent.type_text("hello")
    await agent.key("Return", modifiers=["command"])
    await agent.close()


asyncio.run(main())
```

## 5. 录制 → 回放

```python
from os_agent.recorder import ManualEventSource, Recorder

agent = DesktopAgent(OsaConfig(stub_mode=True))
src = ManualEventSource(
    [
        {"kind": "click", "at": [100.0, 200.0], "button": "left"},
        {"kind": "type", "text": "hello"},
    ]
)
rec = Recorder(src, capture=lambda: None, clock=lambda: 0.0).record()
script = agent.translator.translate(rec)  # generalize fixed coords
report = await agent.replay(script)  # per-step frame assertion
await agent.close()
```

真实采集使用 `CGEventTapSource`（需 Accessibility TCC 授权）。

## 6. 切换到真实模式

1. 确认 fusion-executor 可用（真实 GUI 执行由它承担）。
2. 确认 fusion-mlx 在线并已加载 VL 模型（如 `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`）。
3. `OsaConfig(stub_mode=False)` 或去掉 `--stub`。

约束：缺 VL 模型时视觉回退轨道不可用；prod 档位（`FUSION_PROFILE=prod`）缺 API key 直接 RuntimeError。

## 下一步

- `DesktopAgent` 全量方法见 [api-reference.md](api-reference.md)
- 环境变量与配置见 [config-reference.md](config-reference.md)
- 常见问题见 [troubleshooting.md](troubleshooting.md)
