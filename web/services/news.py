"""Web 侧每日资讯 LLM 调用网关：唯一允许 web → ai.llm_client 延迟 import 的地方。

契约 §5 例外（README 顶部「接口基线」条目 1）：web 层**顶层**绝不
``import scripts.* / ai.*``；函数体内 ``try/except`` 包裹的 lazy import 是
文档化例外，仅用于 LLM 客户端的优雅降级。本文件是该例外的**唯一汇聚点**：

- 新端点需 LLM → 调本文件函数，不直接 ``from ai import llm_client``。
- 失败语义：返回 ``{"news": "", "pick_reason": ""}``，调用方走占位文案。
- 进程级缓存（``_NEWS_CACHE``）维持单端点 1 次/日 节奏，避免重复消耗 LLM 配额。

降级语义（契约 §5.5）：LLM 模块不可用 / 缺字段 / 超时 → 返回空 news，
由 ``web.routers.jc.daily_news`` 端点补占位文案；永不 raise、永不编造赛果/收益。
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger("web.services.news")

_NEWS_CACHE: dict = {}  # key = (date_iso, sport) → {"news": str, "pick_reason": str}


def _build_prompt(context: str, picks_ctx: str, label: str) -> str:
    """组装 LLM prompt（与原 web/routers/jc.py::daily_news 1:1 复制）。"""
    return (
        "你是面向彩民的赛事资讯编辑。用户是买彩票的球迷，他们关心：球队状态/伤停/主客场/赛季压力/让球线合理性等\n"
        "会影响判断的信号。**禁止编造具体伤员、历史比分、天气等你没有的事实**——\n"
        "没有信息就给'看角度'，不要假装你查了数据。\n\n"
        f"基于下面『今日在售赛事（{label}）』与『精选候选』，生成两条短文：\n"
        "① news：3 行今日可关注的客观角度，每行 ≤26 字、共 3 行（用 \\n 分隔），总计 ≤78 字。\n"
        "   **不要复述对阵名/场次号**（表格里已有）——只写角度。\n"
        "   可用角度（每行挑一个，写'今日 + 角度 + 含义'，如'今日多场 +1 深盘需核合理'）：\n"
        "   - 让球盘深度（多场 +1/-1 深盘：是否反映真实实力差）\n"
        "   - 赛季阶段（争冠/保级压力差）\n"
        "   - 主客场近期形态（哪边有主场上风）\n"
        "   - 赛程密度（周中杯赛 + 周末联赛、连场消耗）\n"
        "   - 大球场次（榜首/榜尾/欧战资格争夺）\n"
        "   禁止：收益承诺、感叹号、预测比分、人名/伤员/历史编造。\n"
        "② pick_reason：精选候选为什么值得跟进，≤60 字。\n"
        "   应给玩家'怎么核验这场'的角度（让球合理性 + 主客形态 + 赛季重要性），不要只复述模型方向。\n"
        "   如果精选候选为空 → 返回空字符串。\n\n"
        f"今日在售赛事（按业务日列出 8 场，含主客）：\n{context}\n\n"
        f"精选候选（前 2 条，含官方让球线与模型方向）：\n{picks_ctx}\n\n"
        "只输出严格 JSON：{\"news\":\"line1\\nline2\\nline3\",\"pick_reason\":\"...\"}"
    )


def _try_llm(prompt: str) -> dict[str, str]:
    """调用 LLM 生成；任何异常/字段缺失一律返回空 dict。

    内含契约 §5 例外：函数体内 ``from ai import llm_client`` 是 web 唯一
    允许的 ai 反向依赖点（详见模块 docstring）。
    """
    try:
        from ai import llm_client  # noqa: PLC0415 契约 §5 文档化例外
        res = llm_client.generate(prompt) or {}
    except Exception as exc:
        _LOGGER.warning("LLM daily-news 调用失败：%s", exc)
        return {"news": "", "pick_reason": ""}
    return {
        "news": str(res.get("news") or "").strip(),
        "pick_reason": str(res.get("pick_reason") or "").strip(),
    }


def generate_daily_news(*, date_iso: str, sport: str, context: str, picks_ctx: str,
                        label: str) -> dict[str, str]:
    """生成 (date, sport) 维度缓存的每日资讯。

    返回 ``{"news": str, "pick_reason": str}``；news 限 ≤78，pick_reason 限 ≤60。
    调用方负责补占位文案（参考 web.routers.jc.daily_news）。
    """
    key = (date_iso, sport)
    hit = _NEWS_CACHE.get(key)
    if hit and (hit.get("news") or hit.get("pick_reason")):
        return hit

    prompt = _build_prompt(context, picks_ctx, label)
    raw = _try_llm(prompt)
    out = {
        "news": raw["news"][:78],
        "pick_reason": raw["pick_reason"][:60],
    }
    _NEWS_CACHE[key] = out
    return out


__all__ = ["generate_daily_news"]