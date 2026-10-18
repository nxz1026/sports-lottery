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
import os
import time

_LOGGER = logging.getLogger("web.services.news")

# R21 #1：news LLM 资讯缓存加 TTL + 大小上限，防止长期 web 进程累积 OOM
# 同时 LLM 配额保护（重进程不应每请求都打到 LLM）。
# 默认 TTL 24h（每 (date, sport) 一天 1 次）；默认 max 64（远超现实覆盖）。
# 配置：env 覆盖
#   LEAGUE_PREDICT_NEWS_TTL_S=86400       — TTL 秒数
#   LEAGUE_PREDICT_NEWS_MAX_SIZE=64       — LRU 容量
# 缓存条目形如 {"value": {"news": str, "pick_reason": str}, "ts": float}
# —— value 用 dict 包装保留"同一对象引用"语义（web_news test 守门 ``a is b``）。
_NEWS_CACHE: dict = {}
_NEWS_TTL_S: float = float(os.environ.get("LEAGUE_PREDICT_NEWS_TTL_S", "86400"))
_NEWS_MAX_SIZE: int = int(os.environ.get("LEAGUE_PREDICT_NEWS_MAX_SIZE", "64"))


def _evict_expired_news(now: float) -> None:
    """剔除过期（ts + TTL < now）+ 超出容量上限（最早优先）。

    R21 #1：每次 ``generate_daily_news`` 入口调用一次；成本 O(n) 但 n 实际
    永远 ≤ _NEWS_MAX_SIZE（典型 64）。**保留 ≤ MAX-1 位**给即将写入的
    新 entry，保证写入后 cache 仍 ≤ MAX。
    """
    expired = [k for k, v in _NEWS_CACHE.items()
               if (now - v["ts"]) > _NEWS_TTL_S]
    for k in expired:
        _NEWS_CACHE.pop(k, None)
    # 容量裁剪：留 1 位给新写入，循环结束后 ≤ MAX
    if len(_NEWS_CACHE) >= _NEWS_MAX_SIZE:
        sorted_keys = sorted(_NEWS_CACHE.keys(),
                             key=lambda k: _NEWS_CACHE[k]["ts"])
        excess = len(_NEWS_CACHE) - (_NEWS_MAX_SIZE - 1)
        for k in sorted_keys[:excess]:
            _NEWS_CACHE.pop(k, None)


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


# 体彩关键词守门：避免 LLM 返回「今日天气」「足坛八卦」之类与体彩无关的内容污染资讯条。
# 关键词集合是「让球 / 盘口 / 主场 / 客场 / 赛季 / 形态 / 欧战 / 保级 / 争冠 / 胜平负 / 比分 /
# 大小球 / 让分 / 玩法 / 投注 / 购彩 / 彩民 / 庄家 / 赔率 / 联赛 / 球队 / 球员 / 状态 / 阵容 /
# 体能 / 压力 / 主场优势 / 客场挑战」之一；覆盖足球 + 篮球主要体彩语境。
# 命中即视为合规；零命中 → fallback 到下方静态体彩资讯（绝不回传 LLM 的无关文本）。
_BETTING_KEYWORDS = (
    "让球", "盘口", "主场", "客场", "赛季", "形态", "欧战", "保级", "争冠",
    "胜平负", "比分", "大小球", "让分", "玩法", "投注", "购彩", "彩民",
    "庄家", "赔率", "联赛", "球队", "球员", "状态", "阵容", "体能",
    "压力", "主场优势", "客场挑战",
)


def _is_betting_relevant(text: str) -> bool:
    """LLM 返回值是否与体彩语境相关。

    体育关键词集合（见模块常量）至少出现 1 次才算合规。
    ``text`` 已 strip 过（``_try_llm`` 出口处），空串视为不相关。
    """
    if not text:
        return False
    return any(kw in text for kw in _BETTING_KEYWORDS)


def _fallback_news(label: str) -> str:
    """LLM 输出与体彩无关时的兜底资讯（≤78 字、3 行）。"""
    return (
        f"{label}今日多场在售\n"
        "让球深盘需核合理\n"
        "主客场形态看赛季压力"
    )


def generate_daily_news(*, date_iso: str, sport: str, context: str, picks_ctx: str,
                        label: str) -> dict[str, str]:
    """生成 (date, sport) 维度缓存的每日资讯。

    返回 ``{"news": str, "pick_reason": str}``；news 限 ≤78，pick_reason 限 ≤60。
    调用方负责补占位文案（参考 web.routers.jc.daily_news）。

    关键词守门：LLM 返回值若不含任一 ``_BETTING_KEYWORDS`` → 视为「与体彩无关」，
    替换为 ``_fallback_news(label)``（绝不回传「天气/八卦/比分预测」之类污染资讯条）。
    缓存键 + 占位文案同步刷新（同一日重启进程内行为可预测）。

    R21 #1：缓存加 TTL（默认 24h）+ LRU 大小上限（默认 64）。命中同一对象
    引用（``a is b``，符合 tests/test_web_news.py 守门）。
    """
    now = time.time()
    _evict_expired_news(now)
    key = (date_iso, sport)
    hit = _NEWS_CACHE.get(key)
    if hit and (hit["value"].get("news") or hit["value"].get("pick_reason")):
        return hit["value"]

    prompt = _build_prompt(context, picks_ctx, label)
    raw = _try_llm(prompt)
    news_text = raw["news"][:78]
    pick_text = raw["pick_reason"][:60]
    # 体彩守门：news 不合规 → 替换为静态兜底；pick_reason 不强制（短句易误伤）
    if not _is_betting_relevant(news_text):
        news_text = _fallback_news(label)
    out = {
        "news": news_text,
        "pick_reason": pick_text,
    }
    # 仅在非空时缓存（与既有行为一致：双空不该占位）
    if out["news"] or out["pick_reason"]:
        _NEWS_CACHE[key] = {"value": out, "ts": now}
    return out


__all__ = ["generate_daily_news"]