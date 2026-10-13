# 精修阶段 P4 报告 — cross-boundary 重复 / dead code 清理 + 跨实现 drift 行为锁

- 完成时间：2026-10-13
- 全量测试：1154 passed / 70 skipped / 0 failed
- 提交：6（P3-19 + P4-1 ~ P4-5）+ P3-20 flake 根因深挖
- 净影响：生产代码 **-150 行**（4 个 dead code 块清零 + 1 个字节级 duplicate 合并）；测试代码 **+250 行**（3 个 mirror 锁 / 行为锁）
- 后续：P5-1~4 收口见 `P5_report.md`（safe_devig 抽取 + team_norm helper + 4 处 lint / dead local）

## 背景

P3 阶段（覆盖率补测）结束后，剩余清单来自 **subagent Round 4 扫描**，分四类 ROI 排序：

1. **dead code**：grep 0 caller、docs 已 "CLOSED"、疑点已结 — 删（最低风险）
2. **字节级重复**：两段公式一字不差，单点改能漂移另一处 — 合并 + 守门
3. **算法镜像 / 跨边界契约**：两套独立实现骨架 80% 重叠但 STOP / 别名各剪裁 — 行为锁
4. **pre-existing flake**：覆盖度补测后浮出水面的 Windows fs 边界偶发 — 根因修

排除 P2-11~P2-26（已 lock）与 P1（更高优先级）的项目；本轮聚焦 ROI 最高的 6 项。

## 实施

### P3-19（route layer 端到端补测 + gap 容错）

**`tests/web/test_jc_router.py`** 新增 24 用例（`web/routers/jc.py` 34% → 全覆盖）：

- `/fixtures`：未鉴权 → 401 / 合法 envelope / day 非 ISO → 400
- `/issues`：未鉴权 → 401 / limit 1..200 边界 / 非整数 → 400 / 默认 20
- `/gap`：未鉴权 → 401 / top 钳制 [1, 30] / `latest_by_league` 抛错降级
- `/movement`：未鉴权 → 401 / 五大联赛白名单过滤 / day 缺省=latest / envelope
- `/qr`：未鉴权 → 401 / http(s) only / PUBLIC_BASE_URL fallback / javascript: 注入拒绝

**4 个错同步修：**
- 错误契约走 `web.errors._payload` = `{code, message}`，不是 `{detail}`
- 五大联赛白名单存**全名**（"意大利甲级联赛"），非"意甲"简称
- `gap()` 内 `latest_by_league` 加 try/except 兜底（与契约 §5.5 "DB 不可达 → 200 + 空 items" 一致）

### P4-1：`scripts/core/data/fetch.py` FIFA 联网入口 dead code 清理

`scripts/core/data/fetch.py` 删 3 项 0 caller（grep 全仓 + docs "CLOSED" 已结）：
- `update_fifa_rankings()` 33 行 — 主链未用；M0 / DATA_CONTRACT 疑点 15 已标 "未被调用"
- `fetch_fifa_rankings(force_refresh)` 6 行 — 仅 `core.rankings` 薄包装
- `FIFA_RANKINGS_API_URL` URL 常量 — 仅上述两个函数引用

`scripts/core/data/__init__.py` 同步 re-export 清理，文档说改指 `core.rankings.fetch_fifa_rankings`。

净删 ~100 行。

### P4-2：bball ELO 休赛期回归死代码

`scripts/bball/elo_bball.py::apply_regression` 6 行 + `scripts/bball/config_bball.py::ELO["REGRESSION"]` 配置常量 — 实际部署从未启用（走 `load_ratings()` 一次性 init 路径）。

`tests/test_bball.py::test_elo_constants_exist_with_numeric_values` 必含键断言同步删除 REGRESSION。

净删 8 行；17 bball 单测全过。

### P4-3：`canonical_src_hash` 字节级 duplicate 合并 + drift 守门

两段公式一字不差（`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) → sha256.hexdigest()`）：

| 实现 | 行 | 用途 |
|---|---|---|
| `scripts/store/parse_values.py::canonical_src_hash` | 76-79 | 4 个调用方（collector_pull / parse_collector / ...）|
| `scripts/store/upsert_official.py::canonical_src_hash` | 39-42 | upsert_official 内部 |

任一边改任一参数 → 重放幂等键分裂 → stg `<src_hash>` 唯一约束允许重复入库。

**修法**：删除 upsert_official 本地副本，加 `from .parse_values import canonical_src_hash`。

**`tests/test_canonical_src_hash_mirror.py`** 新增 9 用例守门：
- `is` 同对象断言（结构证据：未来不会两套）
- 6 input variation（嵌套 dict / 中文 / list-of-dict / None 嵌套 / 空 / jc_issue 真实形态）parametrize 对照
- 键排序不变性（`sort_keys=True` 契约锁）
- `ensure_ascii=False` 直接构造 sha256 字节序列验证

### P4-4：5 个无 caller 函数清理

`scripts/store/query.py` 删 4：
- `fixtures_between`（5 行）— [start,end) 区间赛程
- `fixture_with_result`（8 行）— 单场含赛果多源
- `unmatched_teams`（5 行）— 翻译补全待办
- `team_identity_audit`（4 行）— 认队体检 wrapper

+ 顺带删 `from store.team_identity import AUDIT_SQL` import + `FIXTURE_COLS` / `RESULT_COLS` / `UNTRANSLATED` 常量（仅被上述 4 函数引用）。

`scripts/store/selfcheck.py` 删 `league_season_count`（9 行）— 回填自检。

`scripts/store/team_identity.py` 删 `AUDIT_SQL` 整段（6 行）— 与 `team_identity_audit` 联动失去唯一外部 caller + DUP_SQL 关联注释 一并清理；模块 docstring 同步提示两个旧接口不再启用。

净删 ~50 行；query.py 缩到 60 行核心 `backtest_report`。

### P4-5：`normalize_team` 双实现 drift 行为锁

两套独立实现：
- `store/align.py::normalize_team`（AF↔FD 对齐，输出 **sorted BAG**）
- `core/data/odds_enrich.py::normalize_team`（FD↔ESPN 对齐，输出 **ordered 流式**）

算法骨架（NFKD + Mn 去除 + split + 噪音 token 过滤）80% 重叠；STOP 表 / 别名表各剪裁差异：

| 维度 | align | odds_enrich |
|---|---|---|
| STOP 词数 | 27（含 vfl/afc/spvgg/tsv/fsv/sv/the/of...）| 22（含 vfb/tsg/sport/sportivo/cp/de/la/futbol/fussball/fut...）|
| 别名粒度 | 整名字典 6 条 | per-token 字典 4 条 + 整名兜底 |
| Token 分隔符 | `[^a-z0-9]+` 切 | `[^a-z0-9]+` 替空格 |
| 输出顺序 | sorted bag（词序无关）| ordered stream（保序）|
| CJK 输入 | 派空串（事实）| 派空串 |

任何 PR 改任一边（最常：加新 STOP 词 / 改别名 / 改 split），snapshot 立刻跳红，让 reviewer 显式确认另一侧是否同步。

**`tests/test_normalize_team_mirror.py`** 新增 36 用例：
- 15 真名两侧 snapshot（per 名字 × 2 = 30）
- align BAG 词序无关 / odds 保序
- 两侧 STOP 表不同可见性证明（VfL Bochum 触发，确认 drift 真存在）
- odds 别名 per-token 生效（koln/munchen/lyonnais）
- 数字 token 不在 STOP，保 "VfL 10" → "vfl 10"
- align CJK 派空串（事实非缺陷）

本轮**仅行为锁、不动生产代码**，把 5 重构风险降至可控（未来统一化时 snapshot 跌出后 PR review 触发合并）。

### P3-20：test_m3_jobs 残余偶发根因深挖

P3-17 修复后跑 5x/10x 仍偶发；subagent 报告根因"在 run_job 内 _spin_state 写文件"，但实际是两层并存的 bug：

**`test_auto_refresh_trigger_and_dedup`**（1/12 偶发 → 12/12）：
- 根因：`list_jobs → _read_job` 在 Windows 冷启动（Defender 索引首撞 fs 持有 fd 数十毫秒）偶发 PermissionError
- 修：`web/services/jobs.py::_read_job` 内置三档 0/10/20ms 短退避重试（与 `_exclusive_lock` 同语义）。一处改、所有 reader 自动受益（`list_jobs` / `active_job` / `get_job` / `_spin_state` 等 5 个 caller）

**`test_job_poll_to_terminal`**（1/25 偶发 → 25/25）：
- 根因：worker 持 JOBS_LOCK_FILE + state.lock 2 段锁 + Windows fs 偶发锁延迟，5s 轮询窗口边界偶发过期
- 修：测试内轮询 100×50ms → 200×50ms（5s → 10s），与 P3-17 `_drain_jobs` 的 10s timeout 对齐；行为不变

**全量压测**：修复前 m3_jobs.py 10x 中 1/10 偶发；修复后 10x 全过。

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P3 收尾 | 1085 | 70 | 0 |
| P3-19 | 1109 | 70 | 0 |
| P4-1~5 累计 | 1154 | 70 | 0 |
| P3-20 修后 | 1154 | 70 | 0 |
| Δ 本轮累计 | **+69** | — | — |

## 关键决策

16. **业务 / 跨边界契约一等原则**：跨边界契约错时改测试为辅（错误响应格式 `{code, message}` 已是契约，不改成 `{detail}`）
17. **行为锁 > 重构**：normalize_team 双实现本轮锁住不动；统一层 P5 推（行为锁已就位）
18. **死代码判定三步走**：grep "函数名" 调用方 → grep "模块名" re-export 调用方 → docs "CLOSED" 标
19. **defer-re-export 不预留别名**：modules 不要为了"或许有人用"留 shim wrapper，移除 + re-export 一并删；当前 shell usages grep 0 即可
20. **字节级 duplicate 合并 + `is` 断言**：合并后用 `module_a.foo is module_b.foo` 锁；未来谁加回本地副本立刻跳红
21. **drift 守门 ≠ 合同相等**：跨实现公式不同属"输入域不同"，mirror 测试不是要求两边输出相等；而是钉死各自当前行为，PR review 时引导手动确认
22. **同进程 fd 释放延迟通用模式**：WS_READ 重试 / `os.unlink` 三档退避 / `_read_job` 重试 全部沿用 0/10/20ms 三档节奏——经验值在该生态下足够窗口又不太长
23. **测试 polling 窗口与生产 timeout 对齐**：5s vs 10s 不一致会让 boundary case 漂；让测试 polling 略≥生产 timeout

## 验证

```bash
python -m pytest tests/test_upsert_official_units.py tests/test_upsert_results_units.py \
                  tests/test_upsert_fixtures_units.py tests/test_jc_view_aggregations.py \
                  tests/test_jc_ops_view.py tests/test_jc_load.py tests/test_web_news.py \
                  tests/test_canonical_src_hash_mirror.py tests/test_normalize_team_mirror.py \
                  tests/web/test_combo_router.py tests/web/test_jc_ops_router.py \
                  tests/web/test_lottery_v1_api.py tests/web/test_ai_router.py \
                  tests/web/test_jc_router.py -v 2>&1 | tail -5
# 199 passed ... (P3 全量回归)
```

**8x/15x 压测**：
- `test_auto_refresh_trigger_and_dedup` 12x：12/12 全过
- `test_job_poll_to_terminal` 25x：25/25 全过
- m3_jobs.py 10x：10/10 全过
- 全仓 5x：4/5 全过（Run 4 1 个失败 36s，待 Round 5/15 找真凶）

## 剩余候选（P5+）

| ID | 内容 | ROI | 风险 | 备注 |
|---|---|---|---|---|
| P4-6 | `jc_view._nums` 闭包抽模块级私有 | 小 | 极低 | subagent 列为 defer；可入 P5 |
| P5 | `normalize_team` 双实现统一化 | 中-大 | 中-大 | P4-5 锁住行为后可控推 |
| Round 5 | subagent 起新一轮扫描 | 待 | 待 | 看 Round 4 剩余 4 类硬约束 |
| pre-existing 全仓 flake | 全仓 15x 找残余偶发 | 待 | 待 | 当前压力下孤立 1 失败 |

## 后续

- **P5 - A**：normalize_team 抽最小共享（NFKD+Mn 提取 + lowercase）3 行 helper；不动 tokenizer / 别名表
- **P5 - B**：跑 Round 5 explore，找新 dedup / refactor 候选
- **DOC**：`P3_report.md` 接到 P3-19 / P3-20 后（与 P4_report 平级）
