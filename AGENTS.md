# AGENTS.md

联赛预测引擎 — 多数据源融合 + 信号模型 + ELO + Dixon-Coles 双变量泊松 + 蒙特卡洛模拟。覆盖竞彩足球全部玩法 + 传统足彩 + 篮彩（数字彩只做开奖对照，不预测）。当前引擎版本 `4.1.0`（`scripts/core/__init__.py::__version__`，须与 `SKILL.md::version` 一致）。

## Setup commands

```bash
pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt   # 一键四件套（缺任一行 = 新成员 clone 后跑不动测试）
python -m pytest tests -q                    # 全量测试（测试各自注入 sys.path，无需设 PYTHONPATH）
ruff check .                                 # lint 门禁 —— 当前**全绿**，新增问题会挡住
uvicorn web.api:app                          # 启动 web（pyproject [tool.fastapi] entryPoint）
python tools/check_docs_claims.py            # 文档声明校验（README/AGENTS 与代码一致性）
```

预测引擎本体纯 stdlib、零外部依赖；`requests` 只给 AI 富化，`psycopg[binary]` 只给 `scripts/store/`、`scripts/ingest/` 落库取数。
**web 层禁止引入 pandas / sklearn / xgboost**（依赖隔离，见 `requirements-web.txt`）。

> 直接跑引擎脚本需 `PYTHONPATH=scripts`（如 `python -c "import core"`）；
> `pytest` 不需要 —— `tests/` 下各文件自行 `sys.path.insert(0, "scripts")`。

## Project layout

- `scripts/` — 算法层（核心引擎，只产 JSON 产物）
  - `core/` 足球引擎（Onside 信号 / ELO / Dixon-Coles / Monte Carlo / ML）
  - `bball/` NBA 引擎（ELO + The Odds API 赔率）
  - `derive/` 5 玩法派生（纯 stdlib），`store/` 落库，`ingest/` 取数，`market/` 去水与 CLV 校准
- `web/` — 业务层：`routers/` 路由 + VM 编排，`services/` 唯一数据编排入口，`app/` 应用装配
- `static/` — 展示层（纯静态渲染，拿 VM 直接画）；对应模板在 `web/templates/`
- `tests/` — pytest（`tests/web/` API 级，`tests/web_ui/` 浏览器级）
- `ai/` AI 富化 / `collector-cn/` 国内采集机 / `ops/` systemd 定时任务 / `references/` 静态基准数据 / `tickets/` 工单 / `stress_runs/` 压测工具与 pre-push hook

分层依赖方向是接口基线：**业务层 → 接口层 → 算法层**，web 层顶层禁止 import `scripts.*`（lazy-in-try 是文档化例外）。

**产品侧总纲**：`docs/project/产品路线图-竞彩闭环-20261020.md` —— 诊断（实测基线）+ 六项建议 + 度量基线 + 硬约束。
动竞彩链路的任何代码前先读它；改完按里面的「度量基线」表复测并更新该表。

## Code style

- Python 3.11+，ruff `select = ["E","F","W","I"]`，line-length 120，双引号
- **注释与 docstring 用中文**是项目惯例（生产环境 Oracle `LANG=zh_CN.UTF-8`，UTF-8 全程正常）——不要为"编码安全"把中文日志改英文
- 日志用 `core.log.logger`（`logger.info/warning/error`），结构化事件用 `core.observability.log_event/log_error`
- 改动前跑 `ruff check .`（**当前全绿**，新增 F/W/I 问题会挡住提交）；提交前跑全量 pytest
- **「模型概率」只有一个真源**：`scripts/core/model_probs.py`（`resolve_model_probs` / `resolve_prob`）。
  任何地方要读模型概率都调它 —— **不许**读 `ml_proba`（ML 分类器原始输出，未与规则模型融合）、
  **不许**读 `*_true_prob`（ESPN 去水市场先验，不是模型输出）。取不到就返回 `None`（=「不知道」），不许当 0 用。详见路线图 §6.14。
- **「评分」不许冒充「概率」**：`confidence_score` 是把握度评分（`(p−0.25)×2`），
  页面上「赢面」一律取模型概率对应方向那一项；把握度必须另标。
- **跨市场定价必须标来源**：ESPN 赔率与竞彩官方 SP 算出来的 EV 本来就不同，不标来源会被看成自相矛盾。
- **「读失败」不许印成「业务事实」**：DB/上游读失败要显式带 `degraded` 标志，页面印「⚠️ 读取失败」，
  **不许**印成「暂无数据 / 已结算 0 注 / 24 小时无异动 / 你还没跑脚本」。照抄仓库现成范式
  `recommend.offer_freshness()` 的 `degraded` 标志，不新造模式。

## Testing instructions

- 单测：`python -m pytest tests -q`（当前基线 **2981 collected / 2906 passed / 75 skipped / 0 failed**）
- **脚本需 `PYTHONPATH=scripts`**（或 `sys.path` 注入），否则裸 `import core.*` 失败；
  `pytest` 本身不需要 —— 各测试文件自行注入 `sys.path`
- 新增行为必须补测；沿用现有命名 `tests/test_<module>.py`
- `tests/web_ui/` 浏览器级测试缺 playwright 时自动 skip（marker `web_ui`）
- 所有测试必须在开 PR 前通过
- ⚠️ **测试绝不允许写生产仓库的文件**（`tests/conftest.py` 的 autouse `isolate_production_writes` 会守门并 FAIL）：
  - 观测日志由 `LEAGUE_LOGS_DIR` 隔离；**自带 env 的子进程**由 `core.observability._default_logs_dir()` 里的
    `PYTEST_CURRENT_TEST` 兜底 ⇒ 生产 `scripts/logs/` 零写入
  - 占位路径必须是**绝对**路径（`Path("unused")` 这种相对路径落点 = CWD = **仓库根** ⇒ 凭空多出文件）
  - 真库端到端用例**不许 `commit()`**（fixture 的 rollback 会失效 ⇒ 往生产 fact 表塞垃圾行）
  - 共享生产模块的函数用 `monkeypatch.setattr` 替换（直接赋值 = 替身在**整个 session** 生效 ⇒ 后续用例测的是 lambda）

## PR & commit conventions

- 从 `main` 开分支，不要直推 `main`
- Conventional commits，scope 常用模块名：`fix(predict):` / `docs(readme):` / `test(lottery):` / `chore(log):` / `feat(dashboard):` / `refactor(model):`
- 长 commit message 用 `-F <file>` 传（PowerShell 会把 message 里的 `ML` 之类解析成命令）
- `git push` 会触发 `stress_runs/hooks/pre-push` 的 5x 压测（启用：`pwsh stress_runs/install-pre-push.ps1`）。跳过用 `$env:SPORTS_LOTTERY_SKIP_PUSH_HOOK="1"`
- 部署节点同步：push 到 `origin main` 后，在 Oracle 上 `git fetch origin && git reset --hard origin/main`

## Ops / deployment

- 每日定时：systemd `league-daily-predict.{timer,service}`，`OnCalendar=*-*-* 09:30:00 Asia/Shanghai`；脚本 `ops/league-daily-predict.sh`
- 该脚本按顺序触发 足球全联赛 → NBA → AI 富化，退出码 `0`=全成功 / `1`=有作业失败 / `2`=登录失败 / `3`=配额耗尽
- ⚠️ 排障关键：告警判定**只看作业 HTTP status（`wait_job` → `job_field status`），不 grep 日志正文**。所以往日志里写中文不会影响告警，别再以"告警 grep 失败"为由做无谓的 ASCII 化

## Security

- 密钥只放 `.env`（已 gitignore，勿提交）：`API_FOOTBALL_KEY` / `FOOTBALL_DATA_API_KEY` / `ODDS_API_KEY`
- 运行期产物不入库：`scripts/logs/`、`scripts/references/ml_model_*.json`、`references/.elo_ratings.json`、`scripts/{predictions,results}/*.json`
