"""契约 §1 镜像：web/services/store._stable_match_key 与
scripts/core/backtest._bk_stable_key 必须保持键语义一致。

两者均用 home_en|away_en 优先 + name/match 回退（中文显示名）的同一约定；
web 侧只读引擎产物 JSON 而不 import scripts/，故连接键规则需要"双侧手动同步"。
本测试以同一组 {home_en, away_en, name, match} 喂两侧，断言返回值完全相等。

回归意义：若任一侧改动规则（例如先 name 再 home_en、或者增加备用键），
本测试会立即 fail；连接键语义漂移 = "待结算" 计数 / 对账信号 结构性失真。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

from core.backtest import _bk_stable_key as engine_key  # noqa: E402

from web.services.store import _stable_match_key as web_key  # noqa: E402

# 每条 case 喂两侧同一份 rec；两侧返回值应一致，否则 = 镜像漂移
_CASES: list[tuple[str, dict, str]] = [
    (
        "home_en+away_en 完整：两侧都返英文键",
        {"home_en": "Brentford FC", "away_en": "Chelsea FC",
         "name": "布伦特福德 vs 切尔西", "match": "布伦特福德 vs 切尔西"},
        "Brentford FC|Chelsea FC",
    ),
    (
        "home_en 缺：回退 name/match",
        {"home_en": "", "away_en": "Chelsea FC",
         "name": "布伦特福德 vs 切尔西", "match": "布伦特福德 vs 切尔西"},
        "布伦特福德 vs 切尔西",
    ),
    (
        "home_en/away_en 都缺：纯 name",
        {"name": "布伦特福德 vs 切尔西"},
        "布伦特福德 vs 切尔西",
    ),
    (
        "home_en/away_en/name 都缺：纯 match",
        {"match": "布伦特福德 vs 切尔西"},
        "布伦特福德 vs 切尔西",
    ),
    (
        "全部缺：空串",
        {},
        "",
    ),
    (
        "name 与 match 都缺时：取空字符串 strip 结果",
        {"home_en": "  ", "away_en": "  "},
        "",
    ),
    (
        "i18n 漂移场景：home_en 英文 / name 中文：英文优先",
        {"home_en": "Atletico Madrid", "away_en": "Sevilla FC",
         "name": "马德里竞技 vs 塞维利亚"},
        "Atletico Madrid|Sevilla FC",
    ),
]


def _check(label: str, rec: dict, expected: str) -> None:
    e = engine_key(rec)
    w = web_key(rec)
    assert e == w, f"{label}: 镜像漂移 engine={e!r} web={w!r}"
    assert e == expected, f"{label}: 与预期不一致 engine={e!r} expected={expected!r}"


def test_engine_and_web_stable_keys_match_on_each_case() -> None:
    """引擎侧 _bk_stable_key 与 web 侧 _stable_match_key 在所有用例上输出完全相等。"""
    for label, rec, expected in _CASES:
        _check(label, rec, expected)


def test_engine_and_web_agree_when_en_names_have_surrounding_whitespace() -> None:
    """home_en/away_en 含前后空白：两侧都用 .strip() 后再拼键。"""
    rec = {"home_en": "  Brentford FC  ", "away_en": "\tChelsea FC\n"}
    assert engine_key(rec) == web_key(rec) == "Brentford FC|Chelsea FC"


def test_engine_and_web_agree_when_only_one_en_field_is_blank() -> None:
    """只有一侧英文为空时两侧都应回退 name/match，不要返回半边残缺键。"""
    rec_blank_home = {"home_en": "", "away_en": "Chelsea FC",
                      "name": "X vs 切尔西"}
    rec_blank_away = {"home_en": "Brentford FC", "away_en": "",
                      "name": "布伦特福德 vs Y"}
    for rec in (rec_blank_home, rec_blank_away):
        assert engine_key(rec) == web_key(rec)
