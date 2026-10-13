# DATA_CONTRACT — 引擎数据契约（M0 勘察）

> 本文件是 web 层（M1–M4）唯一的数据口径依据。
> 勘察方式：静态读代码 + 读现有 JSON 样本；**未运行 `scripts/predict.py`**（保护免费 API 配额）。
> 所有结论附证据：文件路径+行号 或 JSON 样本片段。
> 状态：M0a 完成第 1–4 节；M0b 完成第 5–9 节（2026-09-09 核验收尾，疑点 1–15 全部 close 或标注需运行时验证）。

## 1. CLI 全景

入口：`scripts/predict.py`（Python 3 CLI，argparse）。启动时 self-insert 脚本目录与仓库根进 `sys.path`，故 `ai/` 包与 `core/` 均可导入（scripts/predict.py:21-26）。

### 1.1 全部参数（scripts/predict.py:51-77）

| 参数 | 默认 | 取值 | 作用 |
|---|---|---|---|
| `--league` | `epl` | `LEAGUE_CONFIG` 的 key | 指定单联赛；与 `--all` 互斥（`--all` 优先） |
| `--all` | False | flag | 遍历 `LEAGUE_CONFIG` 全部联赛逐一运行；stdout 输出**组合 JSON 数组**（predict.py:431-444） |
| `--data-source` | `""` | `football-data` / `espn` / `api-football` | 覆盖联赛默认数据源；空串＝按联赛配置（LEAGUE_CONFIG） |
| `--monte-carlo` | False | flag | 启用蒙特卡洛模拟，结果写入 `output["monte_carlo"]`（predict.py:311-325,343-344） |
| `--n-simulations` | `DEFAULT_N_SIMULATIONS` | int | 蒙特卡洛迭代次数，默认值见 core/constants.py |
| `--backtest` | False | flag | 回测：**先** `_save_output` 落盘预测文件，再 `backtest_with_live_results`，结果写入 `output["backtest"]`（predict.py:370-374） |
| `--cleanup` | False | flag | 调 `cleanup_old_files(days=7)` 后**直接 return**（不跑预测，predict.py:417-419） |
| `--dates` | 无 | `YYYYMMDD-YYYYMMDD` | 指定数据窗口；缺省为 BJT「今天-明天」（predict.py:424-429） |
| `--no-fetch` | False | flag | **语义误导**：实际 `events=[]` 空列表（predict.py:82-84），并非读本地缓存；warning 建议改用 `--data-source football-data`（该源仍联网） |
| `--no-dc` | False | flag | 禁用 Dixon-Coles（`use_dc=False`，rho 不拟合） |
| `--update-rankings` | False | flag | **语义误导**：不联网刷新 FIFA 排名。实际仅 `force_refresh=True` 传给 `get_or_init_elo_ratings`，跳过 ELO 持久化缓存、从本地 FIFA 表重建 ELO（predict.py:264；elo.py:235-238）。联网入口 `core.data.fetch.update_fifa_rankings`（fetch.py:302-334）在主链**未被调用**（疑点 15 已 close） |
| `--train-ml` | False | flag | 训练各联赛 ML 模型后**直接 return**（不跑预测；仅 stderr 打印结果，predict.py:404-415） |
| `--no-ml` | False | flag | 本轮禁用 ML 概率混合（`ML_CONFIG["enabled"]=False`，predict.py:400-402） |
| `--dashboard` | False | flag | 额外生成静态 HTML：`predictions/dashboard_{league}.html`（predict.py:360-367） |

### 1.2 执行流与副作用（run_league，predict.py:233-387）

按序副作用：

1. **抓取+解析**（`_fetch_and_parse`）：产出 `(events, past, future, in_prog)` 四列表（predict.py:250）。
2. **历史累计**：`core.calibration.append_historical_past_matches(league_key, past)` 把已结束比赛写入历史文件（predict.py:253-259）——**每次运行都会写历史累计文件**（路径见 §4）。
3. **ELO**：抓 FIFA 排名 → 更新/初始化 ELO 评分表（predict.py:262-264）。
4. **校准**：`build_calibration` + `compute_calibration_offset`，可能**改写** `PREDICTIONS_DIR/pred_calibration.json`（predict.py:123-130, 178-181）。
5. **写预测文件**（主线必写，predict.py:380）：`PREDICTIONS_DIR/prediction_{now_utc:%Y-%m-%d_%H}.json`（predict.py:171-176），详见 §3。
6. **stderr 摘要**：`_print_summary` + `Total runtime: Nms` + 60 个 `=` 分隔线（predict.py:381-385）。

提前退出分支（**不落盘**——直接 `return output`，无 `_save_output` 调用，predict.py:266-289，返回对象仅存在于内存或被 `--all` 收集进 stdout 数组）：
- 窗口内无任何比赛：返回 `status="no_matches"` 的空 output（predict.py:266-274），`calibration={"note":"no data"}`。
- 无未来比赛且非回测：返回 `status="no_future_matches"` 的 output，附 `reconciliation`（若可对账）（predict.py:276-289）。
- `--train-ml` / `--cleanup`：main 内直接 return（predict.py:404-419），**不落盘预测**。

注意：现仓 `predictions/` 下存在 status=no_future_matches 的 `prediction_2026-07-21_10/11.json` 文件，与现版「不落盘」行为矛盾 → 推断为旧版代码产物（§3.2/§9 疑点）。

`--backtest` 落盘细节：`_save_output` 被调用**两次**——先落盘供 `backtest_with_live_results` 读取（predict.py:371），随后主线第 380 行再落盘一次（此时 output 已含 `backtest` 字段）。**最终磁盘文件含 `backtest`**。

### 1.3 stdout/stderr 约定

| 模式 | stdout | stderr |
|---|---|---|
| 单联赛（非 silent） | 整个 output 的 JSON（`indent=2, ensure_ascii=False`，predict.py:377-378） | logger 日志 + 摘要 + 计时 |
| `--all` | 各联赛 output 组成的 **JSON 数组**（predict.py:444）；每联赛分隔横幅走 stderr | 同上 |
| `--train-ml` | 无 | `league: trained/skipped` 逐行 + logger |
| `--cleanup` | 无 | logger 清理日志 |

注意：`--all` 与单联赛 stdout **形态不同**（数组 vs 对象）；单联赛模式下若 stdout 被消费为「CLI 产物」，`--all` 需要按数组解析。此点已记入 §9 疑点清单（web 层子进程捕获 stdout 时需分支处理）。

## 2. 预测 JSON schema

样本：`scripts/predictions/prediction_2026-07-21_13.json`（status=ok，8 条预测，证据见下引行号）；两个空文件 `prediction_2026-07-21_10/11.json`（status=no_future_matches）由旧版代码产出，现版已不落盘（见 §3.2 疑点）。

### 2.1 顶层 keys（组装代码：predict.py:328-357）

| key | 类型/可为空 | 说明 | 证据 |
|---|---|---|---|
| `generated_at` | str ISO8601 带时区 | BJT 时间戳（`+08:00`，误解见 §3） | predict.py:280,329；样本:2 |
| `data_window` | str `YYYYMMDD-YYYYMMDD` | 查询窗口 | predict.py:280,329 |
| `status` | str | `ok` / `no_matches` / `no_future_matches` | predict.py:270,281,330 |
| `league` | str | `LEAGUE_CONFIG` key（epl/csl…） | predict.py:281,330 |
| `tournament_type` | str | 联赛配置 `tournament_type`，默认 `league` | predict.py:245,330 |
| `data_source` | str 可空 | `--data-source` 原始串（未映射） | predict.py:331 |
| `dixon_coles_enabled` | bool | 是否启用 DC（`--no-dc` 时 False） | predict.py:332 |
| `dixon_coles_rho` | float 可空 | 拟合 ρ；未启用时为 None | predict.py:333 |
| `calibration` | dict | 窗口内完赛统计；无赛时 `{"note": "no past matches to calibrate from"}`（calibration.py:257） | predict.py:334 |
| `calibration_offset` | dict 可空 | 30 天历史修正因子；样本不足 5 场为 null | predict.py:334,166-167 |
| `past_matches` | list | 已结束比赛数组（schema 见 §2.3） | predict.py:335 |
| `predictions` | list | 预测数组（schema 见 §2.2） | predict.py:335 |
| `timing_ms` | dict `{total:int}` | 耗时毫秒（仅 status=ok） | predict.py:336 |
| `reconciliation` | dict 可空 | 仅在 `reconcile_predictions` 命中时出现（predict.py:339-341） | |
| `monte_carlo` | dict 可空 | 仅 `--monte-carlo`（predict.py:343-344） | |
| `backtest` | dict 可空 | 仅 `--backtest`（predict.py:373） | |
| `accuracy_summary` | dict 可空 | `{7d:..., 30d:...}`；`league_accuracy` 有空值或异常时缺省（predict.py:347-357） | |
| `message` | str 可空 | 仅 `no_matches`/`no_future_matches` 分支 | predict.py:272,283 |

**「每联赛每文件还是一次运行一个文件」：已实证，阶段 1（M0a）结论——文件名不含 league，任何一次 `run_league` 写 `prediction_{now:%Y-%m-%d_%H}.json`。`--all` 时多联赛共用同一文件名互相覆盖，只留最后一场联赛的 output（详见 §3.3 覆盖规则）。web 层若按联赛取数，此 schema 不可用，须改 `run_league` 文件名带 league 后缀（记入 §9 疑点清单）。**

### 2.2 `predictions[]` 逐场字段（predict_py 组装 + core/predictor.py 计算）

样本以 `prediction_2026-07-21_13.json:16-91` 为例（第一条 Qingdao Hainiu vs Tianjin Jinmen Tiger）：

|| 字段 | 类型/可为空 | 含义 | 证据 |
|---|---|---|---|
| `match` | str | **英文队名串**（LLM 不可用时回退）；为预测与赛果回填的**关联键**（predict.py:156；backtest.py:49 用 `p.get("match")` 对账） | 样本:88；parse.py:258/280-281；i18n.py:103-110 |
|| `home` / `away` | str | **英文队名串**（LLM 不可用时回退） | 样本:89-90；parse.py:280-281；i18n.py:103-110 |
| `direction` | str | 中文方向如 `"Qingdao Hainiu 胜 (接近)"`；平局含「平」/「平局」子串 | 样本:17；backtest.py:58 |
| `stars` | str | 置信级别 `"1-star"`…（星级体系，值域见 predictor.py THRESHOLDS 相关逻辑） | 样本:18 |
| `confidence_score` | float | 0-1 置信分 | 样本:19 |
| `predicted_score` | str `"H-A"` | 最可能比分 | 样本:20 |
|| `poisson_top3` | list[{score:str, prob:float}] | 前三最可能比分及概率（概率和 < 1，仅 Top3 子集） | 样本:21-34；predictor.py:268/282 |
| `lambda_home` / `lambda_away` | float | 泊松期望进球 | 样本:35-36 |
| `lambda_home_ci95` / `lambda_away_ci95` | list[float,float] | 95% 置信区间 | 样本:37-44 |
| `over_under` | str | `"Over 2.5"` / `"Under 2.5"` | 样本:45 |
| `btts` | str | `"Yes"` / `"No"`（双方进球） | 样本:46 |
| `dixon_coles_used` | bool | 本场是否用 DC | 样本:47 |
| `dixon_coles_rho` | float | 本场 ρ（=fitted_rho） | 样本:48 |
| `dixon_coles_league_rho` | float | 联赛默认 ρ（LEAGUE_DC_RHO） | 样本:49 |
| `onside_signals` | dict | `{home:{fifa_rank,fifa_score,league_footprint,host_advantage,confederation,onside_score}, away:{...}}` 6 字段全为数值 | 样本:50-67 |
| `confidence_note` | str 可空 | 置信说明，通常 null | 样本:68 |
| `odds_data_available` | bool | 是否获得赔率数据 | 样本:69 |
| `reasoning_factors` | dict | 14 个数值字段：`home/draw/away_ml_true_prob`、`home/away_form_score`、`home/away_record_score`、`spread_movement`、`home/away_onside_score`、`home/draw/away_prob_weighted`、`elo_home_expected`、`raw_lambda_home/away` | 样本:70-87 |

**版本漂移提示**：predictor.py:342-343 的返回 dict 还含 `ml_model_used`（bool）与 `ml_proba`（list 可空）两字段，样本 13 无此二字段 → 样本由旧版生成，或该次运行 ML 未启用且字段被裁剪。web 层按此 schema 消费时应把这两个字段视为「可能缺失」（详见 §9 疑点）。

AI 反馈注入：`adjust_prediction(pred, ai_adjustments)` 按联赛加载（predict.py:303,160-161），可能改写上述字段（详见 §5 M0b）。

### 2.3 `past_matches[]` 字段（parse.py:314-368 构造成员）

样本 `prediction_2026-07-21_10.json:20-56`：

| 字段 | 类型/可为空 | 说明 |
|---|---|---|
|| `name` | str | **英文队名串** `"Away vs Home"`（`to_cn` 翻译不可用时回退英文） | 样本10:22；parse.py:258 |
| `status` | str | `STATUS_FULL_TIME` 等 ESPN status name |
| `completed` | bool | |
| `kickoff_utc` | str ISO8601 `...Z` | **UTC** 时间（parse.py:271 从源数据解析） |
| `time_to_kickoff_h` | float | 相对 now 的小时差（负=已过） |
|| `home`/`away` | str | **英文队名串**（`to_cn` 翻译不可用时回退英文） | 样本10:27-28；parse.py:280-281 |
|| `home_en`/`away_en` | str | 源数据 displayName（英文） | 样本10:29-30；parse.py:335-336 |
| `home_abbr`/`away_abbr` | str | `UNK` 表示未知 |
| `score` | str `"H-A"` | 完赛比分 |
| `home_form`/`away_form` | str | W/D/L 串（parse.py:214） |
| `home_form_score`/`away_form_score` | float | 0-1 形态分（parse.py:213） |
| `home_record`/`away_record` | str | `"W-D-L"` 汇总（parse.py:218） |
| `home_record_score`/`away_record_score` | float | |
| `ml_home_close`/`draw_ml` | float 可空 | 钱线赔率（parse.py:296-297 取 `moneyLine`） |
| `home_ml_implied`/`draw_implied` | float 可空 | 隐含概率 |
| `home_true_prob`/`draw_true_prob`/`away_true_prob` | float 可空 | 无赔率时为 null |
| `spread_home_line`/`spread_home_close_odds` | str | 让球线/赔率（无则为空串） |
| `spread_movement_score` | float | 让球移动分 |
| `total_over_close`/`total_under_close` | str | 大小球盘口 |
| `odds_data_available` | bool | |

### 2.4 `calibration` 字段（build_calibration，calibration.py:278-289）

`total_matches/home_wins/draws/away_wins`（int）、`home_win_rate/draw_rate/away_win_rate`（float 0-1）、`favored_by_odds/favored_won`（int）、`odds_accuracy`（float）。样本:8-19。无赛时 `{"note": "no past matches to calibrate from"}`（calibration.py:257；样本 13:11-12）。

`calibration_offset`（compute_calibration_offset，calibration.py:221-232）：`home/draw/away_correction`、`onside_home/away_correction`（float，钳位 [0.5,2.0]）、`sample_size`（int）、`sample_weight`（float）、`actual_home/draw/away_rate`（float）。持久化在 `references/.calibration_state_{league}.json`（calibration.py:15-18,235）。

## 3. 文件命名与时区

### 3.1 谁生成、何时覆盖

预测文件由 `_save_output` 写 `PREDICTIONS_DIR/prediction_{ts}.json`，`ts = now_utc.strftime("%Y-%m-%d_%H")`（predict.py:171-172）。**注意：函数参数名 `now_utc` 是误导，实际传入的是 BJT 时间**——`main()` 中 `now_bjt = datetime.now(timezone(timedelta(hours=8)))`（predict.py:422），且在 `--all`（predict.py:438）和单联赛（predict.py:446）中都以 `now_bjt` 作为 `now_utc` 实参传入。故：

- **文件名时间戳是 BJT**（`prediction_2026-07-21_10.json` = BJT 7月21日10点）。样本文件落在 `scripts/predictions/` 而非仓库根 `predictions/`（`PREDICTIONS_DIR` 解析为 scripts 下目录，见 core/config.py——M0b 确认）。
- **`generated_at` 同样是 BJT**（predict.py:280/329 `now_utc.isoformat()`，样本:2 为 `+00:00` 疑为旧版或 `_save_output` 参数不同，见 §3.2）。
- **同小时内重复运行**：`ts` 粒度到小时，同小时第二次运行**覆盖**同文件名（predict.py:171-172 直接 open w）。

**`--all` 覆盖规则**：所有联赛共用同一 `now_bjt`，`_save_output` 写同一文件名，后跑的联赛覆盖先跑的（predict.py:431-444），最终磁盘只留 **最后一个** 联赛的 output；`--all` 的数组只在 stdout 打印，不落盘。

样本 `2026-07-21_10`（status=no_future_matches，BJT 10:57 生成，`data_window=20250101-20250103`）与 `_11`（BJT 11:03）**同日同小时级别冗余共存**，且 10/11 两个空文件都在 `_13`（status=ok）之前生成——三者非同一小时故不覆盖；10 与 11 是不同小时的两份独立空输出。此样本集无法回答「同一小时多次运行是否覆盖」，但因时间戳粒度到小时，理论上会覆盖（见 §9 疑点）。

### 3.2 样本 `generated_at` 时区矛盾（疑点）

样本 10/11/13 的 `generated_at` 均带 `+00:00`（如 `2026-07-21T10:57:34.956948+00:00`），而现版代码 `now_utc=now_bjt` 应输出 `+08:00`。两种可能：① 样本由旧版（`datetime.now(timezone.utc)`）生成；② 存在另一入口以 UTC 调用（如 GHA workflow 直接 import run_league 传 UTC）。**未运行脚本核实，记入 §9 疑点清单。**

### 3.3 「某联赛最新一次运行」的可靠选取规则

因文件名不含 league 且同小时覆盖，无法从文件名或顶层 `league` 保证拿到「某联赛最新」：同一小时多联赛只剩一份。web 层可靠做法（伪代码）：

```
# 前提：先修 §9-1（文件名带 league）或改用目录按联赛分文件；否则只能取"最新一次某文件"，
# 该文件可能是任意联赛，且无法判断覆盖顺序。
def pick_latest_for_league(league):
    candidates = []
    for f in glob("predictions/prediction_*.json"):
        d = json.load(f)
        if d.get("league") != league: continue
        candidates.append((parse_ts(d["generated_at"]), f))
    return max(candidates)[1] if candidates else None   # 按 generated_at 最大
```

可靠依据是 `generated_at`（BJT）；但由于同小时覆盖，**磁盘上可能只存在最后跑的那个联赛的文件**，此时该文件顶层 `league` 可能不是目标联赛，「最新一次可靠选取」在现命名方案下**无法保证**（§9 疑点）。web 层建议依赖 GHA/scheduler 单联赛独立运行、各自落盘，再按 `generated_at` 取最大。

## 4. 赛果与回填

### 4.1 `results/*.json` schema（现版输出 core/output.py:31-74）

`save_results(past)` 在每次 `_fetch_and_parse` 后被调用（predict.py:91），路径 `RESULTS_DIR / f"result_{today}.json"`，`today = datetime.now(timezone.utc).strftime("%Y-%m-%d")`（output.py:36-37）——**文件名日期是 UTC**（与预测文件名 BJT 不一致，见 §3 疑点）。

样本 `scripts/results/result_2026-07-21.json`：

```json
{
  "date": "2026-07-21",
  "matches": [
    {
      "id": "Brentford FC vs Arsenal FC",
      "kickoff_utc": "2025-01-01T17:30:00Z",
      "home": "Brentford FC",
      "away": "Arsenal FC",
      "home_score": 1,
      "away_score": 3,
      "status": "STATUS_FULL_TIME"
    }
  ]
}
```

| 字段 | 类型 | 说明 | 证据 |
|---|---|---|---|
| `date` | str | UTC 当日 `YYYY-MM-DD` | output.py:36 |
| `matches[].id` | str | `past` 记录里的 `name`（中文 `"主 vs 客"`） | output.py:58 |
| `matches[].kickoff_utc` | str | ISO8601 `...Z` UTC | output.py:59 |
| `matches[].home`/`away` | str | `home_en`/`away_en`（**英文**队名，回退中文） | output.py:60-61 |
| `matches[].home_score`/`away_score` | int | `score="H-A"` 拆分 | output.py:62-63 |
| `matches[].status` | str | ESPN status name | output.py:64 |

合并规则：同日多次运行按 `home_away` 键合并（output.py:40-48,66-69），后写覆盖先写。当日无完赛记录则不写文件。

### 4.2 与 predictions 的关联键

**没有 `match_id` 级别的强关联键**。三条路径各自用不同键：

1. **预测文件内 `reconciliation`**：`reconcile_predictions(past, days=7)` 用 `name`（`"Away vs Home"` 英文队名串）做键，把当前窗口的 `past_matches` 的赛果与 `PREDICTIONS_DIR/prediction_*.json`（近 7 天 mtime）里的 `predictions[]` 比对（backtest.py:17-31,36-49）：
   - 关联键 = `predictions[].match` 字符串 ↔ `past[].name` 字符串（**英文队名串精确匹配**；`to_cn` 翻译不可用时回退英文）。
   - 方向判定基于 `direction` 前缀（backtest.py:56-65）：`d.startswith(home_team)` / `d.startswith(away_team)` / `"平" in d`。
   - 产出 `reconciliation`（backtest.py:90-99）：`reconciled/correct_direction/correct_score/correct_over_under`（int）、`direction_accuracy/score_accuracy/over_under_accuracy`（float）、`details[]`。

2. **`backtest_with_live_results`**（predict.py:370-374，`--backtest`）另走 `football-data.org` 实际赛果回测（backtest.py:278-382），输出 `output["backtest"]`：

   | 字段 | 类型 | 说明 | 证据 |
   |---|---|---|---|
   | `status` | str | `ok` / `no_evaluable_matches` / `skip` / `error` | backtest.py:283,292,296,324,374,377 |
   | `reason` | str 可空 | `skip` 时说明（如 `"FOOTBALL_DATA_API_KEY not set"`） | backtest.py:292,296 |
   | `error` | str 可空 | `error` 时异常信息 | backtest.py:283,324 |
   | `matched_matches` | int | 可评估场次 | backtest.py:374,378 |
   | `correct` | int | 方向命中数 | backtest.py:375,379 |
   | `accuracy` | float | `correct/matched_matches` | backtest.py:380 |
   | `rows[]` | list | 逐场 `{home, away, predicted, actual, correct, predicted_score, actual_score}` | backtest.py:363-371 |

   关联键 = `(home, away)` 元组（backtest.py:340,349），与 `reconcile_predictions` 的 `name` 字符串键**不同**；仅支持 `football-data` 与 `api-football` 源（backtest.py:289-292），ESPN 源返回 `skip`。

3. **`league_accuracy`**（predict.py:349-357，`accuracy_summary` 7d/30d）：按联赛过滤读历史文件统计（backtest.py:102-175，字段 `direction_accuracy/score_accuracy/over_under_accuracy/reconciled`，见 predict.py:226-228）。

**排序/唯一性**：`reconcile_predictions` 中 `actuals` 以 `name` 为键，后到覆盖先到（backtest.py:19-28）。日期+主队+客队组合**等价于** `name` 字符串本身（`name="Away vs Home"` 由 parse.py:258 组成），所以不采用 `kickoff_utc+home+away` 复合键；**`kickoff_utc` 不参与对账**。

### 4.3 命中判定实现位置（各管什么）

| 模块 | 管什么 | 证据 |
|---|---|---|
| `core/backtest.py` | 预测 vs 赛果的三项命中统计（方向/比分/大小球）；`reconcile_predictions`（窗口对账）、`league_accuracy`（按联赛历史精度）、`backtest_with_live_results`（在线回测） | backtest.py:17-382 |
| `core/calibration.py` | 赛果**分布**统计（主/平/客胜率）→ 修正因子；`build_calibration`（窗口）、`compute_calibration_offset`（30 天历史+指数平滑） | calibration.py:158-289 |
| `core/output.py` | `save_results` 写 `results/result_{date}.json`（**只写赛果，不含预测命中**） | output.py:31-74 |
| `core/elo.py` | 用赛果更新 ELO 评分表（`process_match_result`，predict.py:98-104） | predict.py:95-107 |

回填方向：`results/*.json` 是**纯赛果存储**，预测命中分析不读它（读的是 prediction 文件 + `past_matches`）；两份数据源（results 与 past_matches 内嵌）内容重复，格式上 results 是英文队名+int 比分，past_matches 的 `name/home/away` 也是英文（LLM 翻译不可用时），但 `score` 为 str——web 层回填展示需注意这一重复与差异（疑点 §9）。

## 5. AI 富化链路

### 5.1 调用关系全景（三条独立链路）

```
链路 A（预测主链，predict.py 内）:
  predict.py:303  load_ai_adjustments(league_key)   ← 读 ai_scores.json（上一轮富化结果）
  predict.py:160-161 adjust_prediction(pred, adj)   ← 按 match 名改写 confidence/stars

链路 B（富化，独立脚本 scripts/ai_enrich_gha.py，GHA 邮件推送用）:
  predict 输出 → /tmp/predict_output.txt → ai_enrich_gha.py:load_predictions()
    → enrich_via_llm() → ai.batch_pipeline.analyse_batch() → ai.llm_client.generate()
    → save_ai_scores()（写 ai_scores.json，供链路 A 次日消费）

链路 C（队名翻译，predict.py 主链内置）:
  parse_events() → core.i18n.warm_translations() → ai.llm_client.generate()
```

证据：predict.py:44-48（try import `ai.feedback_loop`，失败降级为 no-op lambda——**LLM 依赖缺失不影响主流程**）；scripts/ai_enrich_gha.py:30（`from ai.batch_pipeline import analyse_batch`）。

### 5.2 模块职责

| 模块 | 职责 | 证据 |
|---|---|---|
| `ai/llm_client.py` | OpenAI 兼容 + Gemini REST 双协议 LLM 客户端；`generate()` 返回解析后 JSON dict，任何失败返回 `{}`（**永不抛异常**） | llm_client.py:22-58,60-93 |
| `ai/batch_pipeline.py` | `analyse_batch(items)`：按 batch_size=5 分批构造 prompt，调 LLM 打分，给每 item 附加 `ai_score`(0-100)/`ai_summary`/`ai_notes`；低于 `min_score` 过滤（默认 0 不过滤）；AI 未返回对应项时**原样保留 item**（不丢弃） | batch_pipeline.py:24-67,40 |
| `ai/feedback_loop.py` | 桥接层：`save_ai_scores`（写 ai_scores.json）、`load_ai_adjustments`（按联赛过滤读取）、`adjust_prediction`（改写预测）、`reconcile_results`（AI 维度命中统计） | feedback_loop.py:23-116,119-223 |
| `ai/feedback_memory.py` | 用户反馈（positive/negative）→ prompt 偏好段落；**未被任何引擎代码 import**（孤立模块，grep 无调用方） | feedback_memory.py:1-44；grep 无结果 |
| `scripts/ai_enrich_gha.py` | GHA 邮件富化：读 `/tmp/predict_output.txt`，LLM 分析后追加到 `/tmp/email_body.txt` | ai_enrich_gha.py:14-25,117-149 |

### 5.3 AI 文本落在预测 JSON 的哪些字段

**主预测 JSON（`prediction_*.json`）本身不含 AI 富化文本**。AI 只通过 `adjust_prediction` 改写数值字段 + 打标（feedback_loop.py:86-116）：

| 字段 | 类型 | 说明 | 触发条件 |
|---|---|---|---|
| `confidence_score` | float | `base * (0.7 + 0.3*ai_score/100)`，钳位 ≤1.0，round 3 位 | 该场 match 名命中 ai_scores.json |
| `stars` | str | 按新 confidence 对照 `THRESHOLDS["star_2..5"]` 重定级 | 同上 |
| `ai_adjusted` | bool | `True` 标记 | 同上 |
| `ai_score_used` | int | 使用的 AI 分（0-100） | 同上 |
| `ai_adjustment_factor` | float | 调整因子（0.7–1.3） | 同上 |

**AI 富化文本只出现在邮件推送产物**（ai_enrich_gha.py:75-82）：`=== AI 分析 ===` 段每场一行 `• {name} **{ai_score}/100** — {summary}`；富化的结构化数据（`ai_score`/`ai_summary`/`ai_notes`）仅持久化在 `ai_scores.json`，**不进入 prediction 文件**。

证据：样本 `prediction_2026-07-21_13.json` 全文件无 `ai_` 前缀字段（M0a 已核对）；feedback_loop.py:98-101 字段注入点。

### 5.4 反馈循环的存储文件

- `AI_SCORES_FILE = 仓库根/predictions/ai_scores.json`（feedback_loop.py:19）——**注意：这是 REPO_ROOT 级路径，不是 scripts/ 下**（`Path(__file__).resolve().parent.parent`，feedback_loop.py:18）。与预测文件目录（`FOOTBALL_DIR/predictions`，constants.py:39）**不一致**：`LP_OUTPUT_DIR` 重定向只影响引擎产物，不影响 ai_scores.json。web 层若展示 AI 分需读仓库根 `predictions/ai_scores.json`（疑点 §9-11）。
- schema：`{ "比赛英文名": {"ai_score": int, "ai_summary": str, "ai_notes": str, "league": str, "source": str} }`（feedback_loop.py:59-65）。
- 写入时机：`ai_enrich_gha.py:135-137` 调 `save_ai_scores(enriched_items, league_key="")`——**league_key 传空串**，实际 league 取 item 的 `source` 字段（feedback_loop.py:63）；`load_ai_adjustments(league_key)` 过滤条件 `v.get("league") == league_key`（feedback_loop.py:38）依赖 source 恰好等于引擎 league key（`enrich_via_llm` 中 `league.get("league", "?")` 赋值，ai_enrich_gha.py:35-46）。**league 匹配链脆弱，存在对不上的风险**（疑点 §9-12）。
- 周期：Day N 富化 → Day N+1 预测读（feedback_loop.py:4-8 docstring）。

### 5.5 未配置 LLM 时的降级路径（分三层）

1. **主预测链（predict.py）**：`from ai.feedback_loop import ...` 包在 try/except 中（predict.py:44-48），导入失败 → `load_ai_adjustments=lambda: {}`、`adjust_prediction=lambda pred, adj: pred`（恒等）→ 预测照常生成，无 AI 标记字段。`ai_adjustments={}` 时 `_generate_predictions` 跳过 adjust（predict.py:160-161）。
2. **队名翻译（链路 C）**：`translate_team_names` 任何异常返回 `{}`（i18n.py:79-81）；`generate()` 无 key 直接 `return {}`（llm_client.py:42-43）；`to_cn` 查不到返回原名（i18n.py:110）→ **预测 JSON 全程回退英文队名**（core/data/parse.py:258,280-281）。这是样本中 `match/home/away` 为英文的原因。
3. **富化脚本（链路 B）**：无 `LLM_API_KEY`/`GEMINI_API_KEY` → 打印 skip 并 return（ai_enrich_gha.py:123-126），不写 ai_scores.json，不产生邮件富化段。

**web 层「AI 不阻塞主流程」的吞错位置**：引擎侧已把 LLM 失败全部降级为 no-op/空数据，主预测 JSON 永远可产出（status=ok 不依赖 LLM）。web 层扩展模块只需：① 读 ai_scores.json 失败按空 dict 处理（`load_ai_adjustments` 本身已如此，feedback_loop.py:28-34）；② 展示 AI 字段缺失时回退纯引擎字段；③ 切勿在 web 侧直接 import `ai/`（依赖 requests，见 requirements，且引擎已封装好）。

### 5.6 LLM 消耗与限速

- 每次 predict 运行触发一次 `warm_translations`（仅对未翻译过的新队名，core/data/parse.py:233-249 + i18n.py:84-100）→ 1 次 LLM 调用（rate_limit=0，i18n.py:66，**无节流**）。
- `analyse_batch`：每 5 场 1 次调用，rate_limit 默认 7 秒（batch_pipeline.py:38-43）——**8 场预测约 2 次调用**。注意实际唯一调用方 `ai_enrich_gha.py:64-70` 显式覆盖 `rate_limit_seconds: 3`、`batch_size: 5`、`min_score: 0`——即富化链路实测限速是 3 秒/调用（非默认 7s），且只取每联赛 top 5 场（ai_enrich_gha.py:37 `preds[:5]`）。
- 429 重试：`_call_openai` 3 次、退避 10/20 秒（llm_client.py:70-93）；Gemini 走模型 fallback 链（llm_client.py:14-19,96-119）。
- 环境变量：`LLM_API_KEY`（或 `GEMINI_API_KEY`）、`LLM_API_BASE`（默认 `https://api.agnes-ai.cn/v1`）、`LLM_MODEL`（默认 `agnes-2.5-flash`）（llm_client.py:38-40）。只写变量名，值不落文档。

## 6. 数据源与配额

### 6.1 数据源路由（fetch_events，core/data/fetch.py:109-142）

`--data-source` 空串 → 取 `LEAGUE_CONFIG[league]["data_source"]`（默认全为 `football-data`，leagues.py:39 等）；显式指定时覆盖。三源实现：

| 源 | 入口函数 | 免费档 | 限速/配额 | Key 环境变量 | 证据 |
|---|---|---|---|---|---|
| ESPN | `fetch_espn`（fetch.py:145-170） | **无 key、无配额**（公开 scoreboard API） | 无显式限速；重试 3 次、退避 30/60s、超时 15s | 无 | fetch.py:147-170；constants.py:44-49 |
| football-data.org | `fetch_football_data`（fetch.py:173-214） | 免费档 **10 次请求/分钟** | 超时 15s；重试 3 次指数退避 | `FOOTBALL_DATA_API_KEY` | fetch.py:178-208；constants.py:257 |
| API-Football | `fetch_api_football`（fetch.py:251-299） | 免费档 **100 次/天** | 超时 20s；重试 3 次；响应头追踪剩余配额 | `API_FOOTBALL_KEY` | fetch.py:256-284；constants.py:258 |

**免费档配额数值为上游公开约定，未在代码中硬编码**（代码只读响应头 `x-ratelimit-*`，fetch.py:67-84）。football-data.org 免费档官方为 10 req/min；API-Football 免费档官方为 100 req/day——此为文档性说明，web 层配额展示应以 `get_rate_limit_status()` 返回的响应头数据为准（fetch.py:62-64）。

### 6.2 端点清单

| 源 | 端点 | 用途 | 证据 |
|---|---|---|---|
| football-data.org | `GET https://api.football-data.org/v4/competitions/{league_id}/matches?dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD` | 联赛赛程+赛果（主数据） | fetch.py:196；backtest.py:315 |
| football-data.org | `GET https://api.football-data.org/v4/teams` | FIFA 排名（`FIFA_RANKINGS_API_URL`，**P4-1 (2026-10-13) 已删** — 主链 0 caller；FIFA 排名现走 `core.rankings.fetch_fifa_rankings` 读本地 json / 内置默认表，不联网） | fetch.py:106 ❌ |
| API-Football | `GET https://v3.football.api-sports.io/fixtures?date=YYYY-MM-DD` | 当日全部赛程，**客户端按 league_id 过滤**（免费档不支持 season/league 参数） | fetch.py:279-291 |
| API-Football | `GET https://v3.football.api-sports.io/odds?date=YYYY-MM-DD` | 赔率（非致命，失败返回空表） | fetch.py:233-248 |
| ESPN | `GET https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/scoreboard?dates=YYYYMMDD-YYYYMMDD&limit=50` | 赛程（含实时） | constants.py:44；fetch.py:147 |

请求头：football-data 用 `X-Auth-Token`（fetch.py:202）；API-Football 用 `x-apisports-key`（fetch.py:274）；ESPN 无鉴权（fetch.py:152-155）。User-Agent：football-data/api-football 为 `LeaguePredict/4.1`，ESPN 伪装 `python-requests/2.31`。

### 6.3 失败降级链

- **api-football 源**：`fetch_events` 用 ThreadPoolExecutor(2) **并行**拉 api-football + ESPN；api-football 返回 0 事件时回退 ESPN（fetch.py:131-140）。单联赛一次运行 = 2 次并发 API 调用。
- **api-football 赔率**：失败仅 warning，返回空 lookup（fetch.py:246-248）——赔率缺失不影响主流程。
- **football-data 主源**：fetch 抛异常 → **无自动 fallback**（`_fetch_and_parse` 无 except，predict.py:80-92）；异常向上抛出导致整轮预测失败。web 层若需容错应在子进程层捕获并重试/切源。
- **FIFA 排名**：`core.rankings.fetch_fifa_rankings` 本地 `references/fifa_rankings.json` > 内置默认表（rankings.py:18-46，**不联网**）。**P4-1 (2026-10-13) 清理**：`scripts/core/data/fetch.py` 的 `fetch_fifa_rankings(force_refresh)` / `update_fifa_rankings` / `FIFA_RANKINGS_API_URL` 全部删除——主链 0 caller（疑点 15 已 CLOSED）。FIFA 排名现统一走 `core.rankings` 入口；`--update-rankings` 旗标仅控制 ELO 缓存（`elo.py:235-238`）。

### 6.4 缓存策略（core/cache.py）

**现状：`core/cache.py` 是孤立实现，未被任何 fetch 路径调用**（grep 全仓 `cached_fetch|get_cached|set_cache|clear_cache|purge_expired|cache_key_from_url` 仅命中 cache.py 自身定义）。fetch.py 每次请求直接 `urllib.request.urlopen`（fetch.py:93,156,206），**无磁盘缓存**。缓存模块事实：

- 目录：`FOOTBALL_DIR/.cache`（cache.py:17）；文件 `{key}.json`（cache.py:37）。
- 键：`sha256(url[?params])[:16]`，params 按 key 排序后拼接（cache.py:29-32）。
- TTL：默认 3600s（cache.py:18）；entry 结构 `{"_payload": ..., "_cached_at": ...}`（cache.py:57-58）。
- 操作：`get_cached`（过期返回 None）、`set_cache`、`clear_cache`、`purge_expired`、`cached_fetch` 包装器（cache.py:35-123）。

**web 层含义**：引擎每次运行都是真实联网调用（无缓存兜底）。web 层若频繁触发 predict 子进程，会直接消耗上游配额；「sources 状态」端点读的是 `get_rate_limit_status()` 内存态（fetch.py:58-64）——**该状态是进程内变量，predict 子进程退出即丢失**，无法跨进程读取（疑点 §9-13）。web 层如需节流应自行实现（或用引擎预留的 cache.py 模式）。

### 6.5 Key 预检与启动行为

`validate_api_keys()` 在 fetch.py import 时执行（fetch.py:22-37）：检查 `FOOTBALL_DATA_API_KEY`/`API_FOOTBALL_KEY` 两个环境变量，缺失仅 warning 不中断；ESPN 无需 key。`_API_KEYS_OK` 全局快照（fetch.py:37）目前**无公开读取入口**（仅模块内部，疑点 §9-13 相关）。

## 7. 联赛代码表

### 7.1 引擎内部联赛 key（`LEAGUE_CONFIG` 的 key，`--league`/`--all` 参数取值域）

来源：`scripts/core/leagues.py:36-91`（`LEAGUE_CONFIG`）。**这是 web 层 `/{league}` 路由的参数取值域**。

| 引擎 key | 中文名 | 英文名（配置 `name`） | football-data `league_id` | api-football `api_football_id` | ESPN `espn_slug` | host_country |
|---|---|---|---|---|---|---|
| `epl` | 英超 | English Premier League | `PL` | 39 | `eng.1` | England |
| `laliga` | 西甲 | La Liga | `PD` | 140 | `spa.1` | Spain |
| `bundesliga` | 德甲 | Bundesliga | `BL1` | 78 | `ger.1` | Germany |
| `seriea` | 意甲 | Serie A | `SA` | 135 | `ita.1` | Italy |
| `ligue1` | 法甲 | Ligue 1 | `FL1` | 61 | `fra.1` | France |

证据：leagues.py:37-90（每个 key 的 dict：`name`/`data_source`/`league_id`/`api_football_id`/`espn_slug`/`host_country`/`groups`/`knockout`）。

注意：
- `epl` 配置**缺 `tournament_type` 字段**（其余四联赛均有 `"tournament_type": "league"`，leagues.py:49,59,71,82），predict.py:245 缺省补 `"league"` → 输出 JSON 顶层 `tournament_type` 仍为 `league`，但 schema 上 epl 的配置字典不完整。
- 全部 5 个联赛 `data_source` 默认均为 `"football-data"`（leagues.py:39,51,62,73,84），`groups=False`、`knockout=False`（无分组/淘汰赛结构；世界杯等杯赛若加入需另配，见 PLAN 未覆盖）。
- 中文名不在 `LEAGUE_CONFIG` 中，为本文档按惯例标注（`country-codes.md`/i18n 见 §8）；引擎内联赛级中文显示名**未定义**（疑点 §9-10）。

### 7.2 联赛差异化参数（同 key 域）

`LEAGUE_DC_RHO`（leagues.py:12-18）：`epl 0.17 / laliga 0.22 / bundesliga 0.19 / seriea 0.28 / ligue1 0.21`——Dixon-Coles ρ（低分平局校正强度，意甲平局率最高故最大）。
`LEAGUE_LAMBDA_MULTIPLIER`（leagues.py:25-31）：`epl 2.8 / laliga 2.7 / bundesliga 3.2 / seriea 2.5 / ligue1 2.7`——每场期望进球基线（λ 乘数，用于泊松分布）。

两套映射**与上游 id 一一对应、无差异冲突**：同一引擎 key 下三个上游 id 互不重叠（各上游用自己的命名空间，football-data `PL` 与 api-football `39` 是同一联赛的不同上游标识，非翻译关系）。

### 7.3 上游 id 的用途与验证

- football-data `league_id`：`football-data.org/v4/competitions/{league_id}/matches` 路径参数（fetch 层证据见 §6）。
- api-football `api_football_id`：`api-football.com/v3/fixtures?league={id}` 查询参数（fetch 层证据见 §6）。
- ESPN `espn_slug`：`ESPN_URL_TEMPLATE` 中 `{league_slug}` 占位（constants.py:44 `.../soccer/{league_slug}/scoreboard?...`）。

## 8. i18n

### 8.1 两级名称体系

引擎用「国名/队名」两级处理中文，**没有联赛级中文名**（见 §7.1 与疑点 §9-10）：

| 对象 | 机制 | 存储 | 证据 |
|---|---|---|---|
| 国家名 | 静态表 `COUNTRY_CN`（constants.py:143-254，硬编码约 110 条：英格兰/法国/…/中国） | 代码内 dict | constants.py:143-163（样本） |
| 俱乐部/队名 | **LLM 翻译 + JSON 缓存**（首次出现翻译，落盘复用） | `references/team_translations.json` | i18n.py:3-8,18,48-100 |
| 联赛名 | **无** | — | leagues.py:36-91 仅英文 `name` |

`to_cn(name)` 逻辑（i18n.py:103-110）：国名先查 `COUNTRY_CN`（core/data/parse.py:38-41 的 `to_cn` 是 core.i18n.to_cn 的转发）；队名查翻译缓存；**都查不到返回原英文**。COUNTRY_CN 与翻译缓存**不合并**——`COUNTRY_CN` 只查国家名，队名不走它（i18n.py:104-108）。

### 8.2 翻译缓存文件

`references/team_translations.json`（`TRANSLATION_CACHE_FILE = FOOTBALL_DIR/references/team_translations.json`，i18n.py:18）——样本 99 条（Fulham FC→富勒姆、Arsenal FC→阿森纳、FC Bayern München→拜仁慕尼黑 等，team_translations.json:1-99）。写入时 `ensure_ascii=False`（i18n.py:43）。

关键事实：
- **键是上游英文全名**（`"Manchester City FC"` 与 `"Manchester City"` 两条并存、同为「曼城」，team_translations.json:15,22）——`to_cn` 是**精确匹配**，上游队名形态不同会生成重复条目。web 层若按中文名聚合需自行去重。
- LLM 提示词要求用球迷通用短名（'Fulham'→'富勒姆'，省略 FC 后缀，i18n.py:54-64）。
- 翻译失败（LLM 不可用）→ 不写缓存、`to_cn` 回退英文（i18n.py:79-81,110）→ **预测 JSON 中 `match`/`home`/`away` 保持英文**（core/data/parse.py:258,280-281）。样本 13 的 `match` 为 `"Qingdao Hainiu vs Tianjin Jinmen Tiger"` 混排（英文为主）即此现象（预测样本:88）。

### 8.3 预测 JSON 的双语情况（逐字段）

| 字段 | 语言 | 说明 | 证据 |
|---|---|---|---|
| `predictions[].match` | 英文（翻译可用时中文） | `parse_events` 组装 `f"{to_cn(home_en)} vs {to_cn(away_en)}"`（ESPN 格式） | core/data/parse.py:256-258 |
| `predictions[].home`/`away` | 英文（翻译可用时中文） | `to_cn(displayName)` | core/data/parse.py:280-281 |
| `past_matches[].name` | 英文（翻译可用时中文） | 同上 | core/data/parse.py:258 |
| `past_matches[].home_en`/`away_en` | **恒英文** | 源数据 displayName 原样 | core/data/parse.py:335-336 |
| `predictions[].direction` | 中英混排 | 如 `"Qingdao Hainiu 胜 (接近)"`——**队名部分随 to_cn 结果，方向词恒中文** | 样本:17；predictor.py:299-303 |
| `results/*.json` 的 `home`/`away` | **恒英文** | 取 `home_en`/`away_en` | output.py:60-61 |

**web 层双语展示结论**：预测文件内「队名中英版本并存」（`home`/`away` 可能中文，`home_en`/`away_en` 恒英文，但**仅 past_matches 有 `*_en`，predictions[] 没有 `*_en`**——predictions[] 只有 `home`/`away` 一个版本，语言取决于 LLM 是否可用，见疑点 §9-14）。赛果文件恒英文。web 层若要稳定双语，需自备球队英文→中文表（可用 `references/team_translations.json` + `COUNTRY_CN` 组装），**不能依赖预测文件字段的稳定性**。

### 8.4 其他 i18n 痕迹

- `references/country-codes.md`（6.9KB）为文档性资料（ISO 代码），非代码依赖。
- 联赛级中文名缺失 → `--all` 输出数组里各 league 无中文标识，web 层 `/{league}` 页标题需自备（疑点 §9-10）。

## 9. 疑点清单

（M0a 遗留 + M0b 新增；每条标注状态：`[M0a]` 遗留 / `[M0b新增]` / `[需运行时验证]`）

### 9.0 状态汇总（M0b 收尾，2026-09-09）

| # | 疑点 | 状态 | 依据 |
|---|---|---|---|
| 1 | `--all` 同名覆盖 | **CLOSED**（代码事实确定） | predict.py:171-176,431-444；是否改文件名由队长拍板 |
| 2 | 时区口径矛盾（样本 `+00:00` vs 现版 BJT） | **需运行时验证** | 静态读码无法区分「旧版产物」与「另一入口传 UTC」；M0b 禁止运行，留 M1 |
| 3 | 文件名/时间戳时区不一致 | **CLOSED** | predict.py:422（BJT）vs output.py:36（UTC） |
| 4 | `--no-fetch` 语义误导 + cache.py 孤立 | **CLOSED** | predict.py:82-84；grep 全仓 cache 函数仅命中 cache.py 自身 |
| 5 | Web 层读取路径 | **CLOSED** | constants.py:37-40（`FOOTBALL_DIR = _SKILL_DIR`，即 scripts/） |
| 6 | 样本字段漂移（`ml_model_used`/`ml_proba`） | **CLOSED** | predictor.py:342-343 有字段，样本 13 无 → 样本为旧版产物 |
| 7 | 方向判定健壮性 | **CLOSED** | backtest.py:56-65 前缀匹配；predictor.py:299-303 已留 warning |
| 8 | LLM 消耗源 | **CLOSED** | i18n.py:84-100 仅新队名触发；batch_pipeline.py:38-43 每 5 场 1 次 |
| 9 | `--all` stdout 与落盘不一致 | **CLOSED** | predict.py:431-444（数组仅 stdout） |
| 10 | 联赛中文显示名未定义 | **CLOSED** | leagues.py:36-91 仅英文 `name` |
| 11 | ai_scores.json 路径不一致 | **CLOSED** | feedback_loop.py:18-19（REPO_ROOT）vs constants.py:39（FOOTBALL_DIR） |
| 12 | AI 反馈 league 匹配链脆弱 | **CLOSED**（链脆弱性确定）；实际失配需运行时验证 | ai_enrich_gha.py:35-40（`source=league.get("league","?")`）+ feedback_loop.py:63,38 |
| 13 | 速率限制状态不可跨进程读取 | **CLOSED** | fetch.py:58-64（进程内 `_rate_limit_info`）；`_API_KEYS_OK`（fetch.py:37）无公开读取入口 |
| 14 | predictions[] 无 `*_en` 字段 | **CLOSED** | core/data/parse.py:335-336 仅 past_matches 有 `*_en`；backtest.py:49 用 `match` 字符串做键 |
| 15 | `--update-rankings` 语义与文档不符 | **CLOSED** | predict.py:33 导入 `core.rankings.fetch_fifa_rankings`（无 force_refresh）；fetch.py:337-342 的联网委托版本未被主链调用；elo.py:235-238 仅控制 ELO 缓存 |

**结论**：15 条中 14 条 CLOSED（静态读码即可定论），1 条（#2 时区口径）需运行时验证。web 层（M1–M4）可据此直接开工，无需等待运行时确认。

1. **[M0a] `--all` 多联赛同名覆盖**：`_save_output` 文件名为 `prediction_{now:%Y-%m-%d_%H}.json`，不含 league；`--all` 时各联赛在同一小时内写同一文件，后写覆盖先写，磁盘只留最后一个联赛（predict.py:171-176,431-444）。web 层按联赛取数需改文件名带 league 后缀；由队长拍板是否改代码。
2. **[M0a] 时区口径矛盾**：样本 `generated_at` 为 `+00:00`，而现版 main 传参 `now_bjt`（predict.py:422-446）应得 `+08:00`。可能①样本是旧版产物（`no_future_matches` 分支现版也不落盘，sample 10/11 文件存在即旧版证据）；②存在另一入口以 UTC 调用（如 GHA workflow import run_league）。`[需运行时验证]`：跑一次单联赛预测看 `generated_at` 时区即可定论（M0b 禁止运行，留给 M1）。
3. **[M0a] 文件名/时间戳时区不一致**：预测文件名与 `generated_at` 是 BJT（predict.py:422），而 `results/result_{date}.json` 文件名是 UTC（output.py:36）。web 层展示与清理需各自换算。
4. **[M0a] `--no-fetch` 语义误导**：help 写 "Use local cached data"，实际 `events=[]` 空列表（predict.py:82-84）。**M0b 补充**：`core/cache.py` 存在（TTL 1h、`FOOTBALL_DIR/.cache`、SHA256 URL 键，cache.py:17-18,21-32），但 **fetch 层从未调用**（grep 全仓仅命中 cache.py 自身）——`--no-fetch` 与缓存模块都是「半成品」。
5. **[M0a] Web 层读取路径**：`PREDICTIONS_DIR`/`RESULTS_DIR` 指向 `scripts/predictions`、`scripts/results`（constants.py:37-40），仓库根同名目录为空壳。web 层读文件路径须以 `scripts/` 为根（或由环境变量 `LP_OUTPUT_DIR` 重定向）。
6. **[M0a] 样本字段漂移**：样本 13 缺 predictor.py:342-343 的 `ml_model_used`/`ml_proba`；`prediction_10/11` 空文件属旧版行为。web 层 schema 校验需容忍缺失。
7. **[M0a] 方向判定健壮性**：`direction` 为「中文队名 + 胜/(接近)」自由字符串，回填对账靠前缀匹配（backtest.py:56-65）；队名互为前缀时前缀解析可能歧义，predictor.py:299-303 已留 warning。web 层若自行回填需复用同一解析逻辑。
8. **[M0a] LLM 消耗源**：`parse_events` 的 `warm_translations`（core/data/parse.py:233-249）在每次运行触发 LLM 队名翻译。**M0b 细化**：仅对未翻译过的新队名触发（i18n.py:84-100）；已有 99 条缓存后通常零调用（team_translations.json:1-99）；`analyse_batch` 每 5 场 1 次、7s 限速（batch_pipeline.py:38-43）。
9. **[M0a] `--all` stdout 与落盘不一致**：`--all` 的数组 JSON 只在 stdout，磁盘无对应聚合文件；单联赛输出为对象。web 层子进程捕获需按模式分支解析（§1.3）。
10. **[M0b新增] 联赛中文显示名未定义**：`LEAGUE_CONFIG`（leagues.py:36-91）只有英文 `name`，引擎内无联赛级中文名（§8.1）。web 层 `/{league}` 页面标题中文名需自备。
11. **[M0b新增] ai_scores.json 路径与引擎产物目录不一致**：`AI_SCORES_FILE = REPO_ROOT/predictions/ai_scores.json`（feedback_loop.py:18-19），而预测文件在 `FOOTBALL_DIR/predictions`（constants.py:39）。`LP_OUTPUT_DIR` 重定向不影响 ai_scores.json。web 层读 AI 分需走仓库根路径。
12. **[M0b新增] AI 反馈 league 匹配链脆弱**：`save_ai_scores(..., league_key="")` 用 item 的 `source` 字段存 league（feedback_loop.py:63），而 `source` 来自 `league.get("league", "?")`（ai_enrich_gha.py:35-46）——只有 GHA 流程里 predict 输出顶层的 league key 恰好与引擎 key 一致才匹配得上；`load_ai_adjustments(league_key)` 再按 `v["league"]==league_key` 过滤（feedback_loop.py:38）。任何一环改名即失效。`[需运行时验证]`：跑一轮 enrich + predict 看 `ai_adjusted` 是否出现。
13. **[M0b新增] 速率限制状态不可跨进程读取**：`get_rate_limit_status()` 返回进程内 `_rate_limit_info`（fetch.py:58-64），predict 子进程退出即丢失；web 层「sources 状态」端点**无法**通过此函数拿到真实配额。且 `_API_KEYS_OK`（fetch.py:37）无公开读取入口。web 层需自行维护配额展示（或接受静态说明）。
14. **[M0b新增] predictions[] 无 `*_en` 字段**：`predictions[].home`/`away` 的语言随 LLM 可用性漂移（英文/中文），而 `past_matches[]` 有恒英文的 `home_en`/`away_en`（core/data/parse.py:335-336）。web 层做双语展示时 predictions 部分缺稳定英文锚点（§8.3）。回填对账用 `match` 字符串（英文或中文）做键（backtest.py:49），同一场比赛若一次运行翻译可用另一次不可用，**对账键会失配**——进一步支持「web 层自备球队名称表」的结论。
15. **[M0b新增] `--update-rankings` 语义与文档不符**：predict.py:33 import `from core.rankings import fetch_fifa_rankings`（**无 force_refresh 参数的版本**，rankings.py:12-46 只读本地文件/内置默认表）；`--update-rankings` 的 `force_refresh` 传给 `_update_elo` → `get_or_init_elo_ratings`，仅控制 ELO 持久化缓存是否忽略（elo.py:235-238），**不触发任何 FIFA 排名联网刷新**。真正的联网刷新入口 `core.data.fetch.update_fifa_rankings`（fetch.py:302-334）在 predict 主链**未被调用**（grep 未见）。`[需运行时验证]`：跑 `--update-rankings` 前后对比 `references/fifa_rankings.json` 与 `.elo_ratings.json` 变化可确认（留给 M1）。

