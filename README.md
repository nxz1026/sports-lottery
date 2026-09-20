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
测试基线：python -m pytest tests -q → **660 passed**（v2 起点 215；新增 store/ingest/derive 常驻用例 + ESPN 富化 34 例 + 幽灵守卫 7 例 + 跨文件结算 5 例）
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

### 分层基线（四层，接口基线）

系统按 **业务层 / 接口层 / 算法层 / LLM层** 四层组织，各层职责与依赖方向如下（这是接口基线的开发约束，改动任何一层不得破坏依赖方向）：

```
┌────────────────────────────────────────────────────────┐
│ 业务层                                 │  细分见「应用层机制」
│  ├ 应用层 web/app/（编排 → 产出 VM）   │  调接口层组列/判空/汇总
│  └ 展示层 static/*.html（纯静态渲染）  │  拿 VM 直接画，不做判断
├────────────────────────────────────────────────────────┤
│ 接口层  web/services/*                                │  唯一数据编排入口
│   ├ store.py        读算法层产物 → 结构化数据          │
│   ├ team_match.py   盘口×预测合并（纯函数，单测覆盖）   │
│   ├ jobs.py         引擎子进程调度 + 配额守卫           │
│   └ datasource.py   上游源静态配置（无真实值、无请求）   │
│   ══ 契约 JSON ── 业务层消费的唯一数据 schema（隐式）══  │
├────────────────────────────────────────────────────────┤
│ 算法层  scripts/*（核心引擎）                         │  只产 JSON 产物，不碰展示
│   ├ core/   足球引擎（Onside/ELO/Dixon-Coles/MC/ML）    │
│   ├ bball/  NBA 引擎（ELO + The Odds 赔率）             │
│   ├ derive/ store/ ingest/ market/（纯函数/落库/取数）   │
│   └ CLI 子进程 → 产物 predictions/*.json               │
├────────────────────────────────────────────────────────┤
│ LLM层   scripts/ai/*  （异步、独立于业务）             │  见下「LLM 异步契约」
└────────────────────────────────────────────────────────┘
```

**依赖方向（只许向上，不许下行）：**

| 依赖关系 | 规则 |
|---------|------|
| 业务层 → 接口层 | 业务只调接口层 services，拿编排好的数据；**不死写第二套编排** |
| 接口层 → 算法层 | 接口层**只读算法层持久化产物**（`predictions/*.json`、`ai_scores.json` 等），**绝不 import `scripts/` 与 `ai/`**（契约 §5）；算法经 CLI 子进程异步跑 |
| 算法层 → 数据源 | 算法层只产 JSON 产物，不落 web、不碰展示；数据层已按「数据源对比」配置 |
| LLM 层 | 完全独立于业务，见下 |

**LLM 异步契约（关键纪律）：**
- LLM 调用是**异步**的，经 job 子进程 `python -m web.enrich` 触发，**业务请求链路永不直接同步等 LLM**。
- 展示层只读**已完成的 AI 产物**（`ai_scores.json`）；没有就显示「AI 未评分」，**绝不阻塞主请求**。
- LLM 失败**降级**：`enrich` 返回非 0 并响亮报错，不影响 predictions/展示可用。
- 语义上 LLM 层独立于业务；代码实体在 `scripts/ai/`（算法层目录），但**作为独立抽象对待**，web 侧只经 `enrich.py` 调用、不直接接触 LLM。

**接口基线的三个给定事实（不得违反）：**
1. web 层绝不 import `scripts/` 与 `ai/`（见 `web/services/datasource.py` docstring 与契约 §5）。
2. **应用层编排**在 `web/app/`（见下「应用层机制」），接口层 `web/services/` 只做底层数据读取/单字段合并，写成纯函数 + 单测（`team_match.build_rows` 是样板）。
3. 契约 JSON 目前是**隐式**的（接口层返回 dict、前端自行理解字段）；业务层图片与 web 展示**共同消费同一份接口层数据**，避免业务层重复编排。

### 应用层机制（后端 VM 编排层）

应用层 = **应用(编排) + 展示(静态)** 两层，均属 README 四层中的「业务层」，进一步细分为：

```
├─ 应用层(编排) web/app/            ← 新：每屏一个编排函数
│    调接口层(store/team_match) → 组列/判空/算汇总 → 产出 View Model (VM)
├─ 展示层(静态) static/*.html         ← 纯渲染：拿到 VM 直接画
│    只保留 jcTable/jcVal/bjTime 等纯渲染函数，删除列定义/字段选择逻辑
```

**View Model (VM) 约定** —— 每个「屏 / 图 / 表」对应一个 VM，包含：
- `columns`：列定义（字段名 + 表头 + 渲染提示），替代前端 `JC_FIX_COLS`/`JC_ISSUE_COLS`
- `rows`：已判空、已格式化、已算汇总的行数据
- `meta`：页面元信息（日期、数据源、`degraded` 标记等）
- `actions`：该屏动作（如每日一图的下载 / 二维码）

**展示层契约（前端「取结果就完了」）：**
- 前端 `fetch(/api/v1/app/<screen>)` → 直接拿 VM → 纯模板渲染
- 前端**删除**列定义（`JC_FIX_COLS` 等）与 canvas 绘制的字段组装（上移到应用层 VM）
- 前端**保留** `jcTable`/`jcVal`/`bjTime` 等纯渲染函数（「怎么画」，非「画什么」）

**边界铁律（防「又写一套」）：**
- 应用层只调接口层，**不 import 算法层**（延续契约 §5）。
- 展示层只吃 VM，**不做业务判断**（判断 = 编排 = 应用层）。
- 一屏一个编排函数，**可单测**。
- **渐进迁移**：先做一个屏幕做样板（如每日一图 → `/api/v1/app/daily-image` 产出 VM），验证 VM 契约后再推广，不推倒重来。

## 数据源对比

| 源 | 赔率 | 覆盖 | 免费额度 | 默认联赛 |
|---|------|------|---------|---------|
| **API-Football** | 33 家博彩公司, 1X2/盘口/大小球 | 全联赛, 最近 3 天 | 100 次/天 | EPL, La Liga, Bundesliga, Serie A, Ligue 1 |
| **football-data.org** | 无 | 全联赛, 全历史 | 10 req/min | 备用 |
| **ESPN** | 有限 (DraftKings) | MLS, 中超, 国际赛 | 无限制 | 降级回退 |
| **The Odds API** | 博彩公司 h2h/让分/大小分 | NBA（`basketball_nba` active） | 免费 ~500 次/月（header `x-requests-remaining` 可查）| NBA（`the-odds`，付费源默认关闭，见 D9；当前 .env `LEAGUE_SOURCE_ODDS_API=on` 但执行链路未接 allow_paid 闸门）|

### 采集端代码与数据对齐文档（索引）

竞彩官方数据链路分两层，代码均在仓库内：
- **取数（国内采集机）**：`collector-cn/collector.py`（子目录，契约 v1.3，仅 stdlib，不连 DB）——取官方 JSON → 落 JSONL → tar 流式推 oracle（league key-only）。子命令：`--probe` / `--collect <topic>` / `--collect-all` / `--push-batch <topic>...`。
- **入库（本机 oracle，NDORACLE）**：`scripts/ingest/`——`collector_pull.py`（拉包）、`jc_load.py` / `jc_write.py` / `jc_read.py` / `jc_topic.py`（表读写）、`jc_odds_write.py` / `jc_issue_write.py` / `jbq_result_write.py`（玩法/期次/结果写）、`jc_manifest.py`、`run_backfill.py`、`quota.py`；解析层 `scripts/store/parse_collector.py` + `parse_jczq.py`。

数据对齐/契约文档在 `docs/project/`：
- `国内采集机实施文档-v1.md`（契约单一真源 §5：9 键外壳 / snap_ts / src_hash / 官方键名不许清洗）
- `回传-契约v1.1指令.md`、`回传-验收v1.1第1批.md`（第一批验收）
- `探针核对报告-v1.1-20260915.md`（探针对照/核对）
- `契约v1.4-盘口全历史.md`、`回传-盘口历史-v1.md`（盘口历史契约）
- `待审-竞彩球队对照.md`、`待审-seed_jc_team_alias_sporttery.sql`（球队身份/别名对齐）
- DB schema：`docs/db/schema_v2_draft.sql` + `infra_p0_*.sql` 分片

**堵点记录（2026-09-20）**：`fact.jc_issue_match`（14场/任九/4场进球当期对阵）= 0 行，奖期真实票面对阵未入库。

### 玩法覆盖清单（接口基线：5 大联赛 + NBA）

预测服务对竞彩玩法的覆盖现状（作为接口层向业务层暴露的基线——业务层只读这份清单对应的数据，不在业务层重算覆盖）：

**⚽ 竞彩足球（5 大联赛）**

| 玩法 | 代码键 | 覆盖 | 依据字段 |
|------|:---:|:---:|------|
| 胜平负（含单关/过关）| `had` | ✅ 完整 | `direction` + `reasoning_factors.home/draw/away_true_prob` |
| 让球胜平负 | `hhad` | ✅ 一等预测 | 官方 `goalLine` 下用 λh/λa 独立泊松重算让胜/让平/让负三向（`web/services/team_match.poisson_hhad` → 行内 `hhad_model`）|
| 比分 | `crs` | ✅ 完整 | `predicted_score` + `poisson_top3` |
| 总进球数 | `ttg` | ✅ 可推导 | `lambda_home+lambda_away` 泊松推出档位分布 |
| 半全场 | `haf` | ✅ 一等预测 | 全场 `lambda_home/away` ×0.45 拆上半场率，联合遍历九宫格（`web/services/haf.py compute_haf` → 行内 `haf_model` + `/api/v1/haf` 端点）|

**🏀 竞彩篮球（NBA）**

| 玩法 | 覆盖 | 依据字段 |
|------|:---:|------|
| 胜负（单关）| ✅ 完整 | `direction` + `win_prob` |
| 让分胜负 | ✅ 完整 | `spread_prediction` |
| 大小分 | ✅ 完整 | `total_prediction` |
| 胜分差 | ✅ 完整 | `predicted_margin` |

**缺口（待补：奖期 14场/任九 票面对阵未入库；NBA 让分/大小分上游盘口线仅在有赔率时可用）** —— 均不扩大需求，仅记录，待按需展开。半全场 haf 已用全场λ×0.45 独立泊松近似（不含上半场让球，仅全场让球线）。

## 预测模型

### 算法层三段机制（基本算法 + ML + AI）

预测引擎按「**基本算法 + ML + AI**」三段**串联叠加**，每层独立启停、可逐场组合与降级。这是算法层的选取契约（**方向可被 ML/AI 翻转**，见护栏）。

```
① 基本算法（恒开，零依赖通用）
   Onside 4+1 信号 + ELO + Dixon-Coles + 信号融合公式
   输出：三向概率 h/d/a、比分 top3、基础信心、星级
② ML 修正（可选，条件启用）
   26 维特征 → blend_weight=0.15 稀释混合基本概率；强证据可翻转方向
   启用条件：该联赛已训练模型 + 样本数 ≥30 + 产物 references/ml_model_{league}.json 存在
③ AI 修正（可选，条件启用）
   LLM ai_score(0-100) → 信心 ×(0.7+0.3×score/100)；高分可参考方向
   启用条件：该场次在 ai_scores.json 有记录
```

**权责分工（已定：ML/AI 可影响方向）：**

| 层 | 能改 | 不能改 |
|----|------|--------|
| ① 基本 | 初始三向概率 / 比分 top3 / 基础信心 | — |
| ② ML | 三向概率 + **方向**（强证据翻转，见护栏）| 比分 |
| ③ AI | 信心 / 星级 + **方向**（LLM 判断，见护栏）| 比分 |

**组合与逐场降级（已定）：**
- 每场独立布尔：`used_base` / `used_ml` / `used_ai`，产物记录每场实际用了哪几层。
- 允许三种组合并存：`纯基本` / `基本+ML` / `基本+ML+AI`，按数据可用性自动落在某组合。
- **逐场独立降级**：某场缺某层→该场跳过该层，不影响其它场次；数据不足或失败降级到上一层，不阻断。

**方向翻转护栏（已定，防乱翻）：**
- **ML 翻转**：仅当 ML 三向概率**最高项**与基本层最高项**不同**，且 ML 置信度（winner−margin）≥ 阈值（如 0.6）时才翻转；否则以基本层为准。
- **AI 翻转**：仅当 `ai_score` ≥ 80 且基本层处于**无方向/平局模糊态**时才参考 AI 方向；否则 AI 只调信心/星级。

**三段机制落地现状：**
- 基本算法恒开（✅ 当前默认全开）。
- ML 层：训练流水线已就绪（`python3 scripts/predict.py --train-ml` → 落盘
  `scripts/references/ml_model_{league}.json`）。样本 ≥ `min_train_samples`(30) 的联赛启用
  `ml_model_used=True` 并按 `blend_weight`(0.15) 融合；样本不足的联赛如实跳过、回退规则基线。
  当前已训练启用：epl / laliga / seriea / ligue1（bundesliga 历史样本 28<30、nba 休赛期，跳过）。
- AI 层：已接通。enrich job 写回 `predictions/ai_scores.json`（稳定键 `league|home_en|away_en`，
  回退中文名），`predict.py` 经 `ai.feedback_loop.adjust_prediction` 应用
  信心因子 `0.7+0.3×ai_score/100`，预测产物记录 `ai_adjusted` / `ai_score_used` / `ai_adjustment_factor`。

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
- **联赛模式取「整季赛程 + 当前积分榜播种」**（2026-09-18 修复）：
  - 取数走 `fetch_events(..., whole_season=True)`，football-data **不传日期参数**即返回
    本赛季全部比赛（PL 实测 380 场 = 40 已结束 + 340 未开赛，20 队齐全）。
  - 已结束比赛用于 `build_league_standings()` 播种积分榜、`derive_team_strengths()`
    推导每队攻防强度（乘性模型 + 小样本收缩 `n/(n+6)`）；剩余赛程用于模拟。
  - 播种积分榜与 football-data 官方 standings **逐队对账 20/20 完全一致**。
  - ⚠️ 不要用日期区间代替：传区间会**跨赛季**（±300 天拿到 310 场已结束，混入上赛季），
    且区间 > 750 天会被源直接 `400 Specified period must not exceed 750 days` 拒绝。
  - 旧实现只用「预测窗口内的 6 场」当 fixtures，等于拿 6 场球推整个联赛的夺冠概率，
    只能覆盖 12 支球队，语义不成立 —— 这也是冠军页长期为空的原因之一。
- 淘汰赛: 标准 World Cup 对阵表 (A1vB2, C1vD2, ...)
- 收敛诊断: std_error, 95% CI
- `champion_probs` **保留 0 概率球队**，冠军页展示完整参赛队伍（此前英超 20 队只显示 17 队）

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
- **多维可组合筛选**（2026-09-18 新增，纯前端 AND 组合，含命中条数与生效条件回显、一键重置）：
  - 今日推荐：联赛（动态提取，多选）、开球时间（全部/有开球时间/今天/明天/未来3天/时间待定）、
    星级 ≥1★…≥5★、信心分 ≥5%/10%/15%/20%、运动、玩法、关键词多词 AND；
  - 竞彩盘口：联赛（从 `/api/jc/fixtures` 行动态提取，不写死白名单）+ 场次关键词搜索；
  - 彩票开奖：彩种筛选（动态）+ 期数 10/20/50；
  - ⚠️ 缺失值不当 0：`confidence_score` 为 `null` 的行（如 NBA）此前被 `Number(null)===0`
    静默当成有效分 0，已改显式判空；奖池/设备数为 0 显示「未提供」而非 0。
- **NBA 篮球**：与足球同在「今日推荐」，带主队胜率 `win_prob`、让分 `spread_pred`、总分 `total_pred`；顶部可按运动筛选（全部/足球/篮球）；
- **竞彩盘口**（`view-jc`）：赛程盘口（每场 5 玩法）、传统足彩期次、回测基线、运维快照四个子面板；
- **彩票开奖**（`view-lottery`）：超级大乐透 / 排列3 / 排列5 / 7星彩，按彩种分组、期号降序，号码串原样显示；
  - 2026-09-18 新增**号码分组展示列**（大乐透 5+2、7星彩 6+1、排列3/5 平铺），
    **原样列完整保留**；位数与规则不符时给 ⚠ 警告且不补齐，未收录彩种只原样展示不猜；
  - 7星彩按 6+1 而非平铺 7 位：真实数据第 7 位会出现 `11`/`14`（如 26106 = `5 1 9 5 8 5 11`），
    符合官方「前 6 位 0-9 + 特别号 0-14」规则；
  - 每彩种显示统计摘要（本期数/最新期号/最新开奖日期/最早期号/分组规则）；
  - 2026-09-20 新增**数字彩工具**卡片组：① 奖级判定（命中判定、非金额）② 复式成本 ③ 胆拖注数 ④ 描述统计（近 N 期开奖频次/奇偶/热门号）——调 `/api/v1/lottery/*`；
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
GET /api/v1/ai/status
GET /api/v1/ai/analyze?date=YYYY-MM-DD     AI 异步分析结果（逐场解读/胆材叙事/开奖复盘，只读）
POST /api/v1/jobs/ai-analyze               AI 分析异步 job（与 predict 共享配额）
POST /api/v1/combo/eval                    串关 EV（legs 或 selections+match_num 装配）
GET  /api/v1/haf?home=&away=&line=         半全场九宫格一等预测
POST /api/v1/haf                          {home,away,line} 或 {match_num,line}
GET /api/v1/lottery/prize?game&ticket&draw 数字彩命中判定（非金额）
GET /api/v1/lottery/cost?game&front&back   大乐透复式成本
GET /api/v1/lottery/dantuo?dan&tuo&choose  胆拖注数
GET /api/v1/lottery/stats?game&n           数字彩描述统计
GET /api/v1/sources/status
```

竞彩盘口与彩票开奖（同一 session，`/api/jc/*` 直连 PostgreSQL）：

```text
GET /api/jc/fixtures        每场 5 行（had/hhad/crs/ttg/haf），盘口取该 (场,玩法) 最新一版
GET /api/jc/issues          传统足彩期次 + 开奖（胜负游戏 90 / 任选9场 900129 / 4场进球 94 / 6场半全场 98）
GET /api/jc/backtest        各玩法 Brier / log-loss / argmax 命中率
GET /api/jc/ops             联赛对齐度、采集主题到达情况、配额与拒收
GET /api/jc/lottery         各彩种最近 N 期开奖（超级大乐透 85 / 排列3 35 / 排列5 350133 / 7星彩 04）
GET /api/jc/gap              官方 SP 隐含概率 vs 模型概率错位榜（每日一报）
GET /api/jc/movement         两时点盘口快照链（开盘 vs 临场，只列有变动的场）
GET /api/jc/freshness?threshold_hours=24  topic 新鲜度（超阈值标 stale）
```

`/api/jc/lottery` 的彩种清单来自采集端 `lottery_draw` 主题，解析层无白名单——采集端补采新彩种后自动带出，无需改代码。`per_type` 钳制 1..100，越界返回 400。

时区约定（重要）：`fixtures.kickoff_bj`、`issues.sale_begin/sale_end/draw_at` 是 `timestamp without time zone`，存的是**北京时间墙钟**，原样显示，**不得**再做 `at time zone` 或 +8 小时转换；对比 `fixtures.snap_ts`、`ops.topics.latest_arrival` 是真正的 UTC（带 `Z`）。`lottery.draw_date` 是 `date` 类型，不涉及时区。

数据限制：当前预测摘要本身不携带稳定的 API-Football fixture/team ID，因此 Dashboard 不把外部伤停、首发或 H2H 猜测拼接到比赛上。API-Football EPL enrichment 客户端已完成真实接口验证，但默认由 `API_FOOTBALL_ENRICH_ENABLED=0` 关闭；启用后仍须先完成 fixture/team ID 保留与唯一映射。预测概率校准分桶当前不可用；校准摘要是实际赛果分布/修正信息，不等同于可靠性曲线或 ECE。

### 认证与隔离（2026-09-18）

`/dashboard/jc/` 使用**独立应用认证**，与 Nginx 的 Basic Auth **完全隔离**，避免体彩用户用 Nginx 账号登录后进入其它业务：

- **Nginx 层**：`/dashboard/jc/` 三处 location（`= /dashboard/jc/`、`/dashboard/jc/api/`、`= /dashboard/jc/login`）均 `auth_basic off`，从全局 Basic Auth 剥离。Nginx Basic Auth 只保护 `/` `/dashboard/` `/resume/` `/stock/` `/admin/` 等其它业务。
- **体彩应用层**：`web/auth.py::require_auth` 强制校验 `lp_session` cookie（单账号 session）。登录账号来自 `AUTH_USERNAME` / `AUTH_PASSWORD`（在 `.env`，已 gitignore 不入库），签发 `lp_session` cookie，前端遇 `401` 自动跳 `/dashboard/jc/login`。
- **生产验证**：体彩账号登录后只能访问 `/dashboard/jc/`；`/` `/dashboard/` `/resume/` `/admin/` 对体彩用户一律 `401`（仍由 Nginx Basic Auth 保护）。

> ⚠️ 体彩账号目前是单账号（生产当前为 `a`/`a`）。**正式对外前务必在 `.env` 改为强口令**（改后重启 `league-dashboard.service`）；若 8077 直连端口对外暴露，则绕过 Nginx 后只剩应用层单账号保护。

### 多用户方案（规划中，暂未实现）

当前为**单账号** session（`AUTH_USERNAME`/`AUTH_PASSWORD`）。用户明确后续需**多用户**（不同人不同账号）。暂不做，仅记录方案供落地：

**目标**：体彩 Dashboard 支持多个注册用户独立登录、互不串号，且不引入额外 Nginx Basic Auth。

**可选方案（由简到繁）**：

1. **方案 A：内置用户表 + 账号密码登录（推荐）**
   - 新建 `users` 表（schema 建议 `app`），存 `username`（unique）、`password_hash`（argon2id/bcrypt，绝不存明文）、`display_name`、`role`、`created_at`、`enabled`。
   - 会话从"内存/DB 无状态会话"升级为 `session_token → user_id` 关联；`require_auth` 从校验 cookie 令牌升级为解析出 `user_id`。
   - 登录页加"注册"入口（或由管理员预置账号）；登录/登出/改密路由改由 `users` 表驱动。
   - 优点：改动可控、无额外部署依赖、天然支持禁用/角色。
   - 改动点：`web/auth.py`、`web/session_store.py`、登录前端、新增 `users` 迁移 SQL。

2. **方案 B：OAuth2 / 第三方登录**
   - 接 GitHub/微信等 OIDC；用户跳转授权后回调。适合公开对外、不想自维护密码的场景，但需外部账号体系与 redirect_uri 配置，复杂度高于 A。

3. **方案 C：多租户/业务隔离**（若未来体彩按商户各自运营）
   - 在方案 A 之上加 `tenant_id` 维度，预测/跟单数据按租户隔离。工作量最大，建议仅当确有分店/独立运营需求时再做。

**推荐落地顺序**：先方案 A（单用户表 + 强口令 + 角色），验证顺畅后再评估是否需要 B/C。详见 `docs/web/DASHBOARD_DEPLOY.md` 部署约定；本次仅记录规划，不做实现。

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

### 定时运行（systemd timer，2026-09-18 重建）

**本机自动化一共三条，职责不重叠：**

| 机制 | 载体 | 频率 | 干什么 |
|---|---|---|---|
| Dashboard 常驻服务 | `league-dashboard.service`（systemd，`Restart=on-failure`） | 常驻 | 提供 `/dashboard/jc/` 全部 API |
| 竞彩数据采集 | 用户 crontab `*/10 * * * *` | 每 10 分钟 | 跑 `jc-ingest-run.sh` 落库 `fact.jc_*` |
| **每日全量预测** | `league-daily-predict.timer`（systemd） | 每日 **09:00 Asia/Shanghai** | 足球全联赛 + NBA + AI 富化 |

**为什么从进程内 apscheduler 换成 systemd timer**（旧实现实测 0 次成功触发）：

1. **时区差 8 小时**：宿主 systemd 本地时区是 `Etc/UTC`。旧代码
   `BackgroundScheduler(timezone="Asia/Shanghai")` + `CronTrigger(hour=9, minute=0)` 看着对，
   但**显式传入的 CronTrigger 自带时区、会覆盖调度器默认值**，实测落到 `Etc/UTC`
   → `next_run = 09:00:00+00:00`（= 17:00 BJT），与代码注释和 `CRON_HOUR` 文档声称的
   「每日 09:00 BJT」差 8 小时。对照实验：加 `timezone='Asia/Shanghai'` 才是 `09:00+08:00`。
2. **只跑英超**：旧 cron 传 `argv=[]`，而 `predict.py` 的 `--league` 默认值是 `epl`
   → 每日只刷新英超，其余 4 个足球联赛恒不更新（其注释却写着「全联赛」）。
   惰性刷新路径 `/jobs/auto/refresh` 早就显式传了 `["--all"]` 并留了注释，cron 路径被漏掉。
3. **不含 NBA 与 AI 富化**：旧 cron 只触发 `predict`。
4. **服务重启即丢**：进程内调度每次重启都要重算，而本机一天重启 11 次，极易错过触发点。

**新实现**（`ops/` 三个文件，unit 需 `sudo install -m 644` 到 `/etc/systemd/system/`）：

- `ops/league-daily-predict.timer` — `OnCalendar=*-*-* 09:00:00 Asia/Shanghai`（**时区必须显式写**）
  + `Persistent=true`（错过的触发在开机后补跑一次）。
- `ops/league-daily-predict.service` — `Type=oneshot`，`After/Requires=league-dashboard.service`。
- `ops/league-daily-predict.sh` — **走 HTTP API 而不是直调 CLI**：配额守卫
  （`PREDICT_DAILY_LIMIT`，默认 80）与并发守卫（`active_job`）都在 `web/services/jobs.py` 里，
  直调 `scripts/predict.py` 会绕过它们、与页面上的手动任务撞车吃光配额。
  三个作业**共享同一并发守卫**，因此脚本**串行**执行：发一个 → 轮询 `/jobs/{id}` 到终态 → 发下一个。
  作业体：`{"all":true}`（足球 5 联赛）、`{"ahead_days":90}`（NBA，休赛期默认 1 天会得 0 场）、AI 富化。

**观测**：三个作业端点都接受可选的 `trigger` 字段（白名单 `manual`/`timer`/`cron`/`auto`，
白名单外回落 `manual` 且不报 400），timer 传 `timer`，于是在 Dashboard「数据源与任务」页
可与手点的作业区分——定时任务跑没跑可以自证。此前该字段被硬编码成 `manual`。

```bash
# 查下次触发时刻（应显示 01:00 UTC = 09:00 BJT）
systemctl list-timers league-daily-predict.timer

# 手动触发一次（等价于等到 09:00）
sudo systemctl start league-daily-predict.service
journalctl -u league-daily-predict.service -n 50 --no-pager

# 校验 OnCalendar 的时区解释
systemd-analyze calendar "*-*-* 09:00:00 Asia/Shanghai"
```

> 旧的环境变量 `ENABLE_CRON` / `CRON_HOUR` 已随进程内调度一并移除（`web/services/cron.py` 删除、
> `web/lifecycle.py` 不再启动调度器），避免两套调度并存重复消耗配额。

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

# 蒙特卡洛冠军模拟（2026-09-18 起**默认开启**，无需再加 --monte-carlo）
python3 scripts/predict.py --league epl
python3 scripts/predict.py --league epl --no-monte-carlo   # 显式关闭（冠军页将为空）

# 指定日期范围
python3 scripts/predict.py --league epl --dates 20250101-20250131

# 取数窗口回看天数（默认 30）
# 窗口只取「今天-明天」会让 past_matches 恒为空，连带打死校准/命中率/对账
# 与模型 form/record 特征（无历史比赛可推导 → 回退中性值）
python3 scripts/predict.py --league epl --past-days 30
python3 scripts/predict.py --league epl --past-days 0    # 恢复旧行为（仅未来两天）

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
│   │   ├── odds_enrich.py     # ESPN 1x2 收盘赔率回填（队名归一 + 三层匹配 + 去水，2026-09-18 新增）
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

Dashboard 的 AI 日报由当日预测与已有 `ai_scores.json` 确定性聚合生成，只复用已有预测字段、`ai_summary` 和 `ai_notes`，不生成新闻、赛果或收益结论。AI 分数榜仅按有限数值 `ai_score` 排序，明确标注“非投注热度”；预测与 AI 记录必须通过**稳定主键**和联赛 exact match 关联，未关联项不补分。

**AI 分数的匹配主键（2026-09-18 起）**：`联赛|主队英文原名|客队英文原名`，例如 `epl|Brentford FC|Chelsea FC`。

- 为什么不用中文名：中文名是 `core/i18n.to_cn()` 的**派生显示值**（LLM 翻译 + 缓存）。同一个 LLM 在富化时会把译名“纠正”成别的队 —— 实测 `西班牙人 vs 埃尔切` 回成 `西班牙人 vs 阿根廷`、`勒芒 vs 洛里昂` 回成 `洛森 vs 洛里昂`、`法兰克福 vs 弗赖堡` 回成 `法兰克福 vs 德累斯顿`，**21 条里只有 14 条精确照抄**。按名字配对时这些条目会静默丢分（实测一次富化 68 条只写回 48 条，日志仅报 `wrote 48`）。改用英文原名后精确照抄 5/5。
- LLM 配对改用**不透明整数 `id`**（`_batch_id`），不再要求模型照抄任何名字；并按 id 对漏答条目重试。实测 68/68 全部写回，`ai_matched_count` 由 45/60 提升到 **60/60**。
- 历史条目仍以中文名为键，读侧（`_lookup_score`）先查稳定主键、再回退中文键，**不丢历史数据**。`ai_details` 返回的 `match` 取落盘时保留的中文显示名 `name`，不是字典键。
- 契约 §5 规定 web 层不得 import `scripts/`、`ai/`，故 `web/services/ai.py::_score_key` 是**故意复制**的实现，有单测（`test_web_score_key_matches_engine_score_key`）保证两侧一致。

**已知缺陷**：`ai/feedback_loop.py::adjust_prediction` 的 docstring 声称 factor 为 0.7–1.3、`ai_score=100 → boost 30%`，但实现是 `0.7 + 0.3*(ai_score/100)`，取值仅 0.7–1.0 —— **AI 反馈回路只能降低信心、永远无法提高**。测试 `test_adjust_prediction_factor_range_is_one_directional` 固化了当前实际行为，修实现还是修文档需单独决策（改实现会改变所有预测的信心值）。

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
- **ESPN 赔率富化**（2026-09-18）：football-data / ESPN 源本身不带赔率，预测行 `market.status` 恒为
  `missing`，今日推荐 KPI 与串关组合赔率恒「—」。`scripts/core/data/odds_enrich.py` 按**归一化队名 +
  开球日/时点**将 ESPN scoreboard 的 1x2 收盘赔率（`moneyline.home/draw/away.close`）回填进预测行：
  - 队名归一化：去噪音词（fc/cf/de/la…）、去重音、别名映射（koln→cologne / hamburger→hamburg /
    lyonnais→lyon）；
  - 三层匹配：① 队名精确 ② 队名容器（前/后缀）③ 开球时点唯一 + 多候选打分消歧（主/客容器匹配各 +1，
    并列即跳过绝不错配）；缺开球时刻退化为纯队名唯一匹配；
  - 美式→十进制赔率转换，三向去水出 `home/draw/away_true_prob`（和 ≈ 1.0）；
  - 非致命：ESPN 抓取失败仅记 warning，不阻断预测主流程。五大联赛实测 **28/28 场回填成功**。

## v2 (2026-09-18) 代码审核修复：资源可靠性与逻辑正确性

对 scripts/ 与 web/ 全仓（101 个 .py，~12k 行）做了**结构审查**（健壮性 / 模块化 / 死代码 / 逻辑错误），
经逐条核实后修复真实问题；子代理报告含大量误报（门面 re-export、副作用导入、告警抑制机制、
降级式 `except` 等均被误判为 bug），**只修经验证存在的缺陷**：

- **资源泄漏（7 处，Critical）**：`backtest.py` 的 `_bk_fetch_api_actuals` / `_bk_fetch_fd_actuals`、
  `data/fetch.py` 的 `_retry_request` / `fetch_espn` / `update_fifa_rankings` 的 `urllib.request.urlopen`
  未用 `with` 关闭，以及 `ingest/jc_manifest.py::_count_lines`、`ingest/jc_read.py::read_lines` 的
  `open()` 未关闭 → 全部改用上下文管理器。行为验证：50 轮网络请求 `<proc>/self/fd` 计数零增长。
- **逻辑 bug（3 个）**：
  - `bball/elo_bball.py`：`if home and away and home_score and away_score:` 用 truthiness 判整数，
    0 分比赛（0-0/1-0/0-2）整场被跳过不更新 ELO → 改 `.get(home)` + `is not None`（与 `bball/run.py`
    `_past_detail` 写法对齐）。
  - `store/pg.py::read_conn`：`finally: conn.rollback(); conn.close()` 中 rollback() 抛异常时 close()
    不执行 → 嵌套 `try/finally` 保证连接必关闭（write_conn 用 `with conn:` 本就安全，未动）。
  - `web/services/store.py::results_by_bjt_date`：空 id 行恒走 `not key` 分支无法去重 → 用整条记录
    稳定指纹兜底（该函数当前无调用方，回归风险为零）。
- **死导入**：`ingest/jc_write.py` 移除未使用的 `clock`（`dec` 保留）。
- **甄别为误报而未动**（均为有意设计 / 合理防御）：`config.py` 门面 re-export（docstring 明言向后兼容）、
  `pg.py:15` 副作用导入 `from core import constants`（注释明言"导入即触发 _load_dotenv()"）、
  `poisson.py::_tau_clamp_warned` 告警抑制、fetch.py 宽泛 `except`（均记录日志后降级/重抛）、
  `_load_dotenv`（实为 24 行健康函数，非"267 行怪物"，系审计工具边界误判整个文件为函数体）。

验收：**660 passed**（新增 ESPN 富化 34 例 + 幽灵守卫 7 例 + 跨文件结算 5 例）。

## v2 (2026-09-18) 缺陷修复：ESPN 赔率富化 + 取数窗口 / 命中率连接键 / 蒙特卡洛

真浏览器（Playwright + Chromium）逐页核对线上 Dashboard 后定位的三个 P0 及后续 P0-2 增强：

- **P0-1 取数窗口写死「今天-明天」** → `past_matches` 恒为空（实测 `Past: 0`）。
  连带打死**校准**（`no past matches to calibrate from`）、**命中率**、**对账**，
  以及模型自身的 **form/record 特征**（日志原文：`form/records 数据源未提供且无历史比赛可推导，
  状态/战绩信号回退为中性值`）。新增 `--past-days`（默认 30）：
  修复后 `past=40` 且 40/40 带回比分，校准与 form 特征全部恢复。
- **P0-1b 命中率/对账链路用中文显示名当连接键** → `name`/`match` 是 `to_cn()` 的派生值，
  i18n 表未覆盖的队名会原样返回英文，同一场比赛可能一个中文一个英文
  （实测 41 个 actuals 与 6 个 preds **键交集为 0**）。改用与 AI 打分链路同一约定的
  **英文原名稳定主键** `home_en|away_en`（`backtest._bk_stable_key`），中文名仅用于显示。
- **P0-2 蒙特卡洛输入只有预测窗口内的 6 场** → 等于拿 6 场球推整个联赛夺冠概率，
  只能覆盖 12 支球队；且 `--monte-carlo` 是可选开关，自动刷新与 jobs API 都不传，
  冠军页因此长期为空。改为**默认开启**（`--no-monte-carlo` 关闭）+ **整季赛程 + 积分榜播种**
  （见「蒙特卡洛」节）；播种榜与官方 standings 逐队对账 **20/20 一致**。
- **附带修复**：预测行此前不带 `kickoff_utc` / `data_window`（API 层逐条暴露但生产者不写），
  导致今日页足球 28/28 行「时间待定」、「数据窗口 —」，前端时间筛选恒为 0 场；
  彩票页号码分组展示；`Number(null)===0` 把 NBA 缺失信心分当 0；390px 视口两处既有横向溢出。

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

本仓库 `collector-cn/` 子目录是国内采集机（cn-collector）的代码根（v1.3 契约，独立工具：取官方 JSON → 落 JSONL → 推远端，仅 stdlib，不连 DB）。
完整说明见 `collector-cn/README.md`（运行环境、计划任务、远端通道、节奏、契约、红线）。

- 代码根 = `collector-cn/` 子目录；Windows 机器 `ND-PC-WIN` 本地运行副本 = `E:\2026Workplace\Code\collector-cn`（与本子目录同内容，git 跟踪在本仓库）
- 计划任务 5 个：`collector_offer_10m`（2026-09-17 起 10 分钟→1h，2026-09-21 再降为**每 2 小时**）+ 4 个 daily 档（0930/1530/2130/2330，8 topic 含 `jc_odds_history`）；全部 BootTrigger+LogonTrigger+漏档补跑
- 推送：`ssh oracle-league`（`league@140.83.62.161`，专用 keypair，受限 shell tar 流式白名单）→ `/srv/league-staging/incoming/cn-collector/`（topic 子目录 + `.done` 清单，属主 league:league）；`ssh oracle`（ubuntu）仅运维
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
