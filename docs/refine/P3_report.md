# 精修阶段 P3 报告 — 0% 文件测试补缺（web LLM 网关 + 看板视图）

- 完成时间：2026-10-13
- 全量测试：931 passed / 70 skipped / 0 failed（3 次连跑稳定）
- 提交：1（`test(coverage): P3-4/P3-5 web/services/news + store/jc_ops_view 0%→100%`）
- 覆盖度增量：+2 文件 0%→100%（52 行 → 52 行覆盖）

## 背景

上一轮 P3 清零后，覆盖率扫描锁定两个**真用但无测试**的高价值文件：

| 文件 | 行数 | 0% 原因 | 真实调用点 |
|---|---|---|---|
| `web/services/news.py` | 92 | LLM 网关被标记「文档化例外」，未敢测 | `web/routers/jc.py:247`（`/api/jc/daily-news`） |
| `scripts/store/jc_ops_view.py` | 92 | 「只读视图不需要测」的误判 | `web/routers/jc_ops.py:38, 58`（`/api/jc/ops`、`/api/jc/freshness`） |

两者都是**生产面对用户**的端点（LLM 资讯 + 看板 SQL），缺测试 = 改一行就敢上。

## 实施

### 1. `tests/test_web_news.py` — 12 用例

**隔离策略**：先 `import ai.llm_client` 让模块进 `sys.modules`，再 `monkeypatch.setattr(llm, "generate", fake)` —— 与 `test_ai_analyze.py` 同一模式。news.py 函数体内 lazy `from ai import llm_client` 拿到同一模块对象，setattr 已生效。

| 用例 | 守住的语义 |
|---|---|
| `test_first_call_invokes_llm_and_caches` | 首次调 + 缓存写入 |
| `test_cache_hit_skips_llm_when_any_field_nonempty` | news/pick_reason 任一非空 → 缓存命中，LLM 不再被调；返回**同一对象引用**（`a is b`） |
| `test_cache_miss_when_both_fields_empty_recalls_llm` | 双空 = 缓存未填充，下次必重调（不锁死「降级态」） |
| `test_cache_isolates_by_date_and_sport` | (date, sport) 双维度隔离 |
| `test_truncates_news_to_78` | news 截断 ≤78 |
| `test_truncates_pick_reason_to_60` | pick_reason 截断 ≤60 |
| `test_try_llm_returns_empty_dict_on_exception` | LLM 异常 → `{"news":"","pick_reason":""}`，永不 raise |
| `test_try_llm_returns_empty_dict_on_none_result` | LLM 返回 `None`/`{}` → 同视为空 |
| `test_try_llm_handles_missing_fields_as_empty` | 部分字段缺失 → 缺失补 `""` |
| `test_try_llm_strips_whitespace` | 字段 trim |
| `test_build_prompt_contains_required_tokens` | prompt 必含 label / context / picks_ctx / JSON schema |
| `test_build_prompt_label_substitution` | 不同 label 正确插值 |

### 2. `tests/test_jc_ops_view.py` — 9 用例

**PG mock 策略**：用 fake 上下文管理器替 `pg.read_conn`。`queue` 是 `_make` 闭包变量，所有 fake_read_conn 调用共享同一队列（ops_snapshot 会调 4 次 read_conn，每次走同一 queue，按 cursor.execute 顺序消费）。**关键修正**：cursor 必须实现 `__enter__/__exit__`（代码用 `with conn.cursor() as cur:`），第一次写漏了 → 报 TypeError 而非业务异常。

| 用例 | 守住的语义 |
|---|---|
| `test_ops_snapshot_returns_four_sections_with_ok_marks` | 四段全部 ok + 数据正确 |
| `test_ops_snapshot_degrades_only_failed_section` | topics 异常 → 仅 `ok_topics=False`，其他三段仍 ok |
| `test_ops_snapshot_all_sections_fail_still_returns_shape` | 全失败 → 仍返回 4 段 + 4 个 ok=False |
| `test_ops_snapshot_does_not_raise_on_exception` | 4 种异常（Runtime/Value/Type/Key）混合 → 永不 raise |
| `test_ops_snapshot_logs_exception_class` | 异常时 `logger.error` 记录了 `ConnectionError` 类名 |
| `test_ops_snapshot_sql_keys_complete` | `_SQLS` 必须含 teams/topics/quota/rejects |
| `test_fetch_returns_rows_as_dict_list` | `_fetch` 正常路径：rows 是 `list[dict]` |
| `test_fetch_swallows_exception_and_returns_empty_false` | 异常 → `([], False)` |
| `test_fetch_returns_false_on_invalid_role` | `pg.read_conn` 自身抛 ValueError → 兜底 |

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P3-1/2 收尾（`cac071a`） | 905 | 61 | 0 |
| **本轮 P3-4/5（新增 21）** | **931** | **70** | **0** |

3 次连跑稳定（931/70/0、931/70/0、931/70/0）；首次跑的 1 个 flake（`test_predict_defaults_to_manual_trigger`，test-order dependent 偶发，单跑必过）属预存问题，与本轮新增测试无关。

## 未触及

- `web/routers/jc.py:247` 端点本体的端到端测试（要 mock `web_store.bjt_today` + `_news_context` + `_picks_context`，ROI 较低；单元已守 cache/截断/降级契约）
- `.coverage` 加入 `.gitignore`（下一轮 chore 顺手做）

## 关键决策

1. **monkeypatch setattr > sys.modules 替模块**（news）：先 `import ai.llm_client` 再 `setattr(llm, "generate", fake)`。与 `test_ai_analyze.py` 一致；风险更小（不污染其他测试的 `sys.modules["ai.llm_client"]`）。
2. **闭包共享 queue > 每次新建**（jc_ops_view）：`ops_snapshot` 调 4 次 `pg.read_conn`，每次新建 queue 会让 cursor 始终拿 list[0]——第一次写漏了这个语义，闭包化 `_make` 修好。
3. **cursor 必须实现 `__enter__`**：jc_ops_view 用 `with conn.cursor() as cur:`，cursor 缺失 contextmanager 协议时 Python 抛 `TypeError` 而非业务异常，遮盖真实测试目标——第二次写才意识到。
4. **caplog 不指定 logger 名**：`core.log.logger` 与 caplog 默认 `propagate=True` 时直接抓全局 ERROR 即可，无需绑定 logger name（绑定错了 `msgs == []`）。
5. **缓存命中语义 = 同对象引用**：`_NEWS_CACHE.get(key)` 返回 dict 引用，缓存命中时调用方拿到的就是缓存里的对象（`a is b`）。改实现如果改成 `dict(_NEWS_CACHE[key])` 会破坏这个语义，测试守住。

## 验证

```bash
python -m pytest tests/test_web_news.py tests/test_jc_ops_view.py \
    --cov=web.services.news --cov=store.jc_ops_view \
    --cov-report=term-missing -q
# scripts\store\jc_ops_view.py      27      0   100%
# web\services\news.py              25      0   100%
# TOTAL                             52      0   100%
```
