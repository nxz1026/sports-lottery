---
name: league-predict
description: 联赛预测引擎 — 五联赛（英超/西甲/德甲/意甲/法甲）赔率加权预测
version: 4.1.0
category: sports-prediction
tags: [football, prediction, odds, poisson, elo]
---

> 版本说明：SKILL.md 的 `version` 与 `scripts/core/__init__.py::__version__` 必须保持一致。
> README 中的 "v2" 是 DB / 落库分层架构的分支名（覆盖全玩法），**不是引擎版本号**。

# League Predict — 联赛预测引擎

## 功能

每日预测英超、西甲、德甲、意甲、法甲未来 24h 比赛，输出：
- 方向预测（主/平/客）+ 星级置信度
- 最可能比分（泊松分布 TOP3）
- xG 区间
- 大小球 / 双方进球

## 数据源优先级

| 优先级 | 数据源 | 覆盖范围 | 说明 |
|--------|--------|----------|------|
| 1 | football-data.org | 5 大联赛 + 欧冠 | 免费，含赔率 |
| 2 | API-Football | 全联赛 | 备用 |
| 3 | ESPN | 降级兜底 | 无赔率 |

## 运行

```bash
# 预测今日比赛
python3 scripts/predict.py --league epl

# 预测所有联赛
python3 scripts/predict.py --all

# 指定日期
python3 scripts/predict.py --dates 20260809-20260810
```

## 环境变量

```bash
API_FOOTBALL_KEY=xxx        # API-Football API key
FOOTBALL_DATA_API_KEY=xxx   # football-data.org API key（必需）
```

## 权重配置

- 市场赔率权重: 45%（`MARKET_ODDS_WEIGHT`）
- Onside 信号权重: ~45%
- ELO 权重: 18%
