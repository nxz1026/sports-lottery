-- R51 · 建议 5「滚动重算 + 赔率异动告警」—— DDL（幂等）
-- 生成：2026-10-21 · 依据：docs/project/产品路线图-竞彩闭环-20261020.md §6.7
--
-- 设计要点：
--   ① `analysis.odds_move` **新表**：一行 = **一个侧的一次异动**（相邻两个快照之间）。
--      既有表**一行未动**（`fact.jc_offer` / `analysis.ev_report` / `analysis.rec_settle` 全不改）。
--   ② ⚠️ **告警口径 = 相邻快照的跳变**（4~20 分钟窗口），不是「开盘 vs 临场」的首末差 ——
--      生产实测（2026-10-21，30 天窗口）：
--        · 首末差：SP 相对变动 p50 9.84%，**≥5% 命中 77%**（314/406 场）⇒ 纯噪音，不可用
--        · 相邻跳变（4~20 分钟，23,955 个相邻对）：**≥3% 命中 1.21%（290）、≥5% 命中 0.405%（97）**
--          ⇒ 默认阈值 **5%** ⇒ 按场次聚合 **≈3.2 组/天**（3% ⇒ ≈9.7 组/天，噪音偏大）
--      ⚠️ 窗口**不是**「恰好 10 分钟」：实测采集间隔有 1/3/4/5/7/8/9/10/20/23/60/97/120 分钟
--        ⇒ 卡死 540~660 秒会漏掉 324 场里的 148 场（45%）；
--        且 **>60 分钟间隔的 ≥5% 命中高达 9.868%**（隔夜/重开重定价 ⇒ 假异动）⇒ 必须设上限。
--   ③ ⚠️ **「开盘价」必须取「第一版三项齐全的 had」**：实测 406 场「版本数 ≥ 5」里
--      **405 场的第 1 版 `options` 是 `{}`**（竞彩刚开盘时 had 还没值）⇒ 沿用「第一版快照」
--      会让 delta 全是 NULL（`/api/jc/movement` 长期为空的真因）。
--   ④ 幂等：唯一键 (match_id, play_type, option_code, prev_ts, curr_ts)
--      ⇒ 同一对相邻版本只留一行；重跑只更新价格与幅度，不重复插。
--   ⑤ `pushed` 列：异动**落表才是资产**，推送只是呈现 —— 未配 webhook 时恒为 false，
--      **绝不允许「没推却标已推」**（建议 3/4「静默失败」教训的同款防线）。
--
-- 权限：与既有 analysis.* 一致（league_app 读写、league_ro 只读、league_ing 无权）。
--
-- 执行方式（生产）：
--   sudo -n -u postgres psql -d league -v ON_ERROR_STOP=1 -f docs/db/suggestion5_schema.sql
-- 回滚（**只删新增表**，不动任何既有表）：
--   DROP TABLE IF EXISTS analysis.odds_move;

\set ON_ERROR_STOP on

BEGIN;

-- ─────────────────────────────────────────────────────────────
-- ① analysis.odds_move：赔率异动事件（建议 5 子项 ②）
--    一行 = 一个侧在**相邻两个快照**之间的一次跳变（超过阈值才落）。
--    前后的 snap_ts 与 SP 都存 ⇒ 任何人可用同一公式复算 move_pct。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analysis.odds_move (
    match_id     bigint   NOT NULL,              -- fact.jc_match.match_id（无 PK，故不建外键）
    play_type    text     NOT NULL DEFAULT 'had',
    option_code  text     NOT NULL,              -- 'h' / 'd' / 'a'
    prev_ts      timestamptz NOT NULL,           -- 前一版快照时刻（fact.jc_offer.snap_ts）
    curr_ts      timestamptz NOT NULL,           -- 后一版快照时刻
    sp_prev      numeric  NOT NULL,              -- 前一版官方 SP（竞彩原文数值）
    sp_curr      numeric  NOT NULL,              -- 后一版官方 SP
    move_pct     numeric  NOT NULL,              -- |sp_curr/sp_prev − 1| × 100
    threshold_pct numeric NOT NULL,              -- 触发时用的阈值（百分数，如 5.0）
    league_cn    text,                            -- fact.jc_match.league_cn（原文）
    match_date   date,                            -- BJT 比赛日（kickoff_bj 墙钟日）
    detected_at  timestamptz NOT NULL DEFAULT now(),
    pushed       boolean  NOT NULL DEFAULT false, -- 是否已推送（未配 webhook ⇒ 恒 false）
    pushed_at    timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, play_type, option_code, prev_ts, curr_ts),
    CONSTRAINT odds_move_option_check CHECK (option_code IN ('h', 'd', 'a')),
    CONSTRAINT odds_move_sp_check CHECK (sp_prev > 1 AND sp_curr > 1),
    CONSTRAINT odds_move_pct_check CHECK (move_pct >= 0)
);
CREATE INDEX IF NOT EXISTS odds_move_curr_idx ON analysis.odds_move (curr_ts DESC);
CREATE INDEX IF NOT EXISTS odds_move_match_idx ON analysis.odds_move (match_id, curr_ts DESC);
CREATE INDEX IF NOT EXISTS odds_move_push_idx ON analysis.odds_move (detected_at DESC) WHERE NOT pushed;

COMMIT;

-- ── 授权（与既有 analysis.* 保持一致）───────────────────────────────
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'league_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON analysis.odds_move TO league_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'league_ro') THEN
    GRANT SELECT ON analysis.odds_move TO league_ro;
  END IF;
END $$;

-- 核验（应返回 1 行 + 15 列）
-- SELECT table_name FROM information_schema.tables
--  WHERE table_schema='analysis' AND table_name='odds_move';
-- SELECT column_name FROM information_schema.columns
--  WHERE table_schema='analysis' AND table_name='odds_move' ORDER BY ordinal_position;
