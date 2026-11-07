---
doc_id: fusion-osagent/config-reference
product: fusion-osagent
class: app
entry: developer
doc_type: config-reference
title: { zh: "fusion-osagent 配置参考", en: "fusion-osagent config reference" }
audience: engineer
status: draft
version: 1.1.0rc3
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
  - os_agent/config.py
---

# 配置参考 / Config Reference

配置以 env 驱动的 `OsaConfig`（`os_agent/config.py`）为单点；API key 解析委托 fusion-core（config Phase 2.2，issue #5）。

## 模式与环境

| 变量 | 说明 |
|------|------|
| `OSA_STUB_MODE` | `1` = stub 模式：完全离线，截图为 1x1 占位图，跳过集群接线 |
| `FUSION_PROFILE` | `prod` 时缺 API key 直接 RuntimeError；dev 缺 key 为空串 + warning |
| `OSA_RUN_INTEGRATION` | `1` 才运行 `integration` 标记测试（需 TCC 授权 + 已加载 VL 模型的 fusion-mlx） |

## API key 解析链（fusion-core 单点）

`FUSION_CLIENT_KEY` → `FUSION_MLX_API_KEY` → `MLX_API_KEY`；本仓不再读取 `~/.fusion-mlx/settings.json` 回退（已在 issue #5 中删除）。

## 感知与动作

| 变量 | 默认 | 说明 |
|------|------|------|
| `OSA_INSPECT_TIMEOUT_MS` | 15000 | AX 遍历超时（ms），防止 AX 卡死 |
| `OSA_ASSERT_DIFF_THRESHOLD` | 0.002 | 动作后帧断言的像素 diff 阈值 |
| `OSA_PLANNER_MAX_HEAL_CYCLES` | 4 | 自愈循环上限 |
| `OSA_TRAJECTORY_SEED` | None | 拟人轨迹随机种子（默认按目标随机；设为固定值可复现轨迹） |

## 审计与报告

| 变量 | 默认 | 说明 |
|------|------|------|
| `OSA_AUDIT_PATH` | 见 `os_agent/config.py` | 审计日志路径 |
| `OSA_AUDIT_ROTATE_MAX_BYTES` | 50 MB | 审计轮转大小阈值 |
| `OSA_AUDIT_RETENTION_FILES` | 10 | 保留的轮转文件数 |
| `OSA_AUDIT_RETENTION_DAYS` | — | 按天保留 |
| `OSA_REPORT_ROOT` | — | 报告写入白名单根目录（拒绝路径穿越） |

## 集群与熔断

| 变量 | 说明 |
|------|------|
| `OSA_AGENT_ID` | 集群内节点名 |
| `OSA_CLUSTER_DIR` | 集群状态目录重定位；stub 模式跳过集群接线 |
| `OSA_BREAKER_*` | MLX 熔断器旋钮（半开探测等，全部 env 可调）；DOWN 状态的 mlx 由熔断器短路 |

## 端口

| 端口 | 用途 |
|------|------|
| 11571 | `serve-metrics` HTTP `/metrics`（默认；issue #6 由 9100 迁移） |
| 11434 | fusion-mlx 推理（上游） |

端口登记见工作区 `architecture/port-registry.yaml`。
