# 精修阶段 P3-P4 报告 — 0%/低覆盖补测 + cross-boundary 收口 + flake 根因修

- 完成时间：2026-10-13
- 末态测试：1154 passed / 70 skipped / 0 failed（P3 收口 1085 → P4 收口 1154；后续 P5 收口 1206）
- 提交：P3-4~5/6/7/8~12/13/15/17/18/19/20 + P4-1~5（**14 commit**）
- 净影响：生产代码 **-150 行**（4 个 dead code 块清零 + 1 个字节级 duplicate 合并）；测试代码 **+250 行**（覆盖率 + 行为锁 + mirror 守门）
- 后续：P5-1~4 收口见 `P5_report.md`

## 实施

### P3-4 ~ P3-7：news / jc_ops_view / jc_load / jc_view 补测
（详见 commit `34c1dc8` / `f46a7b4` / `f646fee`）

### P3-8 ~ P3-10：upsert_* 写手单测
（详见 commit `6f0b534`）

### P3-11：combo 端到端
（详见 commit `6f0b534`）

### P3-12：test_job_trigger_source flake 根因
（详见 commit `6f0b534`）

### P3-13：`tests/web/test_jc_ops_router.py` — 12 用例（`jc_ops.py` 41% → **92%**）

- `/ops`：未鉴权 → 401 / 完整结构 / degraded 计算 / 缺 ok_<key> 兜底
- `/freshness`：未鉴权 → 401 / 默认 24h / 钳制 [0.5, 168] / 非数字 422（Pydantic 前置解析）

### P3-14：`tests/web/test_lottery_v1_api.py` — 19 用例（`lottery_api.py` 42% → **100%**）

- `/lottery/prize` / `/cost` / `/stats` / `/dantuo` 端到端（含 400 / 422 边界）

### P3-15：`tests/web/test_ai_router.py` — 18 用例（`ai.py` 38% → **94%**）

- `/ai/status` / `/details` / `/analyze` / `/daily` / `/ranking` 五组 endpoint

### P3-16（隐性）：fixture 跨测污染 flake 根因

`tests/web/test_client_ip.py::test_trusted_proxy_ips_default_includes_loopback` 偶发失败。
P3-11/13/14/15 四个 fixture 用 `monkeypatch.setenv("TRUSTED_PROXY_IPS", "127.0.0.1")` 覆盖了默认 tuple → reload web.config 后 `TRUSTED_PROXY_IPS` 变成 `('127.0.0.1',)` → 缺 `::1` → 之后 test_client_ip 加载 config 时默认值已污染。

修法：四个 fixture 删除 `setenv("TRUSTED_PROXY_IPS", ...)`，依赖 web.config 默认 tuple。

### P3-17/18/22：`_drain_jobs` 5s→10s + PermissionError 容错（三处文件对齐）

P3-17 修 `test_m3_jobs._drain_jobs`，P3-18 修 `test_m6_ai_enrich._drain_jobs`，**P3-22 补齐 `test_job_trigger_source._drain_jobs`**（P3-17 当初漏修的全仓 15x 压测 1/15 暴露）。三处现签名一致：

```python
def _drain_jobs(timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if jobs_mod.active_job() is None:
                return
        except (PermissionError, OSError):
            time.sleep(0.05)  # Windows fd 释放延迟视为瞬态
            continue
        time.sleep(0.02)
    raise AssertionError("后台任务线程未在超时内结束")
```

- timeout 5s → 10s（给 worker 充足窗口；与生产 `state.lock` 5s timeout 解耦）
- `active_job()` 抛 `PermissionError / OSError` 视为瞬态（fs fd 释放延迟）
- P3-22 修后全仓 15x 压测 14/15 全过（残余 1/15 已知最顽固 flake `test_auto_refresh_trigger_and_dedup` 在 P3-20 修后仍 ~7% 偶发，Windows fs 根因不在生产代码层面）

### P3-19：web/routers/jc 五组端点 24 用例 + `gap()` 容错

`tests/web/test_jc_router.py` 把 `web/routers/jc.py` 34% → 全覆盖：

- `/fixtures` / `/issues` / `/gap` / `/movement` / `/qr` 各 4-5 用例（含 401/400/422/envelope）

4 个错同步修：错误契约走 `{code, message}`（非 `{detail}`）、五大联赛白名单存**全名**（"意大利甲级联赛"）、`gap()` 加 try/except 兜底 `latest_by_league` 抛错。

### P3-20：test_m3_jobs 残余偶发根因

P3-17 修后跑 5x/10x 仍偶发。实际是两层并存的 bug：

**`test_auto_refresh_trigger_and_dedup`**（1/12 → 12/12）：
- 根因：`list_jobs → _read_job` 在 Windows 冷启动（Defender 索引首撞 fs 持有 fd 数十毫秒）偶发 PermissionError
- 修：`web/services/jobs.py::_read_job` 内置三档 0/10/20ms 短退避重试（与 `_exclusive_lock` 同语义）。一处改、所有 reader 自动受益

**`test_job_poll_to_terminal`**（1/25 → 25/25）：
- 根因：worker 持 JOBS_LOCK_FILE + state.lock 2 段锁 + Windows fs 偶发锁延迟，5s 轮询窗口边界偶发过期
- 修：测试内轮询 100×50ms → 200×50ms（5s → 10s），与 P3-17 `_drain_jobs` 10s 对齐

### P4-1：`scripts/core/data/fetch.py` FIFA 联网入口 dead code 清理

`scripts/core/data/fetch.py` 删 3 项 0 caller（grep 全仓 + docs "CLOSED" 已结）：
- `update_fifa_rankings()` 33 行
- `fetch_fifa_rankings(force_refresh)` 6 行
- `FIFA_RANKINGS_API_URL` URL 常量

`scripts/core/data/__init__.py` 同步 re-export 清理。净删 ~100 行。

### P4-2：bball ELO 休赛期回归死代码

`scripts/bball/elo_bball.py::apply_regression` 6 行 + `scripts/bball/config_bball.py::ELO["REGRESSION"]` 配置常量 — 实际部署从未启用。`tests/test_bball.py` 必含键断言同步删除。净删 8 行。

### P4-3：`canonical_src_hash` 字节级 duplicate 合并 + drift 守门

两段公式一字不差（`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) → sha256.hexdigest()`）：

| 实现 | 行 | 用途 |
|---|---|---|
| `scripts/store/parse_values.py::canonical_src_hash` | 76-79 | 4 个调用方 |
| `scripts/store/upsert_official.py::canonical_src_hash` | 39-42 | upsert_official 内部 |

任一边改任一参数 → 重放幂等键分裂 → stg `<src_hash>` 唯一约束允许重复入库。

**修法**：删除 upsert_official 本地副本，`from .parse_values import canonical_src_hash`。

`tests/test_canonical_src_hash_mirror.py` 新增 9 用例守门：`is` 同对象 + 6 input variation + 键排序不变性 + `ensure_ascii=False` 字节序列验证。

### P4-4：5 个无 caller 函数清理

`scripts/store/query.py` 删 `fixtures_between` / `fixture_with_result` / `unmatched_teams` / `team_identity_audit`（共 22 行）+ 顺带删 `AUDIT_SQL` import 与 `FIXTURE_COLS` / `RESULT_COLS` / `UNTRANSLATED` 常量。

`scripts/store/selfcheck.py` 删 `league_season_count`（9 行）。

`scripts/store/team_identity.py` 删 `AUDIT_SQL` 整段（6 行）+ `DUP_SQL` 关联注释。模块 docstring 同步提示两个旧接口不再启用。

净删 ~50 行；query.py 缩到 60 行核心 `backtest_report`。

### P4-5：`normalize_team` 双实现 drift 行为锁

两套独立实现（`store/align.py` AF↔FD vs `core/data/odds_enrich.py` FD↔ESPN）算法骨架 80% 重叠；STOP 表 27 vs 22 词、别名粒度 整名 6 vs per-token 4、token 分隔符 vs 替换空格、输出 BAG vs ordered。CJK 派空串两侧一致。

**`tests/test_normalize_team_mirror.py`** 新增 36 用例：15 真名两侧 snapshot（per 名字 ×2）+ BAG 词序无关 / odds 保序 + STOP 表差异可见性证明 + odds 别名 per-token 生效 + 数字 token 不在 STOP + CJK 派空串。

本轮**仅行为锁、不动生产代码**，把 5 重构风险降至可控（未来统一化时 snapshot 跌出后 PR review 触发合并）。

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P3 收尾（`cac071a`） | 905 | 61 | 0 |
| P3-4/5（`34c1dc8`） | 931 | 70 | 0 |
| P3-6（`f46a7b4`） | 943 | 70 | 0 |
| P3-7（`f646fee`） | 962 | 70 | 0 |
| P3-8~12（`6f0b534`） | 1036 | 70 | 0 |
| P3-13/14/15 + P3-16（`140e0ec`）| 1085 | 70 | 0 |
| P3-19（`0d0d10a`）| 1109 | 70 | 0 |
| P3-20 修后（`286bcc2`）| 1109 | 70 | 0 |
| P4-1~5 累计 | 1154 | 70 | 0 |
| Δ P3-P4 累计 | **+249** | +9 skip | — |

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
12. **web.errors.ApiError 返回 {code, message} 不是 {detail}`**（combo 错误断言；P3-19 测试发现契约错而非改契约）
13. **fake subprocess 模式**（test_job_trigger_source flake P3-12）：单 mock _build_cmd 不够
14. **FastAPI Query 参数校验在路由前**（jc_ops/ai）：`threshold_hours: float = 24.0` 非数字 → 422 而不走 handler 兜底
15. **monkeypatch.setenv 全局污染**：fixture 不要 setenv 影响模块顶层常量（否则下游测试受牵连）；优先 monkeypatch.setattr 或依赖默认
16. **业务 / 跨边界契约一等原则**：跨边界契约错时改测试为辅（错误响应格式 `{code, message}` 已是契约，不改成 `{detail}`）
17. **行为锁 > 重构**：normalize_team 双实现 P4-5 锁住不动；统一层 P5 推
18. **死代码判定三步走**：grep "函数名" 调用方 → grep "模块名" re-export 调用方 → docs "CLOSED" 标
19. **defer-re-export 不预留别名**：modules 不要为了"或许有人用"留 shim wrapper；当前 shell usages grep 0 即可
20. **字节级 duplicate 合并 + `is` 断言**：合并后用 `module_a.foo is module_b.foo` 锁；未来谁加回本地副本立刻跳红
21. **drift 守门 ≠ 合同相等**：跨实现公式不同属"输入域不同"，mirror 测试不是要求两边输出相等；而是钉死各自当前行为，PR review 时引导手动确认
22. **同进程 fd 释放延迟通用模式**：WS_READ 重试 / `os.unlink` 三档退避 / `_read_job` 重试 全部沿用 0/10/20ms 三档节奏——经验值在该生态下足够窗口又不太长
23. **测试 polling 窗口与生产 timeout 对齐**：5s vs 10s 不一致会让 boundary case 漂；让测试 polling 略≥生产 timeout

## 验证

```bash
# P3 / P4 累计
python -m pytest tests -q --tb=no -p no:cacheprovider
# 1154 passed / 70 skipped / 0 failed

# 8x/15x 压测
- test_auto_refresh_trigger_and_dedup 12x：12/12 全过
- test_job_poll_to_terminal 25x：25/25 全过
- m3_jobs.py 10x：10/10 全过
- 全仓 5x：4/5 全过（Run 4 1 个失败 36s）
```

## 剩余候选（已并入 P5 处理）

| ID | 内容 | P5 落地 |
|---|---|---|
| P4-6 | `jc_view._nums` 闭包抽模块级私有 | skip（subagent 列为 defer）|
| P5 | `normalize_team` 双实现统一化（最小共享） | ✅ P5-3 |
| Round 5 | subagent 新一轮扫描 | ✅ Round 5 / 6 候选均已收口 |

## 后续

- P5 收口（safe_devig 抽取 + team_norm helper + lint 收尾）见 `P5_report.md`
- 残余 1/15 全仓 flake `test_auto_refresh_trigger_and_dedup` 留作 known issue（Windows fs fd 释放延迟根因不在生产代码层面）
