# 国内采集机（cn-collector）

本目录是国内采集机 `collector-cn` 的完整仓库（v1.3 契约）。服务端主干在 `v2` 分支；
服务端 README 的合并由服务端负责，此文件只作本目录说明。

## 运行环境
- 主机：`ND-PC-WIN`（Windows，无公网），采集机代码根 = 本目录
- Python：`C:\Python314\python.exe`（stdlib only，零依赖）

## 计划任务（5 个）
| 任务 | 周期 | VBS 参数 | topic |
|---|---|---|---|
| `collector_offer_10m` | 每 1 小时（2026-09-17 降频，原 10 分钟） | `offer` | 7 快 topic |
| `collector_0930/1530/2130/2330` | 每日 4 档 | `night` | 8 topic（含 `jc_odds_history`） |

全部走 `wscript scripts/collector_silent.vbs <mode>`（S4U 无窗口）；单例锁
（`.collector.lock` + `OpenProcess` 探活）保证并发 no-op。

## 远端
- `ssh oracle` → `ubuntu@140.83.62.161`（jump 配置见采集机 `~/.ssh/config`）
- 落港目录：`/srv/league-staging/incoming/cn-collector/`（topic 子目录 + `.done` 清单）
- `.done` 行：`topic/<file>.jsonl\t<rowcount>\t<sha256>`（相对路径必带 `topic/` 前缀）

## 节奏（v1.4，2026-09-17）
- 快批 7-topic（**不含** `jc_odds_history`，防僵尸堆积，2026-09-16 死机复盘）：
  - `collector_offer_10m`：2026-09-17 起从每 10 分钟降频为**每 1 小时**（用户要求降密度）
  - 4 个 daily 档 8-topic（0930/1530/2130/2330，含 `jc_odds_history`，单批 ~60-75s，QPS=1.0）
- `jclq_result` 只回传 7 天窗口（v1.2 起）

详见仓库根 `README.md` 与 `docs/project/`。
