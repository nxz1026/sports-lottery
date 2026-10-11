"""web.app.jc — 竞彩看板每日一图 VM（应用层编排）。

设计目标（README「应用层机制」节）：

  - 本模块**不**依赖 FastAPI（无 ``Depends``、无 ``HTTPException``）。
  - 所有外部依赖（``web_store.bjt_today`` / ``jc_view.fixtures_on`` / 算法层）
    通过函数顶层 lazy import 进入；测试可在 ``sys.modules`` 里打桩替换。
  - 返回值是**纯数据字典**，结构与 ``/api/jc/daily-image`` 历史响应一致，
    前端零感知。

新增 router 端点请遵循 ``web.app`` 模式：路由 = 鉴权 + URL + 调本层函数。
"""
from __future__ import annotations

from datetime import date as _date

# ---------------------------------------------------------------------------
# 白名单常量 —— JC 路由独有，搬离 router 让 router 更薄
# ---------------------------------------------------------------------------

# 用户定范围＝NBA + 五大联赛：每日一图足球表只保留这五家。
_BIG5: frozenset[str] = frozenset({
    "英格兰超级联赛", "西班牙甲级联赛", "意大利甲级联赛",
    "德国甲级联赛", "法国甲级联赛",
})

# league_cn 全名 → 预测文件名里的联赛键，供命中率对账（accuracy service）。
_BIG5_KEYS: dict[str, str] = {
    "英格兰超级联赛": "epl",
    "西班牙甲级联赛": "laliga",
    "意大利甲级联赛": "seriea",
    "德国甲级联赛": "bundesliga",
    "法国甲级联赛": "ligue1",
}


def _big5_with_temp() -> set[str]:
    """白名单全集 = 硬编码五大联赛 ∪ env ``LEAGUE_TEMP_LEAGUES``（去重）。

    端点过滤统一使用本集合：临时赛事（亚运/欧冠/世界杯等）出现在
    daily-image / jc_gap / movement / ai_analyze 等候选中，daily-image 在
    VM 内再做 main/temp 分组。
    """
    from web.config import LEAGUE_TEMP_LEAGUES
    return set(_BIG5) | set(LEAGUE_TEMP_LEAGUES)


def _hit_stats() -> dict:
    """五大联赛真实方向命中率：从历史预测文件实时对账已结算赛果。

    ``reconciled=0``（无已结算样本）→ ``available=False``，页面显示"数据积累中"，
    绝不编数。优先 30d 窗口（样本更大），空则退回 7d。算法与 keys 见
    ``web.services.accuracy.league_accuracy_web``（契约 §5：不允许反向
    import scripts/，故 web 端走独立实现）。
    """
    from web.services.accuracy import league_accuracy_web
    n = hit = 0
    for lk in _BIG5_KEYS.values():
        for days in (30, 7):
            acc = league_accuracy_web(lk, days=days)
            if acc and acc.get("reconciled"):
                n += int(acc["reconciled"])
                hit += int(round(acc["reconciled"] * float(acc.get("direction_accuracy") or 0)))
                break
    if n > 0:
        return {"available": True, "n": n, "hit": hit, "note": ""}
    return {"available": False, "n": 0, "hit": 0, "note": "数据积累中"}


def _nba_available(preds: list, today: _date) -> bool:
    """NBA 是否在开赛窗口：存在最近 ≤7 天的排期才算可出图。

    否则（纯赛程预估、休赛期）不生成篮球每日图。
    ``preds`` 含 ``kickoff_date('YYYY-MM-DD')``。
    """
    for p in preds or []:
        kd = str(p.get("kickoff_date") or "")
        try:
            d = _date.fromisoformat(kd)
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= 7:
            return True
    return False


# ---------------------------------------------------------------------------
# 应用层 VM —— 入口
# ---------------------------------------------------------------------------


def daily_image_vm(sport: str = "football", today: _date | None = None) -> dict:
    """每日一图数据（拆分 ``sport``）。

    sport=football（默认）：竞彩足球五大联赛在售 × 模型算法层预判（只读合并）。
    sport=basketball：NBA 预测表；休赛期（最近一场开球 >7 天外）返回
    ``available=False`` 空态，**不**把赛程预估当"每日推荐"（NBA 未开赛 /
    竞彩篮球未开售）。

    纯函数：无 FastAPI 依赖；``today`` 缺省 → ``web_store.bjt_today()``；测试可
    注入固定日期。
    """
    from store import jc_view

    from web.services import store as web_store
    from web.services.team_match import build_rows, nba_rows

    if today is None:
        today = web_store.bjt_today()
    latest = web_store.latest_by_league()

    if sport == "basketball":
        nba_predictions = (latest.get("nba") or {}).get("data", {}).get("predictions", [])
        nba = nba_rows(nba_predictions)
        if not _nba_available(nba_predictions, today):
            return {
                "sport": "basketball", "available": False, "rows": [],
                "date": today.isoformat(),
                "note": "NBA 2026-27 赛季尚未开赛，篮球每日一图暂未生成（预计 10 月中下旬开赛）。",
                "stats": {"scope": "NBA", "on_sale": 0, "recommend": 0, "nba": 0,
                          "hit": {"available": False, "n": 0, "hit": 0, "note": "数据积累中"}},
            }
        return {
            "sport": "basketball", "available": True, "rows": nba,
            "date": today.isoformat(),
            "stats": {"scope": "NBA", "on_sale": len(nba), "recommend": len(nba), "nba": len(nba),
                      "hit": {"available": False, "n": 0, "hit": 0, "note": "数据积累中"}},
        }

    # sport == "football"（默认）
    # 用今天（BJT）作 day 查所有场次——不查 latest business_date，避免被非五大联赛日期
    # （如亚运/友谊赛）覆盖；daily-image 口径 = "今日在售的五大联赛 + 临时赛事"。
    fixture_rows = jc_view.fixtures_on(today.isoformat())
    pred_by_league = {
        lg: (doc.get("data") or {}).get("predictions", [])
        for lg, doc in latest.items()
    }
    rows = build_rows(fixture_rows, pred_by_league)
    # 白名单 = 五大联赛硬编码 ∪ env LEAGUE_TEMP_LEAGUES；临时赛事无训练样本 → algo/hhad/haf 不会计算（仅 odd 盘口层）。
    rows = [r for r in rows if (r.get("league_cn") or "") in _big5_with_temp()]

    # 分组：五大联赛主面板 + 临时赛事附面板（仅盘口层）。
    main_rows = [r for r in rows if (r.get("league_cn") or "") in _BIG5]
    temp_rows = [r for r in rows if (r.get("league_cn") or "") not in _BIG5]
    main_matched = [r for r in main_rows if r.get("matched")]

    from web.config import LEAGUE_TEMP_LEAGUES
    if not rows:
        # 五大联赛 + 临时赛事都没有：彻底空态
        return {
            "sport": "football", "available": False, "rows": [], "main_rows": [], "temp_rows": [],
            "date": today.isoformat(),
            "note": (f"今日（{today.isoformat()}）五大联赛无在售赛事。"
                      "五大联赛通常在周末/周中分散开赛，请到「竞彩盘口」视图选其他日期查看历史图，"
                      "或查看 [NBA 篮球] 每日一图。"),
            "stats": {"scope": "五大联赛", "main_on_sale": 0, "main_recommend": 0,
                      "temp_on_sale": 0, "nba": 0, "hit": _hit_stats()},
            "match_note": ("范围：NBA + 五大联赛。推荐列为官方盘口(odd)与模型算法层的合并；"
                           "让球线取官方 hhad 盘口；命中率待实际结算数据积累后如实展示。"),
        }
    # available 条件：五大联赛有赛事（用户主面板）或临时赛事有赛事（值得展示，但提示主面板空）
    available = bool(main_rows) or bool(temp_rows)
    note = None
    if not main_rows and temp_rows:
        note = (f"今日（{today.isoformat()}）五大联赛无在售赛事；附 "
                f"{len(temp_rows)} 场临时赛事（{ ' / '.join(LEAGUE_TEMP_LEAGUES) }，盘口层，无模型预判）。")
    return {
        "sport": "football", "available": available,
        "rows": main_rows,    # 主面板（五大联赛）；temp_rows 单独暴露，避免前端同场重复
        "main_rows": main_rows,
        "temp_rows": temp_rows,
        "date": today.isoformat(),
        "note": note,
        "stats": {
            "scope": "五大联赛+临时赛事", "on_sale": len(rows),
            "main_on_sale": len(main_rows), "main_recommend": len(main_matched),
            "temp_on_sale": len(temp_rows),
            "nba": 0,
            "hit": _hit_stats(),
        },
        "match_note": ("范围：NBA + 五大联赛 + 临时赛事（env LEAGUE_TEMP_LEAGUES）。"
                       "临时赛事无模型预判，仅显示官方盘口；让球线取官方 hhad 盘口；"
                       "命中率仅统计五大联赛。"),
    }
