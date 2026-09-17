# league-v2 完整验收报告

- **验收对象**：`/home/ubuntu/league-v2/repo`（分支 `v2`，起点 `2341f0c`）
- **线上地址**：https://140.83.62.161/dashboard/jc/ （nginx Basic Auth `admin` + 应用会话 `a`/`a`）
- **验收时间**：2026-09-17 UTC 14:00 – 17:20（BJT 22:00 – 次日 01:20）
- **方式**：本人逐端点实测 + 4 名 OMP 队员分工深挖（数据源/配额、AI-LLM 链路、前端、数据库与采集管道），全部结论以真实命令输出为据，未修改仓库外状态。
- **结论一句话**：**预测主链路真实可用**（`POST /jobs/predict` → 202 → 写预测文件 → `/predictions/today` 有数据）；但**AI 富化链路 100% 失败却被伪装成成功**、**竞彩看板页面在生产上整体不可达**、**竞彩时间显示错 8 小时**——本次已修复其中 12 项，剩余 20 项已定位并给出证据。

---

## 一、结论摘要

### 1.1 链路验收总表

| 链路 | 结论 | 关键实测 |
|---|---|---|
| `.env` / 配置 | ✅ 有效 | `PREDICT_DAILY_LIMIT=80` 在 `.env`、`/health`、`/proc/408541/environ` 三处一致 |
| football-data | ✅ 通 | `count=1`，`X-Authenticated-Client: Jack`；错 key→400、无 key→403（对照已做） |
| API-Football 客户端 | ⚠️ 方法可用、链路不可达 | `fixtures/lineups/h2h` 都能真跑；但 `web/`+`scripts/` **零调用方 = 死代码** |
| ESPN 源 | ❌ 坏 | 区间日期恒 HTTP 400，重试 3 次耗时 91.2s 后抛异常；看板却报 `enabled: true` |
| LLM / AI 富化 | ❌ 全失败 | key **无效**：`HTTP 401 无效的令牌`（伪造 key 得到逐字相同报文）；41 条占位记录落盘 + `exit_code=0` |
| PostgreSQL | ✅ 通 | 18.6 @127.0.0.1:5432，8 schema / 38 表 / 2 视图，三角色均可连 |
| 竞彩采集管道 | ⚠️ 在跑但已停推 | cron 精确每 10 分钟执行，220 次 run header，rc=0 ×219；但**上游已停推 89.6 分钟** |
| 预测端到端 | ✅ 通 | 202 → `Got 1 events` → `Predicted 1 matches` → 写文件 → `/predictions/today` 返回 EPL 1 场 |
| Web / Dashboard | ⚠️ 可达但数据误报 | `/dashboard/jc/` 200；但 3 处数据渲染错误（已修）+ 竞彩看板页整体 404 |

### 1.2 本次修复（12 项，全部有验证）

| # | 严重度 | 缺陷 | 修复文件 | 验证方式 |
|---|---|---|---|---|
| 1 | **P0** | 登录成功后跳转到 `/dashboard/`（NDORACLE 门户 8099）而非 `/dashboard/jc/` | `static/dashboard-login.html` | 线上登录跳转 |
| 2 | **P0** | AI 富化 100% 失败却 `exit_code=0`、任务显示 done | `web/enrich.py`、`ai/feedback_loop.py` | 线上触发 → `status=failed, exit_code=1` |
| 3 | **P0** | 失败时伪造 `ai_score=50` 落盘（修好 league 后会静默削 15% 信心） | `ai/feedback_loop.py` | 线上触发后 `ai_scores.json` md5 **未变** |
| 4 | **P0** | `web/enrich.py` 产物 `league` 恒为 `""` → AI 日报/榜单命中数结构性永久 0 | `ai/feedback_loop.py` | 单测 + 落盘 league 正确 |
| 5 | **严重** | 竞彩开球时间**早 8 小时**（影响全部 51 场） | `scripts/store/jc_view.py` | 线上 `09-18 10:30` → **`09-18 18:30`** |
| 6 | **严重** | 前端对已是北京时间的时间戳再 +8h，且依浏览器时区而异 | `static/jc.html` | node 双时区 6 用例全过 |
| 7 | **高** | 被 409 拒绝的请求也扣当日配额（反复点击即可耗尽额度） | `web/services/jobs.py` | 线上 3 并发 → delta **1**（修复前为 3） |
| 8 | **高** | 任务失败只写 `exit_code` 不写 `error`，失败原因不可见 | `web/services/jobs.py` | 线上 `error` 字段有可读原因 |
| 9 | **高** | 测试污染生产 `web/.data/jobs/`（753 条垃圾记录） | `tests/web/test_m3_jobs.py` | 3 次连跑零泄漏；753 条已隔离 |
| 10 | **中** | `/accuracy/breakdown` 三项准确率读错层级 → 永远显示 `—`；样本数被当百分比渲染成 `180.0%` | `static/dashboard.html` | 对照后端真实形状 + 线上 md5 |
| 11 | **中** | 「校准摘要」卡片内嵌死占位，永久卡在"数据加载中…" | `static/dashboard.html` | 线上已无该占位 |
| 12 | **中** | `injuries()` 读错层级 → 24 行 `type`/`reason` 恒为 `None` | `web/services/apifootball.py` | 单测覆盖真实 provider 形状 |

**数据清理**：`predictions/ai_scores.json` 删除 51 条从未真正分析过的占位记录（143 → 92，无空联赛）；753 条垃圾任务记录隔离至 `/home/ubuntu/backups/jobs-quarantine-20260917T171132Z/`（保留 6 条真实记录）。

**测试**：`494 passed` → **`502 passed`**（新增 8 个回归测试）。

---

## 二、逐项验证证据

### 2.1 预测主链路（真实可用）

```
POST /api/v1/jobs/predict → 202 {"job":{"id":"d29d7100296a","status":"queued"}}
GET  /api/v1/jobs/d29d7100296a → status=done, exit_code=0
  [predict] Fetching football-data.org: .../PL/matches?dateFrom=2026-09-17&dateTo=2026-09-18
  [predict] Got 1 events
  [predict] Predicted 1 matches
  [predict] Saved: /home/ubuntu/league-v2/repo/scripts/predictions/prediction_2026-09-17_23.json
```

`GET /api/v1/predictions/today` → EPL 1 场：布伦特福德 vs 切尔西，`direction="布伦特福德 胜 (接近)"`，`1-star`，`1-0`。

**队员一度看到"今日为空"的根因**：当时**不存在**对应 BJT 比赛日的预测文件——不是链路坏，是没跑过。跑一次即出数据。

### 2.2 时间显示错 8 小时（本次已修）

两处**方向相反**的错误，同一页面内互相矛盾：

**后端**（`scripts/store/jc_view.py`）：`kickoff_bj` 是 `timestamp without time zone` 且**列里存的已是北京时间**，旧代码却写 `at time zone 'Asia/Shanghai'`——先把它当北京时间转成 `timestamptz`，再由 `to_char` 按**会话时区**渲染，而会话时区是 `Etc/UTC`：

```
 TimeZone = Etc/UTC
 kickoff_bj = 2026-09-18 18:30:00
   旧表达式 → 09-18 10:30   ❌ 早 8 小时（影响全部 51 场）
   新表达式 → 09-18 18:30   ✅
```

**前端**（`static/jc.html`）：旧注释断言"库内一律 UTC"并对所有时间戳一律 +8h。但库内**两种口径并存**——`snap_ts`/`update_ts` 是 `timestamptz`（真 UTC），而 `sale_begin`/`sale_end`/`draw_at`/`kickoff_bj`/`odds_update` 是 naive 北京时间。实测 `sale_begin` 库真值 `2026-09-16 20:00`，在 UTC 浏览器下被显示成 `2026-09-17 04:00 北京`。

修复后 node 实测（两种浏览器时区结果一致，这才是关键）：

```
TZ=UTC              TZ=Asia/Shanghai
PASS 2026-09-16T20:00:00 -> 2026-09-16 20:00 北京
PASS 2026-09-17T14:53:01Z -> 2026-09-17 22:53 北京
```

**线上复核**：`/api/jc/fixtures` 的 `kickoff_bj` 已由 `09-18 10:30` 变为 **`09-18 18:30`**。

### 2.3 AI 富化：三重缺陷叠加（本次已修）

**证据链完整**：

1. **key 无效**（对照实验区分了 key / 额度 / 模型）：

   | 实验 | HTTP | 报文 |
   |---|---|---|
   | 不带 Authorization | 401 | `未提供令牌` |
   | 伪造 key `sk-0000…` | 401 | `无效的令牌` ← **与真 key 逐字相同** |
   | 真 key `GET /v1/models` | 401 | `无效的令牌` |
   | 访问 base URL 根 | 404 | 主机与端点可达 |

   服务端能区分"没带 token"与"带了但无效"，且真 key 与伪造 key 同一条报文 → **令牌本身不被认可，不是额度问题**（额度类错误通常在鉴权通过后表现为 402/403/429）。

2. **失败被伪装成成功**：`analyse_batch` 在 LLM 失败时把未评分条目原样返回（这是**有意契约**，有测试守护），而 `save_ai_scores` 用 `item.get("ai_score", 50)` 兜底 → 把"完全没分析"伪造成"中性 50 分"。更糟的是 `0.7 + 0.3*50/100 = 0.85` 会**静默削减 15% 信心**。

3. **league 恒空**：`web/enrich.py:73` 传 `league_key=""`，而 `save_ai_scores` 只读 `item.get("source")`——但 `collect_items()` 写的是 `item["league"]`。结果 `"" or "" = ""`。而 AI 日报要求 `scores[match]["league"] == prediction["league"]`，预测侧 league 恒非空 → **命中数结构性永久为 0**。决定性反例：`布伦特福德 vs 切尔西` 明明已作为 key 存在且逐字节相同、日期窗口也覆盖当日，**依然 `ai_matched:false`**，只因 `"" != "epl"`。

**修复后线上验证**（一次触发同时证实三处修复）：

```
status    : failed          ← 修复前是 done
exit_code : 1               ← 修复前是 0
error     : exit 1: [AI Enrich] processed 41 items, wrote back 0 — 全部条目未评分，AI 富化失败
log_tail  : [LLM] API error 401: 无效的令牌  ×9
            [AI Feedback] wrote 0 this run, 92 total
ai_scores.json  before md5 e0f832b04d23c971f2bbcdcf0be933bc (92)
                after  md5 e0f832b04d23c971f2bbcdcf0be933bc (92)   ← 零污染
```

**「143 vs 107」的谜团已解开**：不是矛盾，是**约 14:07:47 发生了一次带外回滚**——作业确实写入 143 条，但磁盘文件在作业结束后 23 秒被重建为 9-15 基线（与 `/tmp/headv` 逐字节相同，`birth time` 晚于作业结束），同期 `prediction_2026-09-17_22.json` 被删除。我放置的金丝雀文件 4 小时未被删除，证明**没有常驻清理进程**，是一次性人工/部署行为。

### 2.4 配额：数字自洽但语义错（本次已修一半）

| 数字 | 来源 | 实测 | 一致 |
|---|---|---|---|
| `used` | `web/.data/quota.json` | 与文件逐次吻合 | ✅ |
| `limit` | `PREDICT_DAILY_LIMIT=80` | `.env`/`/health`/`/proc/…/environ` 三处一致 | ✅ |
| 语义 | 它是**预测触发预算**，不是数据源配额 | 界面却紧邻"数据源已配置"渲染成"配额 1/80" | ❌ |
| 被拒请求 | `quota_consume()` 排在 `active_job()` 之前 | 3 并发 → `1×202 + 2×409`，count `4→7` | ❌ **已修** |

**修复后线上验证**：

```
quota before: 2
 [HTTP=202] [HTTP=409] [HTTP=409]
quota after : 3
delta = 1（期望 1）PASS
```

### 2.5 竞彩看板页面在生产上整体不可达（**未修，需决策**）

nginx 实际路由（`/etc/nginx/sites-available/dsh-web`）：

```
location = /dashboard/jc/      → 8077/static/dashboard.html
location = /dashboard/jc/login → 8077/static/dashboard-login.html
location /dashboard/jc/api/    → 8077/api/
location /dashboard/           → 8099/  (NDORACLE 门户)
```

**没有任何 `jc.html` 的路由**。实测：

```
https://140.83.62.161/static/jc.html          -> 404
https://140.83.62.161/dashboard/static/jc.html -> 404
https://140.83.62.161/dashboard/jc/jc.html     -> 404
https://140.83.62.161/dashboard/jc/            -> 200   ← 这个才是 dashboard.html
```

而 `static/index.html:71` 有链接 `<a href="/static/jc.html">竞彩看板 →</a>`，指向一个 404。

**后果**：`/api/jc/*`（51 场赛事、11250 条盘口、12 条开奖、回测）**全部可达但没有任何页面消费**——`dashboard.html` 只调 `/api/v1/*`（实测 `/api/jc/` 引用数 = 0）。**竞彩数据在界面上完全不可见**。

这是产品/部署决策（该给 `jc.html` 挂哪个 URL），我没有擅自改生产 nginx，已登记 inbox 待人工确认。

### 2.6 数据库与采集管道

- **连通性**：18.6 @127.0.0.1:5432/league，8 schema、38 表、2 视图。
- **cron 真在跑**：220 次 run header，精确 `:00/:10/…/:50`，rc=0 ×219（1 次历史语法错已修）。
- **上游已停推**：最新批次 `2026-09-17T14-53-18Z`，截至核查时刻 **age = 89.6 分钟**；fact 侧时间戳全部停在 `14:53:01Z`。
- **推送频率**：09-16 为 6-9 批/小时 → 09-17 为**每小时 1 批(:53)**。⚠️ 这**不是缺陷**：`README.md:342` 明确记载采集机 `collector_offer_10m` 自 2026-09-17 起改为每 1 小时（原 10 分钟），属**已记录的有意变更**。仍无法解释的是 05h-08h 四小时空档与 09-16 15h→22h 七小时空档。
- **装载器无增量**：`jc_load.py` 每次重扫全部 115 个 `.done` 标记 → 单次 run 写 **834 条** `ingest_log`；表已 **157157 行**，日增 ≈ 12 万行，**约 98% 冗余 IO**，无清理策略。
- **数据缺口**：`gaps=20` 与 `ops.file_arrival where src_file like '%#MISSING%'` = 20 精确吻合（自检可信）。

---

## 三、未修复缺陷清单（已定位，按严重度）

### P0

| # | 缺陷 | 证据 |
|---|---|---|
| U1 | **`LLM_API_KEY` 无效**，AI 富化链路整体不可用 | `HTTP 401 无效的令牌`；需人工换 key |
| U2 | **竞彩看板页 `jc.html` 无 nginx 路由**，竞彩数据全不可见 | 三条路径均 404；`dashboard.html` 不调 `/api/jc/*` |

### 高

| # | 缺陷 | 证据 |
|---|---|---|
| U3 | `LEAGUE_SOURCE_*` / `LEAGUE_QUOTA_CAP_*` **未接入预测链路**（关闭后照样发真实请求） | `spend_allowed()` 抛 `SourceDisabled` 后 `fetch_events` 仍返回 1 场；违背手册 D9"关闭必须零副作用" |
| U4 | **ESPN 源实际不可用**，看板却报 `enabled: true` | 区间日期恒 HTTP 400，重试 3 次 91.2s 后抛异常 |
| U5 | `--data-source api-football` 是坏的 | 只取窗口起点 → 查 09-17（league 39 = 0 场）→ 回落 ESPN → 崩掉整个预测 |
| U6 | 上游采集停推（89.6 分钟无新批次） | 见 2.6。注：每小时 1 批是 README 已记录的有意变更，**非缺陷**；停推本身仍待查 |
| U7 | 装载器全量重扫，`ingest_log` 12 万行/日无清理 | 见 2.6 |

### 中

| # | 缺陷 | 证据 |
|---|---|---|
| U8 | `fact.jc_issue_match` / `jc_issue_prize` 恒 0 行且**全仓无 INSERT** | `jc_issue.n_matches=14` 却无场次明细 |
| U9 | 篮球 `jclq_offer` 被静默丢弃 | 日志 `[predict] skip jclq_offer n=25`；staging 有 89 文件/2315 行 |
| U10 | `ops.file_arrival` 幽灵行 → 看板数字虚高 1 | `jczq_offer/x.jsonl` 磁盘不存在；11251 − 1 = 11250 = `fact.jc_offer` 行数 |
| U11 | AF 富化/配额停摆 >1 天，看板解释为"当日未发请求（非降级）" | `ops.quota_ledger` max(day)=2026-09-16；`.env` 却 `API_FOOTBALL_ENRICH_ENABLED=true` |
| U12 | backtest 与 `jc_offer` 玩法口径不符 | backtest 返 `crs/had/jqc/ttg`，`jc_offer` 是 `crs/had/haf/hhad/ttg` |
| U13 | 已开赛 28 场仍显示 `match_status=Selling`（51/51 恒定） | 该列不可信 |
| U14 | `/predictions/today` 丢 `kickoff_utc` / `data_window` | 引擎写在文件顶层，`predictions.py:109-110` 从单场 dict 读 → 恒 `""` |
| U15 | 预测对象**无 fixture id、名称未规范化** | 同一场比赛在 `ai_scores.json` 有 3 种拼写、3 个 key |
| U16 | 看板把"预测触发配额"当"数据源配额"展示 | 真实额度（AF 100/天、football-data 30/天）无任何出口 |
| U17 | 两套配额日界不一致（BJT vs UTC） | 每天 08:00-16:00 UTC 必然对不上账 |
| U18 | engine 侧 API-Football 请求绕过 web 配额守卫与缓存 | 每次 2 个请求未记账；真实用量 ≥ 24/100 而 `quota.json` 显示更少 |

### 低

| # | 缺陷 |
|---|---|
| U19 | `degraded` 恒 `False`（`jc_view._fetch` 异常返回 `[]` 但仍报 false）→ 静默降级 |
| U20 | `available` 语义错误：文件缺失也返回 `available: true`（只代表读取没抛异常） |
| U21 | nba 的 `the-odds` 未实现且静默回落 ESPN，slug 兜底 `"epl"`（NBA 拿到英超 URL） |
| U22 | `.env.example` 缺 `PREDICT_DAILY_LIMIT`；env 名（`PREDICT_DAILY_LIMIT`）与常量名（`DAILY_TRIGGER_LIMIT`）不一致，写后者无效 |
| U23 | `h2h(last=N)` 结果被未来赛程污染（5 条里仅 3 条是历史战绩） |
| U24 | 通用重试用 ESPN 的 30s 退避，任一源失败必卡 90s；不检查 `payload["errors"]` |

---

## 四、踩坑记录

1. **"今日为空"差点被误判为链路故障**。真实原因是当日没有预测文件。**先跑一次再判断**——这条省下了大量错误归因。

2. **`test_concurrent_trigger_only_one_producer` 的 `_BlockProc` 自建 `threading.Event()`**，导致用例持有的 `gate` 从未被 `set()`，`finally: gate.set()` 是死代码，`wait()` 空等满 5s。修复必须让子类复用用例持有的 gate，否则线程悬挂到用例之后、在 monkeypatch 撤销后回写真实 `web/.data/jobs`。

3. **时区缺陷是"两处方向相反"而不是一处**。只看后端会以为前端没问题，只看前端会以为后端没问题——实际是后端早 8h、前端晚 8h，**同一页面内互相矛盾**。判断依据必须是"列的 SQL 类型 + 会话时区"，而不是注释里的假设（旧注释"库内一律 UTC"是错的）。

4. **`analyse_batch` 保留未评分条目是"有意契约"**（有测试守护），所以修复必须落在 `save_ai_scores` 而不是上游。改之前先读测试，避免把契约当 bug 改掉。

5. **配额修复要区分"检查"与"扣减"**。第一版我把 `active_job()` 整体提前，结果 `test_quota_shared_between_predict_and_ai_enrich` 由 429 变 409 失败——因为两个条件同时成立时，既有契约是"额度优先"。正解是两段式：`quota_exhausted()` 只读预检 → `active_job()` → `quota_consume()` 确认建任务才扣。

6. **`/api/v1/ai/daily` 的"全 0"有两个独立成因**：`prediction_count` 由"每联赛最新文档的 `data_window` 是否覆盖当日"决定（时间相关，会抖动），而 `ai_matched_count` 恒 0 是**结构性硬缺陷**（league 恒空）。只有后者是 bug。

7. **任务书里两处与实际不符**（供后续参考）：路由是 `/jobs/ai-enrich`（连字符，不是下划线，写错会 405）；`.env` 变量是 `LLM_API_BASE`（不是 `LLM_BASE_URL`）。

8. **`scripts/predictions/` 与 `predictions/` 是两个目录**：前者被 `prediction-metadata` 读，后者（`ai_scores.json`）被 `ai/status` 读。极易混淆。

9. **`predictions/ai_scores.json` 虽被 `.gitignore` 的 `predictions/*.json` 覆盖，但已被 git 跟踪**，所以 gitignore 不生效、改动会进 diff。

---

## 五、无法判定的点（诚实声明）

1. **14:07:47 那次产物回滚的执行者**：无对应 job 记录，仅能证明"文件被重建、内容与 9-15 基线逐字节相同、同期 `prediction_2026-09-17_22.json` 被删除"。金丝雀 4 小时未被删 → 非常驻进程。
2. **`quota.json` 为何与提交数严重不符**：BJT 09-17 有 214 次提交却只剩 `count=4`，文件被中途重置且**无审计痕迹**，无法证明是谁、何时、为何。
3. **753 条垃圾记录的归因**：机制在 `tests/web/test_m3_jobs.py` 有自述，但无法从文件层面证明全部来自 pytest 而非造数脚本。
4. **上游停推原因**：采集机 crontab/日志不在本机，无法查证。
5. **API-Football 真实余量**：免费档不回传额度头，且 engine 侧未记账，精确值不可知。
6. **key"无效"的确切性质**：Agnes 对"令牌未注册"与"账号被停用"返回同一报文，且鉴权发生在额度校验之前，故**无法排除换对 key 后仍遇额度问题**。
7. **看板浏览器渲染**：前端结论基于代码 + API 响应推断；时区部分已用 node 双时区实测，但未做真实浏览器截图。

---

## 六、交付与复现

### 变更文件

**源码（8）**：`ai/feedback_loop.py`、`scripts/store/jc_view.py`、`static/dashboard-login.html`、`static/dashboard.html`、`static/jc.html`、`web/enrich.py`、`web/services/apifootball.py`、`web/services/jobs.py`

**测试（4）**：`tests/web/test_apifootball.py`、`tests/web/test_m3_jobs.py`、`tests/web/test_m6_ai_enrich.py`、`tests/web/test_jc_view_timezone.py`（新增）

**数据**：`predictions/ai_scores.json`（143 → 92，删 51 条占位）

### 复现命令

```bash
# 全量测试
cd /home/ubuntu/league-v2/repo && /home/ubuntu/.venvs/league/bin/python -m pytest -q   # 502 passed

# 线上登录 + 关键端点
curl -s -u admin:ndjack -k -X POST https://140.83.62.161/dashboard/jc/api/v1/login \
  -H 'Content-Type: application/json' -d '{"username":"a","password":"a"}' -c /tmp/c.txt
curl -s -u admin:ndjack -k -b /tmp/c.txt https://140.83.62.161/dashboard/jc/api/jc/fixtures
curl -s -u admin:ndjack -k -b /tmp/c.txt https://140.83.62.161/dashboard/jc/api/v1/sources/status

# 时区验证
sudo -n -u postgres psql -d league -X -c \
  "select match_id, kickoff_bj, to_char(kickoff_bj,'MM-DD HH24:MI') from fact.jc_match limit 3;"
```

### 待人工处理（已登记 inbox）

1. **更换 `LLM_API_KEY`**（Agnes `无效的令牌`）——换后需重跑 `POST /jobs/ai-enrich` 验证。
2. **决定 `jc.html` 的对外 URL**（当前无 nginx 路由，竞彩数据不可见）。
3. **确认 753 条隔离记录可删除**（`/home/ubuntu/backups/jobs-quarantine-20260917T171132Z/`）。
