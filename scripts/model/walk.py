"""上一季拟合 → 当季预测：λ + 9×9 矩阵 + 四玩法概率落 model.*（真跑由队长执行；本模块零网络）。

取数口径 = _SCOPE 的谓词逐字照工单：status='ft' AND round <> 'Relegation Round' 且结果取 source='api_football'
行（排除 3 场升降级附加赛 aet/pen：90 分钟口径不一致，会污染 λ）。训练季 = 同联赛严格更早的最大赛季（run()
硬守卫）；2022 只当训练集、永不作预测目标。model.* 只有 app 角色可写（league_ing 连 SELECT 都 42501）。
未见队（当季升班马）走 model.sides.lambdas 的联盟平均先验：atk=def=1.0、γ 不变、features.fallback 明确标记
⇒ 不再因"怕不准"跳过大把真实场次；已知偏差（升班马弱于平均 ⇒ 主场 λ 系统性高估）见 n_fallback 分层。
jqc 落两侧 4 档 h:0|h:1|h:2|h:3+ / a:…（sides.jqc_sides）；缺口（不许 0.0 占位）：hhad 缺官方让球线、haf 未注册。
"""
import argparse
import json

from core.constants import DC_RHO, MAX_GOALS_MC
from core.log import logger
from derive.grid import admissible_rho, dc_grid
from derive.registry import derive_play
from store import pg

from model.fit import fit_attack_defense
from model.sides import PRIOR, commit_sha, jqc_sides, lambdas

PLAYS = ("had", "crs", "ttg", "jqc")  # 本单只写这四种；hhad 缺官方让球线、haf 未注册
SEASONS_SQL = "SELECT DISTINCT league_key, season FROM fact.fixture WHERE status = 'ft' AND round <> 'Relegation Round'"
_SCOPE = """FROM fact.fixture f
  JOIN fact.fixture_result r ON r.fixture_id = f.fixture_id AND r.source = 'api_football'
  JOIN ref.team th ON th.team_id = f.home_team_id JOIN ref.team ta ON ta.team_id = f.away_team_id
 WHERE f.league_key = %s AND f.season = %s AND f.status = 'ft' AND f.round <> 'Relegation Round'
 ORDER BY f.kickoff_at, f.fixture_id"""
TRAIN_SQL = "SELECT th.name_cn, ta.name_cn, r.ft_h, r.ft_a " + _SCOPE
TARGET_SQL = "SELECT f.fixture_id, th.name_cn, ta.name_cn " + _SCOPE
RUN_SQL = ("INSERT INTO model.pred_run (commit, params, n_fixtures, note)"
           " VALUES (%s, %s::jsonb, %s, %s) RETURNING run_id")
FIX_SQL = ("INSERT INTO model.pred_fixture (run_id, fixture_id, lambda_home, lambda_away, matrix, features)"
           " VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)")
MARKET_SQL = "INSERT INTO model.pred_market (run_id, fixture_id, play_type, option_code, p) VALUES (%s, %s, %s, %s, %s)"


def season_pairs(conn) -> list[tuple[str, int, int]]:
    """可预测组合 (联赛, 预测季, 训练季)；训练季 = 同联赛严格更早的最大赛季 ⇒ 2022 天生只当训练集。"""
    scored = conn.execute(SEASONS_SQL).fetchall()
    return [(lk, s, max(t for k, t in scored if k == lk and t < s))
            for lk, s in sorted(scored) if any(k == lk and t < s for k, t in scored)]


def run(conn, league_key: str, season: int, train_season: int | None = None) -> dict:
    """一个 (联赛, 预测季) = 一个 model.pred_run；泄漏防线在入口硬守卫，返回本 run 的真实计数。"""
    if train_season is None:
        train_season = next((t for lk, s, t in season_pairs(conn) if (lk, s) == (league_key, season)), season)
    if train_season >= season:
        raise ValueError(f"泄漏防线：{league_key} {train_season} 不是 {season} 的严格更早赛季（2022 只当训练集）")
    train = [(h, a, float(gh), float(ga)) for h, a, gh, ga in conn.execute(TRAIN_SQL, (league_key, train_season))]
    fit = fit_attack_defense(train)
    targets = conn.execute(TARGET_SQL, (league_key, season)).fetchall()
    named = [(f, h, a) for f, h, a in targets if h and a]  # skipped 只留给"连队名都没有"的行
    predicted = [(f, (h, a), lambdas(fit, h, a)) for f, h, a in named]
    skipped = len(targets) - len(named)
    n_fallback = sum(1 for _, _, (_, _, sh, sa) in predicted if sh != "fit" or sa != "fit")
    rho_clamped = sum(1 for _, _, (lh, la, _, _) in predicted if admissible_rho(lh, la, DC_RHO) < DC_RHO)
    params = {"train_season": train_season, "plays": list(PLAYS), "gamma": fit.gamma, "rho": DC_RHO,
              "rho_clamped": rho_clamped, "max_goals": MAX_GOALS_MC, "skipped_unknown": skipped,
              "n_fallback": n_fallback}
    note = f"{league_key} {season} 预测（训练季 {train_season}）；跳过 {skipped} 场（无队名）；兜底 {n_fallback} 场未见队"
    run_id = conn.execute(RUN_SQL, (commit_sha(), json.dumps(params), len(predicted), note)).fetchone()[0]
    market = []
    for fixture_id, (home, away), (lambda_home, lambda_away, src_h, src_a) in predicted:
        grid = dc_grid(lambda_home, lambda_away, rho=DC_RHO, max_goals=MAX_GOALS_MC)
        features = {"atk_home": fit.atk.get(home, PRIOR), "def_away": fit.def_.get(away, PRIOR),
                    "gamma": fit.gamma, "rho_eff": admissible_rho(lambda_home, lambda_away, DC_RHO),
                    "fallback": {"home": src_h, "away": src_a}}
        conn.execute(FIX_SQL, (run_id, fixture_id, lambda_home, lambda_away, json.dumps(grid),
                               json.dumps(features)))
        for play in PLAYS:
            probs = jqc_sides(grid) if play == "jqc" else derive_play(play, grid)
            market += [(run_id, fixture_id, play, code, p) for code, p in probs.items()]
    conn.cursor().executemany(MARKET_SQL, market)
    logger.info(f"[walk] {league_key} {season} run_id={run_id} γ={fit.gamma:.6f} 写入 {len(predicted)}"
                f" 跳过 {skipped} 兜底 {n_fallback} ρ收缩 {rho_clamped}")
    return {"run_id": run_id, "n_fixtures": len(predicted), "n_market": len(market), "skipped": skipped,
            "n_fallback": n_fallback, "rho_clamped": rho_clamped, "gamma": fit.gamma,
            "train_season": train_season}


def main(argv: list[str] | None = None) -> int:
    """CLI：--league/--season 过滤；每个 (联赛, 预测季) 一个真事务（write_conn('app') 退出即提交）。"""
    ap = argparse.ArgumentParser(description="上一季拟合 → 当季预测：λ/9×9 矩阵/四玩法落 model.*")
    ap.add_argument("--league", help="只跑一个联赛（默认全部）")
    ap.add_argument("--season", type=int, help="只跑一个预测季（默认全部可预测季）")
    args = ap.parse_args(argv)
    with pg.read_conn("app") as conn:
        pairs = [p for p in season_pairs(conn) if args.league in (None, p[0]) and args.season in (None, p[1])]
    if not pairs:
        logger.error("没有可预测的 (联赛, 赛季)：需在库且存在更早赛季作训练集")
        return 1
    for league_key, season, _ in pairs:
        with pg.write_conn("app") as conn:  # 每个 run 一个事务：退出即提交，异常即回滚本 run
            run(conn, league_key, season)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
