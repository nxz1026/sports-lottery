-- ============================================================================
-- P0-STORE2（DDL 部分，队长执行）：竞彩 / 传统足彩 / 数字彩 的 fact 层落点
-- 依据：docs/探针核对报告-v1.1-20260915.md（字段全部取自真实响应）+ docs/国内采集机实施文档-v1.md §5 契约 v1.1
-- 与 AF/FD 链路的关系：这是**第三条源链路**（webapi.sporttery.cn）。权限口径沿用源数据层：
--   league_ing = SELECT/INSERT/UPDATE（**无 DELETE/TRUNCATE**，落进去就洗不掉 ⇒ 宁可不插，不可插错）；league_app/league_ro = SELECT。
-- ⚠️ 执行时机：必须与代码单 P0-STORE2 **同一轮**（§9-27）。本脚本会 ALTER ref.team（加 jc_id 生成列 + partial unique），
--    而 ref.team 正被 P0-MODEL2 的测试频繁读写 ⇒ 只在**没有 coder 在跑**的时候执行（ACCESS EXCLUSIVE 会排队）。
-- 设计取舍（三条，都是"能不能重算"的分界）：
--   ① 官方原值一律 NOT NULL 落库（sectionsNo999 可以是中文"取消"、result 可以是全角"3＋"），**解析列一律可空**；
--      解析失败 = NULL + 计数，绝不默认 0（0 在盘口/比分里是有意义的数据）。
--   ② 整条官方 payload 不在 fact 里重复存：上游 stg.<topic> 已经每行存了 jsonb（列 line），fact 只放解析后的规范列 + 必要原值。
--   ③ 时序数据（盘口快照）与"状态"（场次/赛果/期头）分开：盘口按 (场次,玩法,快照时刻) 一行长存，收盘价从此可查 ⇒ CLV 不必买境外盘口 API。
-- 跑法：
--   install -m 0644 ~/league-v2/docs/db/infra_p0_8_jc_tables.sql /tmp/
--   sudo -u postgres psql -d league -v ON_ERROR_STOP=1 -f /tmp/infra_p0_8_jc_tables.sql
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- 1) 队：第三个源 sporttery
-- 官方 team id（homeTeamId/awayTeamId）是**跨季稳定**的；今天刚在 AF 身上吃过"按译文名认队"的亏（P0-TEAMKEY1），
-- 这条新链路一开始就按 id 认队。aliases 里存 'sporttery'，与 'api_football'/'football_data' 同构。
ALTER TABLE ref.team ADD COLUMN IF NOT EXISTS jc_id bigint GENERATED ALWAYS AS
  (CASE WHEN aliases ? 'sporttery' AND (aliases->>'sporttery') ~ '^[0-9]+$'
        THEN (aliases->>'sporttery')::bigint END) STORED;
COMMENT ON COLUMN ref.team.jc_id IS '体彩官方 team id（webapi.sporttery.cn 的 homeTeamId/awayTeamId），从 aliases 生成，不手填';
CREATE UNIQUE INDEX IF NOT EXISTS team_jc_key ON ref.team (sport, jc_id) WHERE jc_id IS NOT NULL;

-- ---------------------------------------------------------------- 2) 竞彩场次（足球）
CREATE TABLE IF NOT EXISTS fact.jc_match (
    match_id      bigint       PRIMARY KEY,          -- 官方 matchId（**本表身份**，勿用 matchNum 当键）
    match_num     integer,                           -- 官方 matchNum（int，如 2004；赛果侧同字段是字符串 ⇒ 不在此处混用）
    match_num_str text         NOT NULL,             -- "周二004"（投注号，人读）
    match_num_date text,                             -- "260915"
    business_date date         NOT NULL,             -- 营业日（北京）
    kickoff_bj   timestamp     NOT NULL,             -- matchDate + matchTime 拼接，**北京时间无时区**（全库竞彩口径统一，换算 UTC 在代码层做）
    league_id     bigint, league_cn text, league_abbr text,
    home_sporttery_id bigint NOT NULL, away_sporttery_id bigint NOT NULL,
    home_cn       text NOT NULL, away_cn text NOT NULL,        -- *TeamAllName 官方全名（原样，含"国  安"式空格也要照存）
    home_abbr text, away_abbr text, home_rank text, away_rank text,
    home_team_id  integer REFERENCES ref.team(team_id),        -- 对齐到既有俱乐部才填；**对不上就 NULL，不许猜**
    away_team_id  integer REFERENCES ref.team(team_id),
    match_status  text, sell_status integer,
    betting_single integer, betting_all_up integer,            -- 单关/过关资格（官方 0/1）
    is_hide smallint, is_hot smallint,
    src_hash      text NOT NULL, src_file text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jc_match_date_consistent CHECK (kickoff_bj::date IN (business_date, business_date + 1))
);
COMMENT ON TABLE fact.jc_match IS '竞彩足球开售场次（源：jczq_offer.value.matchInfoList[].subMatchList[]，快照会反复来，按 match_id upsert 取最新）';
CREATE INDEX IF NOT EXISTS jc_match_kickoff ON fact.jc_match (kickoff_bj);
CREATE INDEX IF NOT EXISTS jc_match_team    ON fact.jc_match (home_team_id, away_team_id);

-- ---------------------------------------------------------------- 3) 竞彩盘口时序（一行 = 一场 × 一玩法 × 一次快照）
CREATE TABLE IF NOT EXISTS fact.jc_offer (
    match_id    bigint       NOT NULL REFERENCES fact.jc_match(match_id),
    snap_ts     timestamptz  NOT NULL,               -- **UTC**，采集机请求发出时刻（契约 §5.0）
    play_type   text         NOT NULL,               -- 归一化口径：had/hhad/crs/ttg/haf（与 derive.registry.PLAYS 对齐）
    pool_code   text         NOT NULL,               -- 官方码：HAD/HHAD/CRS/TTG/HAFU
    goal_line        text, goal_line_value numeric(6,2),   -- 让球/让分线；had/crs/ttg 常为空串 ⇒ NULL
    options     jsonb        NOT NULL,               -- 官方玩法块**整块原样**（含 *f 旗标与 updateDate/updateTime）
    odds_history jsonb,                      -- 2026-09-16 队长 ALTER：官方 payload 顶层 `oddsHistory`（同场同玩法最近 ≤5 次变价，含 poolCode/h/d/a/goalLine/updateDate+updateTime，matchId 恒为 0 ⇒ 归属只能靠**外层 payload 的 matchId**）；现解析层**丢弃**此键，捕获由 P0-COLLECT3 负责
    odds_update timestamp,                           -- 块内 updateDate+updateTime 拼接（官方口径的赔率更新时间，北京无时区）
    src_hash    text NOT NULL, src_file text NOT NULL,
    PRIMARY KEY (match_id, play_type, snap_ts),
    CONSTRAINT jc_offer_options_object CHECK (jsonb_typeof(options) = 'object'),
    CONSTRAINT jc_offer_play_type      CHECK (play_type IN ('had','hhad','crs','ttg','haf'))
) TABLESPACE ts_snap;
COMMENT ON TABLE fact.jc_offer IS '竞彩盘口快照序列：同一 (match_id,play_type) 按 snap_ts 排序即盘口时序，**封盘前最后一条 = 远端认定的收盘价**（CLV/EV 来源）';
CREATE INDEX IF NOT EXISTS jc_offer_snap ON fact.jc_offer (snap_ts);
CREATE INDEX IF NOT EXISTS jc_offer_play ON fact.jc_offer (play_type, snap_ts);

-- ---------------------------------------------------------------- 4) 竞彩赛果（足球，一场一行）
CREATE TABLE IF NOT EXISTS fact.jc_result (
    match_id     bigint      PRIMARY KEY REFERENCES fact.jc_match(match_id),
    match_num_str text,
    sections_no_1   text NOT NULL,                   -- 官方"半场"原值："2:1" / "" / 其它非比分串照存
    sections_no_999 text NOT NULL,                  -- 官方"全场"原值："5:1" / **"取消"**（实测中文）
    ht_h smallint, ht_a smallint, ft_h smallint, ft_a smallint,   -- 解析列：**解析不了就 NULL**
    sp_home numeric(6,2), sp_draw numeric(6,2), sp_away numeric(6,2),  -- 实测 14 场里 5 场全空（非竞彩开盘场次）⇒ 可空
    goal_line text,                                  -- 让球线，实测含正号 "+4"
    win_flag text, match_result_status text, result_status text, pool_status text,
    league_id bigint, league_cn text, home_cn text, away_cn text,
    home_sporttery_id bigint, away_sporttery_id bigint,
    betting_single integer,
    src_hash text NOT NULL, src_file text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jc_result_scores_nonneg CHECK (ft_h IS NULL OR (ft_h >= 0 AND ft_a >= 0)),
    CONSTRAINT jc_result_ht_le_ft      CHECK (ft_h IS NULL OR ht_h IS NULL OR ht_h <= ft_h)   -- 解析后的一致性（只约束解析成功的行）
);
COMMENT ON TABLE fact.jc_result IS '竞彩足球官方赛果（源：jczq_result.value.matchResult[]，25 键原值搬运）；**官方自带半场比分** ⇒ 判奖不再依赖 AF 的 halftime';
CREATE INDEX IF NOT EXISTS jc_result_status ON fact.jc_result (match_result_status);

-- ---------------------------------------------------------------- 5) 传统足彩期头（一期一玩法一行）
-- ⚠️ 期号跨玩法会重号（实测 90 与 900129 都是 26126）⇒ 主键必须是 (game_num, issue_no) 两列。
CREATE TABLE IF NOT EXISTS fact.jc_issue (
    game_num   text NOT NULL,                 -- '90' 胜负游戏(14场) / '900129' 任选9场 / '98' 6场半全场 / '94' 4场进球
    issue_no   text NOT NULL,                 -- lotteryDrawNum
    game_name  text, sale_begin timestamp, sale_end timestamp, draw_at timestamp,
    n_matches  smallint NOT NULL CHECK (n_matches > 0),
    draw_num_list jsonb NOT NULL,             -- value.drawNumList（可售期号数组，实测 7 个）
    raw_head      jsonb NOT NULL,             -- drawMatch 整条原样（防官方加字段时信息丢失）
    src_hash   text NOT NULL, src_file text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_num, issue_no),
    CONSTRAINT jc_issue_list_is_array CHECK (jsonb_typeof(draw_num_list) = 'array')
);
COMMENT ON TABLE fact.jc_issue IS '传统足彩奖期期头（源：jc_issue.value.drawMatch）；场数**不固定**（任九给 14 场候选）⇒ 不许按常识断言长度';

CREATE TABLE IF NOT EXISTS fact.jc_issue_match (
    game_num  text NOT NULL, issue_no text NOT NULL, seq smallint NOT NULL,   -- matchNum（投注序号 1..N，判奖顺序官方定）
    gm_match_id bigint,                        -- **与 fact.jc_match.match_id 同源** ⇒ 传统足彩场次可直连竞彩盘口（实测确认）
    league_cn text, home_cn text, away_cn text,     -- masterTeamAllName / guestTeamAllName（另存 *TeamName 短名于 raw）
    home_short text, away_short text,          -- masterTeamName（实测含全角空格 "国  安"，原样）
    start_date date,                           -- startTime 实测只有日期
    raw jsonb NOT NULL,
    is_drawn   boolean NOT NULL DEFAULT false, -- 期头先落，开奖后置真
    cz_score   text, cz_half_score text,       -- 开奖后：官方全场/半场原值 "5:1" / "2:1"
    official_result text,                       -- 开奖后：官方判定串，**三玩法形态不同**（"3" / "3＋,1" / "3,3"）⇒ 原样存不解析
    src_hash text NOT NULL, src_file text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_num, issue_no, seq),
    FOREIGN KEY (game_num, issue_no) REFERENCES fact.jc_issue (game_num, issue_no)
);
COMMENT ON TABLE fact.jc_issue_match IS '传统足彩每期的场次（期头 jc_issue.matchList → 一行一场；开奖后补 cz_score/official_result）';
CREATE INDEX IF NOT EXISTS jc_issue_match_gm ON fact.jc_issue_match (gm_match_id);

CREATE TABLE IF NOT EXISTS fact.jc_issue_prize (
    game_num text NOT NULL, issue_no text NOT NULL, tier_no smallint NOT NULL,   -- prizeLevelList 下标（1 起）
    prize_level text NOT NULL,                  -- "一等奖"/"二等奖"/"任选9场"…
    stake_count text,                           -- **官方原样字符串**（含数字，未定奖时可为空串）
    stake_amount text,                          -- **带千分位逗号的原值**（"12,387"；浮动奖未定为 ""）⇒ 不在库里转数字
    stake_amount_format text,                   -- 干净版（实测存在）⇒ 下游要用数字时读这列
    is_current    boolean NOT NULL DEFAULT true, -- v1.2 加：`tier_no` 是列表下标，官方在开奖前后会整体换形（真包实测 30/120 期跨批变化），
                                                  -- 而 league_ing 在 fact **无 DELETE** ⇒ 重刷同一期时旧下标行只能置 false，不许假删
    total_prizeamount text, award_type integer, grp text, sort_no integer,
    is_rj boolean NOT NULL DEFAULT false,       -- true = 任九孪生（prizeLevelList 里混进来的那批）
    raw jsonb NOT NULL, src_hash text NOT NULL, src_file text NOT NULL,
    -- R48：PK 放宽到含 src_hash。旧 PK (game_num,issue_no,tier_no) 让上面 is_current 的
    -- 「重刷同一期时旧下标行置 false、不许假删」在结构上**无法实现**（同 tier 只能一行
    -- ⇒ is_current 恒 true，实测 167 行全是 true，是死列）。src_hash 是 payload 内容哈希
    -- （不含 snap_ts/fetched_at，实测同内容跨批同 hash）⇒ 天然就是版本标识。
    PRIMARY KEY (game_num, issue_no, tier_no, src_hash),
    FOREIGN KEY (game_num, issue_no) REFERENCES fact.jc_issue (game_num, issue_no)
);
COMMENT ON TABLE fact.jc_issue_prize IS '传统足彩奖级（源：jc_issue_result.*Detail.prizeLevelList，条数不固定：实测 sfc 3 条 / jqc 1 条）';

CREATE TABLE IF NOT EXISTS fact.jc_issue_draw (
    game_num text NOT NULL, issue_no text NOT NULL,
    game_key text NOT NULL CHECK (game_key IN ('sfc','jqc','bqc')),   -- 官方三棵树的键名（不是玩法号，映射由代码层定）
    game_name text,                            -- 2026-09-16 队长 ALTER：解析层 `jc_issue_result` 产出 game_name（"胜负游戏"），不加会插不下
    draw_result text,                          -- lotteryDrawResult 原串
    pool_after text, sales text,               -- poolBalanceAfterdraw / totalSaleAmount **原值**（数字带逗号 ⇒ 不在 DDL 层清洗）
    pool_after_rj text, sales_rj text,         -- 任九孪生
    paid_begin timestamp, paid_end timestamp,
    is_delay smallint, delay_remark text,
    src_hash text NOT NULL, src_file text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_num, issue_no)
);
COMMENT ON TABLE fact.jc_issue_draw IS '传统足彩开奖兑奖头（源：jc_issue_result 的 *Detail 汇总列）；金额一律原值字符串，判奖明细在 jc_issue_match / jc_issue_prize';

-- ---------------------------------------------------------------- 7) 数字彩开奖（只对照，不进任何模型）
CREATE TABLE IF NOT EXISTS fact.lottery_draw (
    game_num text NOT NULL, issue_no text NOT NULL,       -- '85' 超级大乐透 / '35' 排列3 / '350133' 排列5 / '04' 7星彩
    game_name text, draw_date date, status integer,       -- lotteryDrawStatus 实测 20
    numbers_raw text NOT NULL,                            -- **空格分隔原串**（"10 14 30 33 34 09 12"），远端不重排不校验位数
    numbers jsonb,                                        -- 解析后 {"front":[…],"back":[…]} 或 {"digits":[…]}；解析不了 ⇒ NULL
    prizes jsonb NOT NULL,                                -- prizeLevelList 整数组原样（长度随彩种 1/3/6/9）
    pool text, equipment_count integer,
    src_hash text NOT NULL, src_file text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_num, issue_no),
    CONSTRAINT lottery_draw_prizes_array CHECK (jsonb_typeof(prizes) = 'array')
) TABLESPACE ts_snap;
COMMENT ON TABLE fact.lottery_draw IS '数字彩开奖（红线：**只做开奖对照，不进任何模型输入/回测**；见 框架设计 §0 与采集文档 §9）';

-- ---------------------------------------------------------------- 8) 授权（default privileges 已在 p0_2/p0_4 配好，这里兜一层显式）
GRANT USAGE ON SCHEMA fact TO league_ing, league_app, league_ro;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA fact TO league_ing;
GRANT SELECT ON ALL TABLES IN SCHEMA fact TO league_app, league_ro;
REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA fact FROM league_ing;   -- 源数据层：落进去洗不掉

COMMIT;

-- ---------------------------------------------------------------- 9) 验证
SELECT count(*) AS fact_tables FROM information_schema.tables
 WHERE table_schema='fact' AND table_type='BASE TABLE';                    -- 预期 2 + 8 = 10（本脚本建 8 张：jc_match/jc_offer/jc_result/jc_issue/jc_issue_match/jc_issue_prize/jc_issue_draw/lottery_draw）
SELECT column_name FROM information_schema.columns
 WHERE table_schema='ref' AND table_name='team' ORDER BY ordinal_position; -- 应含 jc_id
SELECT indexname FROM pg_indexes WHERE tablename='team' AND schemaname='ref';  -- 应含 team_jc_key
-- 手工复验（三条必须全对，跑法见 OMP-SKILL §5）：
--   · league_ing 能 INSERT fact.jc_match；league_ing DELETE/UPDATE ref.team 仍 42501
--   · league_app 对 fact.jc_offer 只有 SELECT（INSERT 应 42501）
--   · 新表 tablespace：select relname, spcname from pg_class join pg_tablespace …  ⇒ jc_offer/lottery_draw = ts_snap
