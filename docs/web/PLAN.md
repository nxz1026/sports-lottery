# league-predict Web Dashboard — 项目方案（队长版）

> 参照物：welfare_predict 的 Web 栈（FastAPI + SQLite 会话 + 单文件 SPA + ECharts + apscheduler，同类服务已有 FastAPI Cloud 上线先例）。
> 角色：队长＝审核与验收；实现＝OMP（`/root/.local/bin/omp-call --acp`，cwd 本仓）。
> 状态以本目录 WORKLOG.md 为准；每阶段派工单 WO-Mx.md，OMP 交付后队长双验收（代码 review + 功能验收），不通过返工。

## 1. 已拍板的产品决策（2026-09-09，与用户确认）

1. **部署**：FastAPI Cloud（用户另行申请新服务）。代码中服务名/域名一律**占位**（全部走环境变量配置，清单见 .env.example）。
2. **调度（已定稿 2026-09-09：用户套餐＝免费 Hobby 档）**：不再依赖本机。FastAPI Cloud 官方文档确认 Hobby 强制 scale-to-zero（min 恒为 0），常驻 cron 不可靠 ⇒ **主方案＝惰性刷新**：任一页面/API 访问时，若当日（BJT）预测缺失或过期，自动在实例内后台触发一次 predict，前端轮询进度；语义＝每天首次打开即更新。真 cron（apscheduler）仅作为 **Pro 升级后的可选开关**（env `ENABLE_CRON=true`，默认关）。**GHA 的预测 cron 保留到 M5 验收通过后再下线**（期间它持续向 repo 提交最新 JSON，正好充当云端冷启动的种子数据；CI 的 pytest 永久保留）。
   - 理由：API-Football 免费档 100 req/天；GHA 与云端并发跑预测会爆配额，故 M5 前 GHA 单独跑、M5 后云端惰性刷新单独跑，任何时刻只有一方在生产。
3. **AI 赛前解读**：进 MVP，但作为**扩展模块**，不得中断主业务——未配置/失败/超时一律优雅降级（警告不否决，前端显示「AI 解读暂不可用」）。
4. **账号模型**：单账号登录（照 welfare 模式：环境变量注入账号信息，cookie 会话，SQLite 持久化，默认 12h 过期）。
5. **代码组织**：按 router 拆分（**不**照抄 welfare 950 行单文件 api.py）。
6. **引擎红线**：`scripts/`（预测引擎）**零改动**；web 层只读文件产物 `predictions/*.json`、`results/*.json`。M1–M4 期间引擎 diff 非零＝直接打回（例外：M5 允许改 `.github/workflows/*` 与 README/requirements 追加 web 依赖声明）。

## 2. 目录与模块（新增，全部在 web/ 与 static/）

```
web/
  __init__.py
  api.py            # app 工厂 + lifespan（bootstrap/清理会话/启停调度器）+ 静态挂载
  config.py         # env 读取集中点（与 .env.example 对齐）
  session_store.py  # SQLite 会话（建表/校验/清理过期）
  auth.py           # check_auth / require_auth 依赖
  errors.py         # 全局异常脱敏 handler
  services/
    store.py        # 预测/赛果 JSON 文件读取与「每联赛取最新」聚合
    jobs.py         # 后台任务：subprocess 跑 predict.py，单实例锁+状态机
    sources.py      # 数据源/配额状态探测（只读，不发起外部请求优先）
    ai.py           # AI 解读读取器（优雅降级）
  routers/
    auth.py         # POST login/logout, GET me
    predictions.py  # today / {date} / championship / accuracy / history
    jobs.py         # POST predict 触发 / GET status
    sources.py      # GET sources + ai 扩展
static/
  index.html        # 单文件 SPA（原生 JS + ECharts 5.4.3 CDN，参照 welfare）
  login.html
.env.example        # 全部环境变量占位与注释（清单唯一出处，文档不写具体值）
```

约定：env 统一 `config.py` 一处读取；每个函数目标 <50 行，复杂函数超限须注释说明理由。
Python 兼容下限 **3.11**（GHA 3.11 / coder 机 3.12，勿照抄 welfare 的 >=3.13）；web 依赖只加 `fastapi[standard] / uvicorn / apscheduler / pydantic / python-dotenv / loguru`，**不得**引入 pandas/sklearn/xgboost（保护「零依赖引擎」资产；依赖单列 requirements-web.txt，不动 requirements.txt）。

## 3. API 契约 v1（前缀 /api/v1，写操作全 POST，鉴权走会话 cookie）

| 端点 | 方法 | 说明 |
|---|---|---|
| /health | GET | 免鉴权（对齐 welfare） |
| / 与 /login | GET | 页面路由；未登录访问 / → 302 /login |
| /api/v1/login /logout /me | POST/POST/GET | 单账号会话 |
| /api/v1/predictions/today | GET | 今日（BJT 比赛日口径，见 §5），按联赛分组 |
| /api/v1/predictions/{date} | GET | 指定 BJT 日期的预测快照聚合 |
| /api/v1/championship | GET | 各联赛蒙特卡洛冠军/欧战概率 |
| /api/v1/accuracy | GET | 方向/比分命中率 + 校准曲线数据（past_matches+backtest） |
| /api/v1/accuracy/stratified | GET | 分层命中率 + 样本量 + Wilson 95% 区间（建议 3 ①，增量端点；`/accuracy` 字段一字未动） |
| /api/v1/clv | GET | CLV 日汇总（建议 3 ②，主指标；`rows` + `summary`，n 不足带 `insufficient`） |
| /api/v1/history?league=&limit= | GET | 历史预测文件索引（日期/联赛/场次/是否有赛果） |
| /api/v1/jobs/predict | POST | 触发一次预测（body: leagues? all?），返回 job_id |
| /api/v1/jobs/{id} 与 /jobs/latest | GET | 任务状态轮询（queued/running/done/error + 日志尾巴） |
| /api/v1/sources/status | GET | 数据源可用性、上次运行时间、缓存新鲜度、剩余配额提示 |
| /api/v1/matches/{match_key}/ai | GET | AI 解读（扩展；未配置/无数据返回降级 JSON） |

响应统一 JSON；错误统一 detail 字段；未鉴权一律 401（除 /health 与页面重定向）。

## 4. 前端（单文件 SPA，tab 模式对齐 welfare）

- Tab：**预测**（默认：按联赛卡片/表格——方向⭐信心、比分 TOP3、λ±CI、大小球/BTTS）｜**冠军概率**（ECharts 条形）｜**命中率**（趋势线+校准散点）｜**历史**（日期浏览+赛果对照）｜**运维**（手动触发+进度轮询、数据源状态）。
- AI 解读：卡片内「AI 点评」懒加载按钮/折叠区，失败静默降级不阻塞渲染。
- 无 node 构建链；CDN 用 echarts@5.4.3（jsdelivr，与 welfare 一致）。
- 中英对照沿用引擎 i18n 能力（若 M0 契约表明 JSON 已含双字段则直接使用，否则前端中文为主）。

## 5. 时区与数据口径纪律

- 内部存/传一律 UTC ISO（引擎现状），**API 层出口**换算 BJT（Asia/Shanghai），「今日」以 BJT 比赛日定义；禁止在 UTC 容器环境直接取本地日期。
- 「每联赛最新一次运行」选取规则以 M0 契约文档为准（文件名时间戳含义、覆盖/追加行为），先定文档再实现。

## 6. 里程碑（串行，每个都过队长双验收）

| 阶段 | 内容 | 队长验收要点 |
|---|---|---|
| **M0** | 数据契约勘察（只读）→ `docs/web/DATA_CONTRACT.md` | 队长抽查其结论 vs 代码实际；问题清单闭环 |
| **M1** | web 骨架：app+config+session+auth+login 页+/.health+.env.example；可 uvicorn 起 | 401/302 行为合规、仓库内无敏感值、引擎零 diff |
| **M2** | 只读数据 API（predictions/championship/accuracy/history/sources） | 与 CLI 产物逐字段比对；空目录/坏文件容错 |
| **M3** | jobs：手动触发+锁+状态轮询+apscheduler cron+AI 扩展降级 | 并发触发只跑一个；超时杀进程；引擎无改动 |
| **M4** | 前端 SPA 五 tab + ECharts + AI 折叠区 | 登录闭环、真实数据渲染、错误态展示 |
| **M5** | 部署占位（FastAPI Cloud 说明+Dockerfile 复核）+ GHA cron 下线 + README | 上线由用户在云端操作；队长给部署清单与冒烟检查集 |

回滚设计：M1–M4 仅新增 `web/`、`static/`、`requirements-web.txt`、`docs/web/`、pyproject 追加 `[web]` extra——回滚＝`git revert` 或删分支，引擎与数据不受影响。

## 7. 监管流程（队长 SOP）

1. 工单＝`docs/web/WO-Mx.md`（目标/范围/红线/验收标准，OMP 开工前必读）。
2. OMP 执行：统一走 `/root/omp-resilient.sh <TAG> <cwd> <attempt_timeout_s> <max_tries> <prompt_file> <sentinel>`（**锁定付费模型 DeepSeek-V4-Flash-0.1（**必须裸名，带前缀会静默挂死**），用户指令 2026-09-09：抽风不死不休**——哨兵文件未出现 DONE 就自动续跑最多 30 次，每次退避 20s；工单 prompt 必须设计成断点续做+自报完成信号 `<cwd>/docs/web/logs/<TAG>.done`）。日志/尝试计数落 `docs/web/logs/`。
3. 交付＝代码提交到分支 `web-dashboard` + 自测报告（`docs/web/Mx_report.md`：做了什么/自测证据/未尽事项）。
4. 队长验收：`git diff` 审查（红线、行数纪律、敏感值、时区）→ 跑 pytest + 冒烟脚本 → 结论记 `WORKLOG.md`（PASS/REWORK+理由）。
5. 全部 PASS 后：合回 main（用户确认后 push GitHub）。

## 8. 风险登记

| 风险 | 缓解 |
|---|---|
| 云平台无持久盘：predictions 重启即失 | M2 聚合层容忍空数据（前端显示「等待首次预测」）；M3 冷启动检测缺失自动触发；M5 与平台能力实测 |
| 免费配额（API-Football 100/天、football-data 10/min） | 云端每日 cron 仅 1 次；手动触发加冷却（如 ≥2h 间隔，M3 定）；GHA 预测下线 |
| 长任务 vs 云函数超时 | jobs 设计为幂等+断点可重跑；状态落盘，重启后可恢复显示 |
| welfare 代码被 OMP 直接复制导致重依赖混入 | 验收必查 requirements-web.txt 与 import 清单 |
| 会话 SQLite 在云端重置 | 单账号可接受（重登即可），文档注明 |
