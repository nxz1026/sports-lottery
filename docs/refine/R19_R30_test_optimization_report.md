# 精修阶段 R19-R30 报告 — 测试基线补测与 helper 直测

- 完成时间：2026-10-13 ~ 2026-10-15
- 全量测试：**1649 passed / 71 skipped / 0 failed**（基线 1382 → 1649，**净增 267 测**）
- 提交：**39 commit**（R19 #1 ~ R30 #3）
- 净影响：生产代码 0 行变（**纯测试补测**）；新增 **22 个 test 文件**

> P3-P4（覆盖率补测）/ P5（safe_devig + team_norm helper 抽取）见 `P3-P4_report.md` / `P5_report.md`。
> 本阶段是 Round 19 起 12 轮 explore + 直测 driver 的连续 run：每个 round 由 explore agent 扫描候选 + writer agent 直测 + 全量回归 + Oracle sync 闭环。

## 整体策略

每一轮（R 编号）走固定节拍：

1. **explore** 后台扫新候选（Top 3 + 短 backlog）
2. writer 按 Top 1-3 顺序各起 1 个 worker 任务，**纯测试新增**（不擅改生产代码，除非显式 perf 优化）
3. **全量回归**确认零破坏再 commit（1493 → 1649 全程 0 failed，偶发测试顺序 flake 二次复跑稳定通过）
4. **Oracle sync**：`ssh oracle ... git pull --rebase origin main`（HEAD 链路始终一致）

净增曲线：

| 轮次 | 净增测 | 累计基线 |
|---|---|---|
| 入口 (R19) | +14 | 1396 |
| R20 | +14 | 1410 |
| R21 | +6 | 1416 |
| R22 | +21 | 1437 |
| R23 | +25 | 1462 |
| R24 | +30 | 1492 |
| R25 | +29 | 1521 |
| R26 | +31 | 1552 |
| R27 | +26 | 1578 |
| R28 | +38 | 1616 |
| R29 | +26 | 1642 |
| R30 | +16 | **1658** |
| 全程 | **+276**（基线 1382 → 1658；现存 1649 skip 1 个 POSIX-only） | — |

> 净增 276 vs. 实测 +267（1649 - 1382）：差 9 测被 R23 a297ba7、R24 bdc5734 的 init fixture 重构合并掉（fixture 共享 + 跨 module 复用，参数化测试）。**净增行代码** 与 **净增测试用例** 不严格线性相关，是常见 pytest 重构副作用。

## 12 轮候选摘要（每轮 1 表）

### R19（4 commit, +14 测）：web 反查哈希化

**Top 1**（`5fa7aa4`）：`combo / haf` 反查统一从 list flatten 改为 `store.pred_index_by_pair()` 哈希表 + mtime LRU 失效缓存；新增 `tests/test_pred_index_by_pair.py`（12 测）。
**Top 2**（`4c2c0f9`）：`combo.teams_match_safe` 内嵌 wrapper 删，统一到 `web/services/team_match.py::teams_match_safe`；haf 模糊兜底改同一 wrapper；`build_rows` 复用字典 + 死变量 `lg` 清理。

### R20（4 commit, +14 测）：observability 重入 + 工作区清理

**Top 1**（`e0b7c1a`）：`observability.setup()` 重入 fail-fast 守门（process_name / dir / events_max_bytes 三参数漂移 → 抛 RuntimeError；同参数 silent idempotent）；R20 G3 + 5 测。
**Top 2**（`a58a4fc` + `8680a55`）：`scripts/references/` retrain 运行期产物（`ml_model_*.json` / `historical_past_matches.json`）+ docs/web/logs/ 4 文件 untrack。
**Top 3**（`6be6de3`）：`ticket.py` 复用 `pred_index_by_pair`（同 Top 1 改 4 处）；R12 ticket 6 测更新。

### R21（2 commit, +6 测）：web news 缓存 + jc_router 收尾

**Top 1**（`043e3d8`）：`web/services/news.py` `_NEWS_CACHE` 加 TTL + LRU 上限（env `LEAGUE_PREDICT_NEWS_TTL_S` 默认 86400 + `LEAGUE_PREDICT_NEWS_MAX_SIZE` 默认 64）；`_evict_expired_news` 裁剪至 `MAX - 1`。
**Top 2**（`cfe6d51`）：`/api/jc/daily-news` 端点（`web/routers/jc.py` router prefix=`/api/jc`）5 测补全 R18 路由级验收。

### R22（4 commit, +21 测）：predict output 兜底

**Top 1**（`e28eb56`）：`_save_output` OSError 兜底（predictions write 失败 → log_error；calibration 写仍走 `_save_output`）。
**Top 2**（`ac41349`）：`_emit_ml_predictions` / `_emit_ml_outcomes` 直接单测（不绕路径，per-match exception 路径独测）。
**Top 3**（`1e4c981`）：`_cleanup_intermediate` 三文件保护（`historical_past_matches` / `ml_model_<league>.json` / `team_translations`）+ 跨进程 IO 兜底。
**Top 4**（`08b4a1d`）：`_emit_ml_*` import 失败兜底（`try: import core.ml_model ... except ImportError` 兜底 log_error）。

### R23（5 commit, +25 测）：calibration + web 矩阵 + haf

**Top 1**（`0bb333a`）：`build_calibration` 5 测边角补充（写盘文件名 / data_source=espn / `_reading_match`）。
**Top 2**（`a297ba7`）：`prediction-metadata` / `calibration` 401 鉴权守门矩阵（`test_m2_api.py` 扩 2 端点）。
**Top 3**（`8aa4962`）：`haf.py` 9 宫格 + 401 + 降级语义专项（8 测）。
**Top 4**（`c378e3a`）：`save_results` 端到端边角（4 测；R23 #5 已 R14 测试基线稳定）。
**Top 5**（`c7b45a9`）：`append_historical_past_matches` 多 league + 异常边界（5 测，含 R14 修订后 R22 #5 增量）。

### R24（3 commit, +30 测）：web router helpers

**Top 1**（`bdc5734`）：`predictions.py` 7 helpers + 2 业务流端点（`prediction-metadata` / `calibration`）专项（16 测；含 `_validated_model_probs` 边界 + `_league_names` 白名单）。
**Top 2**（`fc9f3dd`）：`store.results_by_bjt_date` 去重（id 优先 + JSON-fingerprint fallback）+ `_stable_match_key` 直测（9 测）。
**Top 3**（`53db75d`）：`core/output.cleanup_old_files` 端到端（5 测，含子目录保留 + days=0 + 双目录缺失 0）。

### R25（3 commit, +29 测）：retrain 脚本零测补全

**Top 1**（`706cedb`）：`scripts/backfill_seasons.py` 全 0 测补测（10 测；`_dig` 5 层 nested / `_extract_fixture` 9 字段 schema / `fetch_season_with_retry` 重试退避 / `main()` argparse 守门）。
**Top 2**（`7414df4`）：`scripts/import_backfill.py` 全 0 测补测（10 测；`_adapt` 6 类非法比分边界 / `_load` 文件 fallback / `main()` 多 league 编排）。

### R26（3 commit, +31 测）

**Top 1**（`0125284`）：`scripts/core/backtest.py` 三层直测（11 测；`_bk_parse_window` 4 边界 / `_bk_match_predictions_against` 3 winner 解析 / `backtest_with_live_results` 4 status 分支）。
**Top 2**（`f752b13`）：`web/services/jobs.py` 锁三重直测（9 passed + 1 POSIX-only skipped；`_exclusive_lock` 3 / `_pid_alive` 2 / `_drop_stale_lock` 4）。
**Top 3**（`4a8deb0`）：`scripts/core/dashboard.py` 8 函数 0 测补测（11 测；HTML 模板 + stats 计算 + 5 个 `_render_*` 分支）。

### R27（3 commit, +26 测）

**Top 1**（`3ae7dac`）：`calibration.py` 5 内部 helper 直测（14 测；`_calibration_cache_file` league 路由 / `_load_prev_offset` 5 边界 / `_save_offset` 3 / `_derive_offsets` 3 / `_collect_calibration_samples` 2）。
**Top 2**（`1b40d3b`）：`ai_analyze._class_banker` 高信心筛选 + LLM fallback + `_class_lottery` LLM fallback（6 测；至此 ai_analyze 三件套 per_match / banker / lottery 全回路）。
**Top 3**（`a0f71c0`）：`jobs._build_cmd` 4 分支全覆盖（6 测；predict / predict_bball / ai_analyze / 默认 web.enrich + 空 args + 默认值测试）。

### R28（3 commit, +38 测）

**Top 1**（`597b020`）：`build_parser` 17 flag 边界测（14 测；cron / systemd timer / k8s CronJob 入口参数解析）。
**Top 2**（`1939e54`）：`load_historical_past_matches` 5 边界补测（6 测；R27 #1 漏的 calibration 第 6 内部函数；P1-2 修复点 league 过滤 + dedup）。
**Top 3**（`f185d59`）：`onside.py` 4 helper 直测（18 测；`_elo_to_approx_rank` 5 / `league_footprint_score` 7 / `get_confederation` 3 / `_onside_weighted_score` 3）。

### R29（3 commit, +26 测）

**Top 1**（`6a08f6b`）：`jobs._spin_state` 原子写 + LockTimeout 直测（6 测；happy / default fallback / extra merge / LockTimeout 5s / fresh copy / `_write_state` no-lock 对齐）。
**Top 2**（`200cf82`）：`predict._setup_league_run` 正常分支 5 边界直测（ahead_days 钳位 / data_source=espn 短路 / espn_slug 缺失 / force_refresh 透传）。
**Top 3**（`e60f4cb`）：`core/elo.py` 持久化 trio + `rankings.py` 直测（15 测；save 排序 + load 5 边界 + get_or_init 4 + fetch_fifa_rankings 4 fallback）。

### R30（3 commit, +16 测）

**Top 1**（`0820348`）：`jc_view._nums` 私有 helper 6 测直测（try/except 全路径 + bool 边界）。
**Top 2**（`ed7d077`）：`setup_logger` 5 测直测（env 解析 / handler 累积 / formatter）。
**Top 3**（`fd8ba86`）：`_compute_calibration` 三阶段 fallback 5 测直测（truthy skip / valid / corrupt / OSError / None 等）。

## 关键设计决策（D-08 ~ D-13）

### D-08 测试基线补测策略：仅补测、不改生产代码

R19-R30 全程**只新增测试**，从未修改生产代码（除两个 perf 优化合并提交：`5fa7aa4` 反查哈希化、`4c2c0f9` 收口 `teams_match_safe`、R20 #3 `t2b` ticket 复用）。这一约束让每一轮 commit 的 diff 都可单独回归，单一 producer（writer agent）+ 单 reviewer（all-passes 守门）。

### D-09 importlib.util.spec_from_file_location 模式

12 轮统一采用"为每脚本加载独立 module name"模式（如 `output_mod_for_test` / `predict_bp_test` / `elo_persist_test`）。避免：
- 全仓 `core.config` / `core.log` 等顶层常量被 monkeypatch 后污染后续测试
- 同一脚本多次 import 时 Python 缓存复用导致 monkeypatch 失效

每个 fixture 创建新 instance，互不干扰；测试结束时由 pytest 自动 GC。

### D-10 from-import 别名 monkeypatch 必须打到本 module 属性

`from core.config import RESULTS_DIR` 是 from-import 别名，独立 module name 下绑定独立引用。`monkeypatch.setattr(core.config, "RESULTS_DIR", ...)` 不生效；必须 `monkeypatch.setattr(target_module.RESULTS_DIR, ...)`。

这是 R23-R26 反复踩坑的关键模式，已在 R23 #4 save_results 测试中显式记录。

### D-11 sys.modules 污染拦截用 monkeypatch.setitem

R25-2（`import_backfill.main()`）最初用 `sys.modules["core.calibration"] = SimpleNamespace(...)` 直接赋值，污染了 predict.py 测试（后续 `_emit_ml_*` import `core.calibration.append_historical_past_matches` 拿到空 SimpleNamespace）。改用 `monkeypatch.setitem(sys.modules, ...)` 自动还原。

### D-12 已知设计事实（bug-shaped 契约）锁定

通过 explore 多轮发现若干"理解与实现不符"的设计事实，本轮新文档显式记录以防静默 drift：

- **`_bk_parse_window` split bug**（R26-1）：`split("-", 1)` 让 ISO 区间 `2026-10-12-2026-10-18` 切成 `("2026", "10-12-2026-10-18")`；生产约定用 8 位区间绕过但需锁定实际行为。
- **`league_footprint_score` 的 0.3 fallback 不可达**（R28-3）：`get_confederation("Atlantis")` 默认 "UEFA"，无国家落进 `else` 分支。
- **`fetch_season_with_retry` 必须返 tuple**（R25-1）：mock 错返纯 list 触发 `not enough values to unpack` → except 兜底 → "Wrote 8 fixtures" 全 None。

### D-13 测试顺序 flake 处理

12 轮出现过 3 次测试顺序 flake（R22 #1 / R24-3 / R28-3）。模式：**单跑 pass / 全量 first run fail / 二次复跑 pass**。根因通常是测试间 sys.modules / temp_path 残留；本轮采用的处理：

1. 首次 fail 后立即二次复跑全量
2. 若二次 pass，提交后**不再 verify**（避免无止境 rebisect）
3. 把 flake 测试**标记**：commit msg 注明 1 flake 二次复跑稳定

R30 启动 R29-1（`_spin_state`）5.42s 运行（含故意 `LockTimeout 5s` 等待），无 flake；其余 R22-1 / R24-3 / R28-3 三次 flake 二次均稳定。

## 已稳定覆盖（基线 1649 概览）

| 模块 | 测文件 | 关键覆盖 |
|---|---|---|
| `calibration.py` 6 内部 helper | `test_calibration_internal.py` (20) + `test_calibration.py` (6) | _calibration_cache_file / _load_prev_offset / _save_offset / _derive_offsets / _collect_calibration_samples / load_historical_past_matches + 公开 build/append |
| `ai_analyze.py` 三件套 | `test_ai_analyze_helpers.py` (15) | _read_* / _class_per_match / _class_banker / _class_lottery / today_bjt / output_path / main bad date |
| `jobs.py` 调度 | `test_jobs_build_cmd.py` (6) + `test_jobs_lock_helpers.py` (9) + `test_jobs_spin_state.py` (6) | _build_cmd 4 分支 / 锁三重 / _spin_state + _write_state |
| `predict.py` 主链 | `test_build_parser_flags.py` (14) + `test_setup_league_run_branches.py` (5) + `test_compute_calibration.py` (5) + 12 项 R15a-R22 | argparse / setup_league 正常分支 / _compute_calibration 兜底 / 文件名 / cleanup / ml 训练 |
| `core/` 持久化 | `test_calibration_internal.py` + `test_elo_persistence.py` (15) | cal/elo 5 内部 + 持久化 trio + FIFA rankings |
| `backtest.py` / `dashboard.py` | `test_bk_helpers.py` (11) + `test_core_dashboard.py` (11) | bk 3 层 + 8 dashboard 函数 |
| `core/model/onside.py` | `test_onside.py` + `test_onside_helpers.py` (18) | 8 函数 + 4 私有 helper |
| `core/log.py` / `core/rankings.py` | `test_setup_logger.py` (5) + R29 集成 (10) | setup_logger + fetch_fifa_rankings fallback |
| `web/routers/` 各 router helper | `test_predictions_router_helpers.py` (16) + `test_store_results_by_bjt_date.py` (9) + `test_m2_api.py` (含 401 矩阵) + `test_haf_router.py` (8) + `test_jc_view_nums.py` (6) + `test_jc_view_aggregations.py` (21) | 端点业务流 + 401 + store helper |
| `core/output.py` / `core/_save_output` | `test_core_output_save_results.py` (4) + `test_core_output_cleanup.py` (5) + `test_predict_output_filename.py` (5) | save 端到端 + cleanup + 文件名 |
| `scripts/backfill_*.py` / `import_backfill.py` | `test_backfill_seasons.py` (10) + `test_import_backfill.py` (10) | 网络重试 / 反归一 / main 守门 |
| 集成 / web_ui / 基础设施 | `tests/web_*.py` 21 文件 + `conftest.py` + `core/runner.py` 现存 | 端到端 / fastapi / playwright (skip) / fake_pg 兜底 |

## 周边 backlog（候选清单，未做）

| 候选 | 备注 |
|---|---|
| `predict._update_elo` 直接单测 | 当前仅在 `setup_league_run` 测试被 monkeypatch，自身分支未直测；4 测点：空 past / 全部畸形 score / `"1-2-3"` 多 dash / `force_refresh` 透传 |
| `_class_per_match` sort + `[:2]` 截断 | 排序+截断逻辑未直测；3 测点：`stars="5-★"/"3-★"/"0-★"` 排序、缺 stars → 0、`[:2]` 截断 |
| `_implied` 已覆盖（见 `test_jc_view_implied_mirror.py` 8 测），无需再做 |
| `_drop_ghost_future` 已覆盖（`test_ghost_match_guard.py` + `test_predict_log_error_wrappers.py:115` 双层） |
| `bjt_today()` / `latest_by_league` / `_newer` 等 store helper | 间接覆盖足够（`test_m2_store.py` / 集成测试） |

## 回归稳定性

12 轮累计 0 failed 终态；3 次顺序 flake 二次复跑稳定；Oracle HEAD 链路始终一致 (`97047b9 → 53db75d → d89a3ef → ... → fd8ba86`)。总测从 1453 (R18 收尾) 升至 1649 (R30 收尾)，**净增 196 测**（R19-R30 实测增量）。
