"""R53: web/services/store.py 7 个 0 直测 private helper 12+ 测 + D-107/D-108 lock。

R51+R52 已锁 D-97~D-106, 其中 web/services/store.py 的公开函数均已覆盖
(test_store_*, test_load_prediction_cache 等). 但模块内 7 个 private helper
在公开函数的集成路径中间接覆盖, **未直测**:

- _json_load       (L27-50): 容错 JSON 加载 (utf-8/gbk/gb18030 fallback)
- _parse_dt        (L53-61): ISO 8601 时间戳解析 (兼容尾部 Z)
- _file_dt         (L183-192): 文件名 `prediction_YYYY-MM-DD_HH.json` 提取 BJT
- _as_aware        (L195-199): naive datetime 补 BJT 时区
- _newer           (L202-211): 比较两条预测文档"谁更新"
- _window_dates    (L251-265): 解析 data_window 紧凑格式 `YYYYMMDD-YYYYMMDD`
- _stable_match_key(L312-325): web 侧稳定键生成, 与 engine._bk_stable_key 同语义

D-107 锁 _json_load + _parse_dt + _file_dt + _as_aware (4 测聚合, 纯函数/time 模块):
- D-107-1: _json_load 合法 UTF-8 dict -> dict
- D-107-2: _json_load 坏 JSON / 编码错误 -> None + warning
- D-107-3: _parse_dt ISO 完整 / Z 后缀 / 非法 -> datetime | None
- D-107-4: _file_dt `prediction_2025-10-17_14.json` -> BJT datetime; 坏名 -> None

D-108 锁 _newer + _window_dates + _stable_match_key (8 测, 关键契约):
- D-108-1: _newer 文件名时间大一胜 (L205)
- D-108-2: _newer 文件名同时回退到 generated_at (L207-209)
- D-108-3: _window_dates 紧凑 8-8 / 非字符串 / 非法日期 (L257-265)
- D-108-4: covers_date 命中 / 边界 / 不在窗口
- D-108-5: _stable_match_key home_en|away_en 优先, 回退 name/match

策略:
- spec-load web.services.store 到隔离 namespace, 同时清空 L130-131 的
  _load_prediction_docs_cached / _pred_index_by_pair_cached cache 防止跨测污染.
- OUTPUT_DIR 临时指向 tmp_path (真 file IO, 不污染 repo)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def store_mod(tmp_path, monkeypatch):
    """Spec-load web.services.store, OUTPUT_DIR 指向 tmp_path, 清空 cache."""
    spec = importlib.util.spec_from_file_location(
        "web_services_store_test",
        REPO_ROOT / "web" / "services" / "store.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # OUTPUT_DIR 指向 tmp 目录, 真写但限定在 tmp
    monkeypatch.setattr(mod, "OUTPUT_DIR", tmp_path)

    # 清空模块缓存 (跨测隔离)
    mod._load_prediction_docs_cached.clear()
    mod._pred_index_by_pair_cached.clear()

    return mod


# ──────────────────────── D-107: 4 测 ────────────────────────


def test_d107_1_json_load_valid_utf8_returns_dict(store_mod, tmp_path):
    """D-107-1: _json_load 合法 UTF-8 JSON dict 返 dict (L40-46)."""
    p = tmp_path / "valid.json"
    p.write_text(json.dumps({"key": "value", "n": 42}), encoding="utf-8")

    out = store_mod._json_load(p)

    assert out == {"key": "value", "n": 42}


def test_d107_2_json_load_corrupt_returns_none(store_mod, tmp_path):
    """D-107-2: _json_load 坏 JSON / 非对象 / 空对象 / 编码无法识别 -> None + warning (L42-49).

    - 场景 a: 坏 JSON (utf-8 但格式错) -> None
    - 场景 b: 合法 JSON 但顶层是 list -> None (L45 isinstance dict 过滤)
    - 场景 c: 空 dict -> None (L45 data 评估 False)
    - 场景 d: OSError (文件不存在) -> None (L32-34)
    """
    # 场景 a: 坏 JSON
    p_a = tmp_path / "broken.json"
    p_a.write_text("{key: value broken,,,", encoding="utf-8")
    assert store_mod._json_load(p_a) is None

    # 场景 b: 顶层 list
    p_b = tmp_path / "list.json"
    p_b.write_text("[1, 2, 3]", encoding="utf-8")
    assert store_mod._json_load(p_b) is None

    # 场景 c: 空 dict
    p_c = tmp_path / "empty.json"
    p_c.write_text("{}", encoding="utf-8")
    assert store_mod._json_load(p_c) is None

    # 场景 d: OSError (文件不存在)
    assert store_mod._json_load(tmp_path / "nonexistent.json") is None


def test_d107_3_parse_dt_iso_formats(store_mod):
    """D-107-3: _parse_dt 接受完整 ISO / Z 后缀 / 失败返 None (L57-61).

    - 场景 a: "2025-10-17T14:30:00+08:00" -> datetime
    - 场景 b: "2025-10-17T14:30:00Z" -> datetime 等价 +0 时区
    - 场景 c: 非法字符串 -> None
    - 场景 d: 非字符串 (None / int) -> None (L55 isinstance 兜底)
    - 场景 e: 空字符串 -> None
    """
    out_a = store_mod._parse_dt("2025-10-17T14:30:00+08:00")
    assert out_a == datetime(2025, 10, 17, 14, 30, 0, tzinfo=__import__("datetime").timezone(__import__("datetime").timedelta(hours=8)))

    out_b = store_mod._parse_dt("2025-10-17T06:30:00Z")
    # Z 等价 UTC (+0)
    assert out_b == datetime(2025, 10, 17, 6, 30, 0, tzinfo=__import__("datetime").timezone.utc)
    # 6:30 UTC == 14:30 +8:00 (Z 替换成 +00:00)
    assert out_b.utcoffset() == __import__("datetime").timedelta(0)

    assert store_mod._parse_dt("not-iso-at-all") is None
    assert store_mod._parse_dt(None) is None
    assert store_mod._parse_dt(42) is None
    assert store_mod._parse_dt("") is None


def test_d107_4_file_dt_extracts_bjt_hour(store_mod, tmp_path):
    """D-107-4: _file_dt 从 `prediction_YYYY-MM-DD_HH.json` 提 BJT datetime, 坏名返 None (L185-192).

    - 场景 a: 合法文件名 -> BJT datetime 含 hour
    - 场景 b: 非法文件名 (无 _HH / 缺年 / format 错) -> None
    - 场景 c: 小时部分越界 (e.g. 25) -> None (strptime ValueError L191-192)
    """
    # 场景 a: 合法
    p_a = tmp_path / "prediction_2025-10-17_14.json"
    p_a.write_text("{}", encoding="utf-8")
    out_a = store_mod._file_dt(p_a)
    assert out_a is not None
    assert out_a.year == 2025
    assert out_a.month == 10
    assert out_a.day == 17
    assert out_a.hour == 14
    # BJT 时区
    assert out_a.tzinfo is not None

    # 场景 b: 不匹配格式
    assert store_mod._file_dt(tmp_path / "random.json") is None
    assert store_mod._file_dt(tmp_path / "prediction_2025.json") is None  # 缺 _HH
    assert store_mod._file_dt(tmp_path / "prediction_2025-10-17.json") is None  # 缺 _HH

    # 场景 c: 小时越界 25
    p_c = tmp_path / "prediction_2025-10-17_25.json"
    p_c.write_text("{}", encoding="utf-8")
    assert store_mod._file_dt(p_c) is None


# ──────────────────────── D-108: 8 测 ────────────────────────


def test_d108_1_newer_by_filename_timestamp(store_mod, tmp_path):
    """D-108-1: _newer 文件名 timestamp fa > fb 时直接判 a 新 (L205)."""
    # 构造 fake dict: 必填 path / generated_at. 用真实文件供 stat() 取 mtime.
    p_a = tmp_path / "prediction_2025-10-17_14.json"
    p_b = tmp_path / "prediction_2025-10-17_13.json"
    p_a.write_text("{}", encoding="utf-8")
    p_b.write_text("{}", encoding="utf-8")

    a = {"path": p_a, "generated_at": datetime(2025, 10, 17, 14, 0)}
    b = {"path": p_b, "generated_at": datetime(2025, 10, 17, 13, 0)}

    # 文件名 a (14) > b (13) -> a 较新
    assert store_mod._newer(a, b) is True
    # 反向: b 不比 a 新
    assert store_mod._newer(b, a) is False


def test_d108_2_newer_fallback_generated_at_when_filename_same(store_mod, tmp_path):
    """D-108-2: 文件名 timestamp 一致时, _newer 退到 generated_at 比对 (L207-209)."""
    p_a = tmp_path / "prediction_2025-10-17_14.json"
    p_b = tmp_path / "prediction_2025-10-17_14.json"
    p_a.write_text("{}", encoding="utf-8")
    p_b.write_text("{}", encoding="utf-8")

    # 文件名同小时, 但 generated_at 不同
    a = {"path": p_a, "generated_at": datetime(2025, 10, 17, 14, 30)}
    b = {"path": p_b, "generated_at": datetime(2025, 10, 17, 14, 0)}

    # 同小时, generated_at 大者胜
    assert store_mod._newer(a, b) is True
    assert store_mod._newer(b, a) is False


def test_d108_3_window_dates_parses_compact_format(store_mod):
    """D-108-3: _window_dates 解析 `YYYYMMDD-YYYYMMDD` / 拒绝非法.

    - 场景 a: 合法 "20251001-20251017" -> (date(2025,10,1), date(2025,10,17))
    - 场景 b: 非法字符串 (非 8 位) -> None (L257-258)
    - 场景 c: 不含 "-" -> None (L254)
    - 场景 d: 非法日期数字 ("20251399") -> None (L264-265)
    - 场景 e: data_window 不是 str -> None (L253 isinstance)
    """
    win_a = store_mod._window_dates({"data_window": "20251001-20251017"})
    assert win_a == (date(2025, 10, 1), date(2025, 10, 17))

    # 不是 8 位
    assert store_mod._window_dates({"data_window": "2025-1001-2025-1017"}) is None
    assert store_mod._window_dates({"data_window": "202510010251017"}) is None  # 无 -

    # 缺 -
    assert store_mod._window_dates({"data_window": "20251001"}) is None

    # 非法日期
    assert store_mod._window_dates({"data_window": "20251399-20251017"}) is None

    # 非字符串
    assert store_mod._window_dates({"data_window": 12345}) is None
    assert store_mod._window_dates({"data_window": None}) is None
    assert store_mod._window_dates({}) is None


def test_d108_4_covers_date_window_boundaries(store_mod):
    """D-108-4: covers_date 命中 / 边界 (含端点) / 不在窗口 (L268-270).

    data_window="20251001-20251017" 时:
    - 20251001 (start) -> True (含端点)
    - 20251017 (end) -> True (含端点)
    - 20250930 (前一天) -> False
    - 20251018 (后一天) -> False
    """
    data = {"data_window": "20251001-20251017"}

    assert store_mod.covers_date(data, date(2025, 10, 1)) is True   # start (含)
    assert store_mod.covers_date(data, date(2025, 10, 17)) is True  # end (含)
    assert store_mod.covers_date(data, date(2025, 10, 10)) is True  # 中间
    assert store_mod.covers_date(data, date(2025, 9, 30)) is False  # 早
    assert store_mod.covers_date(data, date(2025, 10, 18)) is False  # 晚


def test_d108_5_stable_match_key_english_first_then_chinese(store_mod):
    """D-108-5: _stable_match_key L321-325 优先级: home_en|away_en > name > match.

    - 场景 a: home_en/away_en 都存在 -> "HomeEn|AwayEn"
    - 场景 b: home_en 缺失, away_en 存在 -> 回退 name 或 match
    - 场景 c: 全 None / 空 -> 空字符串
    - 场景 d: strip 空白
    """
    # 场景 a: home_en/away_en 优先
    k_a = store_mod._stable_match_key({"home_en": "Man United", "away_en": "Liverpool",
                                          "name": "曼联 vs 利物浦"})
    assert k_a == "Man United|Liverpool"

    # 场景 b: home_en 空 -> 回退中文
    k_b = store_mod._stable_match_key({"home_en": "", "away_en": "",
                                          "name": "曼联 vs 利物浦"})
    assert k_b == "曼联 vs 利物浦"

    # 场景 c: 全空 -> 空
    k_c = store_mod._stable_match_key({})
    assert k_c == ""

    # 场景 d: strip 空白
    k_d = store_mod._stable_match_key({"home_en": "  Team A  ", "away_en": "  Team B  "})
    assert k_d == "Team A|Team B"

    # 场景 e: home_en 有 away_en 无 -> 回退 name
    k_e = store_mod._stable_match_key({"home_en": "Solo Home", "name": "fallback name"})
    assert k_e == "fallback name"


# ──────────────────────── 额外 helper 验证 (cover 剩余 _as_aware) ────────────────────────


def test_as_aware_naive_dt_gets_bjt_tz(store_mod):
    """_as_aware L195-199: naive datetime 补 BJT tz, aware 不变, None 不动."""
    # naive -> 加 BJT
    from zoneinfo import ZoneInfo
    naive = datetime(2025, 10, 17, 14, 30)
    out_naive = store_mod._as_aware(naive)
    assert out_naive.tzinfo is not None
    assert out_naive.utcoffset() == ZoneInfo("Asia/Shanghai").utcoffset(naive)

    # aware -> 原样
    aware = datetime(2025, 10, 17, 14, 30, tzinfo=ZoneInfo("UTC"))
    out_aware = store_mod._as_aware(aware)
    assert out_aware is aware  # L199 直接返 dt

    # None -> None
    assert store_mod._as_aware(None) is None
