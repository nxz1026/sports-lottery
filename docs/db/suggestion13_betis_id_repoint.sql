-- ============================================================================
-- 纠正：竞彩官方 id 279 挂错了队（路线图 §6.13）
--
-- 错在哪（2026-10-22 生产实测）：
--   `docs/db/suggestion10_team_sporttery_map.sql` 第 3 节**手写**了
--       INSERT ... SELECT 'football', 279, 2351      -- 2351 = 「毕尔巴鄂竞技」
--   当时的推断是「279 的 2 场对手是马赛、霍芬海姆 ⇒ 欧联 ⇒ 西甲劲旅毕尔巴鄂竞技」。
--   ⚠️ 这是**从对手反推球队**的猜测，恰恰是同文件第 17~20 行纪律明令禁止的
--      「只取 fact.jc_match 的竞彩官方中文名（唯一真源），不做任何名称推断」。
--   而竞彩 payload 里写着的字就是「**贝西克塔斯**」——
--   贝西克塔斯（Real Betis）同样是踢欧联的西甲球队 ⇒ 推断错误。
--
-- 真相（生产实测，逐条可复核）：
--   · 竞彩原始数据里 **279 = 贝西克塔斯**（3 场：2041511 / 2041982 / 2042126），
--     **505 = 皇家贝蒂斯**（6 场，欧战场次名），**506 = 毕尔巴鄂竞技**（5 场）；
--   · 全库 397 个竞彩 id **没有任何一个 id 对应多个中文名** ⇒ 源数据零歧义；
--   · `ref.team` 两行的身份用 **af_id** 对照 API-Football 原始响应核实过：
--       #2351「毕尔巴鄂竞技」af_id=531 → AF 官方名 **Athletic Club**  ✓
--       #2355「皇家贝蒂斯」  af_id=543 → AF 官方名 **Real Betis**     ✓
--   ⇒ **279（贝西克塔斯）该指向 #2355（Real Betis）**，不是 #2351。
--   ⇒ 同一家俱乐部在竞彩有**两个 id + 两个中文名**（505 皇家贝蒂斯 / 279 贝西克塔斯），
--     这正是 `ref.team_sporttery` 存在的原因（多对一映射表）。
--
-- 本文件做什么（**只改这 1 条映射 + 3 场 team_id + 1 条列注释**，其余一行不动）：
--   ① 备份（改前把 279 的映射行与受影响的场次存下来，可原样回滚）
--   ② `ref.team_sporttery` 里 (football, 279) 改指 #2355
--   ③ `fact.jc_match` 里 3 场「贝西克塔斯」的主/客 team_id 改指 #2355
--   ④ 修 `ref.team_sporttery.jc_id` 的列注释（原文把 279 说成毕尔巴鄂竞技的 id，是错的）
--   ⑤ 对账输出（幂等与否、错配是否清零全看它）
--
-- 纪律：
--   · **不新建 ref.team 行**（#2355 就是 Betis 的行，af_id=543 已核实）
--   · **不碰 aliases**（#2355 的主 id 仍是 505 ⇒ jc_id 生成列不变 ⇒ team_jc_key 不受影响）
--   · **不改别的 id**（506 → #2351 原样保留）
--   · **幂等**：重跑一次 ②③ 的 WHERE 条件不成立 ⇒ 零改动
--
-- 跑法：
--   # ① 预演（把 COMMIT 换成 ROLLBACK ⇒ 一个字节都不落库）
--   sed 's/^COMMIT;/ROLLBACK;/' suggestion13_betis_id_repoint.sql \
--     | sudo -u postgres psql -d league -v ON_ERROR_STOP=1 -f -
--   # ② 真执行
--   sudo -u postgres psql -d league -v ON_ERROR_STOP=1 -f suggestion13_betis_id_repoint.sql
--   # ③ Python 入口（默认预演；--execute 才写库）
--   python scripts/ingest/jc_team_id_repoint.py [--execute]
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- 1) 备份
-- @backup-block:start
--    （这两句需要 CREATE 权限 ⇒ league_ing 角色跑不了 ⇒ 走 psql/postgres 的 DBA 路径；
--      Python 入口 scripts/ingest/jc_team_id_repoint.py 会剥掉本块，改用 JSON 文件备份）
CREATE TABLE IF NOT EXISTS ref.team_sporttery_pre_s13_backup AS
SELECT * FROM ref.team_sporttery WHERE sport = 'football' AND jc_id = 279;

CREATE TABLE IF NOT EXISTS fact.jc_match_pre_s13_backup AS
SELECT match_id, home_team_id, away_team_id, home_sporttery_id, away_sporttery_id
FROM fact.jc_match
WHERE home_sporttery_id = 279 OR away_sporttery_id = 279;
-- @backup-block:end

-- ---------------------------------------------------------------- 2) 前置断言：目标队行必须存在且是 Real Betis
DO $$
DECLARE
    v_target_id  integer;
    v_target_cn  text;
    v_current_id integer;
BEGIN
    -- 目标 = af_id 543（API-Football 官方名 Real Betis，已对 raw.af_raw 核实）
    SELECT team_id, name_cn INTO v_target_id, v_target_cn
    FROM ref.team
    WHERE sport = 'football' AND af_id = 543;

    IF v_target_id IS NULL THEN
        RAISE EXCEPTION '目标队行不存在：ref.team 里没有 af_id=543（Real Betis）⇒ 不执行任何改动';
    END IF;
    IF v_target_cn <> '皇家贝蒂斯' THEN
        RAISE EXCEPTION '目标队行的 name_cn 变了：期望「皇家贝蒂斯」，实际「%」⇒ 请人工确认后再跑', v_target_cn;
    END IF;

    SELECT team_id INTO v_current_id
    FROM ref.team_sporttery WHERE sport = 'football' AND jc_id = 279;

    RAISE NOTICE 'jc_id 279 当前指向 team_id=%（目标 #% = %）', v_current_id, v_target_id, v_target_cn;
END $$;

-- ---------------------------------------------------------------- 3) 改指：279 → Real Betis 行（#2355）
UPDATE ref.team_sporttery s
SET team_id = (SELECT t.team_id FROM ref.team t WHERE t.sport = 'football' AND t.af_id = 543)
WHERE s.sport = 'football'
  AND s.jc_id = 279
  AND s.team_id <> (SELECT t.team_id FROM ref.team t WHERE t.sport = 'football' AND t.af_id = 543);

-- ---------------------------------------------------------------- 4) 3 场「贝西克塔斯」改指向（只改这 3 场）
UPDATE fact.jc_match m
SET home_team_id = (SELECT ts.team_id FROM ref.team_sporttery ts
                    WHERE ts.sport = 'football' AND ts.jc_id = 279)
WHERE m.home_sporttery_id = 279;

UPDATE fact.jc_match m
SET away_team_id = (SELECT ts.team_id FROM ref.team_sporttery ts
                    WHERE ts.sport = 'football' AND ts.jc_id = 279)
WHERE m.away_sporttery_id = 279;

-- ---------------------------------------------------------------- 5) 修列注释（原注释把 279 说成毕尔巴鄂竞技的 id —— 那是 2026-10-21 的误推断）
COMMENT ON COLUMN ref.team_sporttery.jc_id IS
 '竞彩官方 teamId；同一家俱乐部在竞彩可能有多个 id 与多个中文名（例：Real Betis = 皇家贝蒂斯 = 贝西克塔斯，'
 '竞彩给 505「皇家贝蒂斯」（欧战场次名）与 279「贝西克塔斯」（西甲场次名）两个 id ⇒ 两个 id 都指向 #2355）';

-- ---------------------------------------------------------------- 6) 对账
SELECT (SELECT count(*) FROM ref.team_sporttery)                                AS map_rows,
       (SELECT count(DISTINCT jc_id) FROM ref.team_sporttery
         WHERE sport = 'football')                                              AS mapped_ids,
       (SELECT team_id FROM ref.team_sporttery
         WHERE sport = 'football' AND jc_id = 279)                              AS jc279_team_id,
       (SELECT name_cn FROM ref.team
         WHERE team_id = (SELECT team_id FROM ref.team_sporttery
                           WHERE sport = 'football' AND jc_id = 279))           AS jc279_team_cn,
       (SELECT count(*) FROM fact.jc_match
         WHERE home_sporttery_id = 279 OR away_sporttery_id = 279)              AS matches_279,
       (SELECT count(*) FROM fact.jc_match
         WHERE home_sporttery_id = 279
           AND home_team_id <> (SELECT t.team_id FROM ref.team t
                                 WHERE t.sport = 'football' AND t.af_id = 543))  AS wrong_home_279,
       (SELECT count(*) FROM fact.jc_match
         WHERE away_sporttery_id = 279
           AND away_team_id <> (SELECT t.team_id FROM ref.team t
                                 WHERE t.sport = 'football' AND t.af_id = 543))  AS wrong_away_279,
       (SELECT count(*) FROM ref.team_sporttery
         WHERE sport = 'football' AND jc_id = 506)                              AS jc506_still_ok,
       (SELECT count(*) FROM ref.team)                                          AS ref_team_rows;

COMMIT;
