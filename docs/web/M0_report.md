# M0 报告 — 引擎数据契约勘察（第二批 M0b）

## 完成清单

| 节 | 状态 | 备注 |
|---|---|---|
| §1 CLI 全景 | ✅ M0a 完成；M0b 修正 `--update-rankings` 表述 | 疑点 15 close 后，原「强制刷新 FIFA 排名」表述不准确，已改为「不联网刷新，仅跳过 ELO 缓存」 |
| §2 预测 JSON schema | ✅ M0a 完成 | — |
| §3 文件命名与时区 | ✅ M0a 完成 | — |
| §4 赛果与回填 | ✅ M0a 完成 | — |
| §5 AI 富化链路 | ✅ 完成 | 三条链路（预测主链/富化脚本/队名翻译）、降级路径、LLM 消耗 |
| §6 数据源与配额 | ✅ 完成 | 三源路由、端点、限速、缓存现状、Key 预检 |
| §7 联赛代码表 | ✅ 完成 | 5 联赛三上游 id 映射、差异化参数 |
| §8 i18n | ✅ 完成 | 两级名称体系、翻译缓存、预测 JSON 双语情况 |
| §9 疑点清单 | ✅ 完成 | 15 条全部处理：14 CLOSED + 1 需运行时验证（#2） |

## M0b 本轮核验（静态读码，未运行 predict.py）

1. **疑点 15 证据链补全（**P4-1 2026-10-13 已收口**）**：`predict.py:33` 导入 `core.rankings.fetch_fifa_rankings`（rankings.py:12-46，只读本地文件/内置表，无 force_refresh 参数）；联网委托版本 `core.data.fetch.fetch_fifa_rankings(force_refresh)`（fetch.py:337-342）原本在主链未被调用，**现已在 P4-1 中删除整个函数 + `update_fifa_rankings` + `FIFA_RANKINGS_API_URL`**。`--update-rankings` 实际只让 `get_or_init_elo_ratings` 跳过 ELO 持久化缓存（elo.py:235-238）。已修正 §1.1 参数表。
2. **疑点 12 证据链补全**：`ai_enrich_gha.py:35-40` 中 `source = league.get("league", "?")`，写入 `save_ai_scores` 的 `league` 字段（feedback_loop.py:63），`load_ai_adjustments` 按 `v["league"]==league_key` 过滤（feedback_loop.py:38）——链路脆弱性确定。
3. **疑点 11 证据链补全**：`AI_SCORES_FILE = REPO_ROOT/predictions/ai_scores.json`（feedback_loop.py:18-19）vs `FOOTBALL_DIR/predictions`（constants.py:39）——两目录不一致确定。
4. **§5.6 定量估算**：`warm_translations` 仅对未翻译新队名触发（i18n.py:84-100），99 条缓存后通常零调用；`analyse_batch` 每 5 场 1 次、7s 限速（batch_pipeline.py:38-43）。
5. **§6 缓存现状**：`core/cache.py` 为孤立实现（TTL 1h、`FOOTBALL_DIR/.cache`、SHA256 URL 键，cache.py:17-32），fetch 层从未调用——引擎每次运行都是真实联网。

## 证据密度自评

| 结论 | 密度 | 说明 |
|---|---|---|
| §5 AI 链路与降级 | 高 | predict.py:44-48,160-161,303 + feedback_loop.py:28-39,59-65 + batch_pipeline.py:24-67 + llm_client.py:22-119 多处行号 |
| §5 LLM 消耗估算 | 中 | 静态推导（warm_translations 逻辑），实际调用次数受缓存命中影响，需运行时观察 |
| §6 数据源端点/限速 | 高 | fetch.py:145-299 逐源行号；免费档配额为上游公开约定，代码未硬编码 |
| §6 缓存现状 | 高 | grep 全仓仅命中 cache.py 自身定义 |
| §7 联赛代码表 | 高 | leagues.py:36-91 逐 key 核对 |
| §8 i18n | 高 | i18n.py:43,79-110 + team_translations.json 样本 + parse.py:256-281,335-336 |
| §9 疑点 | 高 | 每条均有代码行号依据；14/15 CLOSED，1 条（#2 时区）需运行时验证 |

## 未尽事项

- **§9-2 时区口径矛盾**（样本 `generated_at` 为 `+00:00` vs 现版 BJT）：需 M1 跑一次单联赛预测看 `generated_at` 时区定论。M0b 按红线禁止运行 predict.py。
- **§9-12 AI 反馈实际匹配**：链路脆弱性已确定（静态），但实际是否失配需 M1 跑一轮 enrich + predict 看 `ai_adjusted` 是否出现。
- **§9-1 文件名带 league 后缀**：web 层按联赛取数的前提改动，由队长拍板是否改代码。

## 完成时间

- M0b：2026-09-09（UTC），静态读码核验 + 收尾落盘
- 哨兵：docs/web/logs/M0b.done 已写入

## M0b 收尾核验（2026-09-09，断点续做）

上一会话已完成 5–9 节主体并写入哨兵；本轮为断点续做，抽查全部关键证据行号，发现并修正一处路径标注问题：

| 项 | 结果 |
|---|---|
| `ai/feedback_loop.py`（18-19 路径常量、28-39 过滤、59-65 保存、73-116 adjust） | 与文档一致 |
| `ai/batch_pipeline.py`（24-67 分批、38-43 限速/批大小、64-65 保留原样） | 与文档一致 |
| `ai/llm_client.py`（38-43 无 key 返回 {}、60-93 3 次重试退避 10/20s） | 与文档一致 |
| `scripts/ai_enrich_gha.py`（14-25 load、35-40 source 赋值、117-126 skip、135-137 save） | 与文档一致 |
| `scripts/core/leagues.py`（12-31 差异化参数、36-91 五联赛配置） | 与文档一致 |
| `scripts/core/i18n.py`（43 落盘、48-81 翻译/降级、84-100 warm、103-110 to_cn） | 与文档一致 |
| `scripts/core/data/fetch.py`（22-37 key 预检、58-64 速率限制、106 常量、109-142 路由、302-342 FIFA 排名） | 与文档一致 |
| `scripts/predict.py`（44-48 AI 导入降级、34 导入 core.data.parse） | 与文档一致 |
| **`parse.py` 真实路径** | **修正**：文档原引 `parse.py:xxx`，实际文件在 `scripts/core/data/parse.py`（predict.py:34 `from core.data.parse import parse_events`）。M0b 范围内的引用（§5.5/§5.6/§8.1/§8.2/§8.3/§9-8/§9-14）已统一改为 `core/data/parse.py:xxx`；§2/§4 属 M0a 已验收范围，未动。 |

疑点 1–15 状态不变：14 CLOSED + 1 需运行时验证（#2 时区口径）。本轮未发现新的未决疑点。
