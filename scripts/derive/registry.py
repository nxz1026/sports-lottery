"""玩法注册表（derive_play）：只登记已实现的玩法，未实现的键点名拒绝，绝不放占位假实现。"""

from __future__ import annotations

from collections.abc import Callable

from derive import goals4, haf, scores31, totals
from derive.handicap import probs

PlayOut = dict[str, float] | tuple[dict[str, float], dict[str, float]]


def _had(grid: list[list[float]]) -> dict[str, float]:
    """胜平负：让球线固定 0 的三向（要带让球请走 hhad，别在这里传线）。"""
    return probs(grid, 0)


PLAYS: dict[str, Callable[..., PlayOut]] = {
    "had": _had,
    "hhad": probs,
    "crs": scores31.buckets,
    "ttg": totals.buckets,
    "jqc": goals4.team_buckets,
}


def derive_play(play_type: str, grid: list[list[float]], line: int | None = None) -> PlayOut:
    """未注册键 → KeyError 点名未实现且禁 0.0 占位；hhad 缺 line → TypeError。

    注意：haf 不走本注册表，haf 是二维结构（全场 + 上半场），需要
    ``lambda_home / lambda_away + line``，不接受单 grid 参数。
    走 ``derive.haf.probs(lambda_home, lambda_away, line)`` 单独入口。
    """
    try:
        fn = PLAYS[play_type]
    except KeyError:
        raise KeyError(f"玩法 {play_type!r} 未实现，勿用 0.0 占位") from None
    if play_type == "hhad":
        if line is None:
            raise TypeError("hhad 必须显式给让球线 line，禁默认 0（会把让球悄悄算成胜平负）")
        return fn(grid, line)
    return fn(grid)
