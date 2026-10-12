# 精修阶段 P3 报告 — 0% 文件测试补缺（web LLM 网关 + 看板视图 + 装载器 CLI）

- 完成时间：2026-10-13
- 全量测试：943 passed / 70 skipped / 0 failed（3 次连跑稳定）
- 提交：2（`test(coverage): P3-4/P3-5/P3-6` + `chore(repo): 处理工作区遗留 3 项`）
- 覆盖度增量：+3 文件 0%→高覆盖

## 背景

上一轮 P3 清零后，覆盖率扫描锁定三个**真用但无测试**的高价值文件：

| 文件 | 行数 | 0% 原因 | 真实调用点 |
|---|---|---|---|
| `web/services/news.py` | 92 | LLM 网关被标记「文档化例外」，未敢测 | `web/routers/jc.py:247`（`/api/jc/daily-news`） |
| `scripts/store/jc_ops_view.py` | 92 | 「只读视图不需要测」的误判 | `web/routers/jc_ops.py:38, 58` |
| `scripts/ingest/jc_load.py` | 70 | cron 入口依赖真 DSN + 真包，未敢测 | `scripts/ingest/` CLI 入口（每日 cron 装载器） |

工作区遗留 3 项历史脏改动（CRLF 副作用 + tracked 状态文件 + .coverage 未忽略）一并清理。

## 实施

### 1. `tests/test_web_news.py` — 12 用例（`web/services/news.py` 100%）

**隔离策略**：先 `import ai.llm_client` 让模块进 `sys.modules`，再 `monkeypatch.setattr(llm, "generate", fake)` —— 与 `test_ai_analyze.py` 同一模式。

| 用例 | 守住的语义 |
|---|---|
| `test_first_call_invokes_llm_and_caches` | 首次调 + 缓存写入 |
| `test_cache_hit_skips_llm_when_any_field_nonempty` | news/pick_reason 任一非空 → 缓存命中（`a is b`），LLM 不再被调 |
| `test_cache_miss_when_both_fields_empty_recalls_llm` | 双空 = 缓存未填充，下次必重调（不锁死降级态） |
| `test_cache_isolates_by_date_and_sport` | (date, sport) 双维度隔离 |
| `test_truncates_news_to_78` | news 截断 ≤78 |
| `test_truncates_pick_reason_to_60` | pick_reason 截断 ≤60 |
| `test_try_llm_returns_empty_dict_on_exception` | LLM 异常 → `{"news":"","pick_reason":""}`，永不 raise |
| `test_try_llm_returns_empty_dict_on_none_result` | LLM 返回 `None`/`{}` → 同视为空 |
| `test_try_llm_handles_missing_fields_as_empty` | 部分字段缺失 → 缺失补 `""` |
| `test_try_llm_strips_whitespace` | 字段 trim |
| `test_build_prompt_contains_required_tokens` | prompt 必含 label / context / picks_ctx / JSON schema |
| `test_build_prompt_label_substitution` | 不同 label 正确插值 |

### 2. `tests/test_jc_ops_view.py` — 9 用例（`scripts/store/jc_ops_view.py` 100%）

**PG mock 策略**：`queue` 是 `_make` 闭包变量，所有 fake_read_conn 调用共享同一队列（ops_snapshot 调 4 次 read_conn，按 cursor.execute 顺序消费）。cursor 必须实现 `__enter__/__exit__`（代码用 `with conn.cursor() as cur:`），第一次写漏报 TypeError 遮盖真实目标。

| 用例 | 守住的语义 |
|---|---|
| `test_ops_snapshot_returns_four_sections_with_ok_marks` | 四段全部 ok + 数据正确 |
| `test_ops_snapshot_degrades_only_failed_section` | topics 异常 → 仅 `ok_topics=False` |
| `test_ops_snapshot_all_sections_fail_still_returns_shape` | 全失败 → 仍返回 4 段 + 4 个 ok=False |
| `test_ops_snapshot_does_not_raise_on_exception` | 4 种异常混合 → 永不 raise |
| `test_ops_snapshot_logs_exception_class` | `logger.error` 记录异常类名 |
| `test_ops_snapshot_sql_keys_complete` | `_SQLS` 必须含 teams/topics/quota/rejects |
| `test_fetch_returns_rows_as_dict_list` | `_fetch` 正常：rows 是 `list[dict]` |
| `test_fetch_swallows_exception_and_returns_empty_false` | 异常 → `([], False)` |
| `test_fetch_returns_false_on_invalid_role` | pg.read_conn 自身抛 ValueError → 兜底 |

### 3. `tests/test_jc_load.py` — 12 用例（`scripts/ingest/jc_load.py` 92%）

**mock 策略**：纯 unit 测试，不依赖真 DSN / 真包目录；`pg.connect`、`iter_markers`、`files_for_batch`、`load_topic` 全部 mock。

| 用例 | 守住的语义 |
|---|---|
| `test_topics_constants_complete` | TOPICS = 7 主题；OPTIONAL 不在 TOPICS（防循环 import） |
| `test_tables_constants_match_fact_schema` | 8 表常量是末段对账依据 |
| `test_table_sql_covers_every_table` | `_TABLE_SQL` 键集 = TABLES；含 `fact.*` + `count` |
| `test_load_batch_returns_marker_on_empty_input` | 空 root → 返回 marker 名 + 空 topics/errors |
| `test_load_batch_commits_on_success` | 成功 → `conn.commit()` 一次 |
| `test_load_batch_rolls_back_on_exception_and_records_error` | 异常 → `conn.rollback()` + errors 记「类名:消息」 |
| `test_load_batch_collects_topic_results` | 多 topic × 多 file → topics 列表按调用顺序累积 |
| `test_main_smoke_returns_zero_with_mocks` | main() 端到端走 mock pg.connect → rc=0 + commit + close |
| `test_main_requires_dir_argument` | `--dir` 缺失 → argparse SystemExit(2)，不静默吞错 |
| `test_main_logs_marker_count_and_errors` | logger.info 含 marker 名 + ups + errors |
| `test_main_orphan_scan_skipped_logs_warning` | orphan-scan 抛错 → logger.warning，main 不崩 |
| `test_main_orphan_glob_skipped_logs_warning` | orphan-glob 抛错 → logger.warning，main 不崩 |

未覆盖 5 行：47-48（orphan-scan 第二次 files_for_batch 成功路径）+ 60-61（末段对账失败 rollback 兜底）+ 73（`__main__` 入口），均为无害兜底或脚本入口。

### 4. `chore(repo): 处理工作区遗留 3 项`

- `web/services/ai.py` / `scripts/references/ml_model_epl.json`：纯 CRLF→LF 行尾副作用（无内容差异，git 视为文本差异告警）→ `git checkout -- file` 丢弃
- `scripts/bball_data/nba_elo.json`：运行时 Elo 状态，cron 跑就变；.gitignore L55 早已写 `scripts/bball_data/`，但文件在 ignore 规则之前被 track（4fb3579 / 7622366 时代遗物），规则对 tracked 文件不生效 ⇒ 每次 cron 跑完就脏一次 → `git rm --cached` 让规则真正生效；本地保留文件，下次 cron 写入仍是 untracked
- `.coverage` / `.pytest_cache/` / `htmlcov/` / `coverage.xml` 加入 `.gitignore`（测试产物，不属业务代码）

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P3 收尾（`cac071a`） | 905 | 61 | 0 |
| P3-4/5（`34c1dc8`） | 931 | 70 | 0 |
| **P3-4/5/6 + 遗留清理（`?????`）** | **943** | **70** | **0** |
| Δ 本轮 | +38 新增 | — | — |

3 次连跑稳定（943/70/0 ×3）。

## 关键决策

1. **monkeypatch setattr > sys.modules 替模块**（news）：与 `test_ai_analyze.py` 一致；不污染其他测试的 `sys.modules["ai.llm_client"]`。
2. **闭包共享 queue > 每次新建**（jc_ops_view）：`ops_snapshot` 调 4 次 `pg.read_conn`，每次新建 queue 会让 cursor 始终拿 list[0]——闭包化 `_make` 修好。
3. **cursor 必须实现 `__enter__`**（jc_ops_view）：jc_ops_view 用 `with conn.cursor() as cur:`，cursor 缺失 contextmanager 协议时 Python 抛 TypeError 遮盖真实目标。
4. **缓存命中语义 = 同对象引用**（news）：`_NEWS_CACHE.get(key)` 返回 dict 引用（`a is b`），守住防「复制缓存条目」误改。
5. **纯 mock > 真 DSN 依赖**（jc_load）：单测目标是覆盖代码路径，不依赖真 DSN / 真包目录；e2e 真包测试留给未来工单（参考 `tests/test_jc_topic_issue.py` pytestmark skipif 模式）。
6. **`if __name__ == "__main__"` 行不算缺口**：脚本入口不会被 import 触发跑，不影响生产行为。
7. **git rm --cached > commit 数据**（nba_elo.json）：elo 是运行时数据，每次 cron 跑都变；commit 当前数值仍会立刻变脏，取消 tracked + 保留本地文件 + .gitignore 规则真生效才是正确处置。

## 验证

```bash
python -m pytest tests/test_web_news.py tests/test_jc_ops_view.py tests/test_jc_load.py \
    --cov=web.services.news --cov=store.jc_ops_view --cov=ingest.jc_load \
    --cov-report=term-missing -q
# scripts\ingest\jc_load.py           59      5    92%
# scripts\store\jc_ops_view.py       27      0   100%
# web\services\news.py               25      0   100%
```
