"""
Batch AI analysis pipeline: split items, enrich via Gemini, return scored items.
Adapted from ECC community data-scraper-agent skill.
"""
import json
from ai.llm_client import generate

# 业务评分偏好默认集（P4：改为可配置，避免硬编码在调用方源码中）。
# 可通过环境变量 LP_AI_PRIORITIES（逗号分隔）覆盖，或在调用时通过
# config["priorities"] 传入。
DEFAULT_AI_PRIORITIES: list[str] = [
    "High confidence predictions preferred",
    "Underdog picks preferred",
    "Clear direction signals preferred",
]

# 评分规则（rubric）默认文案，可通过 config["scoring_rubric"] 覆盖。
DEFAULT_SCORING_RUBRIC: str = (
    "Be concise. Score 90+=excellent match, 70-89=good, 50-69=ok, <50=weak."
)


def analyse_batch(
    items: list[dict],
    context: str = "",
    preference_prompt: str = "",
    config: dict = None,
) -> list[dict]:
    """Analyse items in batches. Returns items enriched with AI fields.

    Each item gets: ai_score (0-100), ai_summary, ai_notes.
    Items below min_score are filtered out.
    """
    config = config or {}
    ai_cfg = config.get("ai", {})
    model = ai_cfg.get("model", "gemini-2.5-flash")
    rate_limit = ai_cfg.get("rate_limit_seconds", 7.0)
    min_score = ai_cfg.get("min_score", 0)
    batch_size = ai_cfg.get("batch_size", 5)
    max_retries = int(ai_cfg.get("max_retries", 1))

    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
    print(f"  [AI] {len(items)} items → {len(batches)} API calls")

    enriched = []
    for i, batch in enumerate(batches):
        print(f"  [AI] Batch {i + 1}/{len(batches)}...")
        enriched.extend(_score_batch(
            batch, context, preference_prompt, config, model, rate_limit,
            min_score, max_retries, i + 1, len(batches)))
    return enriched


def _score_batch(
    batch: list[dict], context: str, preference_prompt: str, config: dict,
    model: str, rate_limit: float, min_score: int, max_retries: int,
    batch_no: int, batch_total: int,
) -> list[dict]:
    """给一批打分。LLM 漏答的条目按 id 重试，仍缺则**原样返回**（绝不伪造分数）。

    每条挂一个 batch 内不透明整数 id（``_batch_id``）：LLM 只需回填整数，不必照抄
    队名。实测 agnes-3.0-flash 会把中文译名"纠正"成别的队 —— '西班牙人 vs 埃尔切'
    回成 '西班牙人 vs 阿根廷'、'勒芒 vs 里昂' 回成 '洛森 vs 里昂'、'法兰克福 vs
    弗赖堡' 回成 '法兰克福 vs 德累斯顿'，21 条里只有 14 条精确照抄。按名字配对时
    这些条目会静默丢掉分数（实测一次富化 68 条只写回 48 条，日志无任何提示）。
    """
    tagged = [{**item, "_batch_id": n} for n, item in enumerate(batch, start=1)]
    analyses_by_id: dict[int, dict] = {}
    pending = list(tagged)
    attempt = 0
    while pending and attempt <= max_retries:
        if attempt:
            print(f"  [AI] Batch {batch_no}/{batch_total} 重试 {len(pending)} 条未评分条目...")
        prompt = _build_prompt(pending, context, preference_prompt, config)
        result = generate(prompt, model=model, rate_limit=rate_limit)
        analyses = result.get("analyses", []) if isinstance(result, dict) else []
        # 重试批次是**子集**，位置回退会张冠李戴（把原第 2 条配上第 1 条的分析），
        # 故只在首次全量批次上允许位置回退。
        analyses_by_id.update(_pair_batch(analyses, pending, allow_position=(attempt == 0)))
        pending = [it for it in pending if it["_batch_id"] not in analyses_by_id]
        attempt += 1
    if pending:
        # 不再静默：漏答必须可见，否则"AI 富化成功"会掩盖大面积丢分。
        print(f"  [AI] Batch {batch_no}/{batch_total} 仍有 {len(pending)} 条未评分"
              f"（LLM 未返回），原样保留不伪造分数")

    out: list[dict] = []
    for item in tagged:
        ai = analyses_by_id.get(item["_batch_id"])
        clean = {k: v for k, v in item.items() if k != "_batch_id"}
        if not isinstance(ai, dict):
            out.append(clean)
            continue
        score = max(0, min(100, int(ai.get("score", 0))))
        if min_score and score < min_score:
            continue
        out.append({
            **clean,
            "ai_score": score,
            "ai_summary": ai.get("summary", ""),
            "ai_notes": ai.get("notes", ""),
        })
    return out


def _as_int(value) -> int | None:
    """LLM 可能把整数回成字符串；只接受干净的十进制整数（排除 bool）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _pair_batch(analyses: list[dict], batch: list[dict], allow_position: bool = True) -> dict:
    """把 analyses 配到 batch 条目上，返回 {batch_id: analysis}。

    优先按不透明整数 ``id`` 配对（确定性、与语言无关）；其次回退按
    ``match == name``（兼容旧 prompt 的返回）；最后兼容"全部无 match 键"时按位置
    配对（旧行为，见 test_analyse_batch_falls_back_to_position_without_match）。

    ``allow_position=False`` 用于重试：重试批次是原批次的子集，按位置配对会错配。
    """
    valid_analyses = [a for a in analyses if isinstance(a, dict)]
    if not valid_analyses:
        return {}

    ids = {it.get("_batch_id") for it in batch}
    by_id: dict[int, dict] = {}
    for a in valid_analyses:
        aid = _as_int(a.get("id"))
        if aid is not None and aid in ids:
            by_id[aid] = a
    if by_id:
        return by_id

    by_name = {a["match"]: a for a in valid_analyses if a.get("match") is not None}
    by_name_hit = {
        it.get("_batch_id"): by_name[it.get("name")]
        for it in batch
        if it.get("name") in by_name
    }
    if by_name_hit:
        return by_name_hit

    if allow_position and all("match" not in a for a in valid_analyses):
        return {
            it.get("_batch_id"): a
            for it, a in zip(batch, valid_analyses)
            if isinstance(a, dict)
        }
    return {}



def _build_prompt(batch, context, preference_prompt, config):
    """Build the Gemini prompt for a batch of items.

    每条目带一个不透明整数 ``id``，并要求 LLM **回填 id** 而非照抄队名 ——
    实测模型会把中文译名"纠正"成别的队，照抄式配对会静默丢条目（详见 _score_batch）。
    """
    priorities = config.get("priorities") or DEFAULT_AI_PRIORITIES

    def _dump(item, fallback_id):
        d = {k: v for k, v in item.items() if not k.startswith("_")}
        d["id"] = item.get("_batch_id", fallback_id)
        return d

    items_text = "\n\n".join(
        f"Item {_dump(item, i + 1)['id']}: {json.dumps(_dump(item, i + 1), ensure_ascii=False)}"
        for i, item in enumerate(batch)
    )

    scoring_rubric = config.get("scoring_rubric") or DEFAULT_SCORING_RUBRIC
    return f"""Analyse these {len(batch)} items and return a JSON object.
# Items
{items_text}
# User Context
{context[:800] if context else "Not provided"}
# User Priorities
{chr(10).join(f"- {p}" for p in priorities)}
{preference_prompt}
# Instructions
Return: {{"analyses": [{{"id": <该条目的 id 整数>, "score": <0-100>, "summary": "<2 sentences>", "notes": ""}} for each item]}}
id 必须是整数、原样回填、不得改写或遗漏；analyses 必须覆盖全部条目。
不要回填队名，只用 id 标识条目（队名可能被误写，id 不会）。
所有 summary 与 notes 必须使用简体中文撰写（JSON 键名保持英文）。
{scoring_rubric}"""