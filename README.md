# League Predict

联赛预测引擎。多数据源融合 + 信号模型 + ELO + Dixon-Coles 双变量泊松 + 蒙特卡洛模拟。

**当前引擎版本**：`__version__ = "4.1.0"`（`scripts/core/__init__.py`）。SKILL.md / README / 代码必须保持一致。
**分支命名约定**：README 中的 "v2" 指 DB / 落库分层架构的**分支名**（覆盖全玩法），**不是引擎版本号**。

推理内核零外部依赖（纯 stdlib）。**v2 起 `scripts/store/`、`scripts/ingest/` 另需 `psycopg[binary]`（唯一新增依赖，只用于落库/取数，不进预测链路）**。统一运行在 `league-api`（本机 systemd + Nginx，见「运行与部署」）。GitHub Actions 已于 2026-09-11 下线。

## 全玩法覆盖（分支 `v2`，2026-09-15 起）

目标：从"五大联赛预测引擎"升级为覆盖**竞彩足球全部玩法 + 传统足彩 + 篮彩**的引擎（数字彩只做开奖对照，不预测）。

```
新增分层（v1 的 core/model/web 一律冻结，只 import 不改）
  scripts/derive/   纯函数派生层：Dixon-Coles 9×9 矩阵 → 各玩法概率（had/hhad/crs/ttg/jqc；haf 未注册即 KeyError）
  scripts/store/    落库层：官方赛果 upsert、raw→fact 解析（parse_api / align / upsert_*）、只读查询接口
  scripts/ingest/   取数层：国内采集机 JSONL 拉取（逐文件事务 + 归档）、API 回填（配额账本 + 限速 + 幂等落块）
  scripts/market/   （下一单）去水与 CLV/Brier 校准指标
DB：PostgreSQL 18 单实例多 schema  ref / stg / ops / raw / fact（+ model / analysis 待建），三角色最小权限
     详见 docs/db/infra_p0_*.sql 与 docs/infra/README-infra.md（若未入库则在队长工作区）
测试基线：python -m pytest tests -q → **2750 passed / 75 skipped / 0 failed**（2825 collected。v2.1 660 → v2.2 精修 R3-R5 +546 → **R19-R30 +443** + **R31-R61 +697** + **R62-R66 +83** + **R48/R48b +11** + **建议 1 欧冠落地 +20** + **建议 3 分层命中率/CLV +27** + **建议 4 决策层 +22** + **建议 5 异动告警 +30** + **建议 6 体彩统计面板 +43** + **顺手修两项坏指标 +9** + **建议 6 复算脚本 +14** + **两条遗留收尾 +21** + **§6.12 事故修复 +77** + **§6.12.5 赔率口径修正 +13** + **§6.13 三项全做收尾 +55** + **§6.13.5 手动模式+新鲜度守门+采集端原子性 +32** + **§6.13.6 保留策略+gaps 口径+坏文件不掀翻整批 +5** + **§6.13.7 看板假的无数据（部署后服务没重启）+6**）。0 直测 helper 清零细节见 `docs/refine/R19_R30_test_optimization_report.md` 与下方 v2.3 段。
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
┌────────────────────────────────────────────────────────────────┐
│ 业务层                                 │  细分见「应用层机制」节
│  ├ 应用层 web/routers/*.py            │  路由 + VM 编排合一（端点函数内联
│  │  （编排 → 产出 VM）                  │  build_rows / 判空 / 汇总 / stats；
│  │                                     │  详见「应用层机制」段对 web/app/
│  │                                     │  渐进迁移的说明）
│  └ 展示层 static/*.html（纯静态渲染）  │  拿 VM 直接画，不做判断
├────────────────────────────────────────────────────────────────┤
│ 接口层  web/services/*.py                               │  唯一数据编排入口
│   ├ store.py        读算法层产物 → 结构化数据            │
│   ├ team_match.py   盘口×预测合并（纯函数，单测覆盖）     │
│   ├ accuracy.py     命中率 web 版（= core.backtest.       │
│   │                  league_accuracy 1:1 复制；键用       │
│   │                  _stable_match_key，详见 §契约 1）     │
│   ├ haf.py          半全场（haf）九宫格纯函数             │
│   ├ combo.py        串关 EV / Kelly / 联合 EV             │
│   ├ lottery.py      数字彩四件（命中 / 复式 / 统计 / 胆拖）│
│   ├ ticket.py       传统足彩 14场/任九票面                │
│   ├ jobs.py         引擎子进程调度 + 配额守卫              │
│   ├ freshness.py    采集新鲜度检查                        │
│   ├ ai.py           AI 富化只读产物（ai_scores.json）     │
│   ├ ai_analyze.py   AI 异步分析（per_match / banker /     │
│   │                  lottery review）                     │
│   ├ news.py         LLM 客户端网关（web→ai 唯一允许      │
│   │                  反向 import 点；详见 §契约 1）       │
│   ├ apifootball.py  API-Football HTTP 客户端包装          │
│   └ datasource.py   上游源静态配置（无真实值、无请求）     │
│   ══ 契约 JSON ── 业务层消费的唯一数据 schema（隐式）══    │
├────────────────────────────────────────────────────────────────┤
│ 算法层  scripts/*（核心引擎）                           │  只产 JSON 产物，不碰展示
│   ├ core/    足球引擎（Onside/ELO/Dixon-Coles/MC/ML）    │
│   ├ bball/   NBA 引擎（ELO + The Odds 赔率）             │
│   ├ derive/  5 玩法派生（纯 stdlib）                     │
│   ├ store/   PG 落库层（psycopg 唯一依赖入口）           │
│   ├ ingest/  PG 取数层 + 采集端 tar 流                  │
│   ├ market/  去水与 CLV/Brier 校准指标                   │
│   └ CLI 子进程 → 产物 predictions/*.json                │
├────────────────────────────────────────────────────────────────┤
│ LLM层   scripts/ai/*  （异步、独立于业务）              │  见下「LLM 异步契约」
│   ├ llm_client.py    OpenAI 兼容客户端                   │
│   ├ batch_pipeline.py 批量富化                          │
│   └ feedback_loop.py  ai_score 写回 + adjust_prediction  │
│                                                             │
│ web → ai 网关（唯一允许的反向 import 点）：              │
│   └ web/services/news.py::_try_llm（lazy try/except）    │
└────────────────────────────────────────────────────────────────┘
```

**依赖方向（只许向上，不许下行）：**

| 依赖关系 | 规则 |
|---------|------|
| 业务层 → 接口层 | 业务只调接口层 services，拿编排好的数据；**不死写第二套编排** |
| 接口层 → 算法层 | 接口层**只读算法层持久化产物**（`predictions/*.json`、`ai_scores.json` 等），**绝不 import `scripts/` 与 `ai/`**（契约 §5）；算法经 CLI 子进程异步跑 |
| 算法层 → 数据源 | 算法层只产 JSON 产物，不落 web、不碰展示；数据层已按「数据源对比」配置 |
| LLM 层 | 完全独立于业务，见下 |

**LLM 异步契约（关键纪律）：**
- LLM 调用是**异步**的，经 job 子进程 `python -m web.enrich` / `web.services.news.generate_daily_news` 触发，
  **业务请求链路永不直接同步等 LLM**。
- 展示层只读**已完成的 AI 产物**（`ai_scores.json`、`predictions/ai_analysis/<date>.json`）；
  没有就显示「AI 未评分」，**绝不阻塞主请求**。
- LLM 失败**降级**：`enrich` 返回非 0 并响亮报错，不影响 predictions/展示可用。
- 语义上 LLM 层独立于业务；代码实体在 `scripts/ai/`（算法层目录），但**作为独立抽象对待**。
- **web → ai 的两个合法调用入口**（契约 §5 文档化例外）：
  1. `python -m web.enrich`（CLI 子进程）— 异步批处理入口；
  2. `web/services/news.py::generate_daily_news`（同步 web 调用入口）— 唯一的
     web→ai 反向依赖点：内部 `_try_llm` 函数体 lazy `from ai import llm_client` +
     try/except；新增 LLM 端点必须经本入口，不要在其它 web 文件内重复
     `from ai import llm_client`。
- 业务层 → LLM 层路径：业务端点 → `web.services.news.generate_daily_news` →
  `_try_llm` → `ai.llm_client.generate` → OpenAI 兼容 LLM（agnes-2.5-flash 等）。

**接口基线的三个给定事实（不得违反）：**
1. web 层**顶层**绝不 `import scripts.*` / `import ai.*`（见 `web/services/datasource.py` docstring 与 `tests/web/test_no_scripts_import.py` 静态守护）。
   - **允许的 lazy import 边界**（函数体内 `from ai...`，非顶层）实际有两处：
     1. `web/services/news.py::_try_llm` — LLM 客户端网关，用于优雅降级
        （详见 `docs/web/WO-M6R.md` §1）；
     2. `web/services/ai_analyze.py` 的 `_class_per_match` / `_class_banker` /
        `_class_lottery` 三处 `from ai.llm_client import generate`（异步 job 内调用）。
   - **约定**：新增 LLM 端点优先经 `web.services.news.generate_daily_news`，
     不在其它 web 文件内新增 `from ai import llm_client`；`ai_analyze.py` 的现有
     三处属历史遗留（审计 R45 记录，此前 README 误称`news.py` 为"唯一"边界）。
     两者都在函数体内、都不违反上面第 1 条（顶层禁止）。
   - `web/services/accuracy.py::league_accuracy_web` 是 `core.backtest.league_accuracy`
     的等价 web 侧实现（读 `web.services.store._stable_match_key`），用于 `jc.py::real_accuracy`
     与 `predictions.py::live_accuracy`，完全去除 web → scripts 的反向依赖；键语义由
     `tests/test_stable_match_key_mirror.py` 与引擎侧 `_bk_stable_key` 锁同。
2. **应用层编排**目前在 `web/routers/*.py` 内（见下「应用层机制」），接口层 `web/services/` 只做底层数据读取/单字段合并，写成纯函数 + 单测（`team_match.build_rows` 是样板）。
3. 契约 JSON 目前是**隐式**的（接口层返回 dict、前端自行理解字段）；业务层图片与 web 展示**共同消费同一份接口层数据**，避免业务层重复编排。

### 应用层机制（VM 编排层）

应用层 = **应用(编排) + 展示(静态)** 两层，均属 README 四层中的「业务层」，进一步细分为：

```
├─ 应用层(编排) web/routers/*.py       ← 路由 + VM 编排合一
│     ├ 调接口层（store/team_match/jc_view/...） → 组列/判空/算汇总 → 产出 View Model (VM)
│     ├ 鉴权（require_auth）+ 参数校验 + 降级语义（HTTPException vs available:False）
│     └ 典型 VM 端点：/api/jc/daily-image（available+rows+main_rows+temp_rows+stats+match_note）
├─ 展示层(静态) static/*.html              ← 纯渲染：拿到 VM 直接画
│     只保留 jcTable/jcVal/bjTime 等纯渲染函数，不做列定义/字段选择
```

> **历史背景**：早期计划把 VM 编排拆到 `web/app/` 子目录（每屏一个编排函数，
> 前端 `fetch(/api/v1/app/<screen>)` 直接拿 VM）。该计划在 README 中保留为
> **迁移方向**，但**当前实现 = `web/routers/*.py` 内联 VM 编排**（`daily-image` /
> `gap` / `movement` 等端点已经按此模式产出完整 VM 字段）。后续重构方向：
> - 把 `web/routers/jc.py` 内联的 VM 编排（如 `_daily_image_vm`）抽到
>   `web/app/jc_daily_image.py`，router 退化为纯 `def daily_image(): return app.vm(...)`
>   透传；
> - 路由前缀保持 `/api/jc/*`（前端无感）；
> - 单测位置 `tests/web/test_jc_daily_image.py` 保留，新 `tests/app/test_*.py`
>   加 VM 纯函数单测。
>
> **进展（审计 R45 更新）**：`web/app/` 已创建并落地首个样板 ——
> `web/app/jc.py::daily_image_vm`（纯函数、无 FastAPI 依赖），`web/routers/jc.py`
> 的 `/daily-image` 端点已退化为 `from web.app import jc as app_jc; return
> app_jc.daily_image_vm(...)` 透传，单测在 `tests/app/test_jc_daily_image.py`。
> 其余屏（gap / movement 等）仍在内联阶段，按 `web/app/__init__.py` 记录的
> 迁移路径逐个抽出。原「`web/app/` 不创建」的说法已过时。

**View Model (VM) 约定** —— 每个「屏 / 图 / 表」对应一个 VM，包含：
- `columns`：列定义（字段名 + 表头 + 渲染提示），替代前端 `JC_FIX_COLS`/`JC_ISSUE_COLS`
- `rows`：已判空、已格式化、已算汇总的行数据
- `meta`：页面元信息（日期、数据源、`degraded` 标记等）
- `actions`：该屏动作（如每日一图的下载 / 二维码）

**展示层契约（前端「取结果就完了」）：**
- 前端 `fetch(/api/jc/<screen>)` 或 `/api/v1/<screen>` → 直接拿 VM → 纯模板渲染
- 前端**删除**列定义（`JC_FIX_COLS` 等）与 canvas 绘制的字段组装（上移到应用层 VM）
- 前端**保留** `jcTable`/`jcVal`/`bjTime` 等纯渲染函数（「怎么画」，非「画什么」）

**边界铁律（防「又写一套」）：**
- 应用层只调接口层，**不 import 算法层**（延续契约 §5）。
- 展示层只吃 VM，**不做业务判断**（判断 = 编排 = 应用层）。
- 一屏一个编排函数，**可单测**。
- **渐进迁移**：先把 `daily-image` 的 VM 抽出到 `web/app/` 做样板，验证契约后再推广。

## 数据源对比

| 源 | 赔率 | 覆盖 | 免费额度 | 引擎覆盖 |
|---|------|------|---------|---------|
| **API-Football** | 33 家博彩公司, 1X2/盘口/大小球 | 全联赛, 最近 3 天 | 100 次/天 | EPL, La Liga, Bundesliga, Serie A, Ligue 1 |
| **football-data.org** | 无 | 全联赛, 全历史 | 10 req/min | 五大联赛**默认数据源**（per-league `LEAGUE_CONFIG[*].data_source`）|
| **ESPN** | 有限 (DraftKings) | MLS, 中超, 国际赛 | 无限制 | 赔率富化（`odds_enrich` 在 football-data 缺盘口时回填 ESPN scoreboard）|
| **The Odds API** | 博彩公司 h2h/让分/大小分 | NBA（`basketball_nba` active） | 免费 ~500 次/月（header `x-requests-remaining` 可查）| NBA（`the-odds`，付费源默认关闭，见 D9；当前 .env `LEAGUE_SOURCE_ODDS_API=on` 但执行链路未接 allow_paid 闸门）|

> 「引擎覆盖」列仅说明**该源被引擎哪条链路使用**，不代表它是默认数据源。
> 五大足球联赛的默认数据源由 `scripts/core/leagues.py::LEAGUE_CONFIG[*].data_source`
> 决定（当前 5 家均为 `"football-data"`，CLI `--data-source` 显式传参可覆盖）。
> 详见下方「支持的联赛」表与 `scripts/core/data/fetch.py::fetch_events` 的
> `effective_source = data_source or config["data_source"]` 兜底链。

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
  当前已训练启用：epl / laliga / seriea / ligue1（bundesliga 历史样本 28<30、nba 休赛期，ML 层跳过
  — bundesliga 本身仍**支持**预测与盘口合并，仅 ML 加权项回退规则基线）。
- AI 层：已接通。enrich job 写回 `predictions/ai_scores.json`（稳定键 `league|home_en|away_en`，
  回退中文名），`predict.py` 经 `ai.feedback_loop.adjust_prediction` 应用
  信心因子 `0.7+0.3×ai_score/100`，预测产物记录 `ai_adjusted` / `ai_score_used` / `ai_adjustment_factor`。

### 信号融合公式

```
home_strength = market_home × 45% + onside_home × ~45% + elo_home × 18% + spread_movement × 0.5
away_strength = market_away × 45% + onside_away × ~45% + elo_away × 18% - spread_movement × 0.5
draw_strength = market_draw × 45% + draw_base

P(home) = home_strength / sum
```

> 权重常量见 `scripts/core/constants.py::MARKET_ODDS_WEIGHT = 0.45` 与 `THRESHOLDS["elo_weight"] = 0.18`；
> Onside 系数为 `(1 - market_weight) × (1 - elo_weight) ≈ 0.45`，由 `scripts/core/predictor.py` 装配。
> README 与代码必须保持同步；改权重请同时更新两边。

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

- **Nginx 层**：`/dashboard/jc/` 三处 location（`= /dashboard/jc/`、`/dashboard/jc/api/`、`= /dashboard/jc/login`）均 `auth_basic off`，从全局 Basic Auth 剥离。Nginx Basic Auth 只保护其它业务 —— **R47 生产实测**（2026-10-19，公网不带任何凭据）：`/` 401、`/dashboard/` 401、`/cpt/` 401、`/emotion/` 401、`/trade/` 401；而 `/resume/` `/admin/` `/stock/` 已随 2026-10-19 的失效配置清理**一并移除**（现为 404），本节旧清单此前未同步。
- **体彩应用层**：`web/auth.py::require_auth` 强制校验 `lp_session` cookie（单账号 session）。登录账号来自 `AUTH_USERNAME` / `AUTH_PASSWORD`（在 `.env`，已 gitignore 不入库），签发 `lp_session` cookie，前端遇 `401` 自动跳 `/dashboard/jc/login`。
- **生产验证**（R47 实测，2026-10-19）：`/dashboard/jc/` **免凭据即 200**（`auth_basic off`）；其 API 未登录返回 401 —— 那是**应用层 session** 拒绝，**不是** Nginx Basic Auth。`/` `/dashboard/` 对未带 Basic 凭据的请求才返回 401（这才是 Nginx 层）。`/resume/` `/admin/` 现为 404。

> ⚠️ 体彩账号是单账号，生产 `.env` 里 `AUTH_USERNAME`/`AUTH_PASSWORD` 仍是弱口令（占位值，见 `.env.example`）。**正式对外前务必改为强口令**（改后重启 `league-web.service`）；若 8077 直连端口对外暴露，则绕过 Nginx 后只剩应用层单账号保护。具体口令不写入本文件——本仓是公开仓库。

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

`league-api`（原"league-predict Web Dashboard"）即这套预测读取 API + 自看面板，跑在本机 NDORACLE（`140.83.62.161`）上：systemd `league-web.service`（127.0.0.1:8077）+ Nginx 代理公网 `/dashboard/jc/`，前端在 `static/`，后端在 `web/`。FastAPI Cloud Hobby 档已退役，无 fastapicloud.dev / 无容器 scale-to-zero，session 与预测 JSON 直接落本机盘 / DB。详细部署说明见 [`docs/web/DASHBOARD_DEPLOY.md`](docs/web/DASHBOARD_DEPLOY.md)。

> 单元名以 `ops/league-web.service` 为准（审计 R45 前本节误写 `league-dashboard.service`）。

| 项 | 值 |
|---|---|
| 线上地址 | https://140.83.62.161/dashboard/jc/ |
| 进程 | `league-web.service`（systemd `127.0.0.1:8077`，`Restart=on-failure`） |
| 公网入口 | 本机 Nginx（`/dashboard/jc/` → 静态入口 + `/dashboard/jc/api/` → `league-api /api/`；该路径 `auth_basic off`，鉴权由应用层 session 负责） |

**nginx location → 后端路径速查（P3-D1）**：

| 公网路径 | nginx target | league-api 内部路径 |
|---|---|---|
| `/dashboard/jc/` | `proxy_pass http://127.0.0.1:8077/static/dashboard.html;` | —（静态） |
| `/dashboard/jc/static/...` | `proxy_pass http://127.0.0.1:8077/static/...;` | —（静态文件 mount） |
| `/dashboard/jc/login` | `proxy_pass http://127.0.0.1:8077/static/dashboard-login.html;` | —（静态） |
| `/dashboard/jc/api/...` | `proxy_pass http://127.0.0.1:8077/api/...;` | `/api/v1/...` |
| `/dashboard` | `http://127.0.0.1:8099/` | —（旧门户，Basic Auth 保护） |

**前端 API 基址**（`static/dashboard.html:270`）：`const API="/dashboard/jc/api/v1"`。所有 fetch 均同源（credentials:"same-origin"），浏览器自动带 session cookie（该路径 `auth_basic off`，**不涉及** Basic Auth 头）。

**调试命令**：

```bash
# 公网入口冒烟（/dashboard/jc/ 已 auth_basic off —— 免凭据 200，不涉及 Nginx Basic Auth）
curl -kI https://140.83.62.161/dashboard/jc/          # 200
# 应用层鉴权（401 来自 session 层，不是 Nginx Basic Auth）
curl -kI https://140.83.62.161/dashboard/jc/api/v1/me  # 401（未登录）
# 健康检查：真实路径是 /health，**不在 /api/ 下** → 公网无对应入口，只能本机打
# （旧写法 http://127.0.0.1:8077/api/v1/health 实测 404）
curl -sS http://127.0.0.1:8077/health                   # 200，必须本机
# nginx 实时错误
sudo tail -F /var/log/nginx/error.log
```
| App ID | ——（本机部署，无云端 App ID） |
| 看板鉴权 | 页面单账号（league-api session，取自 `.env` 的 `AUTH_USERNAME`/`AUTH_PASSWORD`）；`/dashboard/jc/` 不走 Nginx Basic Auth |

**临时赛事白名单（亚运 / 欧冠 / 世界杯 / 奥运 等）**：
- 通过 `.env` 的 `LEAGUE_TEMP_LEAGUES`（CSV）控制，五大联赛 + 临时白名单一并出现在每日一图 / dashboard 今日推荐。
- 国内采集机 0 改动（数据本就在 26 个联赛里持续入港），临时赛事**无模型预判**（historical_past_matches.json 仅含五大联赛），仅显示官方盘口。
- 启用：`echo 'LEAGUE_TEMP_LEAGUES=亚运会男足,亚运会女足' >> /home/ubuntu/DSH/sports-lottery/.env && sudo systemctl restart league-web`，`/health` 会暴露当前白名单。
- 下线：`sed -i '/^LEAGUE_TEMP_LEAGUES=/d' .env && sudo systemctl restart league-web`，自动恢复五大联赛主面板。
- 命名：与 `fact.jc_match.league_cn` 官方全名一致（如"亚运会男足"/"欧罗巴联赛"），错误命名会显示空态卡 + note。

**部署链路（本机）**：
1. `/home/ubuntu/DSH/sports-lottery/.env`（gitignore）存凭据：`AUTH_USERNAME/PASSWORD`、`API_FOOTBALL_KEY`、`FOOTBALL_DATA_API_KEY`、`ODDS_API_KEY`/`NBA_API_KEY`、`LEAGUE_*`、`LLM_*` 等。systemd 通过 `EnvironmentFile=` 注入；不要提交该文件。
2. 代码 push 到 `origin/main`，本机 `git pull`（或 `git reset --hard origin/main`）拿到最新代码后，**必须显式重启服务**：`sudo systemctl restart league-web`。
   ⚠️ **订正（2026-10-23）**：本条原来写的是「systemd `Restart=on-failure` 会自动重启加载新代码」——**那是错的**：
   `Restart=on-failure` **只在进程崩溃时**才触发，而「代码更新」**不会**让进程崩溃
   ⇒ 服务会一直跑着**内存里的旧代码**；而 `static/dashboard.html` 是**每次请求现读磁盘**的
   ⇒ 页面变成**「静态页是新的、API 还是旧的」混合态** ⇒ 用户看到「⚠️ 暂无定价赔率快照（未采集）/ 暂无推荐单」，
   而库里明明有新鲜快照和当日荐单（2026-10-23 实测：服务从 10-21 15:21 一直跑到 10-23 09:11 才重启，
   中间隔着 §6.13.5 的部署 ⇒ 页面整整一天半在骗人）。
   ⇒ **部署后必须重启**，并用 `python tools/check_service_freshness.py` 自检（比对 `/health` 的 `code_sha` 与仓库 HEAD，
   不一致 ⇒ 退出码 1 + 打印修复命令）。`.env` 改动同样必须手动 restart。
3. 核验：`curl -kI https://140.83.62.161/dashboard/jc/` 返回 200（该路径 `auth_basic off`，无需 Basic 凭据），`/dashboard/jc/api/v1/me` 过应用层账号（`.env` 的 `AUTH_USERNAME`/`AUTH_PASSWORD`）登录后 200；**再跑 `python tools/check_service_freshness.py`**（必须退出码 0）。

**运行时预测/富化**（在 Web 界面触发，或 API）：
- `POST /api/v1/jobs/predict`（选联赛/数据源/蒙特卡洛）跑 `scripts/predict.py`。**不传参数时只跑英超**（`--league` 默认值 `epl`）；要跑全部 5 个足球联赛须显式传 `{"all": true}`，或传 `{"league": "laliga"}` 等指定单个联赛。
- `POST /api/v1/jobs/predict-bball` 跑 `scripts/bball/run.py`（NBA，独立入口）。参数白名单 `ahead_days`(1..180)/`backtest`(1..60)。需要 `ODDS_API_KEY` **和** `NBA_API_KEY` 同时设置成同一个值——两个名字并存是历史遗留：config 层 `SourcePolicy` 的守卫变量是 `NBA_API_KEY`，而 v1 冻结代码 `scripts/bball/run.py` 实际读的是 `ODDS_API_KEY`，只设一个会出现「守卫说可用、脚本说没 key」的不一致。另需 `LEAGUE_SOURCE_ODDS_API=on`（收费源默认关闭，见 D9）。NBA 休赛期默认 `ahead_days=1` 会得 0 场，需放宽窗口。
- `POST /api/v1/jobs/ai-enrich` 跑 `python -m web.enrich` 生成中文 AI 摘要（LLM 走 agnes-ai，OpenAI 兼容）。
- 每日配额共享计数：`predict` / `predict-bball` / `ai-enrich` 共用同一计数器与并发守卫，同时只允许一个预测类任务运行（并发 409）。结果 JSON 直接落本机盘（`scripts/predictions/*.json`），无容器冷启丢失问题。

### 定时运行（systemd timer，2026-09-18 重建）

**本机自动化一共三条，职责不重叠：**

| 机制 | 载体 | 频率 | 干什么 |
|---|---|---|---|
| Dashboard 常驻服务 | `league-web.service`（systemd，`Restart=on-failure`） | 常驻 | 提供 `/dashboard/jc/` 全部 API |
| 竞彩数据采集 | 用户 crontab `*/10 * * * *` | 每 10 分钟 | 跑 `jc-ingest-run.sh` 落库 `fact.jc_*` |
| **每日全量预测** | `league-daily-predict.timer`（systemd） | 每日 **09:30 Asia/Shanghai** | 足球全联赛 + NBA + AI 富化 |

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

- `ops/league-daily-predict.timer` — `OnCalendar=*-*-* 09:30:00 Asia/Shanghai`（**时区必须显式写**；实际值以 `ops/league-daily-predict.timer` 为准，审计 R45 前本节误写 09:00）
  + `Persistent=true`（错过的触发在开机后补跑一次）。
- `ops/league-daily-predict.service` — `Type=oneshot`，`After/Wants=league-web.service`。
- `ops/league-daily-predict.sh` — **走 HTTP API 而不是直调 CLI**：配额守卫
  （`PREDICT_DAILY_LIMIT`，默认 80）与并发守卫（`active_job`）都在 `web/services/jobs.py` 里，
  直调 `scripts/predict.py` 会绕过它们、与页面上的手动任务撞车吃光配额。
  三个作业**共享同一并发守卫**，因此脚本**串行**执行：发一个 → 轮询 `/jobs/{id}` 到终态 → 发下一个。
  作业体：`{"all":true}`（足球 5 联赛）、`{"ahead_days":90}`（NBA，休赛期默认 1 天会得 0 场）、AI 富化。

**观测**：三个作业端点都接受可选的 `trigger` 字段（白名单 `manual`/`timer`/`cron`/`auto`，
白名单外回落 `manual` 且不报 400），timer 传 `timer`，于是在 Dashboard「数据源与任务」页
可与手点的作业区分——定时任务跑没跑可以自证。此前该字段被硬编码成 `manual`。

```bash
# 查下次触发时刻（应显示 01:30 UTC = 09:30 BJT）
systemctl list-timers league-daily-predict.timer

# 手动触发一次（等价于等到 09:30）
sudo systemctl start league-daily-predict.service
journalctl -u league-daily-predict.service -n 50 --no-pager

# 校验 OnCalendar 的时区解释
systemd-analyze calendar "*-*-* 09:30:00 Asia/Shanghai"
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

# 预测 EPL (默认走 LEAGUE_CONFIG.epl.data_source = "football-data"，不带赔率)
python3 scripts/predict.py --league epl

# 含赔率显式切到 API-Football
python3 scripts/predict.py --league epl --data-source api-football

# 离线模式（football-data 不传日期即返回整季）
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
sports-lottery/
├── web/                                  # 见「分层架构」节 → 接口层 / 业务层
│   ├── api.py                            # FastAPI 入口（uvicorn web.api:app）
│   ├── auth.py / config.py / errors.py / lifecycle.py / session_store.py
│   ├── enrich.py                         # AI 富化 job CLI（python -m web.enrich）
│   ├── routers/                          # 业务层：路由 + VM 编排（详见后文）
│   └── services/                         # 接口层：纯函数 + 单测（详见后文）
├── scripts/                              # 算法层：纯 stdlib（除 store/ingest 需 psycopg）
│   ├── predict.py                        # 足球 CLI 入口（run_league 拆 6 子函数）
│   ├── clv_baseline.py                   # CLV 基线脚本
│   ├── backfill_seasons.py / backfill_validate.py / import_backfill.py  # 历史回填
│   ├── config.py                         # 引擎侧 config（与 web.config 区分）
│   ├── bball/                            # NBA 引擎（v2 引入，独立子包）
│   │   ├── config_bball.py / elo_bball.py / odds_api.py / predictor.py / run.py
│   ├── core/                             # v1 冻结的足球核心（v4.x 算法版本）
│   │   ├── predictor.py / elo.py / calibration.py / backtest.py / rankings.py
│   │   ├── config.py / constants.py / leagues.py / output.py / log.py / jc_gate.py
│   │   ├── i18n.py / dashboard.py
│   │   ├── data/                         # 数据源适配 + 取数
│   │   │   ├── fetch.py / parse.py / odds_enrich.py / convert.py
│   │   ├── model/                        # 模型层
│   │   │   ├── onside.py / poisson.py / features.py / ml_model.py
│   │   │   ├── monte_carlo.py            # 共享层：单场 DC 采样 + 顶层入口 + SE
│   │   │   ├── monte_carlo_league.py     # 联赛语义（剩余赛程 + 积分榜播种）
│   │   │   └── monte_carlo_worldcup.py   # 锦标赛语义（小组 + 标准 16 强 + 退化配对）
│   ├── derive/                           # v2 新增：5 玩法派生（纯 stdlib）
│   │   ├── grid.py / handicap.py / totals.py / scores31.py / goals4.py
│   │   └── registry.py                   # 玩法注册表
│   ├── store/                            # v2 新增：DB 落库层（PG 唯一 psycopg 入口）
│   │   ├── pg.py / query.py / selfcheck.py
│   │   ├── align.py / parse_api.py / parse_collector.py / parse_jcissue.py
│   │   ├── parse_jczq.py / parse_misc.py / parse_odds_hist.py / parse_values.py
│   │   ├── jc_view.py / jc_ops_view.py / team_identity.py
│   │   └── upsert_fixtures.py / upsert_official.py / upsert_results.py
│   ├── ingest/                           # v2 新增：DB 取数层（PG + 采集端 tar 流）
│   │   ├── api_get.py / quota.py
│   │   ├── collector_pull.py / jc_load.py / jc_manifest.py / jc_read.py / jc_topic.py / jc_write.py
│   │   ├── jc_odds_write.py / jc_issue_write.py / jbq_result_write.py
│   │   └── run_backfill.py
│   ├── market/                           # v2 新增：去水与 CLV/Brier 校准指标
│   │   ├── devig.py / clv.py
│   ├── model/                            # 旧 ML 建模（v1 路径，新工作走 core.model）
│   │   └── fit.py / walk.py / backtest.py / sides.py
│   ├── predictions/                      # 预测结果 JSON（运行期产物，gitignore）
│   ├── results/                          # 实际赛果 JSON（运行期产物，gitignore）
│   └── references/                       # 排名 / 文档 / 趋势（historical_past_matches.json 入库）
├── ai/                                   # LLM 层（v2.x 异步，独立抽象；详见「LLM 异步契约」节）
│   ├── llm_client.py                     # OpenAI 兼容客户端（agnes-2.5-flash 等）
│   ├── batch_pipeline.py                 # 批量富化 pipeline
│   └── feedback_loop.py                  # ai_score 写回 + adjust_prediction
├── collector-cn/                         # 国内采集机（cn-collector，v1.3 契约，独立子目录）
├── references/                           # 静态参考数据（fifa_rankings.json / historical_past_matches.json）
├── docs/                                 # 项目文档（DATA_CONTRACT / DECISIONS / WO-Mx / reports）
├── ops/                                  # systemd unit / shell 脚本（部署链路）
├── static/                               # 业务层 → 展示层：纯静态渲染（dashboard.html 等）
├── tickets/                              # 历史工单（P0-* 系列）
└── tests/                                # 全量回归（详见 tests/web/README.md）
```

## 支持的联赛

| 键 | 联赛 | 默认数据源 | API-Football ID | DC ρ | λ 乘数 |
|----|------|-----------|----------------|------|--------|
| epl | English Premier League | football-data | 39 | 0.17 | 2.8 |
| laliga | La Liga | football-data | 140 | 0.22 | 2.7 |
| bundesliga | Bundesliga | football-data | 78 | 0.19 | 3.2 |
| seriea | Serie A | football-data | 135 | 0.28 | 2.5 |
| ligue1 | Ligue 1 | football-data | 61 | 0.21 | 2.7 |
| **ucl** | **UEFA Champions League（欧冠）** | **espn** | 2 | **0.08** | **3.7** |

**欧冠（`ucl`，2026-10-20 新增，路线图《建议 1》）的三点特殊**：

1. **数据源 = ESPN**：football-data.org 没有欧冠 ⇒ 配置里**故意不设 `league_id`**（设了回填器会拼出无效 URL）。
   ESPN scoreboard 的 `dates` **只吃单日**（区间实测 HTTP 400）⇒ 整季取数走 `dates=<日历年>` 通道
   （`core/data/fetch.py::fetch_espn_season`，抓三个自然年、按 `season.year` 保留最近两个赛季）。
2. **冷启动 ⇒ 低置信**：俱乐部不在 FIFA 国家队排名里（`core/rankings.py`），ELO 靠整季历史滚出来
   （实测 111 支队全部脱离 `DEFAULT_ELO`）。预测行的 `confidence_note` 含「冷启动」、
   置信度钳到 **0.5** ⇒ 星级最高 **2-star**、带 `low_confidence=true`，**不与五大联赛同档展示**。
3. **不做夺冠模拟**：欧冠是两阶段赛制，积分榜式蒙特卡洛不适用（实测会给出「利物浦 42.1%」这种
   与赛制无关的数）⇒ `LEAGUE_CONFIG["ucl"]["monte_carlo"] = False`。

队名链：模型产出的中文名 = **竞彩官方中文名**（`scripts/references/team_cn_official.json`，109 条，
人工核对；依据与逐条核验见 `docs/project/待审-竞彩官方队名表-20261020.md`）——
竞彩↔预测是按中文名 join 的，通用叫法（曼城/曼联/热刺/巴黎圣日耳曼）会逐字对不上。

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

## 变更日志（按时间倒序）

> **版本号约定**：
> - **引擎算法版本**（`scripts/core/__init__.py::__version__`）：当前 4.1.0，对应 `v4.x` 系列；
>   改权重 / 加模型信号时升 v4.x。
> - **DB / 分层分支名**：`v2`（2026-09 起，PG 库 + 全玩法覆盖，对应 `v2.x` 系列）；
>   引擎内核 v1 冻结，只 import 不改，新逻辑走 `scripts/store/`、`scripts/ingest/`、`scripts/derive/`。
> - `v2.x` 与 `v4.x` 时间倒序在下方；二者版本号**互不影响**。

## v2.x — DB / 全玩法分支（2026-09 起）

### v2.1 (2026-10) 精修本轮（5 commit）

- **`docs+test: 契约 §5 明文 + 静态守护`**（`a83769d`）— README 重写 §1「web 层顶层禁 / 函数体 lazy-in-try 是文档化例外」，
  新增 `tests/web/test_no_scripts_import.py`（AST 静态检查仅挡顶层 import，函数体 lazy 自动豁免）。
- **`fix(bball): load_ratings 真吞异常改 logger.warning`**（`26c16d4`）— `scripts/bball/elo_bball.py:24-25`
  静默 `pass` 改记 WARNING（带文件路径 + 异常类型），NBA 评分文件损坏时拿得到运维信号。
- **`test: 补三块关键路径单测`**（`f583212`）— 21 用例：连接键双侧镜像 / MC 降级路径 /
  bball CLI 装配（`_save` 文件名带 `_nba` 后缀 + 0-0 比赛 actual 写入）。
- **`refactor(model): 拆 monte_carlo.py 为共享层 + league + worldcup 三文件`**（`c0acf27`）—
  472 → 172 行共享层 + 两个语义独立文件，老路径 re-export 保持兼容。
- **`docs: 同步依赖清单 / 版本号 / 信号权重公式`**（`518bea5`）—
  `requirements.txt` 补 `psycopg[binary]`；`requirements-web.txt` 补 `qrcode`；
  `SKILL.md` / `predict.py` / `core/__init__.py` 版本号统一 4.1.0；
  README 信号融合公式权重 20%/70%/18% → 45%/45%/18%（与 `MARKET_ODDS_WEIGHT = 0.45` 对齐）。
- **`fix: 修 jc_read.py NameError + ruff F401/F841/F811/F821 清零`**（`222fa65`）—
  `jc_read.py:62` PermissionError 触发即 NameError 崩溃修；NBA 翻译表 / `to_cn` shim / 多余 import 一并清理（19 处）。

### v2.2 (2026-10-13) 精修本轮：覆盖率补测 + cross-boundary 收口 + flake 根因修

3 阶段共 18 个 commit（详细见 `docs/refine/P{3,4,5}_report.md`）：

**P3（覆盖率补测 + flake 根因，9 commit）**：
- P3-4/5：news / jc_ops_view 0%→100%
- P3-6/7：jc_load 0%→92% / jc_view 39%→98%
- P3-8/9/10：upsert_official/results/fixtures 30%→100%
- P3-11/13/14/15：combo / jc_ops / lottery_api / ai 路由层 11%→100%
- P3-12/17/18/20/22：5 处 `_drain_jobs` 5s→10s + PermissionError 容错（test_m3_jobs / test_m6_ai_enrich / test_job_trigger_source 三处对齐；`web/services/jobs._read_job` 内置三档短退避重试）
- P3-19：web/routers/jc 五组端点 24 用例补测 + `gap()` `latest_by_league` 抛错降级

**P4（cross-boundary dead code / duplicate 清理，5 commit）**：
- P4-1：`scripts/core/data/fetch.py` 删 `fetch_fifa_rankings` / `update_fifa_rankings` / `FIFA_RANKINGS_API_URL`（主链未用；DATA_CONTRACT 疑点 15 已 CLOSED）
- P4-2：bball `apply_regression` + `ELO["REGRESSION"]` 死代码清理
- P4-3：`canonical_src_hash` 字节级 duplicate 合并（`upsert_official` → `parse_values` 单一真源）+ mirror 守门
- P4-4：query.py / selfcheck.py / team_identity.py 5 个无 caller 函数 + 依赖清理
- P4-5：`normalize_team` 双实现（align vs odds_enrich）drift 行为锁（36 用例 snapshot 钉死两侧当前行为）

**P5（收口 / 抽取 / lint 收尾，4 commit）**：
- P5-1：unused import 3 + source_meta 2 dead local
- P5-2：`market.devig.safe_devig` 收口（jc_view._implied + clv_baseline._devig_home 25 行 dup → 1 helper）+ 33 守门；jc_view._implied 提模块级
- P5-3：`core.team_norm.strip_accents_and_lowercase` 抽字符级共用 helper（normalize_team 双实现 NFKD+Mn+lowercase 3 行共享）+ 19 守门
- P5-4：clv_baseline._BJT dead local

**15x 全仓压测基线**：14/15 全过；残余 1/15 flake `test_auto_refresh_trigger_and_dedup`（Windows fs fd 释放延迟根因不在生产代码层面，留作 known issue）。

**R19-R30 测试基线补测（12 轮 39 commit，2026-10-13 ~ 10-15）**：

每一轮走 explore + writer + 全量回归 + Oracle sync 闭环；**全程只新增测试，不改生产代码**（除两个 R19 perf 合并）。本阶段净增 443 测（1382 + 71 + R12-R18 已稳定 = R19 起点 1453 → **R30 1649**）：

| Round | 主题 | 净增测 |
|---|---|---|
| **R19** | combo/haf 反查哈希化 + `teams_match_safe` 收口 + `build_rows` 字典复用 | +14 |
| **R20** | observability 重入 fail-fast + 工作区清理 (retrain 产物 untrack) + ticket 复用 pred_index | +14 |
| **R21** | news.py 缓存 TTL+LRU 上限 + daily-news 端点覆盖 | +6 |
| **R22** | predict output 兜底 (`_save_output` / `_emit_ml_*` / `_cleanup_intermediate` 三文件保护 / import 兜底) | +21 |
| **R23** | calibration `build_calibration` / web 401 鉴权矩阵 / haf 九宫格 / save_results / `append_historical_past_matches` | +25 |
| **R24** | predictions.py 7 helpers + 2 端点 / `results_by_bjt_date` + `_stable_match_key` / `cleanup_old_files` | +30 |
| **R25** | backfill_seasons / import_backfill 零测补全（含 `from store import` 注入模式） | +29 |
| **R26** | backtest 三层（`split bug` 记录） / jobs 锁三重（POSIX-only skip）/ dashboard 8 函数 | +31 |
| **R27** | calibration 5 内部 helper / ai_analyze 三件套 LLM fallback / jobs 4 分支 | +26 |
| **R28** | build_parser 17 flag / `load_historical_past_matches`（R27 漏 1 函数）/ onside 4 helper（含 `league_footprint_score` 0.3 分支不可达记录） | +38 |
| **R29** | jobs `_spin_state` + LockTimeout / `predict._setup_league_run` 正常分支 / core/elo 持久化 trio + rankings | +26 |
| **R30** | jc_view `_nums` 私有 helper / setup_logger / `_compute_calibration` 三阶段 fallback | +16 |

**关键设计沉淀**（D-08 ~ D-13）见 `docs/refine/R19_R30_test_optimization_report.md`：测试隔离策略（importlib.util 模式 + sys.modules 注入 + from-import 别名处理） + 3 个 bug-shaped 契约锁定（`_bk_parse_window` split bug / `league_footprint_score` 0.3 不可达 / `fetch_season_with_retry` tuple 契约）+ 测试顺序 flake 处理规则。

### v2.3 (2026-10-15 ~ 10-18) R31-R61 测试基线补测：0 直测清零

31 轮 31 commit，全程只新增测试，不改生产代码（除 R50-2 flake fix 一个 commit `3bcee34`）。本阶段净增 +697 测（R30 1649 → **R61 2346**）：

| Round | 主题 | 净增测 |
|---|---|---|
| **R31-R49** | 19 轮：predict/storage/odds 三层多 helper 0 测补全（含 `simulate_league` / 5 个 fetch / `_market_projection` 等）+ 19 个 D 契约（D-87 ~ D-91 集中期） | +545 |
| **R50-R61** | 12 轮：13 个模块 0 直测清零（auth / features / i18n / poisson / llm / rankings / store / news / jobs / ticket / lottery / apifootball / parse / ingest / model / observability）+ 28 个 D 契约（D-92 ~ D-124） | +152 |

**关键沉淀**（D-87 ~ D-124 共 38 个新契约）：
- **R50-2 flake fix `3bcee34`**：`from store import jc_ops_view` 在 `scripts.store` 是 package 时走 `__init__`，`sys.modules['store.jc_ops_view']` 子模块替换无效；改 `monkeypatch.setattr(spec_mod, "jc_ops_view", stub, raising=False)` 写入 store namespace。
- **死分支发现**：`core.model.features` L121 兜底前置 `or 0.5` 已守护，正常 input 永不触发；用负数概率验证 L121-122 逻辑本身。`i18n` L72-75 过滤 flat 分支 `all(isinstance(v, str))` 已先验；用嵌套响应触发嵌套 dict 过滤。
- **from-import 死绑定**：`from core.config import FOOTBALL_DIR` 顶层绑定到 module global，`monkeypatch core.config.FOOTBALL_DIR` 无效；改为 patch spec-load 模块的 global。

详见各 round commit message 与 `docs/refine/R19_R30_test_optimization_report.md`（沿用同设计模式 + D 类契约同编号体例，**不**单独写 v2.3 报告以避免文档展开）。

### v2.4 (2026-10-18 ~ 10-19) R62-R64 生产修复 + AGENTS.md + R43 策略收回

v2.3 之后跨入"测试基线饱和、生产修复优先"阶段。本段记录 e280a3b 之后的 **10 个 commit**（审计 R45 校正，此前误写 7），测试基线 R61 2346 → **R66 2358 passed**（2429 collected）→ **R47 2360 passed**（2431 collected，见下 R47 段）：

**Dashboard 三连发（4aea03d / 093dafa / f78aa40）**：
- `4aea03d` 比赛详情面板人话翻译（取代 ML 术语）+ 三级 `modelProbs` 回退修复（`model.probs` → `prediction.probs` → 推导）
- `093dafa` 5 项每日一图：odds / brier / DC ρ / calibration offset / sharpness；篮球 UI 修复（球员得分缺数据兜底）
- `f78aa40` 默认 24h 时间窗口（`mode=24h`）+ 指定日期过滤（`?date=YYYY-MM-DD`），避免默认窗口拉 30 天过载

**两轮生产修复（3424939 + 4a6dc8b，均冠 P5-Q 编号不入 R## 命名体例）**：

| Commit | P5 编号 | 修复 | 实证 |
|---|---|---|---|
| `3424939` | **Q3** | `scripts/predict.py::_refresh_ml_models` 默认 `max_age_days=0.0`（取消节流）+ 加 INFO 日志："ML model %s is N days old, retraining" | ✅ **10/19 cron 已实测生效**：5 个 `ml_model_*.json` 全部刷新到 01:31-01:35Z（修复前滞后 36-48h，停在 10/17 01:32） |
| `4a6dc8b` | **Q5** | `predict.py::main --all` 过滤 `LEAGUE_CONFIG` 中 `active=False` 联赛；`run_league` 入口加 active 守门（显式 `--league nba` 抛 `SystemExit` 指向 `scripts/bball/run.py`） | ✅ **10/19 cron 已实测生效**：作业 `4e26568f7349 完成` → `退出码 0` → `Finished`（10/18 为 `终态=failed` → `退出码 1` → `Failed`）。根因来自 871KB 的 10/18 cron log：NBA active=False / data_source="the-odds" 不在 `fetch_events` if/elif 链 → 落 `else: fetch_espn(epl)` → ESPN HTTP 400 |

**R43 hot-path logger ASCII 收口（6841f23）→ R44 撤回策略**

R43 以「避免 Windows cp1252/GBK 把日志打成乱码导致告警脚本 grep 失败」为由，把 **14 处** hot-path logger 文案（`predict.py` 3 / `bball/run.py` 3 / `retrain_baseline.py` 7 / `observability.py` 1，合计 3+3+7+1=14；审计 R45 校正，此前正文误写「13 处」而括号分项自相矛盾）改成英文，并加 `tests/test_logger_hot_path_ascii.py` 5 测守门。

**R44 用实证推翻了这个前提**，并撤回策略（守门测试已删）：

| 论断 | 实证结果 |
|---|---|
| 日志会乱码 | ❌ Oracle `LANG=zh_CN.UTF-8`，`sys.stdout.encoding == 'utf-8'`；10/18、10/19 两天 cron 中文输出在 journalctl 完全正常 |
| 告警脚本 grep 日志 | ❌ `ops/league-daily-predict.sh` 的告警只看作业 HTTP status（`wait_job` → `job_field status`），全程不读日志正文；仓库内无任何 grep 日志的脚本 |
| Windows 需要兼容 | ❌ `.github/` 不存在（Actions 2026-09-11 已下线），无 Windows runner；`web/services/jobs.py::read_job_log` 显式 `decode("utf-8", errors="replace")` 本就正确 |

守门测试本身还有**独立缺陷**：`_extract_string_literals` 只取 `node.args` 中直接是 `ast.Constant` 的字符串，而本仓日志绝大多数是 f-string —— 内嵌片段挂在 `ast.JoinedStr` 之下而非 `node.args` 顶层，**全部漏检**。实测 `predict.py`：守门测试检出 0 处，真实非 ASCII 有 14 处（全在 f-string 内）。即测试报 "5 passed" 时，它守护的文件里 14 处违规一处没看到。

处置：删除守门测试（`99d494a`）；R43 已上线的 14 处英文文案保留不动（语言风格问题，非缺陷）。本仓中文注释 + 中文日志是既有惯例（`ops/league-daily-predict.sh` 自身输出全中文、稳定运行数月），已在 `AGENTS.md` 明文记录，后续不再做 ASCII 收口。

**R44 新增 `AGENTS.md`（`d9734ce`）**：仓库根此前缺失（agents.md 标准文件，被 OpenCode/Codex/Cursor/Aider/Devin/Gemini CLI 等消费）。内容全部来自实测：ruff E/F/W/I + line-length 120 双引号、`PYTHONPATH=scripts` 必需、三层分层依赖方向约束、conventional commits（scope 带模块名）、pre-push 5x 压测、systemd 定时任务与退出码语义（0/1/2/3），并把上表"告警只看作业 status 不 grep 日志"的排障结论写入 Ops 段以防重复误判。

**R43/R44 遗留（有意不做）**：`ingest/*` + `core/data/*` + 一次性脚本的中文日志维持原样 —— 前提同上（无危害）。

**R45 全维度审计（4 个并行视角：文档声明 / 测试有效性 / 代码缺陷 / 安全运维）**

方法上吸取 R43 的教训：**不读代码猜，逐条实测**。4 个子代理结论全部人工复核，
推翻了其中 5 条（见下"证伪记录"），只采纳经复现/实证的部分。

**已修复的真缺陷（每条均经复现验证）**：

| 位置 | 缺陷 | 实证方式 |
|---|---|---|
| `web/services/jobs.py:191-194` | `except (ProcessLookupError, OSError)` 排在 `except PermissionError` **之前**（后者 ⊂ OSError，永不可达）→ POSIX 上"活进程但非本用户持有"被误判为已死 → 可能删活锁 | `issubclass(PermissionError, OSError)` → `True`；已调换顺序 |
| `scripts/core/retrain_baseline.py:149` | 分母用 `len(samples)`，但循环对未识别 outcome `continue` → 指标系统性低估 | 实测 3×`home_win`+7×`OT`：`accuracy` **0.3 → 1.0**（改用实际评估计数 `n_eval`；全无效样本指标为 `None` 而非除零） |
| `web/services/lottery.py:162` | `random.sample(...) if False else i` 死分支 → "随机基线"恒为最差值（实测 `-1.0`），任何策略都"跑赢随机"，与证伪目的相反 | 改为按号码列表语义随机取样（固定 seed 可复现）；**连带发现测试 `test_random_baseline` 断言的是假事实**（恒猜 `[1]` 实际输随机），已改写真实契约 + 新增可复现性测试 |
| `scripts/core/data/parse.py:250` | 裸表达式语句 `spread_h.get("open")`（取值即弃） | 已删；并订正 `spread_movement_factor` docstring —— home 分支**有意不启用**：ESPN 主客 line 互为镜像（`-1.5`↔`+1.5`），两侧项数值相同会**翻倍而非互补** |
| `scripts/core/observability.py:255` | `_UNWRITABLE_LOG_WARNED` 定义后全仓零读写 | 已删（`reset_for_test` 也未同步它） |
| `web/services/source_meta.py:95-102` | `left_len` 计算后从未消费（ruff F841），连带 `left_end_excl` / `_TIME_SUFFIXES` 全成死代码 | LEFT 合法性实际由下方 `len(next_tokens)!=4` 兜住（已验证索引边界安全） |

**已修复的运维隐患**：

| 位置 | 问题 | 修复 |
|---|---|---|
| `ops/league-daily-predict.sh:23-24` | 只读 `LEAGUE_AUTH_*`，未注入时兜底 `a`/`a`；而 unit `EnvironmentFile` 注入的是 `AUTH_USERNAME`/`AUTH_PASSWORD` —— **生产能跑只因 `.env` 里恰好也是 `a`/`a`**。一旦改密立刻 401 → `exit 2`，整条日管线静默失效 | 改为直接读 `AUTH_USERNAME`/`AUTH_PASSWORD`（`LEAGUE_AUTH_*` 仍可临时覆盖）；缺凭据时**显式告警 + exit 2**（不再拿空串试登录）。已在 Oracle 用 `env -i` 三场景实测：只有 `AUTH_*` → 正确解析 |
| `ops/league-daily-predict.sh` ALERT_LOG | `:61/65` 引用 `$ALERT_LOG` 但 `:181` 才赋值，`set -u` 下未绑定变量会让告警路径自身崩溃 | 定义上移到 `log()` 之后（首次使用之前）。注：该分支仅 webhook 为空时进入，生产 `FEISHU_WEBHOOK_URL` 已配置 → 当前不可达，属预防性修复 |

**凭据清理（公开仓库，最高优先）**：

`gh repo view` 实测本仓 `visibility: PUBLIC`。以下明文凭据已从工作树删除：
`README.md`（`curl -u admin:<口令>`）、`docs/acceptance-2026-09-17.md`（3 处）、
`docs/web-ux-audit-2026-10-11.md`、`docs/project/收尾-处置记录-20260920.md`
（均含 Nginx Basic 口令）、`docs/web/WORKLOG.md`（旧 `AUTH_PASSWORD` 值 + 两把
数据源 key 前缀）。全部替换为环境变量注入写法或"见生产 `.env`"。
**口令本身是否轮换需运维决策**（见文末"待办"）。

**文档订正（本节同时修正了本文档自身的错误）**：

| 原文 | 实际 |
|---|---|
| timer「每日 09:00」（6 处） | `ops/league-daily-predict.timer` 为 **09:30** |
| 「13 处」hot-path（括号分项 3+3+7+1） | 实为 **14** 处 |
| 「e280a3b 之后 7 个 commit」 | 实为 **10** 个 |
| `league-dashboard.service`（7 处） | 仓库无此单元；真名 **`league-web.service`** |
| `/home/ubuntu/league-v2/repo/.env`（2 处） | `/home/ubuntu/DSH/sports-lottery/.env` |
| `dashboard.html:206` | **266** 行（建议 6 体彩面板插入后 +24） |
| 「`web/app/` 不创建」 | 已创建且已接线（`web/app/jc.py::daily_image_vm` + router 透传） |
| 「无 scale-to-zero」vs「容器 scale-to-zero」 | 自相矛盾；本机 systemd，无容器冷启问题 |
| IP `140.83.91.844` vs `140.83.62.161` | 统一为 **`140.83.62.161`**（7 处） |
| 「`news.py` 是 web→ai **唯一**反向依赖点」 | 另有 `ai_analyze.py` 三处函数体内 lazy import（未违反"顶层禁止"，但契约表述已订正） |
| 「全仓 101 个 .py ~12k 行」 | 历史快照值；当前 138 个 .py / ~16.5k 行 |
| `requirements.txt` 声明 `pyyaml` | 全仓无 `import yaml`，属冗余依赖，已移除 |
| `AGENTS.md`「pytest **须** `PYTHONPATH=scripts`」 | pytest 不需要（各测试自注入 `sys.path`）；仅裸 `import core` 需要 —— 措辞已订正 |

**R45 证伪记录（子代理断言被实测推翻，刻意不采纳）** —— 再次印证
"听起来合理 ≠ 成立"：

1. ❌「认证变量错配 → 每日登录**必然** 401 → `exit 2`，整条管线不执行」（原判危害"高"）
   → 实测生产日志 `登录成功` + `退出码 0`，因为 `.env` 里恰好是 `a`/`a`。代码脆弱性成立，**所述后果不成立**。
2. ⚠️「`ALERT_LOG` 时序 → 告警路径自身崩溃、卡片与 sentinel 都写不出」（原判"高"）
   → 该分支仅 webhook 为空时进入，生产 webhook 已配置 → **当前不可达**。时序问题真实，但非"高"。
3. ❌「`scripts/core/jc_gate.py` 0% 覆盖是**风险**」
   → 该模块 docstring 首行即 `DEPRECATED / 未接线 (R15c)`，0% 是**有意归档**。
4. ❌「README 声称 ruff 干净」
   → README **没有**该声明（误归因）。但 `AGENTS.md` 让跑 `ruff check .` 会失败属实（当前 699 条告警）。
5. ⚠️「『推理内核零外部依赖』声明为假」
   → `core/i18n.py:50` 是函数体内 lazy import + 优雅降级，README 自身已注明 → 非虚假声明。

**ruff 基线**：~~`ruff check .` 当前 699 条~~ → **R46 已清零，见下节**。

**测试有效性（审计发现，本轮未修）**：
- `tests/test_jc_view_aggregations.py:106-176` 有 5 个"钳制"测试名不副实
  （1 个 `assert True` + 4 个无断言）
- `tests/test_lottery_ticket_clv_helpers.py:290-296` 用 `except Exception: pass`
  **吞掉断言** → 异常即静默通过（本轮修 lottery 时已顺带移除同类反模式）
- `tests/web/test_no_scripts_import.py` 只遍历 `tree.body`（模块顶层），
  `try`/`if TYPE_CHECKING:` 块内的 import 不命中（实测 4 种注入只检出 1 种）

**R46：ruff 门禁清零（698 → 0）+ 文档声明校验器**

R45 结尾问"是否纳入门禁"，本轮给出答案：**纳入，且已清零**。

| 类别 | 处置 | 说明 |
|---|---|---|
| `E402` 107 → 0 | **修根因**（12 文件） | 本仓把 `from __future__ import annotations` 写在模块 docstring **之前**，导致 docstring 变成无意义表达式（`__doc__` 为 None）且后续 import 全被报 E402。把 docstring 提回文件首句即同时修好两者 |
| `I001`185 / `W292`142 / `F401`88 / `E401`6 / `W605`1 / `F541`3 | `ruff --fix` 自动修 | 439 处。F401 删除前逐类确认 `__init__.py` 门面重导出走 `__all__`（不受影响） |
| `F841`24（全在 tests） | 逐条判读 | 多为 pytest fixture 注入或有意的可读性锚点（`before = ...` 后断言 `after`），删了只会更难读 → tests/ 豁免 F841；**生产代码 F 类现为 0** |
| `W291`/`W293`4 | 手工修 | 中文 docstring 里的行尾空格 |
| `E501`96 / `E701`19 / `E731`8 / `E741`11 | **显式豁免 + 写明理由** | 行宽集中在长中文注释/SQL/f-string，手工折行易改坏语义；`ruff format` 会一次重排 409 个文件（远超本轮范围）。E701/E731/E741 是 ingest/ 与彩票映射表的有意紧凑写法。**F 类与 W 类不豁免**——那是真问题 |

豁免写在 `pyproject.toml` 的 `ignore` + `per-file-ignores`，每条都带决策说明，
避免变成"默默忽略"。现在 `ruff check .` **All checks passed**，新增 F/W/I 问题会挡住提交。

**新增 `tools/check_docs_claims.py`（17 项断言，改文档后重跑即可发现漂移）**

R43/R44/R45 连续三轮栽在"文档声明与代码不符"（timer 09:00 vs 09:30、13 处 vs 14 处、
pytest 须 PYTHONPATH vs 实际不需要）。本脚本把可机械校验的部分固化：
版本号三方一致、timer 时刻与时区、systemd 单元名、部署路径、测试基线 collected 数、
ruff 门禁退出码、dashboard.html 行号、`DEFAULT_N_SIMULATIONS` / `DEFAULT_PAST_DAYS` /
`FEATURE_COLUMNS` 三处常量、仓库无明文凭据。首次运行即查出 8 处漂移（含
`2428 collected` 已随 R46 变为 2429、AGENTS.md 仍称"ruff 非全绿"），修正后 **17/17 通过**。

### v2.5 (2026-10-19) R47 生产流程实测：登录入口缺陷 + 5 类文档漂移

R46 只在**开发机**验证过。本轮按**生产流程**实测（Oracle 上真跑 `league-daily-predict.service`
+ 逐条打公网入口），查出 1 个真缺陷 + 5 类文档漂移。

**生产流程实测（全部实测，非推演）**

| 环节 | 实测结果 |
|---|---|
| 触发 | `sudo systemctl start league-daily-predict.service`（与 timer 同一条路径） |
| 登录 | `登录成功` —— **R45 改的 `AUTH_USERNAME`/`AUTH_PASSWORD` 读取在生产 `.env` 上真能跑通** |
| 三作业 | 足球全联赛 5m15s（`720b2abd96f5` done）/ NBA 15s（`d184831fd2e5` done）/ AI 富化 5m16s（`3f0dbb9bbc01` done） |
| 退出码 | **0**（`Result=success`，`ExecMainStatus=0`），总耗时 10m46s |
| 产物 | 5 个 `ml_model_*.json` 全部刷新到 11:28–11:32 |
| 告警 | 无 sentinel 日志（webhook 已配置，`ALERT_LOG` 降级分支不可达） |

**真缺陷：公网体彩看板没有可用登录入口（P5 / `093dafa` 引入，非本仓 R45-R46 造成）**

2026-10-18 的 `093dafa` 以「取消登录页面（**Nginx auth 保留**）」为由删掉
`static/login.html`、`static/dashboard-login.html`、`web/api.py` 的 `/login` 路由与
「未登录跳登录页」判定。但该前提在生产**不成立**，实测三证：

1. 生产 nginx（`/etc/nginx/sites-enabled/dsh-web`）对 `/dashboard/jc/` 三处 location
   是 **`auth_basic off`**（2026-09-18 起剥离）→ 公网免凭据 200，Basic 框**永不弹出**；
2. `web/auth.py::require_auth` 只认 `lp_session` cookie，**不接受 Basic 头** →
   就算给该路径开 Basic Auth，API 仍是 401；
3. nginx 的 `location = /dashboard/jc/login` 仍 proxy 到已被删的
   `static/dashboard-login.html` → 实测 **404**。

结果：页面 200、数据 API 全 401、前端提示「浏览器会自动弹窗输入账号密码」——那个框
永远不会出现。**已修复**（用户选定方案）：取回两个登录页、恢复 `/login` 路由与
「未登录 → 302 /login」、前端 401/403 跳回登录页（`dashboard.html` + `dailyimage.html`），
`web/api.py` 与测试同步更新。

**5 类文档漂移（全部实测订正）**

| 原文 | 实际 | 处置 |
|---|---|---|
| README 冒烟 `http://127.0.0.1:8077/api/v1/health` | **404** —— 真实路径是 `/health`（不在 `/api/` 下，公网无对应入口） | README + `ops/README.md` + `DASHBOARD_DEPLOY.md` 订正 |
| README 冒烟 `curl -kI .../dashboard/jc/  # 401 / Basic 触发` | **200**（`auth_basic off`） | 订正 |
| README 表「公网入口 …… 走 Nginx Basic Auth」 | 该路径 `auth_basic off`，鉴权是应用层 session | 订正（与同文档「认证与隔离」段自相矛盾） |
| README「Basic Auth 只保护 `/` `/dashboard/` `/resume/` `/stock/` `/admin/`」 | 实测 `/resume/` `/admin/` `/stock/` 已 **404**（2026-10-19 清理失效配置时移除）；实际受保护的是 `/` `/dashboard/` `/cpt/` `/emotion/` `/trade/` | 订正 |
| `DASHBOARD_DEPLOY.md`「systemd：`league-dashboard.service`」+「提供 HTTPS 和 Basic Auth」 | 单元名是 `league-web.service`（R45 漏改此处）；该路径无 Basic Auth | 订正 |

历史验收/审计文档（`docs/acceptance-2026-09-17.md`、`docs/web-ux-audit-2026-10-11.md`）
加**后记**说明「当时的形态」与现状差异，**不改写历史记录**。

**校验器加严**：`tools/check_docs_claims.py` 增 11 项断言（17 → 28），把本轮全部
已知错误写法固化为负向精确串（health 旧路径、`/dashboard/jc/` 的 Basic Auth 误述、
Basic 触发注释、退役单元名、前端「Basic Auth 把关」提示…），并断言登录入口已恢复。
判定改为**行级**：命中错误写法的行若带订正/否定标记（订正/误写/此前/旧写法…）即放行，
避免"订正记录本身"被误判为违规（R46 的触发时刻规则已踩过这个坑）。
改动文档后重跑即发现新的漂移。

测试基线 2358 → **2360 passed / 71 skipped / 0 failed**（2431 collected；登录相关
测试从 3 个「已下线」断言改为 5 个「已恢复」断言）。

### v2.6 (2026-10-19) R47 续：R46 的 isort 引爆生产启动失败（已修复 + 加守门）

**事故**：`cc64d15`（R46 的 ruff 门禁）上线后，**生产 `league-web.service` 一旦重启即
无限失败**：`ModuleNotFoundError: No module named 'core'` → `Restart=on-failure` 循环。
本轮 19:55 重启 web 服务时才暴露（此前该进程是 10-17 16:10 启动的旧代码，一直没重启过）。

**根因**（导入顺序依赖）：

- `web/routers/admin.py` 在**模块级** `from core import observability`（`core` 在 `scripts/` 下）；
- 此前只有 `web/routers/jc.py` 会在模块级把 `scripts/` 加进 `sys.path`，
  而 `admin` 恰好排在 `jc` **之后** → admin 的可导入性**依赖导入顺序**；
- R46 的 isort（I001）把 `admin` 按字母序排到了 `jc` **之前** → `scripts/` 还没进
  `sys.path`，`import web.api` 直接失败。

本地 2358 个测试全绿也照样漏掉：pytest 进程自身的 `sys.path` 已含 `scripts/`，
**根本复现不了生产的启动条件**。

**修复（双保险）**：

1. `web/api.py`：把 `scripts/` 注入 `sys.path` 的代码块提到**所有 `web.routers.*`
   导入之前**（根因修复，任何后续 router 依赖 `core` 都自动成立）；
2. `web/routers/admin.py`：模块内**自举** `sys.path`（与 `web/services/haf.py`、
   `web/routers/jc.py`、`jc_ops.py` 同模式）→ 与导入顺序彻底解耦；
3. **新增守门测试** `tests/web/test_import_contract.py`（3 例）：用**全新子进程 +
   清空 PYTHONPATH** 执行 `import web.api` / `import web.routers.admin`，复现生产启动条件。
   已负例验证：把两个文件回退到 R46 状态，这 3 例**全部失败**且报错与生产完全一致。

**教训**：ruff 的自动排序不只是格式问题 —— 它会改变**导入顺序**，而本仓存在
「靠导入顺序传递副作用（sys.path 注入）」的隐式耦合。**任何 import 重排都必须
在干净子进程里验证可导入性**，这正是本地测试全绿却上线即挂的原因。

顺带记录：生产实际运行的 unit（`/etc/systemd/system/league-web.service`）与仓库模板
`ops/league-web.service` **并不相同**（生产无 `--workers 2`、Description 不同、
且**没有** `PYTHONPATH=scripts` —— 代码侧自举因此是必需项，不是冗余）。

测试基线 2360 → **2363 passed / 71 skipped / 0 failed**（2434 collected）。

### v2.7 (2026-10-20) R48 体彩装载器 `rc=124` 根因修复（数据新鲜度停滞 17h）

告警：`⚠ 体彩装载（cron */10）⚠ 装载器非零退出（124）——去 ops.ingest_log 看 errors，不要盲重试`。

**先说告警指的那张表**：`ops.ingest_log` 里的 `ok=false` 确实有（`parse_none` 14.8 万行，
自 09-29，每文件固定 2 行），但**真正的元凶不在那张表里** —— 撞唯一键的批次在写
`ingest_log` **之前**就 `rollback` 了，错误只落在装载器 stdout（`logs/ingest-cron.log`）。

**根因（Oracle 逐条实测，非推演）**：

| # | 根因 | 证据 |
|---|---|---|
| A | `fact.jc_issue_prize` 撞唯一键 → **整批 7 个 topic 全部回滚** | 首次出现 **2026-10-01T14:30:28Z**；最近一轮 2580 批中 **1805 批（70%）**带 `UniqueViolation: jc_issue_prize_pkey (90,26144,1)`；**最新批单独试跑也撞**（`(90,26145,1)`）⇒ 补跑无效 |
| B | 装载器**无增量水位** → 每轮全量重扫 2602 个 `.done`，420s 装不完 → **队尾永远补不上** | 每轮实跑 **2580/2601**（差 21 个 = 10-19 当天批次）；`fact.jc_offer` 最新 snap_ts 停在 **10-19 06:56**（落后 17h）且缺口随积压自我放大 |
| C | orphan 阶段每轮**重装 15,816 个老文件**（98% 冗余 IO 的大头） | 单批试跑输出 `orphan 装载 n=15816` |

**关键实测澄清**（决定了修法）：撞 PK 的两批内容**逐字相同**
（`stakeAmount "38,715"` / `totalPrizeamount "9,562,605"`），且 `src_hash` 是 **payload 内容
哈希**（不含 `snap_ts`/`fetched_at`，实测同内容跨批同 hash `37929ad2…`）⇒ 这是「同一期被
反复推送」的**幂等问题**，不是数据变更。

**修法**（用户选定：is_current 翻版，治本）：

1. **A 幂等 + 翻版**（`scripts/ingest/jc_issue_write.py::_insert_prize`）：
   同 `(tier, src_hash)` 已在表里 ⇒ `ON CONFLICT DO NOTHING` **幂等跳过**（不抛异常、不掀翻事务）；
   同 tier 但 hash 不同（官方开奖前后换形）⇒ 旧行 `is_current=false` + 新行插入（append-only 保留历史）。
   为此把 `fact.jc_issue_prize` 的 **PK 放宽为含 `src_hash`** —— 旧 PK `(game_num,issue_no,tier_no)`
   让 DDL 注释里 v1.2 的「旧下标行置 false、不许假删」**在结构上无法实现**（同 tier 只能一行 ⇒
   `is_current` 恒 true，实测 167 行全是 true，是**死列**）。迁移脚本：`docs/db/r48_ingest_fix.sql`。
2. **A' 兜底**（`scripts/ingest/jc_topic.py::_load_saved`）：逐条 savepoint 原本只 catch
   `ValueError` ⇒ `IntegrityError` 直接掀翻整批；现在**约束冲突也走保存点隔离**（只废这一行）。
3. **B 水位**（`scripts/ingest/jc_load.py`）：新增 `ops.ingest_marker` 批级台账 ⇒ 每轮只处理
   新到港的批次；**已装载 marker 不再重扫**（`iter_markers` 全量返回是 420s 超时的主因）。
4. **C 跳过已装载**（同上）：orphan 判定加一道「`ops.file_arrival` 里有没有」⇒ 老文件不再重装。
5. **孤儿前置**：孤儿（可能是老数据）先装、marker 批（更新的数据）后装 ⇒ `is_current` 指针单调向新。

**不是本轮引入**：首个 `rc=124` 在 **10-03**（早三轮）；R46 对 `scripts/ingest/` 的改动仅是
import 排序（无逻辑变更）。数据**没丢**（文件都在 `incoming/`，幂等由 `src_hash` 保证），只是延迟。

测试基线 2363 → **2371 passed / 71 skipped / 0 failed**（2442 collected；新增 8 例：水位跳过、
已装载跳过、台账写入、保存点隔离 ×3、prize 幂等/翻版 ×2）。

#### 上线实测（Oracle，2026-10-20；迁移 → 拉代码 → 补跑 → 验收）

| 项 | 修复前 | 修复后（实测） |
|---|---|---|
| cron `*/10` 退出码 | **124**（每轮撞 420s timeout） | **0**（00:30 轮 **5 秒**跑完：00:30:01 → 00:30:06） |
| 每轮处理批次数 | 2602（全量重扫） | **8**（水位：已处理 2594 / 待处理 8） |
| `fact.jc_offer` 最新 snap_ts | 10-19 06:56（**落后 17h**） | 10-19 23:53（**落后 32min**，随到港滚动） |
| `fact.jc_offer` 行数 | 84,450 | **163,590**（补跑 2602 批耗时 11 分钟） |
| 本轮 `UniqueViolation` | 2580 批中 **1805 批（70%）** | **0** |
| `fact.jc_issue_prize` | 167 行 / `is_current` 恒 true（死列） | 174 行 / **同 tier 仅 1 条 `is_current=true`（0 违规）** |
| `ops.ingest_marker` | 表不存在 | 2602 行（2594 ok / 8 失败） |

部署顺序**不可颠倒**：新代码的 `ON CONFLICT (..., src_hash)` 要求新 PK 已生效 ⇒
先 `sudo -u postgres psql -d league -f docs/db/r48_ingest_fix.sql`，再 `git reset --hard origin/main`。

**`is_current` 翻版本轮没触发（如实记录）**：撞 PK 的两批内容逐字相同 ⇒ 实际走的是
`ON CONFLICT DO NOTHING` **幂等跳过**分支；`is_current=false` 行数 **0**、同 tier 多版本 **0 组**
⇒ 翻版路径目前只有测试覆盖，生产这一轮没发生（与"同一期被反复推送"的诊断一致）。

#### R48b（部署后实测追加）：orphan 阶段整段静默回滚 ⇒ 修复不收敛

上线验收时暴露**第四个根因**（被 R48 新加的日志照出来）：

| # | 根因 | 证据 |
|---|---|---|
| D | **orphan 阶段整段是一个事务 + 裸 `except Exception: conn.rollback()`** ⇒ 第一个坏文件把**整段 1239 个文件**的写入全部回滚，且**异常被吞掉**（日志只剩 `orphan 装载 n=1239`，零异常栈） | 00:30 轮 `ops.file_arrival` 行数**两轮不变**（17858 → 17858）⇒ 整段确实被回滚；逐文件复现 **11+ 个**文件 `JSONDecodeError`（上游撕裂 JSON，如 `{"kind": "line", "topic": "lot{"kind": …`，行数对得上但内容被写坏，且与 manifest 的 sha256 **全不匹配**） |

**为什么必须一起修**：根因 C 的「已装载跳过」依赖 `ops.file_arrival` 有记录，而 D 让 orphan 段
的写入**永远提交不了** ⇒ 每轮还是 1239 个文件白跑白丢，**修复不收敛**（也正是 420s 超时的大头）。

修法（隔离粒度从「整段 / 整批」降到「单文件」，与 P0-COLLECT2eB「坏指令不废批」同原则）：

1. `jc_load._load_file`：每个文件一个保存点 ⇒ 坏文件只回滚**它自己**，同段/同批好文件照常落库；
2. orphan 失败清单打进 `logger.warning`（不再静默）；marker 批的文件级失败照旧进 `errors`
   （该批仍标 `ok=false` ⇒ 下轮重试，语义不变）。

连带更正既有测试的旧语义：`test_load_batch_rolls_back_on_exception_and_records_error`
（"一个文件炸 ⇒ 整批回滚"）拆成两例 —— 新的**文件级隔离**例 + 保留的**整批回滚**例
（后者改成"文件清单都拿不到"才整批回滚）。

测试基线 2371 → **2374 passed / 71 skipped / 0 failed**（2445 collected；新增 3 例：
orphan 阶段坏文件隔离 + 失败被报告、文件级回滚到保存点、文件级失败不连坐好文件）。

#### R48b 上线实测（Oracle，2026-10-20 00:38–00:40）

| 项 | R48b 前 | R48b 后（实测） |
|---|---|---|
| orphan 阶段每轮文件数 | 1239（**整段每轮被回滚**） | **21**（只剩真正损坏的文件；坏文件逐个点名进 warning） |
| `ops.file_arrival` 行数 | 17858 → 17858（**两轮不变 ⇒ 整段回滚**） | **19115**（+1257 ⇒ 真的提交了） |
| `fact.jc_offer` 行数 | 163,590 | **179,050**（+15,460 ⇒ 孤儿文件的数据**真落库了**） |
| 那 8 个批次的 ups | 0（整批回滚） | **180–815**（只有坏的那个 topic 失败，其余落库） |
| cron `*/10` 退出码 / 耗时 | 124 / 420s 超时 | **0 / 3 秒**（00:40 轮 00:40:01 → 00:40:04） |

**残留（上游问题，不在本仓）**：全目录扫描出 **26 个 JSON 撕裂文件**（行数对得上但内容被写坏，
且与 manifest 的 sha256 全不匹配）—— 采集机写文件时的并发/截断问题，实测形态如
`{"kind": "line", "topic": "lot{"kind": "line", …`；分布：`lottery_draw` 12、`jczq_offer` 5、
`jclq_result` 3、`jczq_result` 2、`jczq_offer/2026-10-12T11-47Z` 1、`jclq_result/2026-10-11T11-20Z` 1 等，
跨 10-09 → 10-19。**需要采集机重推这些文件**（重推后装载器会自动补上，无需人工干预）；
在重推前，这些文件的对应行永久缺失，其余数据不受影响。


### v2.8 (2026-10-20) 产品侧总纲：竞彩闭环路线图（诊断 + 六项建议 + 度量基线）

**总纲在 `docs/project/产品路线图-竞彩闭环-20261020.md`**（诊断与基线全部为生产实测）。
本段只给结论与索引，细节不重复。

**诊断一句话**：用户真问题是「我该买哪注、凭什么」，但产品现在只能答「赔率多少」和「模型概率多少」——
**A（竞彩盘口）与 B（模型概率）几乎不相交，D（决策推荐）是空的**。

| 症状（2026-10-20 生产实测） | 数字 |
|---|---|
| `/api/jc/gap`（模型 vs 竞彩官方盘口错位榜） | **1 行**（当前在售 23 场里五大联赛只占 1 场，欧冠占 17 场而模型不覆盖） |
| `/api/v1/ticket/sfc` 任选9场 | `n_with_probs = 0`，14 场全 `no_probs` |
| 球队对照 | 竞彩 396 个去重队名，`ref.team` 128 支、含 `sporttery` 键的仅 **21** 支；双队已映射场次 **36/446（8.1%）** |
| 决策层 | `analysis.ev_report` **0 行**；`model.pred_run` 10 行（还是 09-15 的一次性回测） |
| 命中率可信度 | n=16~19/联赛，方向命中率 0.389~0.667 ⇒ **95% 区间宽约 ±0.23，统计上不可区分**；pending 189 场 |

**六项建议**（用户 2026-10-20 全部同意，按依赖顺序）：
1. **模型补欧冠**（覆盖竞彩在售 74%）· 2. **中文队名归一 + 补 `ref.team` 池子**（接通缝）
3. **命中率降级为分层+样本量，主指标换 CLV** · 4. **填决策层（今日推荐单 + Kelly + 竞彩赔率结算）**
5. **用上竞彩的滚动特性（滚动重算 + 异动告警）** · 6. **体彩侧当独立小产品**（不碰模型，可并行）

**与既有计划的关系**：`实施计划-全玩法覆盖` 的 Phase 4 #12「欧冠 ➖ 不做」**本次推翻**（依据见路线图 §6.1：
竞彩在售 74% 是欧冠 + 数据侧无阻 + ELO 有 FIFA 冷启动）；`待审-竞彩球队对照` 的**方案 A（补池）仍未执行**，
本轮接着做（+ 归一化 + 人工确认台）。

**新发现的硬约束**（全部实测，写代码前必看）：API-Football 免费档**只开放 2022–2024 赛季**
（查 2026 报 "try from 2022 to 2024" ⇒ 只能当训练集，当日赛程走 ESPN；ESPN `uefa.champions` 实测可用，
今日 9 场）；`?date=` **必须同时带 `?season=`**；配额 **100 次/天**；
`ref.team.aliases` 是 jsonb 且存的是**外部数据源 id**（不是中文别名）。

**不做**：多用户方案 · 半全场/总进球独立模型（无概率源）· 铺开 34 联赛 · 体彩收益承诺 · 动 core 零依赖。

### v2.9 (2026-10-20) 建议 1 落地：模型补欧冠（`ucl` 进 `LEAGUE_CONFIG` + 队名链 + 低置信降档）

**路线图 `docs/project/产品路线图-竞彩闭环-20261020.md` §建议 1 的代码侧已落地**；
订正与落地清单见该文档 **§6.3**（含 6 条推翻路线图自身判断的实测证据）。本段只给结论。

**结果**：模型现在覆盖**欧冠**（`LEAGUE_CONFIG["ucl"]`），且产出的中文名**逐字等于竞彩官方名**
⇒ `/api/v1/ticket/sfc` 的 `n_with_probs`、`/api/jc/gap` 的错位榜，第一次有欧冠的行。

| 改动 | 落点 |
|---|---|
| 欧冠联赛配置 + 差异化参数 | `scripts/core/leagues.py`（`LEAGUE_CONFIG["ucl"]`、`LEAGUE_DC_RHO=0.08`、`LEAGUE_LAMBDA_MULTIPLIER=3.7`） |
| ESPN 整季取数通道 | `scripts/core/data/fetch.py::fetch_espn_season`（`dates` 不吃区间 ⇒ 按日历年抓；`limit` 必须放大，50 会静默截断 189 场） |
| 完赛状态修正 | `scripts/core/data/parse.py::FINISHED_STATUSES`（补 ESPN 的 `STATUS_FINAL_AET`，原白名单只有 AF 的 `STATUS_FINAL_ET` ⇒ 欧冠 2 场加时赛被误判 in_progress） |
| **队名链（路线图没提、但同样阻塞）** | `scripts/references/team_cn_official.json`（109 条，人工核对）+ `core/i18n.py::to_cn` 把它**前置**到 `COUNTRY_CN`/LLM 缓存之前 |
| 冷启动低置信降档 | `scripts/core/predictor.py::_apply_league_confidence_cap`（置信度钳 0.5 ⇒ 最高 2-star；`confidence_note` 含「冷启动」；预测行带 `low_confidence`） |
| 夺冠模拟按赛制关掉 | `scripts/predict.py::_setup_league_run`（`LEAGUE_CONFIG["ucl"]["monte_carlo"]=False`） |
| web 侧主面板纳入欧冠 | `web/config.py`（`MODEL_LEAGUES`/`MAIN_LEAGUES`）、`web/app/jc.py`（`LEAGUE_KEYS`）、`web/routers/jc.py`（scope 文案）、`web/services/datasource.py` |

**本地实跑 `python scripts/predict.py --league ucl`（2026-10-20）**：产出 **18 场**；
18/18 场 `low_confidence=true`、星级 ≤ 2-star、`confidence_note` 含「冷启动」；
队名逐字对齐竞彩（费内巴切/萨巴赫/罗马/波尔图/曼彻斯特城/曼彻斯特联/巴黎圣日尔曼/国际米兰…）；
`home_en`/`away_en` 保留 ESPN 原名作稳定键；赔率信号 18/18 场可用（ESPN 自带 moneyline/让球/大小球）。

**ELO 冷启动靠历史而非 FIFA**（路线图原判断被实测推翻）：FIFA 排名是**国家队**排名，俱乐部不在其中；
真正的路径是 ESPN 整季历史滚 ELO —— 实测落地后 **111 支队全部脱离 `DEFAULT_ELO`**、107 个不同取值
（巴黎 1637.5 / 皇马 1569.4 / 利物浦 1557.7 / 阿贾克斯 1440.5）⇒ 有区分度。

**DB 侧两件已落库**（2026-10-20，用户点头；明细见路线图 **§6.4**）：
① `ref.league` 增补 `ucl` 行（`docs/db/ucl_league_seed.sql`，幂等）⇒ `ref.league` 5 → **6** 行；
② AF 回填 UCL 2022/2023/2024 三季（3 次请求，配额 100 → 97）⇒ `raw.af_raw` 22 → **25** 块
→ 物化后 `fact.fixture` 5341 → **6048**（ucl **707** 场：2022=214 / 2023=214 / 2024=279）、
`fact.fixture_result` +707、`ref.team` 179 → **267**（+88 新建队行）。
落库自检全绿：af_id 重复 0 组、name_cn 重复 0 组、两队悬挂 0、赛果覆盖 707/707、老行 `name_cn` 被回改 0。
**λ 拟合训练集已打通**（只读实测）：`walk.season_pairs` 给出 `('ucl',2023←2022)` / `('ucl',2024←2023)`，
训练行 203 / 202（各 78 支队），五大联赛的训练行数未受影响。

**两条顺带查实的事实**（避免下一个人走弯路）：

1. **raw 落库与 fact 物化之间没有「自动」入口** —— `ops/league-daily-predict.sh` 只跑预测；
   Oracle 的 `~/bin/jc-ingest-run.sh` 走 `jc_load.py`，是竞彩官方通道，与 AF/FD 的 raw→fact 无关
   ⇒ **每次回填后仍要显式跑一次物化**：`python scripts/store/materialize.py --execute`
   （**v4.5 已补上这个 CLI**；默认 `--dry-run` 预演，零落库。它**不会**被采集 cron 自动触发）。
2. **`ref.team.name_cn` 不参与任何 join** —— 竞彩↔`ref.team` 的 join 键是 `aliases->>'sporttery'`
   （竞彩官方 team id），面板展示名来自 `fact.jc_match.home_cn/away_cn`，`web/` 零处读 `ref.team`
   ⇒ **回改 `name_cn` 不提升映射率、也不改用户可见内容**。想解锁映射只能**新建行 + 填 `sporttery` 键**。

### v4.8 (2026-10-23) 「今日推荐单」页显示**假的无数据**：部署了但服务没重启

用户报：早上跑完采集器，看板仍是「⚠️ 暂无定价赔率快照（未采集） / 暂无推荐单」。

**实测结论：数据完全正常，是页面在说谎。**

| 实测项 | 值 |
|---|---|
| `fact.jc_offer` 的 `had` 快照 | 36520 行，最新 `snap_ts` = **2026-10-23 00:22:48 UTC（北京 08:22）** |
| 守门的 `_FRESH_SQL` 返回 | **非空** ⇒ `age_hours` 不该是 None ⇒ 页面不该说「未采集」 |
| 今日（BJT）荐单 | **3 条**（斯图加特vs门兴 / 阿拉维斯vs马拉加 / 都灵vs蒙扎），`made_at` = **09:30 北京**（= 用户跑 `refresh_now.bat` 那一次） |
| `web.services.recommend.snapshot()` | `n_picks=3`、`offer_freshness.age_hours=1.63`、`stale=false` |
| 真实会话打 `GET /api/v1/recommend` | 同样 `n_picks=3` + 完整 `offer_freshness` ⇒ **服务端是对的** |

**根因链（逐环实测）**：

1. `static/dashboard.html` 是**每次请求现读磁盘**的 ⇒ 10-22 15:44 部署 §6.13.5 后页面**立刻**变新；
2. 而 `league-web` 服务（`uvicorn`，无 `--reload`）**内存里还是旧代码** ⇒ `/api/v1/recommend` 的响应里**还没有** `offer_freshness` 字段；
3. 看板 JS `const fr=d.offer_freshness||{}` ⇒ `fr={}` ⇒ `fr.age_hours==null` ⇒ 印「**⚠️ 暂无定价赔率快照（未采集）**」；
4. 面板 `loadRecommend()` **只在页面加载时拉一次**（`Promise.all([...])`，无定时器）⇒ 用户不按 F5 就一直看旧渲染；
5. **为什么服务没重启**：README 部署链路原来写「systemd `Restart=on-failure` 会自动重启加载新代码」——**这条是错的**：
   `on-failure` 只在**进程崩溃**时触发，**代码更新不会让进程崩溃** ⇒ 服务一直跑旧代码。
   实测：`journalctl -u league-web` 显示最后一次正常重启是 **10-21 15:21**，直到 **10-23 09:11** 才重启 ⇒ 中间隔着 §6.13.5 的部署，
   **页面整整一天半在骗人**。

**修法（三层）**：

- **① 看板自愈**：推荐单面板印「**数据截至 HH:MM:SS · 本面板每 60 秒自动刷新**」+ 加 **60 秒定时器**自动重拉该面板
  ⇒ 不刷新也不会停在旧状态；页面在后台（`document.hidden`）时不刷。
- **② 服务版本可见**：`/health` 暴露 `code_sha`（运行中代码的 git 短 sha）⇒ 「服务落后于仓库」一眼可辨。
- **③ 部署后自检**：新增 `tools/check_service_freshness.py` —— 比对 `/health` 的 `code_sha` 与仓库 `HEAD`，
  不一致 ⇒ **退出码 1** + 打印 `sudo systemctl restart league-web`；并把 README 部署链路里那条**错的**「自动重启」说法订正为「**必须显式 restart**」。

**新增 6 项守门断言**（`tools/check_docs_claims.py`，**359 项**）：`/health` 必须暴露 `code_sha`、
自检脚本必须在库且会比对 `code_sha` 与 HEAD、看板必须印「数据截至」、看板必须自动刷新、
看板冒烟必须断言「数据截至」（`tools/dashboard_smoke.js` 的 `must2`）——否则这条行为没了也照样绿。

### v4.7 (2026-10-23) 保留策略（ops 台账 + 落港目录）+ gaps 口径订正 + 「文件不存在」不再掀翻整批

路线图 **§6.13.6** 有完整排查过程与验收数据。本节只记**结论与必须记住的口径**。

**A. 「`ops.ingest_log` 每天 1 万行」的根因不是表太大，是装载器每 10 分钟重装同一批老文件**

- 实测：最近 24h 写 **10201 行**，其中**没有一个新文件** ⇒ 重复 **52 倍**；
- 水位台账 `ops.ingest_marker` 里 **10 个 `ok=false`** ⇒ 水位（只认 `ok=true`）永不认它们 ⇒ 每轮重跑；
- 而它们失败的原因 = 盘上 **29 个坏文件**（上一轮修掉的 TOCTOU 竞态产出的「两批内容交织」文件）⇒
  `read_lines` 抛异常 ⇒ 整批 marker 失败；
- ⇒ **治本 = 清坏文件 + 修 marker 判定**（实测：1 小时日志 **2412 → 554 行**，降 **77%**；marker 2655 **全部 ok=true**）。
  **只做清理是治标。**

**B. 保留窗口（**数据驱动定的，不是拍脑袋**）**

| 对象 | 窗口 | 依据 |
|---|---|---|
| 落港目录 `incoming/cn-collector` | **30 天** | 职责 = 传输中转 + 排障取证；业务数据早已落库（`fact.jc_offer` 182600 行），且 `ops.file_arrival` 逐文件记「已装载」⇒ 删文件不影响数据完整性 |
| `ops.ingest_log`（纯日志） | **7 天** | 治本后每天只写 ~550 行 ⇒ 一周足够排障；14 天窗口光历史噪音就占 390 万行 |
| `ops.file_arrival` / `ops.ingest_marker`（**台账**） | **37 天** | ⚠️ **必须 > 落港的 30 天**（见 C） |

落地物**全部进仓库**（不靠手敲 crontab）：`docs/db/ops_retention.sql`、`docs/infra/jc-staging-retention.sh`、
`docs/infra/jc-ops-retention.sh`、`docs/infra/install-jc-retention-cron.sh`（**幂等** cron 安装器，
UTC 18:00 / 18:30 = 北京 02:00 / 02:30 夜间低峰）。**坏文件隔离而非删除**（移到 `/srv/league-staging/bad/<戳>/`，
保留证据可回滚）；**台账默认不删**（留着才防将来重复装载）；**只删 ops 台账，绝不碰业务表**。

**C. ⚠️ 踩坑实录（这条最容易再踩）：台账窗口必须 > 文件窗口**

第一版把 `ops.file_arrival` 也按 7 天删 ⇒ **跑完的下一轮 cron 就把 4295 个老文件当「未装载的孤儿」全部重装**
（`ingest_log` 当场 +4295 行）。侥幸没损坏数据（重装命中「同 `src_hash` 幂等跳过」），但**这是运气好不是设计对**。
⇒ 口径改成两个窗口（7 / 37），并把「**台账窗口 > 落港窗口**」写成**机器断言**
（`tools/check_docs_claims.py` 解析两个文件的默认值来判定，不靠注释里写一句「必须大于」）。

**D. gaps 口径订正：「清单里没有该 topic」≠ 缺档**

原来「清单没列某 topic」一律造 `(None, "missing")` ⇒ 凭空记一条 `#MISSING` ⇒ **gaps 虚高**。
实测 `ops.file_arrival` 里 **49 条 gaps 全是这类假缺档**（逐条查清）。
⇒ 现在：**清单压根没列 ⇒ `absent`**（不是缺档、**不落任何痕迹**）；**清单列了但文件读不出来 ⇒ `rejected`**（真缺档）。

**E. 第三个真缺陷：「文件不存在」掀翻整批 ⇒ 永久重跑**

清单列了但文件不在盘上（被隔离/被清理/半传丢）⇒ `read_lines` 抛 `FileNotFoundError` ⇒ 整批失败。
⇒ 现在：**记真缺档 + 留痕（`ingest_log` `ok=false`, `rejected=manifest-file-missing`），但不抛异常** ⇒ 批次能标 `ok=true`。
⚠️ `read_lines` 的「坏行直接抛」契约（ticket P0-COLLECT2f/2g/2q3b）**保持不变** ——
坏文件的正确处置是**装载前隔离**，不是改契约掩盖损坏。

**生产验收**：`ingest_marker` 2655 **全部 ok=true** ⇒ 重复重装停止；日志增速降 **77%**；
**gaps 口径终态 = 10**（修复前的 49 条**全是假缺档**；修复后新口径只认「清单列了、
文件被隔离」的真缺档 10 条，**修复前残留的 17 条假货按 marker 清单逐条判定后清掉**，
实测 FAKE=17 / REAL=10 / UNKNOWN=0）；
`bash ~/bin/jc-ops-retention.sh` ⇒ **rc=0**，`file_arrival` **will_delete=0**（台账窗口 37 天生效）、
`ingest_log` 磁盘 **419 MB**（治本前 1740 MB）；**cron 已装**（UTC 18:00 / 18:30 = 北京 02:00 / 02:30，
幂等复检不叠加）；
业务表行数与修复前**逐字一致**（`jc_match=471` / `jc_offer=182600` / `jc_result=474` / `jc_issue=184` /
`jc_issue_prize=191` / `lottery_draw=214`）。

**⚠️ 顺带挖出并修掉一个「测试污染生产库」缺陷（既有问题，本轮才发现）**：
核对行数时发现 `fact.jc_issue` 从 184 涨到 194 ⇒ 按 `first_seen_at` 定位到新增行
`src_file='f.jsonl'` / `src_hash='h1'` / `game_num='90'` / 随机 `issue_no` ⇒
真凶是 `tests/test_jc_issue_match_prize.py` 的两个真库用例**`conn.commit()`**，
使 fixture 的 `finally: rollback()` **完全失效** ⇒ **每跑一次全量 pytest 就往生产库
塞 35 行夹具垃圾**（与上一轮修的「测试不许写生产仓库文件」同类，上一轮只堵了文件没堵库）。
⇒ **去掉 `commit()`**（断言同事务内读，事务由 fixture 回滚收尾 ⇒ 库里一行不留），
已按 `src_file='f.jsonl'` 精确清掉这批垃圾行，并加守门（AST 判定真库用例不许 `commit()`）。

**新增 43 项守门断言**（`tools/check_docs_claims.py`，**359 项**）—— 重点：gaps 口径三处**走 AST 判定**
（`_from_manifest` 的 state 字面量集合 == `{"absent"}` / `load_topic` 的 `state=="absent"` 早退分支且不写 `ops.*`、不抛异常 /
`load_batch` 空候选兜底 == `absent`）、坏文件不掀翻整批四连、`read_lines` 只许 catch `PermissionError`、
保留策略九项、**台账窗口 > 落港窗口**（机器判定）、cron 安装器两条任务 + 幂等标记块、文档同步三项、
**`load_topic` 三条返回路径的键集合必须一致**（既有契约 `{topic,lines,ups,gap}` —— 本轮**自己踩过**：
absent 分支多返一个 `"absent"` 键 ⇒ Oracle 上那条真包用例 failed ⇒ 连带 **830 个 order-dependent error**）、
**真包测试不许硬编码落港目录里的具体文件名**（保留策略 30 天窗口合法删掉了 `2026-09-16T10-13Z__001.jsonl`
⇒ 那条真包用例直接 failed ⇒ 改成**动态挑「盘上最老的可解析文件」**；断言走 **AST** 并排除 docstring，
因为测试说明里逐字引用了旧文件名，字符串匹配会命中自己的说明）。
新增 `tests/test_jc_load_resilience.py`（**4 条**）。

### v4.6 (2026-10-22) 三项全做收尾：队名漏配（静默）+ `jc_id` 279 纠正 + 采集多日窗口

路线图 **§6.13**。本轮是用户「**三项全做**」的收尾：① 修文档漂移、② 纠正 `jc_id` 279 映射、③ 改采集配置。
**执行中又挖出一个比这三项都严重的缺陷**（① 队名 join 静默漏场次），一并修掉。

**A. 队名 join 静默漏场次 —— 3 个别名缺口 ⇒ 10 场荐单被丢掉，页面上看不出任何异常**

- **根因**：竞彩场次与模型预测按**中文名** join（`scripts/model/archive.py::_match_jc`
  → `scripts/core/team_alias.py::teams_match`）。竞彩侧叫法若模型**永远产不出来**
  ⇒ `model.pred_archive_match.jc_match_id` 留空 ⇒ 不进 `analysis.ev_report` ⇒ 不产 CLV
  ⇒ **页面上没有任何异常信号**。
- **怎么查出来的（可复算）**：把 6 大联赛 **107 个竞彩队名**逐个与「模型侧可产出名集合」对拍
  （官方队名表的值 ∪ LLM 缓存 ∪ `COUNTRY_CN` ∪ 别名表目标名）⇒ **只剩 3 个对不上**：
  巴伦西亚(模型:瓦伦西亚)、云达不来梅(模型:云达不莱梅)、贝西克塔斯(模型:皇家贝蒂斯)。
- **生产影响（⚠️ 部署后逐场对账订正了最初的说法）**：最初写「10 场荐单被丢掉」—— 现象为真，
  但**归因错了**：别名缺口只直接造成 **3 场 / 9 条归档行**
  （2041852 / 2041918 / 2042046）；其余 6 场**根因不是别名** ——
  **4 场早于第一份预测文件**（09-15~09-20 当时没有预测可归档）、
  **1 场对手不在模型覆盖联赛**（2042031 帕德博恩）、
  **1 场是跨联赛杯赛对阵**（2042126 贝西克塔斯 vs 水晶宫 ⇒ 模型按 6 大联赛分组，不产出跨联赛对阵）。
  **今天（10-22）荐单 0 条也不是别名造成的**：当天竞彩入库的 11 场**全是跨联赛杯赛**
  （欧冠 10 + 南美解放者杯 1）⇒ `候选 11 / 有模型概率 0`。
- **修**：`scripts/core/team_alias.py` 与 `web/services/team_match.py` **两侧同时**加 3 条别名
  （键 = 竞彩侧用词，值 = 预测侧用词）；`scripts/references/team_cn_official.json`
  **只在 `_comment` 追加说明、109 键值逐字未动**（diff 验证仅 1 行）。
- **新增** `tests/test_team_alias_mirror.py`（**7 条**，两侧镜像必须一致）+
  **只读复算脚本 `scripts/ingest/jc_name_join_audit.py`**（**退出码 1 = 有缺口**）+ `tests/test_jc_name_join_audit.py`（8 条）。
  **为什么单测不够**：别名表是**两张**，少加一侧 ⇒ 照旧漏场且**零报错** ⇒ 静默失败必须变成**可复算的审计命令**。

**A2. 第二批：审计脚本自己漏了 5 条 ⇒ 口径从「理论能产出」改成「实际产出」**

- **第一版审计报「零缺口」，生产仍在漏**（**假阴性**）：它的判据是「`to_cn()` 四级链的
  **理论可产出名**集合」⇒ LLM 缓存/`COUNTRY_CN` 里**有**「帕德博恩」「美因茨」就判「可达」；
  但模型**实际产出**的是「**帕德博恩07**」「**美因茨05**」（ESPN displayName 带数字后缀，翻译后残留）
  ⇒ 真实 join 仍然不上。
- **修法**：主判据改成「**`model.pred_archive_match` 里模型实际产出过的队名**」，
  理论集合只在**还没有任何实际产出**时兜底；并给每条缺口打印「最像的模型侧名」。
  ⇒ **「理论能产出」≠「实际会产出」** —— 审计必须对着真实产出说话。
- **补的 5 条**：`巴黎圣日尔曼 → 巴黎圣日耳曼`、`帕德博恩 → 帕德博恩07`、
  `托特纳姆热刺 → 热刺`、`美因茨 → 美因茨05`、`赫塔费 → 赫塔菲` ⇒ **别名缺口共 8 条**。
- **生产验收（部署 + 重跑归档后实测）**：归档已关联行 **321 → 436**（总行 1841 → 1911）；
  靠别名桥接才连上的 **83 行 / 19 场**，其中**本次 8 条直接救回 54 行 / 12 场**；
  审计脚本 **rc=0**（108 个竞彩队名全可达）；回溯荐单 dry 从「候选 40 / 达标 26」（§6.6.5 口径，
  **日期窗口不同不可直接相减**）变为 **候选 53 / 达标 38**，达标清单里**确实出现了别名救回的场次**
  （托特纳姆热刺 vs 考文垂、赫塔费 vs 巴列卡诺、皇家贝蒂斯 vs 费耶诺德 …）。
  ⚠️ **荐单/结算表没有手工补跑** —— 刷新由每日 09:30 的定时任务完成（今天那次跑在修复之前），
  手工补跑会插入非 cron 流水行、污染统计口径。

**B. `jc_id` 279 错映射纠正（生产已落地并复核）**

- **真相**：竞彩源 **279 = 贝西克塔斯**（3 场）/**505 = 皇家贝蒂斯**（6 场）/**506 = 毕尔巴鄂竞技**（5 场），
  全库 397 个 id **无一对应多个中文名** ⇒ **错的是我们的映射，不是源**。
- **身份核对用 `af_id`（俱乐部身份真主键），不靠中文名猜**：#2351「毕尔巴鄂竞技」af_id=531 → AF 官方名
  **Athletic Club**；#2355「皇家贝蒂斯」af_id=543 → AF 官方名 **Real Betis** ⇒ **279 应指向 #2355**。
  **不新建行、不动 `aliases`、不动 `jc_id` 生成列**（`ref.team_sporttery` 本就设计成多对一）。
- **错映射的出生证明**：`docs/db/suggestion10_team_sporttery_map.sql` §3 手写
  `INSERT ... SELECT 'football', 279, 2351`，依据是「**279 的对手是马赛、霍芬海姆 ⇒ 欧联 ⇒ 西甲劲旅毕尔巴鄂竞技**」
  —— **从对手反推球队的猜测**，恰是同一份文件纪律明令禁止的「不猜」；而 payload 字面就是「贝西克塔斯」。
- **落法**：`docs/db/suggestion13_betis_id_repoint.sql`（单事务 / 幂等 / 备份 / 前置断言 / 对账输出）
  + `scripts/ingest/jc_team_id_repoint.py`（**读同一份 .sql、不复制语句**；默认**预演 ROLLBACK**，`--execute` 才真跑；
  改前导出 JSON 备份）+ `tests/test_suggestion13_betis_repoint.py`（**18 条**）。
- **生产验收**：先干跑数字全对（wrong_home=0、wrong_away=0、397 个 id 未变、506 未被动）⇒ 真执行成功；
  终态 **279→2355 / 505→2355 / 506→2351**、3 场改指、`ref.team_sporttery` **397 行/397 distinct**、
  `fact.jc_match` **457 行双队全映射 457**、备份表在位可回滚。
  **DDL 源文件已翻过来**（§3 改按 `af_id = 543` 定位并留「原推断错在哪」的痕迹；列注释里的错误例子已改）。

**C. 采集端：65 场赛果永久缺失 ⇒ 4 日窗口 + pageSize 自愈 + 回补 CLI**

- **实测**：`fact.jc_match` 457 场里 **65 场（14%）有 `jc_result` 行但无比分**（`win_flag` 空串）。
  **根因**：只查 `matchBeginDate=matchEndDate=当天` ⇒ **跨日完赛场次次日才发布赛果，被 `matchDate` 过滤永久漏掉**
  （旁证：篮球 topic 早已用 **7 日窗口**，所以只有足球在漏）。
- **代码**（`collector-cn/collector.py`）：占位符**收口成唯一真源** `_date_vars(base_date)`/`_resolve()`
  （原先 3 份重复替换）+ 新增 `{today_minus_2}`/`{today_minus_3}`；
  **`pageSize` 自愈兜底** `_fetch_with_page_fallback()`（失败自动退回 30 并写 `page_size_fallbacks`
  —— 因为 30→100 是否被接口接受**没有验证过**）；**分页截断自检**（单页返回 == pageSize ⇒ 告警 +
  写 `page_limit_warnings`，**不许静默**）；**回补 CLI** `--backfill-range START END`（逐日 × 逐页、
  按 `matchId` 去重、推送与日常批**共用** `_push_paths`）。
- **配置**（`collector-cn/config.json`）：`jczq_result` 由 1 条单日 URL 改为 **4 个单日窗口**
  （today_minus_3 / today_minus_2 / yesterday / today，各自 `matchBeginDate=matchEndDate=该日`）；
  `pageSize` 30 → **100**（带兜底）；**篮球的 7 日窗口未动**。
  用 4 个单日窗口而不是 1 个区间 URL：区间会把多天挤进同一页，逐日查则每天各拿满一页、
  且**跨日场次能在次日被捞到**。
- **契约文档**：`docs/project/国内采集机实施文档-v1.md` §5.2 同步为 **v1.4**（并明确「**多查 3 天 ≠ 违反
  「不要自己往前扫」，一次性回补仍由远端下发区间**」），§5.2.1 补**可照做的升级清单**。

**C2. 采集机就在本机 ⇒ 已直接部署 + 回补（2026-10-22 22:2x，实测）**

- **先纠正一个假设**：本轮一直以为「国内机是另一台机器、只能用户去部署」——
  **实测这台 Windows 就是采集机**（计划任务 `collector_0930/1530/2130/2330/offer_10m` 全部指向
  `E:\2026Workplace\Code\collector-cn\scripts\collector_silent.vbs`；工作副本自带 git 仓库、
  `out/` 6742 个产物、5 个任务在跑）⇒ 已由本会话直接部署。
- **部署**：只换 **2 个文件**（`collector.py` + `config.json`，md5 与仓库版本逐字一致，旧版备份 `*.bak-20261022`）；
  本机副本单独提交 `deploy(collector): 升到上游 v1.4` 作为部署记录。
  ⚠️ 覆盖前 diff 过：两边的 push 配置、**篮球 7 日窗口**、UA/Referer 逐字相同，只差 `jczq_result` 一处；
  上游的 `_stream_local_tar`/`_push_paths` 与本机「scp+sudo → key-only tar 流式」**逐字等价**
  ⇒ 覆盖**不会破坏推送通道**。
- **空跑一轮**：`--collect jczq_result` ⇒ **37 行**（旧版单日只有 **10 行**）⇒ 4 窗口生效；
  `collector.status.json` 里**既无 `page_limit_warnings` 也无 `page_size_fallbacks`**
  ⇒ 接口**接受 `pageSize=100`**（兜底没触发）、没顶到分页上限。
- **回补**：`--backfill-range 2026-09-15 2026-10-21 --backfill-pages 3` ⇒
  **111 次请求、去重后 452 行**、已推远端 ⇒ 远端装载 **ups=452 / errors=[]**
  ⇒ `fact.jc_result` **431 → 462 行**，09-15~10-21 区间 **412/446 场补上比分**。
- **顺带查实**：仍剩 34 场 `win_flag` 空，但 `sections_no_999`（比分）**有值**、`sp_*` **全空**
  ⇒ **官方对这几场没开售胜平负**（已直接查接口原始 payload 核实）⇒ **不是采集缺失**，
  且无 SP 就无法定价 ⇒ 本来就不会进推荐单 ⇒ **不影响结算**。
- **结算打通**：`settle.py` ⇒ **32/32 全部结算**（原 13 已结 + 19 卡住）、pending 0、
  命中 **2 → 7**、ROI **−22.08% → −6.47%**（投入 64 / 回报 59.86 元）⇒ 页面战绩卡同步显示
  （web 层 `record()` 实测 `n_settled=32 / roi=-0.064688`）。⚠️ 仍 n=32 < 100 ⇒ **阈值判据不变**。
- **今日推荐单仍 0 条**（`today_picks` 实测 0）：当天入库 11 场**全是跨联赛杯赛** ⇒ 模型无对应预测
  ⇒ **与别名/采集无关**（不是回归）。
- **顺带发现**：`pageNo=2/3` 返回行数与 `pageNo=1` **完全相同** ⇒ 该接口翻页参数可能未真正生效；
  因单日最多 32 场 < `pageSize=100` ⇒ 本轮无影响，但**将来单日超 100 场时必须先验证翻页**。

**D. 文档漂移修复 + 反向守门**

- **§6.11.3 遗留表两行**仍写「待排期 / 暂不批量做」（**实际早已完成**）⇒ 改为「✅ 已完成」并指向对应章节
  （**保留「写下时确实待办」的历史说明**，不篡改历史）。
- **README 同款旧话两处**（**已逐处加订正标注，不篡改历史记录**）：v4.3 的「补数据属待排期」、v4.5 的「275 个竞彩中文名压根没有行」与
  「一支队两个官方 id = 1 例（279 ↔ 506）」⇒ **加订正标注**。
- **反向守门**：`tools/check_docs_claims.py` 新增断言 —— **§6.11.3 表里若某项在 §6.12+ 已宣告完成，
  那一行不许再写「待排期 / 暂不批量做」**；并锁住 README 旧话不再复现、279 错误例子不再出现在 DDL 注释、
  采集配置必须保持 4 个窗口。
  **「补能力时必须把所有『说它不存在』的地方一起翻过来（包括守门断言本身）」** —— 本轮就是这个模式。

### v4.5 (2026-10-21) 两条遗留收尾：竞彩官方 id 多对一映射 + raw→fact 物化 CLI

路线图 **§6.10**。两项都**先做生产实测再决定怎么做**——量完之后 A 的做法和原计划**不一样**。

**A. 竞彩官方 id「一队多 id」—— 没有照原计划改成数组，改成了映射表**

原计划是把 `ref.team.aliases->>'sporttery'` 从**单值**升级为**数组**。实测后**没这么做**：

| 实测（2026-10-21 生产库） | 数 |
|---|---|
| `fact.jc_match` 场次 / 出现过的 distinct 官方 id | 457 / **397** |
| `ref.team` 行数 / 带 `sporttery` 键 / **数组形态** | 267 / **152** / **0** |
| 397 个 id 里命中 / 未命中 | **152** / **245** |
| 未命中的 245 个 id，其中文名能精确命中 `ref.team` 任何行的 | **0** |
| 全库「一支队两个官方 id」 | **1 例**（贝西克塔斯 279 ↔ 毕尔巴鄂竞技 506，#2351） |

⇒ **映射缺口不是 schema 造成的，是「那一行根本不存在」**（275 个竞彩中文名在 `ref.team` 里压根没有行）。

> ⚠️ **订正（2026-10-22）：上表三条已被后续动作推翻，勿当现状读** ——
> ① **245 个未映射 id 已全部建行**（`docs/db/suggestion12_team_backfill.sql`：244 个未映射 id 批量建行）
> ⇒ 现状 **397/397 全映射**、`fact.jc_match` **457 行双队全映射**（路线图 §6.12.3 A）；
> ② 「一支队两个官方 id = 1 例」这条**本身是一处错映射**，不是数据事实：279 的 payload 字面是
> 「**贝西克塔斯**」，却挂在「毕尔巴鄂竞技」(#2351) 头上 —— 那是**从对手反推球队的猜测**。
> **2026-10-22 已纠正**：279 改指「**皇家贝蒂斯**」(#2355，`af_id=543` = AF 官方名 Real Betis)
> ⇒ 现状 **0 例**，备份表在位可回滚（路线图 §6.13.2）；
> ③ 「275 个竞彩中文名压根没有行」⇒ 现状 `ref.team` **511 行**。
而数组化的 blast radius 很大：`->>` 作用在 jsonb **数组**上返回**数组的 JSON 文本**（`["506","279"]`）而不是 NULL
⇒ 旧 SQL `(aliases->>'sporttery') = ANY(%s)` **一条都匹配不上** ⇒ **152 支队的映射静默全断**
（而这个映射率本身是被观测的指标）；且 `jc_id` 是 STORED 生成列、`team_jc_key` partial unique
只能守**主 id** ⇒ **次 id 失去防撞，比现状更弱**。

**改为（纯增量、零破坏）**：新增 **`ref.team_sporttery`** 映射表

- `PRIMARY KEY (sport, jc_id)` ⇒ **一个官方 id 只能属一支队**，对**所有** id 生效（比数组方案更强）；
- `aliases->>'sporttery'` **语义一字不动**（仍是主 id）⇒ 现有读者零影响；`jc_id` 生成列不动 ⇒ 零 DDL 风险；
- `resolve_jc_teams` 改为「**映射表为准 + `aliases` 兜底**」，且 aliases 侧**同时认字符串与数组两种形态**
  ⇒ 将来真迁数组，**读者已就绪**；
- 新增 `set_team_sporttery()`（幂等、**绝不覆盖既有映射**）。

**实测收益（生产实测）**：救回 **3 个 side**（sid 279 的 2 主 + 1 客）—— 这 3 场原本是
「**单边已映射**」（对手 马赛 / 水晶宫 / 霍芬海姆 本就有行）⇒ 现在**双边都有**
⇒ `fact.jc_match` 双队已映射 **226 → 229**、`only_one` **17 → 14**、`none` 仍 **214**。


**B. raw→fact 物化入口的生产 CLI**

物化函数**早就在**（`store/upsert_fixtures.run` / `store/upsert_results.run`），但**全仓只有 `tests/` 在调**
⇒ 每轮回填后只能手工物化。新增 **`scripts/store/materialize.py`**：

- **默认 `--dry-run`**：真跑一遍**同一套逻辑**、最后 `ROLLBACK` ⇒ 预演数字与真跑**逐字相同**，一个字节都不落库；
- **一个事务包住两个 run** ⇒ 不留「fixture 写了、赛果没写」的半截状态
  （`fact`/`ref` 没有 DELETE，半截比不跑更糟）；
- 输出 **before/after 行数** ⇒ 幂等与否一眼可查；
- 退出码 0 / 1 / 2（成功 / 失败 / 参数或环境错）。

```bash
python scripts/store/materialize.py                 # 预演（默认，不写库）
python scripts/store/materialize.py --execute       # 真物化
```

**守门断言累计 359 项**（`tools/check_docs_claims.py`，359 项；建议 6 再加 21 项、顺手修再加 7 项、
复算脚本再加 16 项、两条遗留再加 21 项、收尾订正 +2 项、**§6.12 事故修复再加 15 项**、
**§6.12.5/6.12.6 赔率口径修正再加 5 项**、**六联赛重训对拍再加 4 项**、**§6.13 本轮再加 30 项**、
**§6.13.5 采集端改手动 + 新鲜度守门 + 原子性再加 33 项**、
**§6.13.6 保留策略 + gaps 口径 + 坏文件不掀翻整批再加 43 项**）—— 重点：映射表主键防撞、DDL 不许改 `ref.team`、
回填只补 NULL、映射表优先于 aliases、`set_team_sporttery` 不覆盖既有映射、缺表必须出声、
物化 CLI 默认只读 + 有 `__main__` 入口 + before/after 对账、§6.10 记录实测数字、
**「回填后仍要显式跑一次物化」这条警告不许消失**（且**不许**再写「生产没有 CLI 入口」这句过期话）、
**静态页内联 JS 不许有跨作用域引用**（`tools/check_js_scope.py`，与 `tsc --checkJs` 逐条对拍过）、
**看板运行时冒烟必须过**（`tools/dashboard_smoke.js`：三个面板真渲染出内容，不许出现「暂不可用」）、
**分区 header 必须跨全宽**（不许再独占一个卡片格）、
**训练集去重不许遮蔽回填的先验**（同键重复必须补齐缺的赔率字段，只补缺不覆盖）、
**路线图 §6.12.5/6.12.6 必须记着「闸门基线也踩了坑」**（0.55 是含平局口径，正确期望要由价格自己算）、
**六个联赛必须全部用修后数据重训且不劣化**（漏跑也要写进文档）、
**in-sample 对拍不许被写成泛化指标**、**服务侧生效路径必须记着**（子进程 ⇒ 进程内缓存不跨次沿用）。

**§6.13 新增 24 项守门**：**§6.11.3 遗留表不许再把已完成的项写成「待排期 / 暂不批量做」**（反向守卫）、
两条已完成项必须标「已完成」、§6.13 必须记着 3 个别名缺口 / 279 的纠正目标 / 65 场赛果缺口 /「改完 ≠ 生效」、
**3 个别名缺口两侧都补上**（`scripts/` 与 `web/` 必须镜像，少一侧就静默漏场次）、
官方队名表 Real Betis 的值**不许被「改对」成贝西克塔斯**、
**队名 join 必须有只读复算审计脚本**（退出码 1 = 有缺口，不许静默）、
suggestion10 的 279 **必须按 `af_id=543` 定位**（不许再写死 team_id）、
**279 重指 CLI 必须读同一份 .sql**（不许复制语句 ⇒ 不会两处改一处）、
采集配置必须保持 **4 个单日窗口 + pageSize=100**、**篮球 7 日窗口不许被足球的改动带跑**、
采集器必须有 **pageSize 自愈兜底 + 分页截断自检 + `--backfill-range` 回补**、
README/路线图**不许再把「补数据属待排期」当现状陈述**（**已订正**）。

**生产验收**（Oracle `0793153` + `3a209c1`，详见路线图 §6.10.4）：

- DDL **先 ROLLBACK 预演再真执行**（预演输出 `UPDATE 2` / `UPDATE 1` / `ROLLBACK` ⇒ 一个字节都没留）；
- `ref.team_sporttery` **153** 行、一个 id 指向多支行 **0**、一支队持多 id **1**（#2351）
  （**2026-10-22 订正：那个 #2351 是错映射，279 已改指 #2355 皇家贝蒂斯，见 v4.6**）；
  **`ref.team` 267 行 / 带键 152 / 数组形态 0（一行未增未改）**、`jc_id` 非空 **152**（生成列未改）；
- `resolve_jc_teams(279,506)` 与 `(506,279)` **都是 (2351, 2351)**、查不到 **返回 None**
  （**2026-10-22 订正：279 现指 #2355，506 仍指 #2351**）；
- 映射率 **both 226 → 229**、only_one 17 → 14、none 仍 214；
- 物化 CLI：`--dry-run` 后 **零写入**；`--execute` 与再跑一次 **fact/ref 行数一行不变（幂等坐实）**
  —— 而 `fd_align` 留痕 0→1→2 是 **append-only 运行日志**（不是重复行，已据此订正验收标准 ④）；
- 采集入口 `~/bin/jc-ingest-run.sh` 实跑 **rc=0**（日志里的 `JSONDecodeError` 是 2026-10-09 就有的既有现象，
  来自 `parse_collector.py`、**不经过** `jc_write`）；
- Oracle 侧单测 **21 passed**、守门 **227 项 / 失败 0**。

### v4.4 (2026-10-21) 建议 6 收尾：零假设检验脚本入仓 ⇒ §6.8.4 的数字「任何人可复算」

**路线图 §6.8.4 的结论已按新口径重写**（**结论方向不变，数字全变**）。
新增 `scripts/research/lottery_selection_check.py`（约 420 行，无 web 依赖，只读生产库）
+ `tests/test_lottery_selection_check.py`（**14 条**）。

```bash
python scripts/research/lottery_selection_check.py                  # 读生产库 fact.lottery_draw（只读）
python scripts/research/lottery_selection_check.py --tsv draws.tsv  # 离线复算（game|issue|date|raw）
```

默认 `--pools 300 --seeds 20`（生产 209 期数据上**实测约 1 分钟**）。

| 项 | 订正后 |
|---|---|
| 合并双尾 p | **0.273**（热）/ **0.323**（冷）/ **0.200**（遗漏）⇒ 仍**与纯随机无法区分** |
| 多重比较 | **20 项**（5 任务 × 4 策略**含 `random` 作自检锚点**）⇒ Bonferroni 门槛 **99.750%**，实际最高 7星彩·遗漏第 **99.3%** ⇒ **不达标** |
| 拆半复现 | **12/20 项方向相反**（非 random 的 15 项里 10 项反向） |

**五个方法学坑，全部踩过并已修**（详见路线图 §6.8.4）：

1. **零分布必须多池** —— 1 个合成池 vs 1 个真实池却用跨 seed 方差当误差棒 ⇒ 无统计效力；
2. **零分布值必须与真实值同类聚合** —— 实测 `random` 策略真实值是 20 种子平均（sd 0.028），
   而「单种子」零分布值 sd 0.123 ⇒ **离散度失配 4.5 倍** ⇒ z 被压到 ±0.2
   ⇒ p 恒在 0.82 附近（**假阴性**）；
3. **留一法自检**（60 个纯 iid 池）⇒ z 均值 **+0.0025** / sd **1.0348** / 百分位均值 **0.4786**
   ⇒ 标准正态（**已固化成单测**）；
4. **百分位分辨率门槛** —— 池 < 100 时某项会被顶到 100% ⇒ 结论翻转 ⇒ 脚本返回码 2 **不给结论**；
5. **畸形任务守卫** —— 大乐透缺后区 2 个号 ⇒ 后区命中率恒 0 ⇒ 纯 iid 上也会报显著（**假阳性**）
   ⇒ 脚本**跳过该任务并打印原因**。

**顺带把「号码池离散度卡方检验」也纳入脚本**（原先是调研期临时脚本算的、不可复算）：
大乐透前区真实卡方 **24.36**（每号期望 6.29 次）vs 零分布均值 **30.00** ⇒ 第 **24.0%** 百分位
⇒ **真实数据比随机更均匀**。**并订正一处理论值**：卡方零分布均值 = **池大小 − 每期个数**
（35−5 = 30），**不是** df = 34 —— 因为每期号码**不放回**抽取、计数向量有负相关（已被单测固化）。

**新增 16 项守门断言**（`tools/check_docs_claims.py`，206 项；建议 6 再加 21 项、顺手修再加 7 项、
复算脚本再加 16 项）—— 重点：脚本与单测必须入仓；三个「不许给假结论」的守卫必须都在；
同类聚合口径必须写死；卡方理论值口径必须写死；web 层不得 import 研究脚本；
§6.8.7 必须指向已入仓脚本（不许再写「未入库」）；§6.8.4 不许残留旧口径数字。

**纪律沉淀**：**结论印在用户可见的页面上 ⇒ 必须有一条人人可跑的复算命令**，
否则那些数字只是作者的断言。

### v4.3 (2026-10-21) 顺手修：两处「无数据被当成 0 / 条件恒假」型缺陷

路线图 **§6.9** 记录了完整排查过程。两条共同教训：
**「无数据」必须显示成「无数据」**（`0%` 与 `N/A` 的差别不是措辞，是事实与幻觉的差别）；
**前端条件要对着后端的真实语义写**。

**A. `/api/jc/daily-image` 的「主面板空」提示条永远不显示**

- 现象：某天五大联赛/欧冠没在售、只有临时赛事时，「每日一图」给出一张**空白主图**
  + 临时赛事图，**没有任何文字解释**。
- 根因（实测）：VM 里 `rows` **就等于** `main_rows`（`web/app/jc.py:247-248`），
  而 `static/dailyimage.html` 的提示条件写的是
  `d.main_rows.length===0 && (d.rows||[]).length` ⇒ **两者同一个数组 ⇒ 条件恒假**。
  **不是「rows=0」的问题，是提示条件写错了对象。**
- 生产实况（2026-10-21）：`sport=football` → `available=True` / `rows=9` / `main_rows=9` /
  `temp_rows=0` ⇒ **当天并不可复现**；`sport=basketball` → `rows=16`。
- 修：条件改看 `d.temp_rows`；**不**改 VM 的 `rows` 语义（老字段不动）。

**B. `favored_by_odds=0` —— 「无赔率样本」被印成「赔率热门从没赢过」**

- 现象：`predict.py` 印出 `"Odds favorite accuracy: 0% (0/0)"`。
- 根因（生产实测）：`references/historical_past_matches.json` 是 **list[76]**，
  样本字段只有 `away/away_abbr/date/home/home_abbr/name/result/score/status`
  —— **没有一场带 `home_true_prob`** ⇒ `total_odds_based=0` ⇒ `odds_accuracy` 兜底 0。
  ⇒ 实际含义是「**根本没有赔率样本**」，不是「热门全输」。
- 违反的纪律：建议 3 定的「**n 不足不显示均值**」。
- 修：`odds_accuracy` 无样本时返回 **`None`** + 新增 `odds_note="no_odds_samples"`；
  `predict.py` 印 **N/A**；`scripts/core/dashboard.py` 模板改预格式化
  （原 `{odds_acc:.1%}` 遇 `None` 会抛 TypeError）；`docs/web/DATA_CONTRACT.md` §2.4 同步。
- 为什么不补数据（**当时**的判断）：历史样本的 `home_true_prob` 要靠**赛前**赔率快照回填，
  旧文件里没有 ⇒ 补数据属待排期（**2026-10-22 订正：已做掉，见下方订正框**），
  **本轮只保证「没有数据时不装作有数据」**。
  - ⚠️ **订正（2026-10-22）**：**补数据后来已经做掉了** —— `scripts/backfill_hist_odds.py`
    回填 **409 条**（629 样本命中 ESPN event 413），`favored_by_odds` 0 → **167**、
    `odds_accuracy` `None` → **0.725**；**并挖出「训练集去重遮蔽先验」这个训练/服务口径漂移并修掉**
    ⇒ 6 个联赛模型全部用修后数据重训。见路线图 **§6.12.3 B / §6.12.5 / §6.12.6**。

**新增 7 项守门断言**（`tools/check_docs_claims.py`，**190 项**）：odds_accuracy 必须是 None、
odds_note 标记、predict.py 必须印 N/A、dashboard 模板不得直接格式化 None、
daily-image 提示条件必须用 temp_rows、VM 的 `rows == main_rows` 契约被单测锁住、
DATA_CONTRACT 口径同步。新增 `tests/test_hotfix_bad_metrics.py`（**7 条**）。

### v4.2 (2026-10-21) 建议 6 落地：体彩侧独立小产品（遗漏值 + 拆法对比，不碰模型）

**路线图 §6.8** 记录了建议 6 的全部实测订正与落地清单。两条验收（① 遗漏值可查可解释
② 复式拆法给出注数与成本对比）都已达成，但**建议 6 原文里「热号/冷号/遗漏值提示」不能做成
选号建议** —— 实测不支持（见下方零假设检验）。

| 改动 | 落点 |
|---|---|
| 服务层纯函数（无 DB/IO） | `web/services/lottery.py`：`omission_stats` / `lottery_pools` / `omission_report`（遗漏值·热号·冷号）+ `compound_splits`（复式/拆单式/胆拖 拆法对比） |
| 新端点（只读，老端点一字未动） | `GET /api/v1/lottery/omission?game=85&n=100`、`GET /api/v1/lottery/splits?game=85&front=1,2,3,4,5,6&back=1,2,3` |
| 看板 | 「彩票开奖」视图新增**独立体彩统计面板**：遗漏值表（每个号码池：当前/平均/最大遗漏 + 出现次数）+ 拆法对比表，**顶部常驻诚实提示** |
| 测试 | `tests/test_suggestion6_lottery_panel.py`（**43 条**） |

**三条必须写明的口径订正**（路线图 §6.8.2 / §6.8.3 / §6.8.4）：

1. **遗漏值按「期」计，不是按「天」计** —— 大乐透 2~3 天一期、7星彩 2 天一期，
   按天算会把「间隔天数」误当「遗漏期数」。口径：`cur`＝距上次出现后未出现的期数、
   `avg`＝总期数÷出现次数＝1/p、`max`＝历史最长**连续未出现**期数。
2. **复式「最优拆法」的真实差别是出票张数，不是成本** —— 一注＝前5+后2 ⇒
   复式注数 = **C(m,5)×C(n,2)**（不是 m×n）；**拆单式与复式覆盖同一批组合 ⇒ 注数与成本必然相同**
   （6/3 ⇒ 都是 18 注 / 36 元，差别是 1 张票 vs 18 张票）；
   **胆拖注数更少但覆盖是复式的真子集**（胆码须全中）⇒ 不是等价替换。
3. **选号能力实测与纯随机无法区分** —— 见下。

**选号能力的零假设检验（建议 6 最要紧的一条，路线图 §6.8.4）**：
零假设＝每期开奖在号码池上 iid 均匀（彩票的物理前提）；方法＝walk-forward
（第 t 期**只用** 0..t-1 选号、验第 t 期命中）+ **300 个合成 iid 池**做零分布
（真实值与零分布值**同类聚合**，两边都取 20 个种子的平均）。
**框架自检**：零分布均值（`random` 策略）＝理论基准 0.7152/0.3328/0.2995/0.5003/0.6682。

| 策略 | 合并证据（跨 5 任务固定效应 z 求和） | 双尾 p | 判定 |
|---|---|---|---|
| 热号 | z = [+1.05, +1.29, +0.50, +0.43, −0.83] ⇒ +1.095 | **0.273** | 与纯随机无法区分 |
| 冷号 | z = [+0.72, −0.11, +0.46, −0.33, +1.48] ⇒ +0.988 | **0.323** | 与纯随机无法区分 |
| 遗漏 | z = [+0.41, −1.19, +1.00, +0.33, +2.32] ⇒ +1.281 | **0.200** | 与纯随机无法区分 |

**多重比较**（5 任务 × 4 策略含 random = 20 项）：Bonferroni 需第 **99.750%** 百分位才可能族内显著，
实际最高是 7星彩·遗漏第 **99.3%** ⇒ **不达标**（且纯随机预期 20 项里就有 0.50 项会到 97.5%）。
**拆半复现**：20 项里 **12 项前后半段方向相反**（连头条数字 7星彩·遗漏也是 1.273/0.533 反向）
⇒ 全部来自前半段，不是稳定规律。**号码池离散度**：大乐透前区真实卡方 24.36
（零分布均值 30.00，95% 区间 17.36~45.36）⇒ 真实值落在第 **24.0%** 百分位，
**真实数据比随机更均匀**，没有「样本偏热」这回事。

**这段结论已可复算**：脚本入仓 `scripts/research/lottery_selection_check.py`
（读生产库或 `--tsv` 离线跑，默认 300 池 × 20 种子，**实测约 1 分钟**），
单测 `tests/test_lottery_selection_check.py`（14 条）把框架自检、
留一法标准正态自检（z 均值 +0.0025 / sd 1.0348）、端到端「纯 iid 不报显著」固化成门禁。

⇒ **页面只做统计呈现，不做任何选号推荐**。规范句逐字出现在三处
（接口 `note` / 看板面板顶部 / 路线图 §6.8.6），守门锁死：

> 本页只做历史统计呈现与组合数学计算，不构成投注建议；热号/冷号/遗漏值只描述已发生的历史事实，实测选号能力与纯随机无法区分。

**本轮又炸出 2 个真 bug**（都是单测抓到的，不是想出来的）：
① `GAME_SPEC` 的 `front_pool/back_pool` 是**上界**（1..35 / 1..12），
而 `pool/special_pool` 是**个数**（0..9 共 10 个 / 0..14 共 15 个）
⇒ 特别号上界写成 `special_pool` 会把**不存在的 15 号**算进遗漏值（已改 `special_pool - 1`）；
② `max` 最初算成「相邻两次出现的索引差」，与 `cur` 的「未出现期数」口径**差 1** ⇒ 已统一。

**本轮的方法学订正**（五个坑，全部踩过并已修）：第一版样本外检验拿「1 个合成池」跟「1 个真实池」比、却用**跨 seed 方差**
当误差棒 ⇒ 无统计效力（热号策略的选号在 seed 间几乎不变，**池与池之间的方差才是主导项**）。
改成 300 个合成池后，零分布均值才对齐理论基准。**第二个坑：零分布值必须与真实值同类聚合** ——
实测 `random` 策略真实值是 20 种子平均（sd 0.028），而「单种子」零分布值 sd 是 0.123
⇒ **离散度失配 4.5 倍** ⇒ z 被压到 ±0.2 ⇒ p 恒在 0.82 附近（**假阴性**）。
**第三个坑：百分位分辨率** —— 合成池 < 100 时某项可能被顶到 100% ⇒ 结论翻转 ⇒ 脚本不给结论。
**第四个坑：畸形数据**（大乐透缺后区号码 ⇒ 命中率恒 0）⇒ 纯 iid 上也会报显著（**假阳性**）
⇒ 脚本跳过并说明原因。另一个坑：初版选号实现有
`set(list(pick)[:5])` 截断（把大号截掉）⇒ 已改为「分组内随机打散取前 5」。

### v4.1 (2026-10-21) 建议 5 落地：滚动重算（错位三分量）+ 赔率异动告警

**路线图 §建议 5 的代码侧已落地**；实测订正与落地清单见该文档 **§6.7**
（原文 2 条「怎么做」：① 拆成两件事、② 的前提是错的；另发现 2 条隐含 bug）。

**一条前提订正（原文是错的）**：原文说「赔率异动告警**复用既有告警 webhook**」——
**既有 webhook 并不存在**（全仓无、crontab 无、systemd timers 无）⇒ 必须**新建**推送通道。
另一条：告警口径**不能用**「开盘 vs 临场」的首末差（实测 ≥5% 命中 **77%**，纯噪音），
必须用「**相邻快照的跳变**」（实测 ≥5% 命中 0.405% ⇒ **≈3.2 组/天**，按场次聚合的推送粒度）。
⚠️ 窗口最终取 **4~20 分钟（240~1200 秒）**而不是「恰好 10 分钟」—— 实测采集间隔并不恒定
（1/3/4/5/7/8/9/10/20/23/60/97/120 分钟都有），卡死 10 分钟会漏掉 45% 的场次；
而 **> 60 分钟间隔的 ≥5% 命中高达 9.868%**（隔夜/重开重定价 ⇒ 假异动）⇒ 必须设上限。

| 改动 | 落点 |
|---|---|
| 错位三分量 | `scripts/market/gap_calc.py`：`gap_decide`（决策时错位，**固定**）/ `gap_now`（当前错位，**滚动**）/ `drift_pp`（漂移，pp）。决策时盘口取 `snap_ts <= 归档 run_at` 的**最后一版三项齐全的 had**；`p_model` 取 `model.pred_archive_match` 的归档概率（**精确匹配 `jc_match_id`**，不用市场先验） |
| 异动检测 | `scripts/market/odds_move.py`：相邻跳变检测（只认 **240~1200 秒**即 4~20 分钟的间隔，跨批次/隔夜重开不算）→ 幂等落 `analysis.odds_move`；**每侧一行**，**按场次聚合成一条**推送（不刷屏）；**只推没推过的**（`--repush` 才重发） |
| 推送通道 | `scripts/market/notify.py`：**只从环境变量 `FEISHU_WEBHOOK_URL` 读**；未配置 ⇒ `{"ok": false, "reason": "not_configured"}` ⇒ `pushed` 恒 false，**绝不谎报已推** |
| DDL | `docs/db/suggestion5_schema.sql`：`analysis.odds_move` 新表（15 列，PK `(match_id, play_type, option_code, prev_ts, curr_ts)`，3 索引，幂等，**既有表零改动**） |
| 只读视图 | `web/services/odds_move.py`（事件 + 运维视图）、`web/services/gap.py`（三分量附加）、`web/services/notify.py`（**顶层不 import scripts**，函数内延迟 import） |
| 接口 | `GET /api/jc/gap` **增量**加 `gap_decide`/`gap_now`/`drift_pp`/`p_decide`/`p_now`（**老字段一字未动**）；**新端点** `GET /api/jc/odds-move`；**运维端点** `GET /api/jc/ops/odds-move`（阈值/命中量/推送状态，**只回布尔值不回 URL**）；`GET /api/jc/movement` 加**可选** `window_days`（**缺省行为不变**） |
| 看板 | 「盘口变动」区加**赔率异动事件表**（跳变 % + **已推/未推**标记 + 运维计数）；错位榜加**三分量列** |
| 修 bug | `/api/jc/movement` 的「开盘价」原来取**第一版快照**，而实测 **405/406 场第 1 版 `options` 是 `{}`** ⇒ delta 全是 None（页面「有快照却无变动」）⇒ 改取**第一版三项齐全的 had**；全场都没有齐全版本时退而取第一版**有有效值**的快照（老口径单边 100%，与历史 ABI 兼容） |

**三条必须写明的硬约束**（路线图 §6.7.3）：

1. **告警是「信息」，不是「信号」** —— 盘口跳变大 **≠** 模型有价值。§6.6.5.1 实测：当前 EV 的主要成分是
   **跨市场定价差**，且推荐侧系统性偏向长赔侧，而单边投注要先扛 **~25~33% 的水位**
   ⇒ **盘口异动本身不构成下注理由**（告警文案与页面都带这句，守门锁死）。
2. **没有 webhook URL ⇒ 只落表 + 页面可见 + `pushed=false`** —— 绝不假装「已推送到人」
   （建议 3/4「静默失败」教训的同款防线）。
3. **异动事件落表才是资产** —— 每条存（场次/侧/前后两版 `snap_ts`/前后 SP/跳变幅度/阈值/是否已推），
   **任何人可用同一公式复算**。

**新增 38 项守门断言**（`tools/check_docs_claims.py`，190 项；建议 6 再加 21 项、顺手修再加 7 项）—— 重点 6 条：
① **看板内联 JS 必须语法合法**（`node --check`）—— 本轮实测踩到：往 `dashboard.html` 插函数时
少写一个大括号 ⇒ 整个内联脚本解析失败 ⇒ **看板所有交互全挂**（页面还"能打开"，只是什么都不响应）；
② **仓库任何文件都不得含飞书 webhook URL**（凭证只走 env，不入代码/文档/commit/日志）；
③ **未配 webhook 不得谎报已推** + **告警文案必须带「不是下注理由」**；
④ **推送标记逐条精确**（不许 `ANY` 的笛卡尔积）+ **推送必须去重**（检测窗口 24h，
不去重就会每 10 分钟重推同一条 ⇒ 刷屏）；
⑤ **落表 SQL 与 DDL 对账**（生产首跑实测：INSERT 写了 `RETURNING move_id`，而表里
**根本没有这列** ⇒ 每次落表都抛 `UndefinedColumn` ⇒ 异动一条都进不去；
单测用**假连接**不执行真 SQL ⇒ 验不到这一层）；
⑥ **全仓 UTF-8 BOM 扫描**（实测踩坑：脚本传到生产机时管道给加了 BOM ⇒
`#!/usr/bin/env bash` 变成 `﻿#!/usr/bin/env bash` ⇒ bash 每轮都报错）。

**生产落库与验收实测（2026-10-21，路线图 §6.7.7）**：

| 项 | 实测 |
|---|---|
| 建表 | `analysis.odds_move`：**15 列** / 复合 PK / 4 索引 / `league_app` 可写 `league_ro` 可读 **`league_ing` 无写权** / 既有表零改动 |
| 30 天回填 | 相邻对 **23,952** → 异动 **123 侧 / 54 场** → 落表 **123 行**（rc=0）；**123/123 行可复算**（最大误差 0.00005） |
| **真实飞书推送** | 4 侧 / 3 场 ⇒ `ok=true / http=200 / success`；库中 **4 行 `pushed=true`**、其余 119 行 `pushed=false`；**重跑验证不重复推** |
| 端点 | `/api/jc/odds-move` 200（字段齐）、`/api/jc/ops/odds-move` 200（`n_events=123` / `n_pushed=4` / `push_configured=true`）、`/api/jc/movement?window_days=7` 200（**117 行 delta 全有值** ⇒ 开盘价 bug 修复在生产生效） |
| 错位三分量 | 78 场有当前错位、**47 场**同时有决策时错位 + 漂移（seriea 2041922：`gap_decide=0.0667` / `gap_now=0.1027` / `drift_pp={h:+4.95, d:−2.18, a:−2.77}`pp） |
| 采集钩子 | `/home/ubuntu/bin/jc-ingest-run.sh` 装载后跑异动检测（**不另开 cron**）⇒ 脚本 rc 仍为 0；顺手把上游 10:02 新批次入库（`fact.jc_offer` 180,655 → **180,760**） |
| 凭证 | webhook 只进生产机 repo `.env`（**gitignored**），不入代码/文档/commit/日志 |

**本轮首跑又炸出 3 个真 bug**（都是「跑完了不等于跑对了」家族，详见 §6.7.7.2）：
① `RETURNING move_id` 引用了**表里没有的列** ⇒ 每次落表都抛 `UndefinedColumn`、一条都进不去
（**假连接单测验不到** ⇒ 已加「DDL 与 SQL 对账」的单测 + 守门）；
② 推送标记用 `ANY` 的**笛卡尔积** ⇒ 会把没推的行标成已推（已改逐条精确标记）；
③ 推送**不去重** ⇒ 每 10 分钟重推同一条（已加 `already_pushed` + `--repush`）。

### v4.0 (2026-10-21) 建议 4 落地：决策层（今日推荐单 + Kelly + 竞彩官方赔率结算）

**路线图 §建议 4 的代码侧已落地**；实测订正与落地清单见该文档 **§6.6**
（4 条「怎么做」里 2 条照做、1 条改口径、1 条补原料，外加 3 条必须诚实标注的硬约束）。

| 改动 | 落点 |
|---|---|
| 今日推荐单 | `scripts/decision/recommend.py` → `analysis.ev_report`（一行 = 一条推荐腿）；候选 = **竞彩当前在售**场次 ∩ 模型有概率 ∩ EV 超阈值，**每场最多推一侧** |
| Kelly / EV 唯一真源 | `scripts/market/kelly.py`（分数 Kelly + EV + 1/4 Kelly 建议注额）；`web/services/combo.py` **改为委托**它，不再自己写一份 |
| 结算与战绩 | `scripts/decision/settle.py` → `analysis.rec_settle`；结算价取 `fact.jc_result.sp_home/sp_draw/sp_away`（**竞彩官方结算 SP**），命中取 `win_flag`（缺失用比分） |
| 接口与看板 | 新端点 `GET /api/v1/recommend`；看板「今日推荐」视图加「今日推荐单」卡，「复盘」视图加「推荐单战绩」卡 |
| DDL | `docs/db/suggestion4_schema.sql`：`analysis.rec_settle` 新表 + `analysis.ev_report` 加 7 个**可空列** + 幂等唯一键（既有 8 列语义不动） |

**三条必须写明的诚实标注**（路线图 §6.6.3）：

1. **EV 里混着「跨市场定价差」** —— 本模型的概率是**市场锚定**的（ESPN 赔率 + onside + ELO + 形态加权），
   所以 `EV = p_model × 竞彩SP − 1` 不等于「模型有同等真实优势」。项目手册早已写死同一条纪律
   （「现在这些 p **不许直接进 EV/Kelly**」）。落地方式：**阈值保守**（EV ≥ 5%）、**1/4 Kelly**、页面标注。
2. **推荐单依赖当天的预测运行** —— 归档与推荐单都在 `predict.py` 跑完时写入（生产 timer 09:30），
   ⇒ **09:30 之前看板是空的**（节奏，不是故障）。
3. **ucl 是冷启动联赛**（置信上限 0.5、最高 2-star）⇒ 在售场次里 8 场是欧冠，推荐单里必须标「低置信」。

**口径纪律**：战绩按**固定注额**统计（Kelly 是「建议」，战绩要可比就得用统一注额）；
**n 不足（< 5）不给 ROI** —— 与建议 3 同一套纪律：宁可空白不误导。

**生产首跑实测（2026-10-21，路线图 §6.6.5 / §6.6.6）**：

| 项 | 实测 |
|---|---|
| 回溯推荐单 | 候选 **40** → 达标 **26**（**达标率 65%**）→ `analysis.ev_report` **26 行**（6 个 BJT 日；同场同日推多侧 **0**） |
| EV 分布 | min **+0.0608** / 中位 **≈ +0.22** / max **+1.2215** ⇒ **EV ≥ 5% 这个阈值基本不起过滤作用** |
| 结算与战绩 | 可结算 **13 / 26**（其余赛果未出 ⇒ pending、**不计未中**）、命中 **2 / 13**、投入 26 元、回报 20.26 元、**ROI −22.08%** |
| HTTP 层 | `GET /api/v1/recommend` **200**；BJT 今日正确；二次请求一致（纯只读）；无鉴权 **401**；n<5 时 `roi=null` |

**两个结论（别被 ROI 数字误导）**：

1. **负 ROI 的机制是「竞彩水位」，不是「选边选错」** —— 生产实测对照（13 场已结算）：
   推荐侧每元回报 **0.779**，而「三注等额、不选边」的无偏基线只有 **0.751**
   ⇒ 推荐侧**方向上没错**（不比瞎选差），但**竞彩 had 三侧含水约 25~33%**，
   单边投注要先扛掉这笔水 ⇒ **EV 必须显著高于 ~33% 才谈得上盈利**，
   而当前 EV 中位仅 **+0.22** ⇒ **差着一个数量级**。
   顺带量化出一个**结构性偏差**：13 条里 **10 条推的是「该场赔率最高的一侧」**（0 条推最低），
   推荐侧平均 SP 4.458 vs 三侧平均 3.296 ⇒ 模型 p 与竞彩 SP 不同源时，
   **EV 最高的侧会系统性偏向长赔侧**，而长赔侧边际命中概率最低。
2. **当前口径下这套推荐单不具备可证实的盈利能力** —— n=13、命中 2、ROI −22.08%。
   样本太小 ⇒ **既不能断言策略无效，也不能声称它有效** ⇒ 页面/文档一律按「**未验证**」呈现。
   **阈值暂不校准**（把 EV 门槛调高只是「挑跨市场定价差更大的场」，不等于挑到模型更有优势的场），
   等样本 **n ≥ 100** 再重做校准，判据是「EV 是否稳定高于水位」。
2. **首跑暴露 1 个「静默失败」型真 bug（已修）**：`store.pg.connect()` 用 psycopg3 默认 `tuple_row`
   ⇒ 行是**普通 tuple**，而 `load_sale_matches` 写的是 `dict(r)` ⇒ `TypeError: object is not iterable`
   ⇒ **「今日推荐单」自上线起一直是空的**（`settle.py` 侥幸没炸：它全程用位置索引）；
   失败又被 `logger.warning` 吞掉 ⇒ 无人发现。
   修复 4 处：位置索引 / settle 与 recommend 拆开且失败记 `logger.error` + `log_error` / 补 3 条 tuple 行回归测试 /
   加 3 条守门（AST 扫 `fetchall()+dict(行变量)` 形态、`load_sale_matches` 必须位置索引、决策层失败必须可见）。
   **全仓同类排查**：只有 3 处 `dict(row)`，另 2 处显式设了 `cur.row_factory = dict_row` ⇒ 无第二处隐患。

### v3.0 (2026-10-21) 建议 3 落地：命中率改「分层 + 样本量 + 区间」，主指标换 CLV

**路线图 `docs/project/产品路线图-竞彩闭环-20261020.md` §建议 3 的代码侧已落地**；
订正与落地清单见该文档 **§6.5**（含 1 条被实测推翻、1 条被改写）。

**结果**：命中率不再是单一直播数字（每个比率都带 **n** 与 **Wilson 95% 区间**，并按星级/置信度分层）；
主指标换成 **CLV**（模型概率 vs 竞彩收盘去水概率），看板有日更记录。

| 改动 | 落点 |
|---|---|
| 分层命中率 + 样本量 + 区间 | `web/services/accuracy.py`（`wilson_interval` / `league_accuracy_stratified`）；端点 `GET /api/v1/accuracy/stratified`（**增量**，`/accuracy` 老字段一字未动） |
| 每次预测落库归档 | `scripts/model/archive.py`（写 `model.pred_archive` / `model.pred_archive_match`）+ `scripts/predict.py::_archive_league_output`；历史预测文件回填 `scripts/model/archive_backfill.py` |
| CLV 计算 | `scripts/market/clv_calc.py`（收盘取 `fact.jc_odds_history` 的 `max(seq_no)` = 开球前最后一版；CLV 复用现成的 `market/clv.py::clv`）；端点 `GET /api/v1/clv`。**`p_model` 只取预测行的 `probs.h/d/a`（模型最终输出）**，不取顶层 `*_true_prob`（那是市场先验，用它算 CLV 是循环论证） |
| `data_window` 口径 | 每条预测行新增 **`match_date`**（BJT 比赛日）；顶层 `data_window` 仍是「取数区间」，语义写进契约 |
| 队名归一（CLV 关联用） | `scripts/core/team_alias.py`（别名表**唯一真源**，与 `web/services/team_match.py` 同语义） |
| BJT 日口径 | `scripts/core/bjt.py`（与 web 层 `bjt_today()` 同一时区，避免两侧差一天） |
| 新增 DDL | `docs/db/suggestion3_schema.sql`（4 张表，幂等：`model.pred_archive` / `model.pred_archive_match` / `analysis.clv_match` / `analysis.clv_daily`） |
| 前端 | `static/dashboard.html`：命中率加「分层命中率（30 天 · Wilson 95% 区间）」块；复盘视图新增「CLV · 收盘价值」卡片 |

**一条必须写明的限制**（实测，不是推测）：**存量那 175,200 行 `analysis.backtest_market` 永远算不出 CLV** ——
它的 `fixture_id` 指向 `fact.fixture`（API-Football 的 2022–2024 赛季回测），
而竞彩盘口是 2026-09-15 之后的**当日**场次，**两者日期零重叠 ⇒ join 实测 0 行**。
⇒ CLV 走的是**新管线**（先归档 → 再按收盘快照算），样本从管线启用当天起累积。

**样本不足不显示均值**：CLV 日汇总与命中率分层都带 `n`；`n < 5` 时标 `insufficient`，
前端**不显示均值**（宁可空白不误导）—— 与 §1.2「n=18 的命中率不可信」同一条纪律。

**生产落库与首算（2026-10-21 实测）**：4 张新表建成（既有表一行未动）；
回填历史预测 72 次运行 / 1592 场，其中 **312 场**关联上竞彩；
CLV 首算：候选 312 → 可算 **71** 场 → 落 **213 行**明细 + **31 行**日汇总，
**整体 CLV 均值 +0.0498**（客胜 +0.176 / 主胜 +0.061 / 平局 -0.087）。
分层命中率 30 天窗口：epl 20 / laliga 19 / seriea 20 / bundesliga 18 / ligue1 18 场，全部带 Wilson 95% 区间。

**首跑暴露 5 个「静默失败」型 bug**（不报错、退出码 0、页面空白）：SQL 选了不存在的列、
naive/aware 时间戳直接比、`p_model` 误取市场先验、CLI 漏 `__main__` 入口、
整体均值被逐行 insufficient 抹掉 —— 全部已修并补测，详见路线图 **§6.5.7**。
**教训**：「跑完了」不等于「跑对了」——纯函数单测一条都没抓到这 5 个。

**顺带修掉一处文档编码损坏**：路线图里「怎么做」三个字曾被写成三个 U+FFFD 替换字符
（commit `1e4fdac` 引入，肉眼极易漏过）。已修复，并给守门加了**全仓 U+FFFD 扫描**
（`tools/check_docs_claims.py`，当时 124 项）⇒ 这类损坏以后进不了库。

### v2 (2026-09-18) 代码审核修复：资源可靠性与逻辑正确性

对 scripts/ 与 web/ 全仓（**当时**101 个 .py，~12k 行；审计 R45 时已增至 138 个 .py / ~16.5k 行）做了**结构审查**（健壮性 / 模块化 / 死代码 / 逻辑错误），
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

### v2 (2026-09-18) 缺陷修复：ESPN 赔率富化 + 取数窗口 / 命中率连接键 / 蒙特卡洛

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

### v2 (2026-09-15) 变更日志
- 新增 `scripts/derive/`（网格与五玩法派生，纯 stdlib）、`scripts/store/`、`scripts/ingest/`（落库与取数）、7 个常驻测试文件
- 新增 DB 底座：`league` 库 + `ref/stg/ops/raw/fact` 五 schema + `league_ing/app/ro` 三角色最小权限 + `ts_snap/ts_stg` 表空间
- 修正文档：`fetch.py` 关于"免费档不支持 season 过滤"的注释已失效（内核冻结未改，本 README 与 v2 段为准）

## v4.x — 引擎算法版本（历史）

### v4.3 变更日志

#### 变更 (P5)
- **移除 MLS 预测**: 从 `LEAGUE_CONFIG` 及 ρ/λ 映射中移除美职联，`--all` 不再运行
- **中超队名中文化**: `COUNTRY_CN` 新增 15 支中超俱乐部中文映射（青岛海牛、上海申花、浙江队、成都蓉城等）
- **GHA 触发时间**: 调整为每日 21:33 (BJT)

### v4.2 变更日志

#### 修复 (P0)
- **ESPN 403 修复**: ESPN 封锁 `Mozilla/5.0` 与 `LeaguePredict/4.1` 等 User-Agent，改用 `python-requests/2.31`，恢复 ESPN 数据源可用性（MLS/中超降级路径）

#### 增强 (P5)
- **.env 自动加载**: 支持在项目根目录 `.env` 写入 `API_FOOTBALL_KEY` / `FOOTBALL_DATA_API_KEY`，启动自动加载，免手动 export

### v4.1 变更日志

#### 致命修复 (P0)
- **ELO 公式符号**: `expected_score` 指数项 `(effective_a - elo_b)` → `(elo_b - effective_a)`, 修复强队系统性低估
- **Dixon-Coles 负概率**: `tau_correction` 加 `max(0, tau)` 钳位, 防止 1-1 比分时 ρ×λ_h×λ_a 超限
- **Calibration 双重修正**: Onside 信号使用专用 `onside_home_correction` 减半因子, 不再与 market odds 重复修正

#### 高优先级 (P1)
- 测试 mock 字段名统一 (`home_prob` → `home_true_prob`)
- 移除 MyMemory 翻译 API (COUNTRY_CN 字典已覆盖)
- Backtest 移除硬编码数据源检查, 新增 `_backtest_api_football()`
- Monte Carlo 标准世界杯淘汰赛对阵表 (A1vB2 模式)
- Fetch API 响应结构校验 (`_validate_api_response`)

#### 中优先级 (P2)
- `ELO_WEIGHT` 移入 `THRESHOLDS` 配置化
- FIFA 排名获取统一委托 `rankings.py`
- `COUNTRY_CONFEDERATION` 重复键清理
- API-Football 速率限制追踪 (`_rate_limit_info`)
- 日志级别环境变量支持 (`LEAGUE_PREDICT_LOG_LEVEL`)

#### 低优先级 (P3)
- `__import__` 改顶部 import
- 线程安全问题随 MyMemory API 移除消除
- ELO 测试覆盖 20 个用例
- `save_results` 改合并策略

#### 重构 (P4)
- P-label 注释清理
- `run_league` 拆分为 6 个子函数
- `config.py` 拆分为 `constants.py` + `leagues.py` + `config.py` (re-export)

#### 增强 (P5)
- ML 特征管线标记为实验性
- Cache 补全 TTL 过期清理 + URL 键生成 + `purge_expired()`
- API-Football + ESPN fallback 并行获取 (`ThreadPoolExecutor`)
- JSON→SQLite/Parquet 评估: 数据量 ~95KB, 无迁移必要

## 国内采集机（cn-collector，v1.3 契约）

本仓库 `collector-cn/` 子目录是国内采集机（cn-collector）的代码根（v1.3 契约，独立工具：取官方 JSON → 落 JSONL → 推远端，仅 stdlib，不连 DB）。
完整说明见 `collector-cn/README.md`（运行环境、计划任务、远端通道、节奏、契约、红线）。

- 代码根 = `collector-cn/` 子目录；Windows 机器 `ND-PC-WIN` 本地运行副本 = `E:\2026Workplace\Code\collector-cn`（与本子目录同内容，git 跟踪在本仓库）
- 计划任务 5 个：`collector_offer_10m`（2026-09-17 起 10 分钟→1h，2026-09-21 再降为**每 2 小时**）+ 4 个 daily 档（0930/1530/2130/2330，8 topic 含 `jc_odds_history`）；全部 BootTrigger+LogonTrigger+漏档补跑
- **2026-10-22 起本机 5 个计划任务已停用，改「按需手动」**（采集机是工作电脑，不是 24h 开机）：
  - 依据（近 7 天实测，`jczq_offer` 117 次相邻批比较）：**54% 的批次完全空转**（与上一批逐字相同）；07–09 时 36 批只有 1 批有产出（2.8%）；新增开售 111 场全部落在 11–17 时且一天只有一次「开售潮」；赔率变动集中在赛前 17–24 时（21 时 81% 的批次有变化）
  - **机制完整保留**（任务定义 XML + `collector_silent.vbs` + config 一字未改）；恢复命令见 `collector-cn/scripts/tasks_backup/restore_scheduled_tasks.ps1`（需管理员）
  - **主路径 = 双击 `collector-cn/scripts/refresh_now.bat`**：采集+推送 → 远端装载 → 出荐单 → 结算，一条链跑完（日志 `logs/refresh_now.log`）
  - **配套新增「定价赔率新鲜度守门」**（`scripts/decision/recommend.py`）：手动模式下「忘了先采集就出荐单」是新的静默失败路径（页面看着完全正常）。全局闸：全库 `had` 最新快照超 6h ⇒ 拒绝出单（退出码 3，打印修复命令，`--allow-stale` 可显式放行并在 note 留痕）；逐场闸：该场快照超龄 ⇒ 跳过该场。页面同步显示「定价赔率快照 X 小时前」，过期标红
  - **顺带修掉一个数据丢失级缺陷**：篮球赛果 `jclq_result` 的 pageSize=30 时接口 `total=42 / pages=2` ⇒ **每批只取第 1 页 30 场、漏 12 场真实比赛**（埃菲斯vs拜仁、巴黎vs博洛尼亚、皇马vs阿斯维尔 等）⇒ 已改 pageSize=100（重采实测 30 → 42 行）
  - **再修一个文件损坏级缺陷（2026-10-23 查日志时挖出）**：采集端**两处 TOCTOU** ⇒ Boot+Logon 把多个计划任务在同一刻拉起时（实测 21:49 一次拉起 4 个），多个批次会**同时拿到单例锁**、给同一 topic 选到**同一个文件名** ⇒ 两个进程各写一半 ⇒ **文件内容交织、变成非法 JSON**（远端装载报 `JSONDecodeError`，坏文件留在盘上反复报错）⇒ 两处都改用 **O_EXCL 独占创建**（`open(path,"x")`），回归测试 `tests/test_collector_atomicity.py`（并发写不撞名 + 4 个真进程抢锁只有一个赢家）
  - **再补第三处 TOCTOU（同日，回归测试自己抓出来的）**：**「锁文件已存在」与「读得到有效 pid」不是同一时刻** —— 创建者 `open(lock,"x")` 建好锁后还要**再写一次 pid** ⇒ 中间有「文件已存在、内容还是空的」窗口 ⇒ 竞争者读到 `0` 若按「旧 pid 已死 ⇒ 接管」处理，会**覆盖创建者的 pid 并同时返回 True** ⇒ **两个进程真的同时持锁**（实测两个 ACQ 间隔仅 **0.6 毫秒**、持锁区间重叠）⇒ 现在**空内容 ⇒ 拒绝拿锁**（并短暂重试等创建者写完）+ **老空锁（创建者崩溃残留）⇒ 允许接管**（避免后续批次永久卡死）
  - **再订正一处平台耦合（同日，Oracle 侧实测发现）**：拿锁后的「旧 pid 是否还活着」判定原本直接用 `ctypes.windll`（**Windows 专有**）⇒ 采集机跑得好好的，但 `collector.py` 同时是仓库里的生产代码、其测试会在 **Linux（Oracle）** 上跟着全量 pytest 跑 ⇒ Oracle 上子进程直接 `AttributeError` 崩、**2 条锁测试 failed** ⇒ 抽出 `_pid_alive()`：**Windows 走 `OpenProcess`**、**POSIX 走 `os.kill(pid, 0)`**（只探测不发信号；⚠️ Windows 上**不能**用 `os.kill(pid,0)`，那边会 `TerminateProcess` 把目标杀掉）⇒ **两侧都实测 7 passed**，且不再有平台跳过
  - **顺带清掉 Oracle 上 5 条既有失败（2026-10-23 复核全量时挖出，`c78c5be` 上同样失败 ⇒ 非本轮引入）**：① **队名映射断言旧了**（`tests/test_suggestion10_deferred.py` 仍断言 `279→#2351`，而 **2026-10-22 已纠正**为 `279→#2355 皇家贝蒂斯`（`af_id=543`=Real Betis），生产库实测 `279|2355` / `506|2351` ⇒ **生产是对的、断言旧了**）；② **未知表抛错类型不对**（`jc_issue_write._validate` 里 `_PK_BY_TABLE[table]` 在校验前求值 ⇒ 未知表先炸 `KeyError`，与契约「响亮 ValueError」不符 ⇒ 调用方按契约 `except ValueError` 会**漏接**）；③ **node 依赖的用例在没装 node 的机器上报成失败**（应按 `test_dashboard_smoke` 惯例 skip）；④ **order-dependent 失败**（`from store import jc_view` 取的是**包属性**，测试只换 `sys.modules` 条目换不掉它 ⇒ 全量跑时拿到**真模块**、真连生产库）；⑤ **测试会写生产仓库文件**（`parse._warm_team_names` → 真调 LLM → 写 `scripts/references/team_translations.json`，实测 Oracle 上多出「阿森纳 vs 切尔西」「Liverpool」「Everton」等测试夹具队名 ⇒ 已加 `tests/conftest.py` 的 **autouse 隔离**，**测试永远碰不到生产文件**）
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
