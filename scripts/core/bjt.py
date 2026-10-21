"""BJT（北京时间）日口径 —— 引擎侧唯一真源。

为什么要有这个模块：竞彩/体彩的一切「比赛日」口径都是**北京时间日历日**，
而引擎内部的赛事时间戳是 UTC（ESPN 给的 `date` 字段）。跨日转换一旦两侧口径不一致，
就会出现「差一天」的问题（UTC 16:45 = BJT 次日 00:45）。

口径统一到 `web/services/store.py::bjt_today`（它用 `ZoneInfo("Asia/Shanghai")`），
两边用同一套 ⇒ 引擎写出的 `match_date` 与 web 层算的「今天」永远一致。

⚠️ 只做**日历日**换算，不做时刻换算；时刻换算请用 `astimezone` 显式做。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BJT = ZoneInfo("Asia/Shanghai")


def now_bjt() -> datetime:
    """当前北京时间（aware）。"""
    return datetime.now(BJT)


def to_bjt(moment: datetime) -> datetime:
    """任意 aware/naive datetime → BJT（naive 按 UTC 解释后转换）。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(BJT)


def bjt_date(value: str | datetime | None) -> str | None:
    """ISO 字符串 / datetime → **BJT 日历日** 'YYYY-MM-DD'。

    解析不了返回 None（**不猜**）—— 调用方决定跳过还是用别的口径。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_bjt(value).date().isoformat()
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return to_bjt(moment).date().isoformat()
