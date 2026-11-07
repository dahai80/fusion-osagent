---
doc_id: fusion-osagent/troubleshooting
product: fusion-osagent
class: app
entry: developer
doc_type: troubleshooting
title: { zh: "fusion-osagent 故障排查", en: "fusion-osagent troubleshooting" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: "@dahai"
source_of_truth:
  - README.md
  - os_agent/cli.py
  - os_agent/config.py
---

# 故障排查 / Troubleshooting

## 安装与环境

**`fusion-osagent` 命令不存在**
确认已 `pip install -e fusion-osagent`（工作区共享 venv：`source /Users/dahai/fusion/.venv/bin/activate`）。要求 Python ≥ 3.12。

**preflight 报 fusion-mlx 不可达**
`preflight` 会真实 ping fusion-mlx health 端点——停机的引擎会显式 fail（避免 ops 自检通过但每次点击都失败）。启动 fusion-mlx：`~/fusion/fusion-mlx/start.sh start`。

**prod 档位启动报 RuntimeError**
`FUSION_PROFILE=prod` 时缺 API key 直接失败。设置 `FUSION_CLIENT_KEY`（或旧别名 `FUSION_MLX_API_KEY` / `MLX_API_KEY`）；本仓不再有 settings.json 回退。

## stub 与真实模式

**截图是 1x1 占位图**
stub 模式的预期行为（`--stub` / `OSA_STUB_MODE=1` / `stub_mode=True`）。真实模式需要 fusion-executor 在线且 `stub_mode=False`。

**真实模式点击无效果**
真实 GUI 执行由 fusion-executor 承担；确认 executor 可达（`fusion-osagent health` 聚合探活）。需要 Accessibility TCC 授权的场景（录制 `CGEventTapSource`、integration 测试）先在系统设置授权。

**`click_by` 定位失败**
双轨定位先 AX 后视觉。AX 树里没有该标签时走视觉 grounding——需要 fusion-mlx 已加载 VL 模型（如 `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`）。可用 `OSA_INSPECT_TIMEOUT_MS` 排查 AX 遍历是否超时（默认 15s）。

**动作后断言频繁失败 / 误报**
帧断言阈值为 `OSA_ASSERT_DIFF_THRESHOLD`（默认 0.002）；被测界面有动画时先等界面稳定，或按界面特性微调阈值。自愈上限 `OSA_PLANNER_MAX_HEAL_CYCLES`（默认 4）耗尽会显式失败。

**回放步骤失败**
回放每步带帧断言 + 视觉守卫——步骤失败通常是界面状态与录制时不一致；从报告定位首个失败步，必要时重新录制。

## 推理与缓存

**推理请求被短路**
MLX 熔断器（`OSA_BREAKER_*`）在 fusion-mlx DOWN 时短路请求，半开探测恢复后自动放行；先恢复 fusion-mlx。

**VLM 结果过期或重复**
VLM 缓存 key 为 model + prompt + image hash——相同输入命中缓存属预期；需要强制重推理时改变输入图像或调整 TTL 相关配置。

**轨迹不可复现**
`OSA_TRAJECTORY_SEED` 默认 None（按目标随机）；设为固定值可复现 Bezier 轨迹。

## 运维

**`/metrics` 端口冲突**
`serve-metrics` 默认 11571（issue #6 由 9100 迁移，避免与 node-exporter 冲突）；`--port` 可改，端口登记见工作区 `architecture/port-registry.yaml`。

**报告写入被拒**
报告根目录白名单（`OSA_REPORT_ROOT`）拒绝路径穿越——把输出路径放到白名单根目录内。

**审计日志膨胀**
轮转由 `OSA_AUDIT_ROTATE_MAX_BYTES`（默认 50 MB）触发，保留策略见 `OSA_AUDIT_RETENTION_FILES` / `OSA_AUDIT_RETENTION_DAYS`。

## 测试

```bash
pytest tests/ -v                 # 全部离线单元/stub 测试
pytest tests/test_api.py::test_click_logs_and_returns_ok -v   # 单测
OSA_RUN_INTEGRATION=1 pytest -m integration    # 集成测试（需 TCC + VL 模型）
ruff check .
```

默认测试完全离线（进程内 stub 适配器）；集成测试由 `integration` marker 门控，默认跳过。

## 仍未解决

- 可靠性目标与测量口径见 [SLO.md](SLO.md)。
- 在 `gh issue view <N> --repo dahai80/fusion-osagent` 查看已知问题，或新建 issue（英文描述，附 `pip show fusion-osagent`、`preflight` 输出与最小复现步骤）。
- 上游缺口（E1/E2/B2/C1/AT1）按 README「Upstream dependencies」跟踪，勿在本仓修补兄弟仓库。
