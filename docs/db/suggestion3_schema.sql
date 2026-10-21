-- R49 · 建议 3「命中率改分层 + 样本量，主指标换 CLV」—— 新增 4 张表（幂等 DDL）
-- 生成：2026-10-21 · 依据：docs/project/产品路线图-竞彩闭环-20261020.md §6.5
--
-- 为什么必须新建表而不能复用既有表（三条硬证据，§6.5.3）：
--   ① `analysis.backtest_market`（175,200 行）的 `fixture_id` 是 `fact.fixture` 的 FK
--      ⇒ 装的是 AF 的 5 联赛/欧冠 **2022–2024 赛季**回测；
--   ② `fact.jc_offer.match_id` / `fact.jc_odds_history.match_id` 是**竞彩官方 match_id**
--      （446 场，2026-09-15 ~ 2026-10-21）；
--   ③ 两者日期区间**零重叠** ⇒ 实测 join **0 行**
--      （`select count(*) from analysis.backtest_market bm join fact.jc_match m
--        on bm.fixture_id = m.match_id` → 0）。
--   ⇒ CLV 必须是「当日预测 ↔ 当日竞彩盘口」的**新管线**，存量永远算不出 CLV。
--
-- 权限：与既有 `model.*` / `analysis.*` 一致（league_app 读写、league_ro 只读）。
-- 角色由既有授权模式推导（新建表后需 GRANT，见文件末尾）。
--
-- 执行方式（生产）：
--   sudo -n -u postgres psql -d league -v ON_ERROR_STOP=1 -f docs/db/suggestion3_schema.sql
-- 回滚（**只删新增表，不动任何既有表/数据**）：
--   DROP TABLE IF EXISTS analysis.clv_daily, analysis.clv_match,
--                      model.pred_archive_match, model.pred_archive;

\set ON_ERROR_STOP on

BEGIN;

-- ─────────────────────────────────────────────────────────────
-- ① model.pred_archive：每次预测运行一行（建议 3 子项 3）
--    现状 `model.pred_run` 只有 10 行（全是 2026-09-15 的一次性赛季回测），
--    且 `model.pred_fixture.fixture_id` 是 `fact.fixture` 的 FK —— 当日 ESPN/竞彩场次
--    **不在** fact.fixture 里 ⇒ 不能复用，必须另起一套「当日归档」。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model.pred_archive (
    archive_id   bigserial PRIMARY KEY,
    league_key   text        NOT NULL,
    run_at       timestamptz NOT NULL DEFAULT now(),
    generated_at timestamptz NOT NULL,
    commit       text,
    data_window  text,
    n_matches    integer     NOT NULL DEFAULT 0,
    source_file  text,
    params       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT pred_archive_league_key_not_null CHECK (league_key IS NOT NULL),
    CONSTRAINT pred_archive_generated_at_not_null CHECK (generated_at IS NOT NULL),
    CONSTRAINT pred_archive_n_matches_check CHECK (n_matches >= 0)
);
-- 幂等：同一联赛同一次运行只留一行（重跑 predict.py 不产生重复归档）
CREATE UNIQUE INDEX IF NOT EXISTS pred_archive_league_generated_uq
    ON model.pred_archive (league_key, generated_at);
CREATE INDEX IF NOT EXISTS pred_archive_league_run_at_idx
    ON model.pred_archive (league_key, run_at DESC);

-- ─────────────────────────────────────────────────────────────
-- ② model.pred_archive_match：每次运行的每一场一行
--    存**竞彩官方中文名**（= 预测的 home/away），因为 CLV 的关联是「按名 join」
--    （web/services/team_match.py 已实现该 join，含中文别名表）。
--    p_home/p_draw/p_away 取**模型自己的**输出：预测行的 `probs.h/d/a`
--    （缺失时回退 `reasoning_factors.*_prob_weighted`）。
--    ⚠️ 绝不用顶层 `home_true_prob`/`draw_true_prob`/`away_true_prob` —— 那些是
--       `predictor.py::_ODDS_PROPAGATION_FIELDS` 从 `match` 透传的**市场先验**（ESPN 赔率去水）；
--       也绝不用 `home_ml_implied` 等市场隐含概率 ⇒ 拿市场概率当模型概率算 CLV 是循环论证。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model.pred_archive_match (
    archive_id       bigint   NOT NULL REFERENCES model.pred_archive(archive_id) ON DELETE CASCADE,
    match_key        text     NOT NULL,          -- 稳定键：home_en|away_en（引擎侧 _bk_stable_key 同口径）
    match_date       date,                      -- BJT 比赛日（web 层 bjt_today 同口径）
    kickoff_utc      timestamptz,
    home_cn          text     NOT NULL,
    away_cn          text     NOT NULL,
    home_en          text,
    away_en          text,
    direction        text,
    stars            text,
    confidence_score numeric,
    low_confidence   boolean,
    p_home           numeric,
    p_draw           numeric,
    p_away           numeric,
    predicted_score  text,
    jc_match_id      bigint,                    -- 竞彩官方 match_id（按名 join 得上才填，join 不上就留 NULL）
    PRIMARY KEY (archive_id, match_key),
    CONSTRAINT pred_archive_match_home_cn_not_null CHECK (home_cn IS NOT NULL),
    CONSTRAINT pred_archive_match_away_cn_not_null CHECK (away_cn IS NOT NULL),
    CONSTRAINT pred_archive_match_p_home_check CHECK (p_home IS NULL OR (p_home >= 0 AND p_home <= 1)),
    CONSTRAINT pred_archive_match_p_draw_check CHECK (p_draw IS NULL OR (p_draw >= 0 AND p_draw <= 1)),
    CONSTRAINT pred_archive_match_p_away_check CHECK (p_away IS NULL OR (p_away >= 0 AND p_away <= 1)),
    CONSTRAINT pred_archive_match_confidence_check
        CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1))
);
CREATE INDEX IF NOT EXISTS pred_archive_match_jc_idx
    ON model.pred_archive_match (jc_match_id) WHERE jc_match_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pred_archive_match_date_idx
    ON model.pred_archive_match (match_date);

-- ─────────────────────────────────────────────────────────────
-- ③ analysis.clv_match：CLV 逐选项明细（建议 3 子项 2）
--    p_close = 竞彩官方 had 赔率在**开球前最后一版**的比例去水结果。
--    收盘取 `fact.jc_odds_history` 的 max(seq_no) —— 上游 is_close 恒 False
--    （「本层不猜封盘」），故收盘由本层按「最后一版 + 早于开球」判定。
--    实测：402 场的 had 最后一版**全部早于开球**，平均提前 **9.52 小时**（0.04 ~ 59.48 小时）。
--    ⚠️ 时区陷阱：`fact.jc_match.kickoff_bj` 是 `timestamp without time zone`（BJT 墙上时钟），
--       `fact.jc_odds_history.update_ts` 是 `timestamptz` ⇒ 在 SQL 里直接比这两列时，
--       PostgreSQL 按**会话时区**解释 kickoff_bj（会话 UTC ⇒ 领先时间凭空多 8 小时）。
--       本项目一律在 Python 侧显式按 BJT 处理。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analysis.clv_match (
    match_id    bigint   NOT NULL,
    clv_date    date     NOT NULL,              -- BJT 比赛日（与归档行 match_date 同口径）
    league_key  text     NOT NULL,
    archive_id  bigint   REFERENCES model.pred_archive(archive_id) ON DELETE SET NULL,
    play_type   text     NOT NULL DEFAULT 'had',
    option_code text     NOT NULL,              -- 'h' / 'd' / 'a'
    p_model     numeric  NOT NULL,              -- 模型 true_prob（0 会被 clv() 拒，故 >0）
    p_close     numeric  NOT NULL,              -- 收盘去水概率
    clv         numeric  NOT NULL,              -- ln(p_model / p_close)
    odds_close  jsonb,                          -- 收盘赔率原文（竞彩官方，未经换算）
    close_ts    timestamptz,                   -- 收盘快照时刻（= jc_odds_history.update_ts）
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (match_id, play_type, option_code),
    CONSTRAINT clv_match_p_model_check CHECK (p_model > 0 AND p_model <= 1),
    CONSTRAINT clv_match_p_close_check CHECK (p_close > 0 AND p_close <= 1),
    CONSTRAINT clv_match_option_check CHECK (option_code IN ('h', 'd', 'a'))
);
CREATE INDEX IF NOT EXISTS clv_match_date_idx ON analysis.clv_match (clv_date);
CREATE INDEX IF NOT EXISTS clv_match_league_idx ON analysis.clv_match (league_key, clv_date);

-- ─────────────────────────────────────────────────────────────
-- ④ analysis.clv_daily：日汇总（看板主指标的载体）
--    n_matches 必带 —— n 小时**不展示 CLV 均值**（宁可空白不误导，
--    与 §1.2「n=18 的命中率不可信」同一条纪律）。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analysis.clv_daily (
    clv_date    date     NOT NULL,
    league_key  text     NOT NULL,
    n_matches   integer  NOT NULL DEFAULT 0,
    n_options   integer  NOT NULL DEFAULT 0,
    mean_clv    numeric,
    median_clv  numeric,
    min_clv     numeric,
    max_clv     numeric,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (clv_date, league_key),
    CONSTRAINT clv_daily_n_matches_check CHECK (n_matches >= 0),
    CONSTRAINT clv_daily_n_options_check CHECK (n_options >= 0)
);

COMMIT;

-- ── 授权（与既有 model.* / analysis.* 保持一致）────────────────
-- 说明：既有表的授权是显式 GRANT 出来的，新建表不会自动继承 ⇒ 必须显式补。
--   league_app：读写；league_ro：只读；league_ing：无权（model.* 写入由 app 角色承担）。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'league_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON model.pred_archive, model.pred_archive_match TO league_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON analysis.clv_match, analysis.clv_daily TO league_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'league_ro') THEN
    GRANT SELECT ON model.pred_archive, model.pred_archive_match TO league_ro;
    GRANT SELECT ON analysis.clv_match, analysis.clv_daily TO league_ro;
  END IF;
END $$;

-- 核验（应返回 4 行）
-- SELECT table_schema, table_name FROM information_schema.tables
--  WHERE (table_schema, table_name) IN (('model','pred_archive'),('model','pred_archive_match'),
--                                        ('analysis','clv_match'),('analysis','clv_daily'))
--  ORDER BY 1,2;
