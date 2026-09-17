# League Predict

联赛预测引擎。多数据源融合 + 信号模型 + ELO + Dixon-Coles 双变量泊松 + 蒙特卡洛模拟。

推理内核零外部依赖（纯 stdlib）。**v2 起 `scripts/store/`、`scripts/ingest/` 另需 `psycopg[binary]`（唯一新增依赖，只用于落库/取数，不进预测链路）**。统一运行在 FastAPI Cloud（见「运行与部署」）。GitHub Actions 已于 2026-09-11 下线。

## v2 进行中：全玩法覆盖（分支 `v2`，2026-09-15 起）

目标：从"五大联赛预测引擎"升级为覆盖**竞彩足球全部玩法 + 传统足彩 + 篮彩**的引擎（数字彩只做开奖对照，不预测）。

```
新增分层（v1 的 core/model/web 一律冻结，只 import 不改）
  scripts/derive/   纯函数派生层：Dixon-Coles 9×9 矩阵 → 各玩法概率（had/hhad/crs/ttg/jqc；haf 未注册即 KeyError）
  scripts/store/    落库层：官方赛果 upsert、raw→fact 解析（parse_api / align / upsert_*）、只读查询接口
  scripts/ingest/   取数层：国内采集机 JSONL 拉取（逐文件事务 + 归档）、API 回填（配额账本 + 限速 + 幂等落块）
  scripts/market/   （下一单）去水与 CLV/Brier 校准指标
DB：PostgreSQL 18 单实例多 schema  ref / stg / ops / raw / fact（+ model / analysis 待建），三角色最小权限
     详见 docs/db/infra_p0_*.sql 与 docs/infra/README-infra.md（若未入库则在队长工作区）
测试基线：python -m pytest tests -q → 305 passed（v1 起点 215；新增均为 store/ingest/derive 的常驻用例）
```

两条已推翻转 v1 文档的实测结论（都带取证）：
1. **API-Football 免费档支持 `?league=&season=` 批量返回**（`scripts/core/data/fetch.py` 里的旧注释"免费计划不支持 season 过滤"已被实测推翻）
   ⇒ 五联赛×三赛季一次回填 **15 次请求**即完成：**5341 场完赛，`score.halftime` 缺失 0 场** ⇒ 半全场玩法的数据前提成立。
   另注意免费档除 100 次/天外还有 **10 次/分钟**（连发第 11 条起 429），两源统一 7s 限速。
2. **football-data 免费档 `season=2022` 直接 403**（只给近两季）⇒ 2022 赛季只有 API-Football 单源，多源交叉核对只能在 2023/2024 生效。

## 架构

```
数据源                             特征                     模型                      输出
────                               ────                     ────                      ────
API-Football (含赔率)               赔率去水 (remove_vig)       Onside 4+1 信号加权        JSON (stdout)
football-data.org (历史)          → form/record 评分       → ELO 场级更新            → stderr 人类摘要
ESPN (无 key 降级)                  盘口移动量化                Dixon-Coles 双变量泊松
                                    ELO 期望得分             蒙特卡洛 10k 次模拟
                                    26 维 ML 特征向量 (实验性)
```

## 数据源对比

| 源 | 赔率 | 覆盖 | 免费额度 | 默认联赛 |
|---|------|------|---------|---------|
| **API-Football** | 33 家博彩公司, 1X2/盘口/大小球 | 全联赛, 最近 3 天 | 100 次/天 | EPL, La Liga, Bundesliga, Serie A, Ligue 1 |
| **football-data.org** | 无 | 全联赛, 全历史 | 10 req/min | 备用 |
| **ESPN** | 有限 (DraftKings) | MLS, 中超, 国际赛 | 无限制 | 降级回退 |

## 预测模型

### 信号融合公式

```
home_strength = market_home × 20% + onside_home × ~70% + elo_home × 18% + spread_movement × 0.5
away_strength = market_away × 20% + onside_away × ~70% + elo_away × 18% - spread_movement × 0.5
draw_strength = market_draw × 20% + draw_base

P(home) = home_strength / sum
```

### ELO 评分

- K=20, 主场加成 100, 净胜球自适应 K 值
- `expected_score` 使用标准公式: `1 / (1 + 10^((elo_b - elo_a_home) / 400))`
- 场级即时更新，持久化至 `references/.elo_ratings.json`

### 比分预测 (Dixon-Coles)

- λ_home = raw_home_strength × league_multiplier (联赛差异化，非统一 2.8)
- λ_away = raw_away_strength × league_multiplier
- ρ 按联赛差异化: EPL=0.17, Serie A=0.28, Bundesliga=0.19 等
- τ 校正含 `max(0, tau)` 钳位，防止负概率
- 三分搜索 + 网格兜底拟合
- 遍历 0-8 球联合概率, 输出 top-3 + 95% CI

### 校准

- 历史累积分布修正
- Onside 信号使用专用 `onside_home_correction` / `onside_away_correction` 减半因子
- 指数平滑持久化

### 蒙特卡洛

- 逐场 Poisson 采样, 10k 次完整赛季模拟
- 淘汰赛: 标准 World Cup 对阵表 (A1vB2, C1vD2, ...)
- 收敛诊断: std_error, 95% CI

### ML 特征工程 (实验性)

> ⚠️ 此模块为实验性功能，特征集和接口可能在版本间变更。

26 维特征向量已就绪, 可直接用于 XGBoost / sklearn 训练:

| 类别 | 特征数 | 包含 |
|------|--------|------|
| 赔率特征 | 4 | 去水概率, 赔率可用性 |
| 球队状态 | 4 | form score, record score |
| 市场信号 | 3 | spread movement, ML implied |
| 信号模型 | 4 | Onside score, FIFA score |
| ELO 特征 | 4 | expected, rating, diff |
| 交叉特征 | 6 | form/record 差积, odds/elo 差 |
| 主场 | 1 | is_host_country |

```python
from core.model.features import extract_features, build_training_set, FEATURE_COLUMNS

# 单场特征
vec = extract_features(match, {"elo_ratings": elo})

# 训练集
X, y = build_training_set(past_matches, {"elo_ratings": elo})
```

## 体彩 Dashboard（`/dashboard/jc/`）

当前体彩 Dashboard 保留原门户 `/dashboard/`，独立挂载于：

```text
https://140.83.62.161/dashboard/jc/
```

当前能力：

- 今日推荐、冠军概率、历史预测与数据源任务状态；
- 串关工作台：选择赛事、结构化市场赔率可用时计算组合参考赔率；
- 结构化 1X2 市场的隐含概率、比例去水概率、Edge 与 EV 展示；缺少完整市场数据时显示不可用，不使用置信度伪造赔率或价值；
- 历史页展示来源已有的命中率、Brier、Log Loss、Hit Rate 与校准摘要；没有数据时不显示为 0；
- 本机投注记录：使用浏览器 `localStorage` 保存手工输入的本金、赔率和结算状态，可计算已结算记录 ROI；不上传服务器、不代表真实赛果或平台账单；
- 推荐卡片收藏与本地持久化；
- 明确区分模型预测、市场数据、人工投注记录和实际赛果。

主要只读接口（均需要应用登录 session）：

```text
GET /api/v1/predictions/today
GET /api/v1/predictions/{YYYY-MM-DD}
GET /api/v1/championship
GET /api/v1/accuracy
GET /api/v1/accuracy/breakdown
GET /api/v1/calibration
GET /api/v1/history
GET /api/v1/backtest
GET /api/v1/prediction-metadata
GET /api/v1/ai/daily
GET /api/v1/ai/ranking
GET /api/v1/sources/status
```

竞彩盘口与彩票开奖（同一 session，`/api/jc/*` 直连 PostgreSQL）：

```text
GET /api/jc/fixtures        每场 5 行（had/hhad/crs/ttg/haf），盘口取该 (场,玩法) 最新一版
GET /api/jc/issues          传统足彩期次 + 开奖（胜负游戏 90 / 任选9场 900129 / 4场进球 94 / 6场半全场 98）
GET /api/jc/backtest        各玩法 Brier / log-loss / argmax 命中率
GET /api/jc/ops             联赛对齐度、采集主题到达情况、配额与拒收
GET /api/jc/lottery         各彩种最近 N 期开奖（超级大乐透 85 / 排列3 35 / 排列5 350133 / 7星彩 04）
```

`/api/jc/lottery` 的彩种清单来自采集端 `lottery_draw` 主题，解析层无白名单——采集端补采新彩种后自动带出，无需改代码。`per_type` 钳制 1..100，越界返回 400。

时区约定（重要）：`fixtures.kickoff_bj`、`issues.sale_begin/sale_end/draw_at` 是 `timestamp without time zone`，存的是**北京时间墙钟**，原样显示，**不得**再做 `at time zone` 或 +8 小时转换；对比 `fixtures.snap_ts`、`ops.topics.latest_arrival` 是真正的 UTC（带 `Z`）。`lottery.draw_date` 是 `date` 类型，不涉及时区。

数据限制：当前预测摘要本身不携带稳定的 API-Football fixture/team ID，因此 Dashboard 不把外部伤停、首发或 H2H 猜测拼接到比赛上。API-Football EPL enrichment 客户端已完成真实接口验证，但默认由 `API_FOOTBALL_ENRICH_ENABLED=0` 关闭；启用后仍须先完成 fixture/team ID 保留与唯一映射。预测概率校准分桶当前不可用；校准摘要是实际赛果分布/修正信息，不等同于可靠性曲线或 ECE。

部署与验证详情见 [`docs/web/DASHBOARD_DEPLOY.md`](docs/web/DASHBOARD_DEPLOY.md)。

## 运行与部署

主应用部署说明沿用 **FastAPI Cloud**；体彩 Dashboard 是部署机上的独立 Nginx + systemd 入口，两者不是同一公网路由。Dashboard 的本机部署、`/dashboard/jc/` 前缀代理和应用 session 说明见 [`docs/web/DASHBOARD_DEPLOY.md`](docs/web/DASHBOARD_DEPLOY.md)。

| 项 | 值 |
|---|---|
| 线上地址 | https://league-predict.fastapicloud.dev |
| 平台 | FastAPI Cloud Hobby（scale-to-zero，免费档） |
| App ID | `b94a43da-be9f-47da-bfde-18188c489dad` |
| 看板鉴权 | 用户名 `admin`（口令见运维机 `deploy/runtime.env`，**不入库**） |

**部署链路**（运维机 `/root/projects/league-predict`）：
1. `deploy/runtime.env`（gitignore）存凭据：`AUTH_*`、`API_FOOTBALL_KEY`、`FOOTBALL_DATA_API_KEY`、`LLM_API_KEY/BASE/MODEL`。本机 systemd Dashboard 当前使用 `/home/ubuntu/league-v2/repo/.env`，并通过 `EnvironmentFile` 注入；不要提交该文件。
2. `bash /root/build_league_web.sh` 组装 staging 到 `/root/build/league-web`（拷 web/static/scripts/ai/… + 写 pyproject `[tool.fastapi] entrypoint="web.api:app"` + 顶层 `main.py` 引导 + `.env`→`config.env` + 注入 `web/__init__.py` load_dotenv）。
3. 用部署 token 免登录发布：`FASTAPI_CLOUD_TOKEN=<deploy token> FASTAPI_CLOUD_APP_ID=<id> fastapi cloud deploy /root/build/league-web`。
4. 核验：`GET https://api.fastapicloud.com/api/v1/apps/<id>` → `latest_deployment.status == success`。

**运行时预测/富化**（在 Web 界面触发，或 API）：
- `POST /api/v1/jobs/predict`（选联赛/数据源/蒙特卡洛）跑 `scripts/predict.py`。**不传参数时只跑英超**（`--league` 默认值 `epl`）；要跑全部 5 个足球联赛须显式传 `{"all": true}`，或传 `{"league": "laliga"}` 等指定单个联赛。
- `POST /api/v1/jobs/predict-bball` 跑 `scripts/bball/run.py`（NBA，独立入口）。参数白名单 `ahead_days`(1..180)/`backtest`(1..60)。需要 `ODDS_API_KEY` **和** `NBA_API_KEY` 同时设置成同一个值——两个名字并存是历史遗留：config 层 `SourcePolicy` 的守卫变量是 `NBA_API_KEY`，而 v1 冻结代码 `scripts/bball/run.py` 实际读的是 `ODDS_API_KEY`，只设一个会出现「守卫说可用、脚本说没 key」的不一致。另需 `LEAGUE_SOURCE_ODDS_API=on`（收费源默认关闭，见 D9）。NBA 休赛期默认 `ahead_days=1` 会得 0 场，需放宽窗口。
- `POST /api/v1/jobs/ai-enrich` 跑 `python -m web.enrich` 生成中文 AI 摘要（LLM 走 agnes-ai，OpenAI 兼容）。
- 每日配额共享计数：`predict` / `predict-bball` / `ai-enrich` 共用同一计数器与并发守卫，同时只允许一个预测类任务运行（并发 409）。容器 scale-to-zero，结果 JSON 不跨冷启持久（冷启回退 git 种子）。

**落盘文件名（2026-09-18 修复）**：预测文件为 `prediction_<YYYY-MM-DD_HH>_<league>.json`，联赛后缀不可省。时间戳只有小时精度，而 `--all` 会在同一次运行里依次跑完全部联赛、篮球与足球又共用同一 `PREDICTIONS_DIR`——不带后缀时同小时的多次运行会写同一个文件、互相覆盖，只剩最后一个联赛（实测 5 个联赛 22 场被静默覆盖）。归并侧 `store.latest_by_league()` 读 JSON 里的 `league` 字段、不解析文件名。
- API-Football EPL（league id `39`）实测：2024 赛季返回 380 场，伤停接口返回数据，`/fixtures/lineups` 返回 2 队阵容；免费档不支持 2025 赛季和 H2H 的 `last` 参数，客户端需省略该参数。免费额度为每日 100 次、每分钟 10 次，客户端有缓存与配额保护。

> 关键约束（实测）：平台部署链会**静默丢弃 dotfile `.env`**，故凭据经 `config.env` 上传并在 `web/__init__.py` 用 `load_dotenv(override=True)` 注入；runtime 日志/环境变量接口需 user token（deploy token 只够发布 + 读构建日志）。

## 快速开始

```bash
# 环境变量 (API-Football 用于赔率, football-data.org 备用)
# 方式一：写入 .env 文件（推荐，自动加载）
echo 'API_FOOTBALL_KEY=your_key' > .env
# 方式二：export 临时
export API_FOOTBALL_KEY=your_key
export FOOTBALL_DATA_API_KEY=your_key
export ODDS_API_KEY=your_…  # NBA（The Odds API，注册 the-odds-api.com）

# NBA 前瞻（休赛期 --ahead-days 90 拉揭幕赛程；历史回测 --backtest N）
python3 scripts/bball/run.py --ahead-days 90

# 预测 EPL (默认 API-Football, 含赔率)
python3 scripts/predict.py --league epl

# 使用 football-data.org (历史数据)
python3 scripts/predict.py --league epl --data-source football-data

# 蒙特卡洛冠军模拟
python3 scripts/predict.py --league epl --monte-carlo

# 指定日期范围
python3 scripts/predict.py --league epl --dates 20250101-20250131

# 回测
python3 scripts/predict.py --league epl --backtest

# 用历史数据训练各联赛 ML 模型（落盘 references/ml_model_{league}.json）
python3 scripts/predict.py --train-ml

# 本次运行禁用 ML 融合
python3 scripts/predict.py --league epl --no-ml
```

## 文件结构

```
scripts/
├── predict.py                 # CLI 入口 (run_league 拆分为 6 个子函数)
├── core/
│   ├── predictor.py           # 预测计算入口
│   ├── elo.py                 # ELO 评分系统
│   ├── config.py              # re-export (向后兼容)
│   ├── constants.py           # 路径/URL/重试/模型参数/权重/阈值/映射
│   ├── leagues.py             # 联赛配置 + DC ρ + λ 乘数
│   ├── cache.py               # 文件缓存 (TTL 过期 + 键生成 + 清理)
│   ├── log.py                 # 日志 (支持 LEAGUE_PREDICT_LOG_LEVEL)
│   ├── data/
│   │   ├── fetch.py           # API-Football / football-data / ESPN 并行聚合
│   │   ├── parse.py           # 赔率解析 + 去水 + 特征提取
│   │   └── convert.py         # API-Football → ESPN 格式 (含赔率)
│   ├── model/
│   │   ├── onside.py          # Onside 4 信号 (FIFA排名/联赛足迹/主场/足联)
│   │   ├── poisson.py         # Poisson / Dixon-Coles (τ 钳位)
│   │   ├── monte_carlo.py     # 蒙特卡洛 10k 模拟 (标准淘汰赛对阵)
│   │   └── features.py          # 26 维 ML 特征工程 (生产使用)
│   ├── calibration.py         # 自动校准
│   ├── backtest.py            # 回测 + 复盘
│   ├── rankings.py            # FIFA 排名统一入口
│   └── output.py              # 输出 / 文件清理 (合并策略)
├── predictions/               # 预测结果 JSON (自动生成)
├── results/                   # 实际赛果 JSON (自动生成)
└── references/                # 排名 / 文档 / 趋势
```

## 支持的联赛

| 键 | 联赛 | 默认数据源 | API-Football ID | DC ρ | λ 乘数 |
|----|------|-----------|----------------|------|--------|
| epl | English Premier League | football-data | 39 | 0.17 | 2.8 |
| laliga | La Liga | football-data | 140 | 0.22 | 2.7 |
| bundesliga | Bundesliga | football-data | 78 | 0.19 | 3.2 |
| seriea | Serie A | football-data | 135 | 0.28 | 2.5 |
| ligue1 | Ligue 1 | football-data | 61 | 0.21 | 2.7 |

## Dashboard AI 日报与分数榜

Dashboard 的 AI 日报由当日预测与已有 `ai_scores.json` 确定性聚合生成，只复用已有预测字段、`ai_summary` 和 `ai_notes`，不生成新闻、赛果或收益结论。AI 分数榜仅按有限数值 `ai_score` 排序，明确标注“非投注热度”；预测与 AI 记录必须通过比赛名称和联赛 exact match 关联，未关联项不补分。

新增鉴权接口：`GET /api/v1/ai/daily`、`GET /api/v1/ai/ranking`。缺少或损坏 AI 文件时返回降级状态，不影响预测主流程。

## Dashboard 推荐详情与数据边界

推荐卡片提供“详情”展开，展示 API 已返回的预测字段：推荐方向、预测比分、大小球、双方进球、模型概率、Poisson Top 3、λ 及置信区间、推理因素和市场溯源。伤停、首发/阵容、历史交锋在当前数据链路中没有可靠字段，页面明确显示不可用，不从历史比赛或 AI 文案推断。

## 技术栈

- **零外部依赖**: urllib + json + gzip + math + pathlib (全 stdlib)
- **赔率处理**: 十进制 → 美式 → 三向去水 (比例法)
- **去水方法**: `p_home / (p_home + p_draw + p_away)`
- **ELO**: K=20, 主场加成 100, 净胜球自适应 K 值
- **Dixon-Coles**: 联赛差异化 ρ, τ 钳位防负概率, 三分搜索优化
- **蒙特卡洛**: 逐场 Poisson 采样, 10k 次完整赛季模拟, 标准 World Cup 淘汰赛对阵
- **校准**: 历史累积分布修正, onside 信号专用减半因子
- **ML 融合**: 零依赖多分类器（softmax 逻辑回归）从 26 维特征产出 H/D/A 概率，与主模型按权重融合（默认开启，可用 `--no-ml` 关闭）；模型按联赛用历史数据训练（`--train-ml`）
- **缓存**: 文件级 TTL 缓存, 过期清理, URL 键生成
- **并行获取**: API-Football + ESPN fallback 并行请求
- **API 校验**: 响应结构验证 + 速率限制追踪

## v2 (2026-09-15) 变更日志
- 新增 `scripts/derive/`（网格与五玩法派生，纯 stdlib）、`scripts/store/`、`scripts/ingest/`（落库与取数）、7 个常驻测试文件
- 新增 DB 底座：`league` 库 + `ref/stg/ops/raw/fact` 五 schema + `league_ing/app/ro` 三角色最小权限 + `ts_snap/ts_stg` 表空间
- 修正文档：`fetch.py` 关于"免费档不支持 season 过滤"的注释已失效（内核冻结未改，本 README 与 v2 段为准）

## v4.3 变更日志

### 变更 (P5)
- **移除 MLS 预测**: 从 `LEAGUE_CONFIG` 及 ρ/λ 映射中移除美职联，`--all` 不再运行
- **中超队名中文化**: `COUNTRY_CN` 新增 15 支中超俱乐部中文映射（青岛海牛、上海申花、浙江队、成都蓉城等）
- **GHA 触发时间**: 调整为每日 21:33 (BJT)

## v4.2 变更日志

### 修复 (P0)
- **ESPN 403 修复**: ESPN 封锁 `Mozilla/5.0` 与 `LeaguePredict/4.1` 等 User-Agent，改用 `python-requests/2.31`，恢复 ESPN 数据源可用性（MLS/中超降级路径）

### 增强 (P5)
- **.env 自动加载**: 支持在项目根目录 `.env` 写入 `API_FOOTBALL_KEY` / `FOOTBALL_DATA_API_KEY`，启动自动加载，免手动 export

## v4.1 变更日志

### 致命修复 (P0)
- **ELO 公式符号**: `expected_score` 指数项 `(effective_a - elo_b)` → `(elo_b - effective_a)`, 修复强队系统性低估
- **Dixon-Coles 负概率**: `tau_correction` 加 `max(0, tau)` 钳位, 防止 1-1 比分时 ρ×λ_h×λ_a 超限
- **Calibration 双重修正**: Onside 信号使用专用 `onside_home_correction` 减半因子, 不再与 market odds 重复修正

### 高优先级 (P1)
- 测试 mock 字段名统一 (`home_prob` → `home_true_prob`)
- 移除 MyMemory 翻译 API (COUNTRY_CN 字典已覆盖)
- Backtest 移除硬编码数据源检查, 新增 `_backtest_api_football()`
- Monte Carlo 标准世界杯淘汰赛对阵表 (A1vB2 模式)
- Fetch API 响应结构校验 (`_validate_api_response`)

### 中优先级 (P2)
- `ELO_WEIGHT` 移入 `THRESHOLDS` 配置化
- FIFA 排名获取统一委托 `rankings.py`
- `COUNTRY_CONFEDERATION` 重复键清理
- API-Football 速率限制追踪 (`_rate_limit_info`)
- 日志级别环境变量支持 (`LEAGUE_PREDICT_LOG_LEVEL`)

### 低优先级 (P3)
- `__import__` 改顶部 import
- 线程安全问题随 MyMemory API 移除消除
- ELO 测试覆盖 20 个用例
- `save_results` 改合并策略

### 重构 (P4)
- P-label 注释清理
- `run_league` 拆分为 6 个子函数
- `config.py` 拆分为 `constants.py` + `leagues.py` + `config.py` (re-export)

### 增强 (P5)
- ML 特征管线标记为实验性
- Cache 补全 TTL 过期清理 + URL 键生成 + `purge_expired()`
- API-Football + ESPN fallback 并行获取 (`ThreadPoolExecutor`)
- JSON→SQLite/Parquet 评估: 数据量 ~95KB, 无迁移必要

## 国内采集机（cn-collector，v1.3 契约）

本仓库 `collector-cn/` 子目录是 `collector-cn` 分支的全量内容（v1.3 契约采集机）。
完整说明见 `collector-cn/README.md`（运行环境、计划任务、远端通道、节奏、契约、红线）。

- 代码根 = 本仓库根目录（`collector.py` 等），Windows 机器 `ND-PC-WIN` 直接 checkout 本分支
- 计划任务 5 个：`collector_offer_10m`（2026-09-17 起**每 1 小时**，原 10 分钟）+ 4 个 daily 档（0930/1530/2130/2330，8 topic 含 `jc_odds_history`）
- 落港：`ssh oracle`（`ubuntu@140.83.62.161`）→ `/srv/league-staging/incoming/cn-collector/`（topic 子目录 + `.done` 清单）
- `.done` 行格式：`topic/<file>.jsonl\t<rowcount>\t<sha256>`（相对路径必带 `topic/` 前缀）
- 契约版本：v1.3（2026-09-16 升级：`topic/` 前缀修复 + `jc_odds_history` topic + 8-topic daily 批）
- 服务端篮彩赛果链路已接通（2026-09-17）：`jclq_result` → `parse_jclq_result` → `fact.jbq_result`；`jclq_offer` 仍因契约未冻结而保持不解析。
- 最近验收基线：全量测试 **515 passed**（2026-09-18 追加 NBA 链路与彩票端点后；2026-09-17 完整验收为 502 passed，详见 `docs/acceptance-2026-09-17.md`）。
- 代码质量审核（2026-09-17）：硬门禁 0 违规；已按审核修复 savepoint 分支重复、错误边界、`pk` 白名单校验与动态 SQL 标识符安全。
- 项目整理：运行时日志目录 `logs/` 已加入忽略规则；预测/结果/output 等生成物继续不入库，保留已有历史样本与文档记录。

## 完整验收（2026-09-17）

完整报告见 `docs/acceptance-2026-09-17.md`。以下为需要长期记住的环境事实：

**时区口径（易踩）**：`fact.jc_match.kickoff_bj`、`fact.jc_issue.sale_begin|sale_end|draw_at`、
`fact.jc_offer.odds_update` 都是 `timestamp without time zone` 且**列里存的已是北京时间**，
只能直接 `to_char`，**绝不能**再写 `at time zone 'Asia/Shanghai'`（会按会话时区 `Etc/UTC`
渲染成早 8 小时）。对照：`jc_offer.snap_ts`、`jc_odds_history.update_ts` 是 `timestamptz`（真 UTC）。
前端 `static/jc.html` 只对**带时区标识**（`Z` / `±HH:MM`）的字符串做 +8h 换算，naive 时间戳原样显示。

**AI 富化**：`ai/feedback_loop.save_ai_scores` 只落盘**带 `ai_score`** 的条目——LLM 失败时
`analyse_batch` 会原样返回未评分条目（有意契约），若在此兜底成 50 分会把"完全没分析"
伪造成"中性 50 分"，并经 `adjust_prediction` 的 `0.7+0.3*50/100=0.85` 静默削减 15% 信心。
`league` 必须回退到条目自带的 `league`，否则 AI 日报命中数结构性永久为 0。
全部条目未评分时 `web/enrich.py` 返回非 0，任务记为 `failed`。

**配额**：`web/.data/quota.json` 是**预测触发预算**（`PREDICT_DAILY_LIMIT`，BJT 日界），
**不是数据源配额**；后者（API-Football 100/天 UTC 日界、football-data 30/天）目前无 API 出口。
`_spawn` 采用"先检查后扣减"两段式，被 409 拒绝的请求不扣配额。

**已修复（2026-09-18）**：`LLM_API_KEY` 已更新并实测可用（`HTTP 200`，模型 `agnes-3.0-flash` 正常回话）；真实富化 46 条 → 41 条落盘，`ai_scores.json` 92 → 133 条，`/api/v1/ai/daily` 的 `ai_matched_count` 由**结构性永久 0** 变为真实计数（此前 league 字段丢失导致永不匹配）。

**已知未修**：`static/jc.html`（竞彩看板，上一代）在生产 nginx 上**无路由**；其消费的 `/api/jc/*` 竞彩盘口数据（5 玩法 × 场次）此前无页面展示。新 `static/dashboard.html` 已并入 fixtures/issues/backtest/ops 四个视图与彩票开奖视图，`jc.html` 与 `index.html` 可视为被取代的上一代页面。
