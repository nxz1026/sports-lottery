"""队名中文化：用 LLM 翻译并持久化缓存，避免硬编码对照表（P6）。

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
_CACHE: dict | None = None


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
    """英文队名/国名 → 中文。国名走 COUNTRY_CN；队名走 LLM 翻译缓存。"""
    if not name:
        return name
    cn = COUNTRY_CN.get(name, COUNTRY_CN.get(name.replace("'", ""), None))
    if cn:
        return cn
    return load_translations().get(name, name)
