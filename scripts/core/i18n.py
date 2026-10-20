"""队名中文化：用 LLM 翻译并持久化缓存，避免硬编码对照表（P6）。

- 竞彩官方队名：``references/team_cn_official.json``，**最高优先级**。
  竞彩↔模型是按中文名 join 的（``web/routers/ticket.py`` / ``web/services/team_match.py``），
  所以模型产出的中文名必须与竞彩官方中文名**逐字相等**。通用叫法（曼城/曼联/热刺/
  巴黎圣日耳曼/塞尔塔）与竞彩叫法（曼彻斯特城/曼彻斯特联/托特纳姆热刺/巴黎圣日尔曼/
  维戈塞尔塔）不同 ⇒ 走通用叫法必然 join miss。本表是人工核对的产物，不进 LLM 缓存
  （``save_translations`` 会整文件重写那个缓存，混进去会被冲掉）。
- 国名：走稳定的 COUNTRY_CN（国名集合固定、不会变化）。
- 队名/俱乐部名：首次出现时通过 LLM 批量翻译，结果写入
  references/team_translations.json 并跨运行复用（无需每次翻译，也保证一致性）。
- LLM 不可用时优雅降级：返回原英文名。
"""

from __future__ import annotations

import json

from core.config import COUNTRY_CN, FOOTBALL_DIR
from core.log import logger

TRANSLATION_CACHE_FILE = FOOTBALL_DIR / "references" / "team_translations.json"
OFFICIAL_NAMES_FILE = FOOTBALL_DIR / "references" / "team_cn_official.json"
_CACHE: dict | None = None
_OFFICIAL: dict | None = None


def load_translations() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        if TRANSLATION_CACHE_FILE.exists():
            with open(TRANSLATION_CACHE_FILE, encoding="utf-8") as f:
                _CACHE = json.load(f) or {}
        else:
            _CACHE = {}
    except Exception:
        _CACHE = {}
    return _CACHE


def load_official_names() -> dict:
    """竞彩官方中文队名表（``references/team_cn_official.json``）。

    以 ``_`` 开头的键是元信息（说明/来源/生成日），不进查询。
    """
    global _OFFICIAL
    if _OFFICIAL is not None:
        return _OFFICIAL
    try:
        if OFFICIAL_NAMES_FILE.exists():
            with open(OFFICIAL_NAMES_FILE, encoding="utf-8") as f:
                raw = json.load(f) or {}
            _OFFICIAL = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str)}
        else:
            _OFFICIAL = {}
    except Exception as e:  # noqa: BLE001 - 表坏了也不能拖垮主流程
        logger.warning(f"Failed to load official team names: {e}")
        _OFFICIAL = {}
    return _OFFICIAL


def save_translations(d: dict) -> None:
    global _CACHE
    _CACHE = d
    try:
        TRANSLATION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save team translations: {e}")


def translate_team_names(names: list[str]) -> dict:
    """通过 LLM 批量翻译英文队名/国名 → 中文。返回 {英文: 中文} 子集。失败返回 {}。"""
    if not names:
        return {}
    from ai.llm_client import generate
    try:
        prompt = (
            "You are a football name translator. Translate the following English "
            "football club or national-team names into their most common CONCISE Chinese names. "
            "Use the short form fans actually use, e.g. 'Fulham' -> '富勒姆', 'Chelsea FC' -> '切尔西', "
            "'Bologna FC 1909' -> '博洛尼亚', 'AS Roma' -> '罗马'. Omit '足球俱乐部' / 'FC' style suffixes "
            "unless they are part of the standard short name. "
            "Return ONLY a JSON object, either of the form "
            '{"translations": {"EnglishName": "中文名"}} or a flat object '
            '{"EnglishName": "中文名"}. If you are unsure of a name, use the original '
            "English text as its value. No explanations.\n"
            f"Names: {json.dumps(names, ensure_ascii=False)}"
        )
        resp = generate(prompt, model=None, rate_limit=0)
        if not isinstance(resp, dict):
            return {}
        mapping: dict = {}
        if isinstance(resp.get("translations"), dict):
            mapping = resp["translations"]
        elif all(isinstance(v, str) for v in resp.values()):
            mapping = resp
        out = {}
        for k, v in mapping.items():
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out
    except Exception as e:
        logger.warning(f"LLM team-name translation failed: {e}")
        return {}


def warm_translations(names: list[str]) -> None:
    """预热翻译缓存：把未翻译过的队名批量翻译并持久化。"""
    cache = load_translations()
    seen: set[str] = set()
    missing: list[str] = []
    for n in names:
        if not n or n in cache or n in seen:
            continue
        seen.add(n)
        missing.append(n)
    if not missing:
        return
    translated = translate_team_names(missing)
    if translated:
        cache.update(translated)
        save_translations(cache)
        logger.info(f"Translated {len(translated)} new team names via LLM")


def to_cn(name: str | None) -> str:
    """英文队名/国名 → 中文。

    优先级：① 竞彩官方队名表 ② COUNTRY_CN（国名 + 兼容旧俱乐部叫法）
    ③ LLM 翻译缓存 ④ 原样返回英文。

    ① 必须排在 ② 前面：COUNTRY_CN 里写的是通用叫法（``Manchester City`` → ``曼城``），
    而竞彩官方叫法是 ``曼彻斯特城``；按通用叫法产出的预测与竞彩场次**逐字对不上**，
    join 必然 miss（实测 sfc 端 ``n_with_probs=0`` 的成因之一）。
    """
    if not name:
        return name
    cn = load_official_names().get(name)
    if cn:
        return cn
    cn = COUNTRY_CN.get(name, COUNTRY_CN.get(name.replace("'", ""), None))
    if cn:
        return cn
    return load_translations().get(name, name)
