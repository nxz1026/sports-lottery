# 精修阶段 P3 报告 — 0%/低覆盖文件补测 + pre-existing flake 修复

- 完成时间：2026-10-13
- 全量测试：1036 passed / 70 skipped / 0 failed（10x 压测验证）
- 提交：4（P3-4/5/6/7 累积）
- 覆盖度增量：6 文件 0%/低覆盖 → 高覆盖 + 1 pre-existing flake 根因修复

## 背景

上一轮 P3 清零后，覆盖率扫描锁定 + 候选列表包含：
- 4 个真用 0%/低覆盖文件（news / jc_ops_view / jc_load / jc_view 聚合）
- 3 个 DB 密集 upsert 写手（upsert_fixtures / upsert_official / upsert_results）
- 1 个 web 路由层（web/routers/combo.py 11%）
- 1 个 pre-existing test-order dependent flake（test_predict_defaults_to_manual_trigger）

加上工作区遗留清理与 .gitignore 规则生效。

## 实施

### P3-8 ~ P3-10：upsert_* 写手单测

**`tests/test_upsert_official_units.py`** — 21 用例（`upsert_official.py` 30% → **62%**）
- 常量完整性：STG_TABLES（7 topic）/ REQUIRED_FIELDS / INSERT_SQL 模板
- canonical_src_hash：稳定 / 键序无关 / Unicode 安全 / 嵌套 dict
- reject_reason：合法 / 非 dict / 缺 src_hash / 缺必需字段 / 空值视缺
- _parse：合法 / 非法 JSON / Unicode / 空串
- _reject：累计 + MAX_REJECTED_DETAIL 截断 + RAW_PREVIEW_CHARS 预览

未覆盖 20 行：`load_file` 端到端（已被 test_upsert_from_raw.py 真包测试覆盖）

**`tests/test_upsert_results_units.py`** — 13 用例（`upsert_results.py` 34% → **100%**）
- 常量模板：RESULT_SQL / LOG_SQL
- _params 字段映射（AF/FD source 区分）
- _reject 明细字段映射
- _af_rows / _fd_rows：mock psycopg cur + align + 执行次数（含 orphan 分支）
- run：mock 全链路 → 写 ops.ingest_log（仅 FD rejected 时）+ 聚合统计

**`tests/test_upsert_fixtures_units.py`** — 17 用例（`upsert_fixtures.py` 30% → **88%**）
- 常量完整性：BLOCK_TABLES / FIXTURE_SQL / FD_ID_SQL 模板
- league_maps：返回 (af_map, fd_map) / 跳过空 id
- read_blocks：表名校验（防 SQL 注入）/ SQL 构造 / params_hashes 过滤
- _store：单行 upsert + resolve_team 认队
- _merge_fd：FD 对齐分支（OK / unmatched / ok_synonym / rowcount=0）+ merge_fd_teams
- run：mock 全链路 → stats 字段齐全 + dropped leagues 日志 + fd_id_taken 日志

### P3-11：`tests/web/test_combo_router.py` — 20 用例（`combo.py` 11% → **93%**）

**端到端 + 单元混合**：
- `_assemble_legs` 单元 6：fixture 命中取 odds + 预测命中取 p_model + match_num 反查 + fixtures 异常降级 + load_predictions 异常降级 + 未知 side + hhad 回退
- `teams_match_safe` 单元 2：复用 team_match / 异常退化字符串相等
- `combo_eval` 端点 12：未鉴权 401 / legs 模式 200 / legs >32 → 400 / 缺 legs+selections → 400 / selections 模式 200 / selections >32 → 400 / joint_prob 范围 / stake 默认 2.0 / stake 字符串容错

**关键决策**：
- web.auth.login 用 JSON body 不是 Basic Auth（httpx `auth=(...)` 不工作 → 改 `json={username, password}`）
- web.errors.ApiError 返回 `{code, message}` 不是 `{detail}`（FastAPI HTTPException 形态）
- 响应 schema：`{legs, n_legs, n_valid, joint_prob, ..., stake, warning}` — `ev` / `kelly_fraction` 不在顶层，是 `parlay_ev` / `kelly_total`

### P3-12：pre-existing flake 根因修复

**Flake**：`tests/web/test_job_trigger_source.py::test_predict_defaults_to_manual_trigger` 偶发失败。

**根因**：fixture 只 monkeypatch `_build_cmd` 返回 `["python3", "predict.py", ...]`，但真 `subprocess.Popen` 仍执行——在 Windows 某些环境（git bash / conda / 系统 PATH）有 `python3.exe` 时真跑 predict.py → STATUS_RUNNING → `proc.wait(timeout=PREDICT_TIMEOUT_SECONDS=600)` 卡死 → `_drain_jobs(5s)` 超时。

**修法**：与 `test_m3_jobs.py` 同模式，fixture 加 fake subprocess（`_FakeProc.wait() → 0` 立即 done）：

```python
monkeypatch.setattr(jobs_mod, "subprocess",
    types.SimpleNamespace(
        Popen=_FakeProc, STDOUT=0,
        TimeoutExpired=_real_subprocess.TimeoutExpired,
    ))
```

**保留**：`_build_cmd` mock 保留（保持 trigger 测试的 cmd 语义），fake Popen 是关键。

### 工作区遗留清理

- `web/services/ai.py` / `scripts/references/ml_model_epl.json` 纯 CRLF→LF 行尾副作用 → `git checkout` 丢弃
- `scripts/bball_data/nba_elo.json` 运行时 Elo 状态 → `git rm --cached` 让 .gitignore L55 真生效
- `.coverage` / `.pytest_cache/` / `htmlcov/` / `coverage.xml` 加入 `.gitignore`

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P3 收尾（`cac071a`） | 905 | 61 | 0 |
| P3-4/5（`34c1dc8`） | 931 | 70 | 0 |
| P3-6（`f46a7b4`） | 943 | 70 | 0 |
| P3-7（`f646fee`） | 962 | 70 | 0 |
| **本轮累计（`?????`）** | **1036** | **70** | **0** |
| Δ 本轮 | **+131** | +9 fixture skip | — |

## 关键决策

1. **monkeypatch setattr > sys.modules 替模块**（news）：与 test_ai_analyze.py 一致
2. **闭包共享 queue > 每次新建**（jc_ops_view / jc_view）：多次 `pg.read_conn` 必须消费同一队列
3. **cursor 必须实现 `__enter__`**（PG mock）：`with conn.cursor() as cur:` 缺失 contextmanager 协议时抛 TypeError
4. **缓存命中 = 同对象引用**（news）：`_NEWS_CACHE.get(key)` 返回 dict 引用
5. **纯 mock > 真 DSN 依赖**（jc_load / upsert_*）：不依赖真 DSN / 真包目录
6. **`if __name__ == "__main__"` 行不算缺口**：脚本入口不被 import 触发
7. **git rm --cached > commit 数据**（nba_elo.json）：运行时数据，取消 tracked
8. **MagicMock side_effect=list 不能 mock keyword args 函数**（upsert_fixtures）：list 被当作 return_value 序列消耗，必须用 function side_effect
9. **`Composed.as_string` 需要 conn 有 `.encoding` + `.connection`**（upsert_fixtures）：Mock conn 不行，用最小 fake conn
10. **`core.log.logger` 用 loguru，pytest caplog 抓不到**（upsert_* 日志断言）：直接 mock `module.logger` 用 MagicMock 验证 `.warning.call_args_list`
11. **web.auth.login 走 JSON body 不是 Basic Auth**（combo 鉴权）：httpx `auth=(...)` 不工作
12. **web.errors.ApiError 返回 `{code, message}` 不是 `{detail}`**（combo 错误断言）：FastAPI HTTPException 形态不适用
13. **fake subprocess 模式**（test_job_trigger_source flake）：单 mock `_build_cmd` 不够，必须 mock `subprocess.Popen` 立即 done

## 验证

```bash
python -m pytest tests/test_upsert_official_units.py tests/test_upsert_results_units.py \
                  tests/test_upsert_fixtures_units.py tests/web/test_combo_router.py \
    --cov=store.upsert_official --cov=store.upsert_results --cov=store.upsert_fixtures \
    --cov=web.routers.combo --cov-report=term-missing -q
# scripts\store\upsert_fixtures.py      57      7    88%
# scripts\store\upsert_official.py      52     20    62%
# scripts\store\upsert_results.py       47      0   100%
# web\routers\combo.py                 114      8    93%
```

压测验证：10x 全量压测稳定 1036/70/0（详细结果见 background task `bg_e492cc84`）。
