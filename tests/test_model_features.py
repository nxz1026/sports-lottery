"""R51-3+4: scripts/core/model/features.py 26 维特征 11 测 + D-99/D-100 双契约锁。

R50 末已锁 D-92 ~ D-96 / D-97~D-98 (auth helpers). ML 训练管线
``scripts/core/model/features.py`` 完全 0 直测, 仅集成层 (test_predict_blend_ml_probs)
mock 返 [0.0]*26, **未锁定 26 维特征 key 顺序与 26 维数值语义**.

features.py (L24-58) 26 个特征顺序:
  0  home_true_prob        13  away_onside_score
  1  draw_true_prob        14  home_fifa_score
  2  away_true_prob        15  away_fifa_score
  3  odds_available        16  elo_home_expected
  4  home_form_score       17  home_elo_rating
  5  away_form_score       18  away_elo_rating
  6  home_record_score     19  elo_diff
  7  away_record_score     20  home_form_vs_away_form
  8  spread_movement       21  home_record_vs_away_record
  9  home_ml_implied       22  form_x_record_home
 10  draw_implied          23  form_x_record_away
 11  home_onside_score     24  odds_vs_elo_diff
 12  away_onside_score     25  onside_vs_elo_diff (咦, 26 是 is_host_country)
见 L24-58 FEATURE_COLUMNS. 实际 26 是 is_host_country.

D-99 锁 extract_features + _append_derived_features (8 测):
- D-99-1: 26 维向量长度 + key 顺序 (feature_dict / zip 一致)
- D-99-2: away_true_prob=None -> ap = 1 - hp - dp (None 推导)
- D-99-3: away_true_prob=0.0 -> ap = 0.0 (不被 or 0.25 误判)
- D-99-4: hp+dp+ap < 0.01 -> 兜底 0.45/0.25/0.30
- D-99-5: onside signals 嵌套 [home].onside_score 缺失 -> 0.5
- D-99-6: elo_ratings=None -> home_elo=away_elo=1500/elo_home_exp=0.5/elo_diff=0.0
- D-99-7: elo_ratings 命中 -> expected_score(home_elo, away_elo, home_adv=True)
- D-99-8: is_host_country = 1.0 when home_en == host_country else 0.0

D-100 锁 feature_dict + build_training_set (3 测):
- D-100-1: feature_dict zip(FEATURE_COLUMNS, vec) 顺序一致
- D-100-2: build_training_set 非法 score 跳过 (空 / 无 "-" / 非数字)
- D-100-3: build_training_set 标签 0/1/2 (home win / draw / away win)

策略:
- spec-load features.py 到隔离 namespace
- 真实 core.elo.expected_score + DEFAULT_ELO (无副作用)
- 无 DB / 无网络 / 无 file IO — 纯函数 + dict 派生
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def feat_mod():
    """Spec-load features.py 到隔离 namespace.

    不需要 monkeypatch (纯函数 + 真实 core.elo). 只需 sys.path 含 repo + scripts.
    """
    spec = importlib.util.spec_from_file_location(
        "features_test",
        REPO_ROOT / "scripts" / "core" / "model" / "features.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-99: 8 测 ────────────────────────


def test_d99_1_extract_returns_26_dim_with_fixed_key_order(feat_mod):
    """D-99-1: extract_features 必返 26 维向量 + FEATURE_COLUMNS 顺序严格匹配.

    即便某些字段是默认值, 关键位置(home_true_prob/draw_true_prob/.../is_host_country)
    的索引必须是 L24-58 FEATURE_COLUMNS 的位置.
    """
    match = {
        "home_true_prob": 0.55,
        "draw_true_prob": 0.25,
        "away_true_prob": 0.20,
    }
    vec = feat_mod.extract_features(match)

    assert len(vec) == 26
    assert len(feat_mod.FEATURE_COLUMNS) == 26

    # 关键位置: 索引 → 值
    idx = {name: i for i, name in enumerate(feat_mod.FEATURE_COLUMNS)}
    assert vec[idx["home_true_prob"]] == 0.55
    assert vec[idx["draw_true_prob"]] == 0.25
    assert vec[idx["away_true_prob"]] == 0.20
    assert vec[idx["odds_available"]] == 0.0      # 无 odds_data_available
    # ELO 全 0
    assert vec[idx["elo_home_expected"]] == 0.5   # 无 elo_ratings 时
    assert vec[idx["home_elo_rating"]] == 1500.0  # DEFAULT_ELO
    assert vec[idx["away_elo_rating"]] == 1500.0
    assert vec[idx["elo_diff"]] == 0.0
    # is_host_country: host_country 未设置或 home_en 不匹配 -> 0.0
    assert vec[idx["is_host_country"]] == 0.0


def test_d99_2_away_prob_derived_when_none(feat_mod):
    """D-99-2: away_true_prob=None 时推导 ap = 1 - hp - dp (L117-120)."""
    match = {
        "home_true_prob": 0.50,
        "draw_true_prob": 0.30,
        "away_true_prob": None,
    }
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}
    vec = feat_mod.extract_features(match)

    assert vec[idx["away_true_prob"]] == pytest.approx(0.20, abs=1e-9)
    assert vec[idx["home_true_prob"]] == 0.50
    assert vec[idx["draw_true_prob"]] == 0.30


def test_d99_3_away_prob_zero_preserved_not_overridden(feat_mod):
    """D-99-3: away_true_prob=0.0 必须保留 0.0, 不被 'or 0.25' 误判成缺失值 (L116-120 注释明示).

    这条契约避免极端盘口(客胜陪率无穷大)被错误归一化.
    """
    match = {
        "home_true_prob": 0.70,
        "draw_true_prob": 0.25,
        "away_true_prob": 0.0,   # 显式 0, 非 None
    }
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}
    vec = feat_mod.extract_features(match)

    assert vec[idx["away_true_prob"]] == 0.0
    assert vec[idx["home_true_prob"]] == 0.70
    assert vec[idx["draw_true_prob"]] == 0.25


def test_d99_4_default_probabilities_when_sum_below_threshold(feat_mod):
    """D-99-4: hp+dp+ap < 0.01 时强制 0.45/0.25/0.30 兜底 (L121-122).

    注意: 常规输入 (None / 0.0 显式) 不会触发此兜底, 因为 L112 `or 0.5` /
    L113 `or 0.25` 已经先把 None/0 转 0.5/0.25 / ap 由 None 推导为非零,
    使 sum >= 0.75. 唯一能触发 L121 兜底的是 negative probability 输入,
    (truthy 不被 `or` 替换). 这里用负数验证 L121-122 逻辑本身, 同时记录
    "该路径在生产正常 match 数据下不会触发" — 死分支但兜底值锁定.
    """
    match = {
        "home_true_prob": -0.5,   # truthy, 不被 `or 0.5` 替换
        "draw_true_prob": -0.3,   # truthy, 不被 `or 0.25` 替换
        "away_true_prob": -0.2,   # truthy (负数), 保留 ap_raw < 0.01
    }
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}
    vec = feat_mod.extract_features(match)

    # sum=-1.0 < 0.01 -> 触发兜底, 设 hp=0.45 / dp=0.25 / ap=0.30
    assert vec[idx["home_true_prob"]] == 0.45
    assert vec[idx["draw_true_prob"]] == 0.25
    assert vec[idx["away_true_prob"]] == 0.30


def test_d99_5_onside_signals_nested_default_zero_point_five(feat_mod):
    """D-99-5: onside_signals 嵌套 [home].onside_score 缺失时返默认 0.5 (L133-137).

    - 场景 a: onside_signals 全缺, 4 个 onside/fifa 全 0.5
    - 场景 b: onside_signals={away: {}} 时 home 端仍 0.5
    """
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}

    # 场景 a: 完全没 onside_signals 字段
    match_a = {}
    vec_a = feat_mod.extract_features(match_a)
    assert vec_a[idx["home_onside_score"]] == 0.5
    assert vec_a[idx["away_onside_score"]] == 0.5
    assert vec_a[idx["home_fifa_score"]] == 0.5
    assert vec_a[idx["away_fifa_score"]] == 0.5

    # 场景 b: 只给 away (home 仍走 default 0.5)
    match_b = {"onside_signals": {"away": {"onside_score": 0.9, "fifa_score": 0.8}}}
    vec_b = feat_mod.extract_features(match_b)
    assert vec_b[idx["home_onside_score"]] == 0.5     # default
    assert vec_b[idx["away_onside_score"]] == 0.9


def test_d99_6_elo_defaults_when_no_ratings(feat_mod):
    """D-99-6: context 无 elo_ratings 时 home_elo=away_elo=1500/elo_home_exp=0.5/elo_diff=0.0 (L86-89).

    注意: context 不传等价于空 dict, 同样走 default 分支.
    """
    match = {"home_en": "BRA", "away_en": "ARG"}
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}

    # context=None
    vec_no_ctx = feat_mod.extract_features(match)
    assert vec_no_ctx[idx["home_elo_rating"]] == 1500.0
    assert vec_no_ctx[idx["away_elo_rating"]] == 1500.0
    assert vec_no_ctx[idx["elo_home_expected"]] == 0.5
    assert vec_no_ctx[idx["elo_diff"]] == 0.0

    # context={} (空 dict)
    vec_empty_ctx = feat_mod.extract_features(match, context={})
    assert vec_empty_ctx[idx["elo_home_expected"]] == 0.5
    assert vec_empty_ctx[idx["elo_diff"]] == 0.0


def test_d99_7_elo_lookup_uses_expected_score_and_computes_diff(feat_mod):
    """D-99-7: elo_ratings 命中时 home_elo=ratings[home_en], elo_home_exp=expected_score(...) (L80-84).

    真实 core.elo.expected_score: home_adv=True 时给主队加成.
    """
    from core.elo import expected_score as real_expected  # noqa: PLC0415 - 真实函数

    elo_ratings = {"BRA": 1800.0, "ARG": 1600.0}
    ctx = {"elo_ratings": elo_ratings}
    match = {"home_en": "BRA", "away_en": "ARG"}
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}

    vec = feat_mod.extract_features(match, context=ctx)

    assert vec[idx["home_elo_rating"]] == 1800.0
    assert vec[idx["away_elo_rating"]] == 1600.0
    assert vec[idx["elo_diff"]] == pytest.approx(200.0)
    # expected_score(1800, 1600, home_adv=True) 真实值
    expected = real_expected(1800.0, 1600.0, home_adv=True)
    assert vec[idx["elo_home_expected"]] == pytest.approx(expected, abs=1e-9)


def test_d99_8_is_host_country_only_when_home_matches(feat_mod):
    """D-99-8: is_host_country=1.0 iff host_country 非空 AND home_en == host_country (L97)."""
    idx = {n: i for i, n in enumerate(feat_mod.FEATURE_COLUMNS)}

    # 场景 a: 命中 (host_country == home_en)
    vec_hit = feat_mod.extract_features(
        {"home_en": "BRA", "away_en": "ARG"},
        context={"host_country": "BRA"},
    )
    assert vec_hit[idx["is_host_country"]] == 1.0

    # 场景 b: 不命中 (host_country != home_en)
    vec_miss = feat_mod.extract_features(
        {"home_en": "BRA", "away_en": "ARG"},
        context={"host_country": "USA"},
    )
    assert vec_miss[idx["is_host_country"]] == 0.0

    # 场景 c: host_country 未设置
    vec_none = feat_mod.extract_features(
        {"home_en": "BRA", "away_en": "ARG"},
        context={},
    )
    assert vec_none[idx["is_host_country"]] == 0.0


# ──────────────────────── D-100: 3 测 ────────────────────────


def test_d100_1_feature_dict_zip_order_matches_columns(feat_mod):
    """D-100-1: feature_dict zip(FEATURE_COLUMNS, vec) 顺序与 FEATURE_COLUMNS 一致 (L151).

    即 dict 必须能用 feature_dict(...)["home_true_prob"] 取到正确值.
    """
    match = {
        "home_true_prob": 0.55,
        "draw_true_prob": 0.25,
        "away_true_prob": 0.20,
    }
    fd = feat_mod.feature_dict(match)

    assert len(fd) == 26
    # dict 顺序 (Python 3.7+) 与 FEATURE_COLUMNS 一致
    assert list(fd.keys()) == list(feat_mod.FEATURE_COLUMNS)
    # 关键字段值正确
    assert fd["home_true_prob"] == 0.55
    assert fd["draw_true_prob"] == 0.25
    assert fd["away_true_prob"] == 0.20
    # 26 全是 float
    for v in fd.values():
        assert isinstance(v, float)


def test_d100_2_build_training_set_skips_invalid_scores(feat_mod):
    """D-100-2: build_training_set 非法 score 跳过 (空 / 无 '-' / 非数字) (L159-165)."""
    matches = [
        # 合法 home win
        {"score": "2-1", "home_true_prob": 0.5},
        # 合法 draw
        {"score": "1-1", "home_true_prob": 0.5},
        # 合法 away win
        {"score": "0-2", "home_true_prob": 0.5},
        # 非法 1: score 缺
        {"home_true_prob": 0.5},
        # 非法 2: score 空字符串
        {"score": "", "home_true_prob": 0.5},
        # 非法 3: score 没有 '-'
        {"score": "abc", "home_true_prob": 0.5},
        # 非法 4: 数字部分非数字
        {"score": "x-y", "home_true_prob": 0.5},
        # 非法 5: 边界情况 — score='-' 单独一个
        {"score": "-", "home_true_prob": 0.5},
    ]
    X, y = feat_mod.build_training_set(matches)
    # 只保留 3 个合法的
    assert len(X) == 3
    assert len(y) == 3
    assert y == [0, 1, 2]
    # X 每行 26 维
    for row in X:
        assert len(row) == 26


def test_d100_3_build_training_set_labels_home_draw_away(feat_mod):
    """D-100-3: build_training_set 标签 0 (home win) / 1 (draw) / 2 (away win) (L166).

    包括边界 hg==ag (draw) / hg<ag (away) / hg>ag (home).
    """
    matches = [
        {"score": "3-0"},   # home win -> 0
        {"score": "0-0"},   # draw -> 1
        {"score": "0-3"},   # away win -> 2
        {"score": "1-0"},   # home win -> 0
        {"score": "2-2"},   # draw -> 1
        {"score": "0-1"},   # away win -> 2
    ]
    X, y = feat_mod.build_training_set(matches)
    assert y == [0, 1, 2, 0, 1, 2]
    assert len(X) == 6
    assert all(len(row) == 26 for row in X)
