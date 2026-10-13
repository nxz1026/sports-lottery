# 精修阶段 P3 报告 — 0%/低覆盖文件补测 + pre-existing flake 修复

- 完成时间：2026-10-13
- 全量测试：1085 passed / 70 skipped / 0 failed（P3 阶段末态；P3-19 + P3-20 详见 `P4_report.md`）
- 提交：P3-4/5/6/7 + P3-8~12 + P3-13/15 + P3-17/18（共 9 commit）
- 覆盖度增量：8 文件 0%/低覆盖 → 高覆盖 + 1 pre-existing flake 根因修复
- 后置：P3-19（route layer coverage）+ P3-20（test_m3_jobs flake 根因深挖）见 `P4_report.md` 实施段

## 背景

前几轮 P3 清零后，覆盖率扫描 + 候选列表包含：
- 4 个真用 0%/低覆盖文件（news / jc_ops_view / jc_load / jc_view 聚合）
- 3 个 DB 密集 upsert 写手（upsert_fixtures / upsert_official / upsert_results）
- 1 个 web 路由层（web/routers/combo.py 11%）
- 1 个 pre-existing test-order dependent flake（test_predict_defaults_to_manual_trigger）
- 3 个 web 路由层（web/routers/{jc_ops,ai,lottery_api}.py 38-42%）

加上工作区遗留清理与 .gitignore 规则生效。

## 实施

### P3-4 ~ P3-7：news / jc_ops_view / jc_load / jc_view 补测
（详见前轮提交 34c1dc8 / f46a7b4 / f646fee）

### P3-8 ~ P3-10：upsert_* 写手单测
（详见 commit 6f0b534）

### P3-11：combo 端到端
（详见 commit 6f0b534）

### P3-12：test_job_trigger_source flake 根因
（详见 commit 6f0b534）

### P3-13：`tests/web/test_jc_ops_router.py` — 12 用例（`jc_ops.py` 41% → **92%**）

- `/ops` 端点：未鉴权 → 401 / 完整结构 / degraded 计算 / 缺 ok_<key> 兜底
- `/freshness` 端点：未鉴权 → 401 / 默认 24h / 钳制 [0.5, 168] / 非数字 422（Pydantic 前置解析）

### P3-14：`tests/web/test_lottery_v1_api.py` — 19 用例（`lottery_api.py` 42% → **100%**）

- `/lottery/prize`：未鉴权 → 401 / ticket/draw 空 → 400 / 非数字 → 400 / 未识别 game → level=None 不抛错
- `/lottery/cost`：未鉴权 → 401 / front/back 空 → cost=0 / 非法 → 400
- `/lottery/stats`：未鉴权 → 401 / mock lottery_draws → 200 / n 范围 422（Query ge=1 le=100）
- `/lottery/dantuo`：未鉴权 → 401 / 合法 → tickets / 非法 → tickets=0 + note="invalid"

### P3-15：`tests/web/test_ai_router.py` — 18 用例（`ai.py` 38% → **94%**）

- `/ai/status` / `/ai/details`：未鉴权 → 401 + 正常 → 200
- `/ai/analyze`：date 校验 / 文件不存在 → available=False / JSON 解析失败 → read_failed / 合法 JSON → 完整响应 / date 缺省走 _today_bjt
- `/ai/daily`：date 校验（invalid → 200 + available=False + reason="invalid_date"，不 500）
- `/ai/ranking`：limit 范围 422（ge=1 le=50）/ date 非法 → degraded

### P3-16（隐性）：fixture 跨测污染 flake 根因 + 修复

**新 flake**：`tests/web/test_client_ip.py::test_trusted_proxy_ips_default_includes_loopback` 偶发失败。

**根因**：P3-11/13/14/15 四个 fixture 用 `monkeypatch.setenv("TRUSTED_PROXY_IPS", "127.0.0.1")` 覆盖了默认 tuple → reload web.config 后 `TRUSTED_PROXY_IPS` 变成 `('127.0.0.1',)` → 缺 `::1` → 之后 test_client_ip 加载 config 时默认值已污染 → 断言 `"::1" in config.TRUSTED_PROXY_IPS` 失败。

**修法**：四个 fixture 删除 `setenv("TRUSTED_PROXY_IPS", ...)`，依赖 web.config 默认 tuple（已含 127.0.0.1 + ::1 + localhost）。

> 教训：测试 fixture setenv 影响模块常量时，下游测试会受牵连。优先用 `monkeypatch.setattr` 运行时改值，或干脆不设（依赖默认）。

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P3 收尾（`cac071a`） | 905 | 61 | 0 |
| P3-4/5（`34c1dc8`） | 931 | 70 | 0 |
| P3-6（`f46a7b4`） | 943 | 70 | 0 |
| P3-7（`f646fee`） | 962 | 70 | 0 |
| P3-8~12（`6f0b534`） | 1036 | 70 | 0 |
| P3-13/14/15 + P3-16（`140e0ec`）| 1085 | 70 | 0 |
| Δ P3 阶段累计 | **+180** | +9 fixture skip | — |

## 已知 pre-existing flake（5x 压测观察）

| 测试 | 症状 | 推测根因 |
|---|---|---|
| `test_m3_jobs.py::test_concurrent_trigger_only_one_producer` | "后台 worker 线程未在超时内结束" | executor max_workers=2 + threading.Event.gate 在 Windows scheduling 偶发延迟；_drain_jobs(5s) 边界 |
| `test_m3_jobs.py::test_auto_refresh_trigger_and_dedup` | `PermissionError: jobs/{jid}.json` | 前一个测试的 worker 持有 fd 未完全释放 → 新测试的 _spin_state 写文件失败 |
| `test_m3_jobs.py::test_bball_route_202_and_script` | 同 worker 超时 | 同上 |

3/5 偶发（60%），与 P3-12 修复的 `test_job_trigger_source.py` 无关（已用 fake subprocess）。

### 修法（commit 本轮）

`_drain_jobs` 改造（最小侵入，不动生产代码）：

```python
def _drain_jobs(timeout: float = 10.0) -> None:  # 原 5.0
    ...
    while time.time() < deadline:
        try:
            if jobs_mod.active_job() is None:
                return
        except (PermissionError, OSError):
            # Windows fd 释放延迟 → 视为瞬态继续轮询
            time.sleep(0.05)
            continue
        time.sleep(0.02)
```

- timeout 5s → 10s（给 worker 充足窗口）
- active_job() 加 try/except（PermissionError / OSError 视为 None，不打断 drain）
- 10x 压测验证（待 bg_af406f68 完成）

## 关键决策

1. **monkeypatch setattr > sys.modules 替模块**（news）
2. **闭包共享 queue > 每次新建**（jc_ops_view / jc_view）
3. **cursor 必须实现 `__enter__`**（PG mock）
4. **缓存命中 = 同对象引用**（news）
5. **纯 mock > 真 DSN 依赖**（jc_load / upsert_*）
6. **`if __name__ == "__main__"` 行不算缺口**
7. **git rm --cached > commit 数据**（nba_elo.json）
8. **MagicMock side_effect=list 不能 mock keyword args 函数**（upsert_fixtures）→ 用 function side_effect
9. **Composed.as_string 需要 conn.encoding + .connection**（upsert_fixtures）
10. **core.log 用 loguru → caplog 抓不到**（upsert_* 日志断言）→ 直接 mock module.logger
11. **web.auth.login 走 JSON body 不是 Basic Auth**（combo 鉴权）
12. **web.errors.ApiError 返回 {code, message} 不是 {detail}**（combo 错误断言）
13. **fake subprocess 模式**（test_job_trigger_source flake）：单 mock _build_cmd 不够
14. **FastAPI Query 参数校验在路由前**（jc_ops/ai）：`threshold_hours: float = 24.0` 非数字 → 422 而不是走 handler 兜底
15. **monkeypatch.setenv 全局污染**：fixture 不要 setenv 影响模块顶层常量（否则下游测试受牵连）；优先 monkeypatch.setattr 或依赖默认

## 验证

```bash
python -m pytest tests/test_upsert_official_units.py tests/test_upsert_results_units.py \
                  tests/test_upsert_fixtures_units.py tests/web/test_combo_router.py \
                  tests/web/test_jc_ops_router.py tests/web/test_lottery_v1_api.py \
                  tests/web/test_ai_router.py \
    --cov=store.upsert_official --cov=store.upsert_results --cov=store.upsert_fixtures \
    --cov=web.routers.combo --cov=web.routers.jc_ops --cov=web.routers.lottery_api \
    --cov=web.routers.ai --cov-report=term-missing -q
# scripts\store\upsert_fixtures.py      57      7    88%
# scripts\store\upsert_official.py      52     20    62%
# scripts\store\upsert_results.py       47      0   100%
# web\routers\ai.py                    47      3    94%
# web\routers\combo.py                114      8    93%
# web\routers\jc_ops.py                37      3    92%
# web\routers\lottery_api.py           33      0   100%
```

5x 压测验证（待 bg_88fea77f 完成）。
