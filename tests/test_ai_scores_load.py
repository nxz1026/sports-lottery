"""R42-2: web/services/ai.py AI scores cluster 5 tests direct + D-45 lock.

_load_ai_scores / ai_status / ai_details 仅通过 test_ai_router.py 路由冒烟
+ test_ai_score_key.py monkeypatch, 0 直测。JSON 损坏/非 dict 容错契约无锁。

helper 含三条契约 (D-45):
- _load_ai_scores: 文件缺 / OSError / ValueError / 非 dict -> {} (永不抛)
- ai_status: 空 scores -> available=True, matches=0, note='no ai_scores yet';
             失败 -> available=False, reason='load_failed: <exc-name>'
- ai_details: match 字段取 value['name'] 不取 key (R22 回归锁);
              排序按 ai_score 倒序; ai_score=None 排序当 0 兜底;
              items[:50] 截断

5 tests (HIGH):
- 缺文件 -> {} (不抛)
- ValueError (坏 JSON) -> {} (warn 但不抛)
- 非 dict JSON (e.g., list) -> {}
- ai_status 空 -> {available=True, matches=0, note='no ai_scores yet'}
- ai_details match 取 name 字段 (R22 锁)
- bonus: ai_details 按 ai_score 降序 + None 当 0
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def ai_mod(tmp_path, monkeypatch):
    """加载 web/services/ai.py + 把 AI_SCORES_FILE 重定向 tmp 隔离真实目录。"""
    spec = importlib.util.spec_from_file_location(
        "ai_scores_cluster_test",
        REPO_ROOT / "web" / "services" / "ai.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    # exec 后 AI_SCORES_FILE 已被绑定为原 BASE_DIR 路径; 现 patch 到 tmp
    monkeypatch.setattr(mod, "AI_SCORES_FILE", tmp_path / "ai_scores.json")
    return mod


# ──────────────────────── D-45 _load_ai_scores 三态兜底 ────────────────────────


def test_ai_load_d45_missing_file_returns_empty_dict(ai_mod):
    """R42-2 D-45: AI_SCORES_FILE 不存在 -> {} (永不抛, 路由层 available=true, matches=0)。"""
    out = ai_mod._load_ai_scores()
    assert out == {}


def test_ai_load_d45_invalid_json_returns_empty_dict(ai_mod, tmp_path):
    """R42-2 D-45: AI_SCORES_FILE 是坏 JSON -> {} (warn 但不抛, 上层降级仍可走)。

    锁住 ValueError 兜底路径; 上游持久化 / 第三方写入污染都不能让路由 500。
    """
    ai_mod.AI_SCORES_FILE.write_bytes(b"{not valid json at all")
    out = ai_mod._load_ai_scores()
    assert out == {}


def test_ai_load_d45_non_dict_root_returns_empty_dict(ai_mod, tmp_path):
    """R42-2 D-45: JSON 是 list (非 dict) 顶层 -> {} (防 dirty 类型污染 dict 路径)。

    锁定 `isinstance(data, dict) else {}` defensive check。
    """
    ai_mod.AI_SCORES_FILE.write_text(
        json.dumps([{"k": "v"}]), encoding="utf-8")
    out = ai_mod._load_ai_scores()
    assert out == {}


# ──────────────────────── D-45 ai_status 降级 ────────────────────────


def test_ai_status_d45_empty_scores_returns_no_ai_note(ai_mod):
    """R42-2 D-45: 空 scores -> available=True, matches=0, note='no ai_scores yet'。

    锁定前端可用性: 空数据不报失败, 显式 note 区分场景。
    """
    out = ai_mod.ai_status()
    assert out == {"available": True, "matches": 0, "note": "no ai_scores yet"}


# ──────────────────────── D-45 ai_details match-name + 排序 ────────────────────────


def test_ai_details_d45_match_takes_name_field_not_key(ai_mod):
    """R42-2 D-45 R22 锁: match 字段取 value['name'] (落盘中文字段), 不是字典 key。

    主键是 '联赛|主队英文|客队英文' (R17 #7 起), 直接当比赛名会把 UI 显示
    成 'nba|Detroit Pistons|Boston Celtics'。value['name'] 是落盘时的中文
    显示名, 必须优先使用。
    """
    payload = {
        "nba|Detroit Pistons|Boston Celtics": {
            "name": "活塞 vs 凯尔特人",  # 中文显示名
            "ai_score": 8.0,
            "league": "nba",
        },
    }
    ai_mod.AI_SCORES_FILE.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = ai_mod.ai_details()
    items = out["items"]
    assert len(items) == 1
    # 关键: match == value['name'], 不是字典键
    assert items[0]["match"] == "活塞 vs 凯尔特人"
    assert "nba|Detroit" not in items[0]["match"]
