# 精修阶段 P5 报告 — cross-boundary 收口 / helper 抽取 / lint 收尾

- 完成时间：2026-10-13
- 全量测试：1206 passed / 70 skipped / 0 failed
- 提交：P5-1 + P5-2 + P5-3 + P5-4
- 净影响：生产代码 -25 行（4 处 dup/dead 收口），新增 **52 守门测试**（safe_devig 33 + team_norm 19）

## 背景

P3/P4 阶段把覆盖率、dead code、跨边界契约都收过一轮。Round 5 / Round 6
subagent 扫描聚焦"前几轮已 lock 但未触动"的桥接机会 + 残余 lint 缺口。

## 实施

### P5-1：lint 收尾（trivial）

- `web/api.py` `import time` 删
- `scripts/clv_baseline.py` `import math` 删（P2-23 后无用）
- `scripts/store/upsert_official.py` `import hashlib` 删（P4-3 收口到 parse_values 后的可观察残留）
- `web/services/source_meta.py` 2 dead local 删（`activates` / `left_start`）

### P5-2：safe_devig 收口 + jc_view._implied 提模块级

**`scripts/market/devig.py`** 新增 `safe_devig(odds, *, allow_single=False)`：
- 过滤 None / bool / 非数值 / 字符串 / 0 / <=1 / -1 / inf / nan
- 0 有效 → `None`；1 有效 + `allow_single=True` → `{key: 1.0}`（单边退化，与历史 jc_view ABI 一致）
- 1 有效 + `allow_single=False`（默认）→ `None`（h2h 必 2-side）
- ≥2 有效 → 调 `devig` 返其结果；**不做 round**（caller 决定精度）

**两处调用方收口**：
- `clv_baseline._devig_home`（18 行）→ 8 行
- `jc_view._implied`（22 行）→ 8 行

**S-4 提级**：`_implied` 嵌套闭包 → 模块级。`tests/test_jc_view_implied_mirror.py` 删 `types.FunctionType` 重建 workaround；源码层断言从 `devig(valid)` 升级为 `safe_devig`。

**新守门** `tests/test_market_devig_safe.py` **33 用例**：
- 0 有效 / 1 有效（双 mode）/ 11 类非法项过滤
- 2-side / 3-side 与 `devig` 数学一致（11 fixture）
- 数值精度（5 项 full-float 黄金值）/ 退化情形

### P5-3：normalize_team 双实现抽字符级共用 helper

P4-5 锁住 align / odds_enrich 双实现的输出行为后，本轮只抽**字符级预处理**这 100% 等价的 3 行：NFKD 分解 + 去除组合重音（Mn）+ lowercase。

**新增** `scripts/core/team_norm.py`：
- `strip_accents_and_lowercase(name) -> str`

**调用方收口**：
- `store/align.py::tokens` 改用 helper
- `core/data/odds_enrich.py::normalize_team` 改用 helper

STOP 词表 / 别名表 / token 切分 / 排序等下游动作各模块自留——P4-5 mirror
测试（36 用例）已就位，行为锁守住。

**新守门** `tests/test_team_norm_helper.py` **19 用例**：
- 真实队名（与 P4-5 mirror 共用 fixture）/ 边界 None/空/0/False
- 重音 / 上下 case / Łódź（L-with-stroke 非组合重音，NFKD 不分解）/ 数字 / CJK
- 幂等性（连续两次 strip 等价）

### P5-4：clv_baseline._BJT dead local

Round 6 explore 扫描确认 `scripts/clv_baseline.py:30 _BJT = timezone.utc` 是
dead local（全文 0 引用，且 line 69/99 直接用 `timezone.utc`），注释
"报告只写 UTC 时刻，不本地化"本身也是误导（常量名 = BJT vs 实际值 = UTC）。

P5-1 漏了：`_` 前缀模块常量被 pyflakes 跳过（pyflakes 不会标记带下划线的模块级未用 import 或赋值）。

## 测试基线

| 阶段 | passed | skipped | failed |
|---|---|---|---|
| P4-5 收尾 | 1154 | 70 | 0 |
| P5-1 | 1154 | 70 | 0 |
| P5-2 | 1187 | 70 | 0 |
| P5-3 | 1206 | 70 | 0 |
| P5-4 | 1206 | 70 | 0 |
| Δ P5 累计 | **+52** | — | — |

## 关键决策

24. **mirror 测试 ≠ 公式相等**：跨实现公式不同属"输入域不同"，mirror 不是
    要求两边输出相等；而是钉死各自当前行为，PR review 时引导手动确认
25. **P5 收口守门覆盖全分支**：`safe_devig` 33 用例覆盖 0/1/2/3-side 全部
    输入矩阵 + 11 类非法项过滤；`strip_accents_and_lowercase` 19 用例
    覆盖真实队名 / 边界 / Łódź 单一字符 / CJK / 幂等
26. **`_` 前缀 dead local pyflakes 盲区**：P5-4 显式标出此 caveat；后续扫描需
    单独检查 `_<name>` 模式
27. **不 round 的 helper 边界**：`safe_devig` 不做 round，caller 决定精度
    （jc_view 3dp / clv_baseline 原值 / future 调用方自选）；先抽公共部分，
    精度策略保持 caller 自治

## 验证

```bash
# P5-2: market.devig 守门
python -m pytest tests/test_market_devig.py tests/test_market_devig_safe.py \
                  tests/test_clv_baseline_mirror.py tests/test_jc_view_implied_mirror.py -v
# 86 + 33 + 22 + 8 = 149 passed

# P5-3: team_norm 守门
python -m pytest tests/test_team_norm_helper.py tests/test_normalize_team_mirror.py -v
# 19 + 36 = 55 passed
```

## 剩余 P 候选（Round 6 explore 输出）

- 1 处 P5-4 已收口
- **无更大桥接空间**（align vs odds_enrich 剩余差异是 domain-specific，
  P4-5 mirror 已锁）
- 不建议做：`_clv` / `_devig_home` wrapper 拆解（mirror 锁住"语义守门"价值）
- 不建议做：predict.py 局部 `import time` 提级（有意 lazy-import 模式）

## 后续

- `test_auto_refresh_trigger_and_dedup` 全仓 15x 仍 1/15 偶发（P3-20 修后）——Windows fs 锁延迟根因不在生产代码层面，已留作 known issue
- P6 候选待新 explore 扫描
