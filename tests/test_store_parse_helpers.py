"""R60: scripts/store/* ROI 低 0 直测 helper 17 测 + D-121/D-122/D-123 lock。

R59 末已锁 D-120 (apifootball 4 helper). ROI 低批量扫描找出剩余 7 个 0 直测
helper, 散落在多个 parse 系列模块:

- parse_collector.row_error     (L56-62): 一行 JSON + envelope 校验
- parse_misc._parse_ft          (L19-26): status=2 + finalScore 解析
- parse_odds_hist._goal_line_value (L15-21): 空/数字/非法 -> None/float/None
- parse_values.split_numbers    (L48-53): 大乐透 7 球 split, 其它彩种原序
- pg.dsn                        (L21-24): 按角色取 DSN env
- align._side_hit + _pair_hit   (L71-90): 队名配对 hit 判定

D-121 锁 parse_collector/parse_misc/parse_values/parse_odds_hist 4 helper (8 测):
- D-121-1: row_error 合法 JSON + envelope 通过 / None + None (缺字段)
- D-121-2: row_error 非 JSON -> (None, "invalid json: <msg>")
- D-121-3: _parse_ft status=2 + 合法 final_score -> (h, a)
- D-121-4: _parse_ft status != 2 或 final_score '-' -> (None, None)
- D-121-5: _parse_ft status=2 但非法 final_score -> ValueError
- D-121-6: _goal_line_value None / 非数字 -> None
- D-121-7: _goal_line_value 数字 -> float
- D-121-8: split_numbers 大乐透 7 球 -> {front, back}; 其它 {digits}

D-122 锁 pg.dsn 角色解析 (3 测, monkeypatch env):
- D-122-1: 合法 role (ing/app/ro) -> 返 env value
- D-122-2: 非法 role -> ValueError 含 expected
- D-122-3: env 缺失 -> KeyError (不静默降级)

D-123 锁 align._side_hit + _pair_hit (6 测):
- D-123-1: _side_hit 同义队名匹配 (Fulham vs FC Fulham)
- D-123-2: _side_hit 不同义 -> None
- D-123-3: _side_hit 一边 None -> None
- D-123-4: _pair_hit (home, away) 完全匹配 sorted -> (similar)/(reverse)
- D-123-5: _pair_hit swapped 模式
- D-123-6: _pair_hit 完全不匹配 -> None

策略:
- spec-load 全部模块到隔离 namespace, sys.path 加 scripts/
- _side_hit / _pair_hit 是 align 模块的内部 helper, 用 monkeypatch 关联.
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


# ───────── fixtures ─────────


def _load_store_package():
    """先加载 store package, 让 ``from .x import`` 相对 import 能 resolve. 返回 parent module."""
    pkg_init = REPO_ROOT / "scripts" / "store" / "__init__.py"
    pkg_spec = importlib.util.spec_from_file_location("r60_store_pkg", pkg_init)
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules[pkg_spec.name] = pkg
    pkg_spec.loader.exec_module(pkg)
    return pkg_spec.name  # 'r60_store_pkg'


def _spec_load_pkg(name: str, path: Path, pkg: str):
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _spec_load(name: str, path: Path):
    """独立 spec-load, 用于无相对 import 的 module (parse_misc / parse_odds_hist 等)."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_PKG = _load_store_package()


@pytest.fixture
def parse_collector_mod():
    return _spec_load_pkg("parse_collector",
                          REPO_ROOT / "scripts" / "store" / "parse_collector.py", _PKG)


@pytest.fixture
def parse_misc_mod():
    # parse_misc 第 8 行用 ``from .parse_values import``, 必须靠 store parent package
    return _spec_load_pkg("parse_misc",
                          REPO_ROOT / "scripts" / "store" / "parse_misc.py", _PKG)


@pytest.fixture
def parse_odds_hist_mod():
    return _spec_load("r60_parse_odds_hist_test",
                      REPO_ROOT / "scripts" / "store" / "parse_odds_hist.py")


@pytest.fixture
def parse_values_mod():
    # parse_values 本身无相对 import, 但 parse_collector 间接引用, 用 pkg 加载
    return _spec_load_pkg("parse_values",
                          REPO_ROOT / "scripts" / "store" / "parse_values.py", _PKG)


@pytest.fixture
def pg_mod(monkeypatch):
    """Spec-load scripts/store/pg.py + stub env DSN_ING."""
    mod = _spec_load("r60_pg_test",
                     REPO_ROOT / "scripts" / "store" / "pg.py")
    # monkeypatch env 让 dsn(role) 返非空. 但不要污染 tests 一起的 env pollution, 用 monkeypatch.
    monkeypatch.setenv("DSN_ING", "postgresql://ing@host/db")
    monkeypatch.setenv("DSN_APP", "postgresql://app@host/db")
    monkeypatch.setenv("DSN_RO", "postgresql://ro@host/db")
    return mod


@pytest.fixture
def align_mod():
    return _spec_load("r60_align_test",
                      REPO_ROOT / "scripts" / "store" / "align.py")


# ──────────────────────── D-121: 8 测 (parse 系列) ────────────────────────


def _valid_envelope(topic: str = "x", payload: dict | None = None) -> str:
    """构造符合 ENVELOPE 8 字段的合法 JSON 行. src_hash 通过 canonical_src_hash 校验."""
    import hashlib
    import json
    payload = payload or {"result": "data"}
    # 仿照 parse_values.canonical_src_hash 算 sha256 (简化版)
    canon = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    src_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    envelope = {
        "kind": "line", "topic": topic,
        "snap_ts": "2025-10-17T14:30:00Z",
        "endpoint": "https://example.com/api",
        "http_status": 200,
        "collector_host": "test-host",
        "payload": payload,
        "src_hash": src_hash,
    }
    return json.dumps(envelope, ensure_ascii=False)


def test_d121_1_row_error_valid_envelope_returns_obj(parse_collector_mod):
    """D-121-1: row_error (L56-62) 合法 JSON + envelope 通过 -> (obj, None).

    用 _valid_envelope 构造符合 8 字段 envelope 的 JSON 行, src_hash 与 payload 同步.
    """
    mod = parse_collector_mod

    line = _valid_envelope(topic="x")
    obj, err = mod.row_error(line, topic="x")
    assert err is None
    assert obj.get("kind") == "line"


def test_d121_2_row_error_invalid_json_returns_msg(parse_collector_mod):
    """D-121-2: row_error 非 JSON -> (None, "invalid json: <reason>")."""
    mod = parse_collector_mod

    obj, err = mod.row_error('not json {"broken', topic="x")
    assert obj is None
    assert err.startswith("invalid json:")
    assert err != "invalid json: "  # 错误消息必须有具体原因


def test_d121_3_parse_ft_status_2_valid_score(parse_misc_mod):
    """D-121-3: _parse_ft (L19-26) status=2 + 合法 finalScore -> (h, a) ints."""
    mod = parse_misc_mod

    assert mod._parse_ft(2, "2-1") == (2, 1)
    assert mod._parse_ft(2, "0-0") == (0, 0)
    assert mod._parse_ft(2, "5-3") == (5, 3)


def test_d121_4_parse_ft_status_not_2_or_empty_score(parse_misc_mod):
    """D-121-4: _parse_ft status!=2 或 finalScore '-' / '' -> (None, None)."""
    mod = parse_misc_mod

    # 非 status 2 -> skip
    assert mod._parse_ft(0, "1-0") == (None, None)
    assert mod._parse_ft(1, "1-0") == (None, None)
    assert mod._parse_ft(3, "1-0") == (None, None)
    # status=2 但 finalScore 为占位
    assert mod._parse_ft(2, "-") == (None, None)
    assert mod._parse_ft(2, "") == (None, None)


def test_d121_5_parse_ft_status_2_invalid_score_raises(parse_misc_mod):
    """D-121-5: _parse_ft status=2 但 finalScore 非法 -> ValueError."""
    mod = parse_misc_mod

    with pytest.raises(ValueError, match="无法解析"):
        mod._parse_ft(2, "abc")
    with pytest.raises(ValueError, match="无法解析"):
        mod._parse_ft(2, "1-2-3")  # 多段
    with pytest.raises(ValueError, match="无法解析"):
        mod._parse_ft(2, "1")  # 单段


def test_d121_6_goal_line_value_invalid_returns_none(parse_odds_hist_mod):
    """D-121-6: _goal_line_value (L15-21) None / 空 / 非数字 -> None."""
    mod = parse_odds_hist_mod

    assert mod._goal_line_value(None) is None
    assert mod._goal_line_value("") is None
    assert mod._goal_line_value("abc") is None  # 非数字
    assert mod._goal_line_value("not-a-number") is None


def test_d121_7_goal_line_value_returns_float(parse_odds_hist_mod):
    """D-121-7: _goal_line_value 数字 -> float."""
    mod = parse_odds_hist_mod

    assert mod._goal_line_value("0.5") == 0.5
    assert mod._goal_line_value("2.5") == 2.5
    assert mod._goal_line_value("-1.5") == -1.5
    assert mod._goal_line_value("3") == 3.0


def test_d121_8_split_numbers_dlt_and_other(parse_values_mod):
    """D-121-8: split_numbers (L48-53) 大乐透 (85) 7 球 -> {front, back}; 其它彩种 -> {digits}."""
    mod = parse_values_mod

    # 大乐透: 7 球 split 为 front[5] + back[2]
    out_dlt = mod.split_numbers("85", "01 05 12 23 34 03 09")
    assert out_dlt == {"front": ["01", "05", "12", "23", "34"], "back": ["03", "09"]}

    # 大乐透但球数 != 7 -> 走 digits 分支 (不重排)
    out_dlt_bad = mod.split_numbers("85", "01 05 12")
    assert out_dlt_bad == {"digits": ["01", "05", "12"]}

    # 其它彩种: 原序不重排
    out_other = mod.split_numbers("90", "3 5 8 1 2 7")
    assert out_other == {"digits": ["3", "5", "8", "1", "2", "7"]}

    # 空 raw -> None (L51 not balls)
    assert mod.split_numbers("85", "") is None


# ──────────────────────── D-122: 3 测 (pg.dsn) ────────────────────────


def test_d122_1_dsn_returns_role_env_value(pg_mod, monkeypatch):
    """D-122-1: pg.dsn (L21-24) 合法 role -> 返 env value."""
    mod = pg_mod

    assert mod.dsn("ing") == "postgresql://ing@host/db"
    assert mod.dsn("app") == "postgresql://app@host/db"
    assert mod.dsn("ro") == "postgresql://ro@host/db"
    # 默认 role = "ing"
    assert mod.dsn() == "postgresql://ing@host/db"


def test_d122_2_dsn_unknown_role_raises(pg_mod):
    """D-122-2: pg.dsn 非法 role -> ValueError 含 expected roles 列表."""
    mod = pg_mod

    with pytest.raises(ValueError, match="unknown role"):
        mod.dsn("invalid_role")
    # 错误信息应提及合法 role 集合
    with pytest.raises(ValueError, match="'ro'|app|ing"):
        mod.dsn("xyz")


def test_d122_3_dsn_missing_env_raises_runtime(pg_mod, monkeypatch):
    """D-122-3: pg.dsn env 缺失不静默降级 -> RuntimeError (实际实现 L26-29).

    pg.py L26 ``os.environ.get(env_key, "").strip()`` + L28 缺失时 raise RuntimeError
    含 ``{env_key} 未设置`` 提示. 不静默降级成另一个角色.
    """
    mod = pg_mod

    # DSN_ING 缺失
    monkeypatch.delenv("DSN_ING", raising=False)

    with pytest.raises(RuntimeError, match="DSN_ING 未设置"):
        mod.dsn("ing")


# ──────────────────────── D-123: 6 测 (align._side_hit / _pair_hit) ────────────────────────


def test_d123_1_side_hit_alias_match_returns_token_hit(align_mod):
    """D-123-1: align._side_hit (L71-79) 同义队名匹配 -> "tokens" (TOKEN_HIT).

    align.tokens(L31) 剥前缀/后缀/abbr, _side_hit 在已归一口径下比对,
    对子集关系返 "tokens".
    """
    mod = align_mod
    # "Fulham" tokens({"fulham"}) <= "Fulham FC" tokens({"fulham"}) (L74 子集), 返 TOKEN_HIT
    out = mod._side_hit("Fulham", "Fulham FC")
    assert out == "tokens"


def test_d123_2_side_hit_no_match_returns_none(align_mod):
    """D-123-2: align._side_hit 完全不同队名 (token 不重叠) -> None (L79)."""
    mod = align_mod

    out = mod._side_hit("Manchester United", "Liverpool FC")
    assert out is None


def test_d123_3_side_hit_none_handled(align_mod):
    """D-123-3: align._side_hit 一边 None -> 跳过 token 检查后 None, 不抛."""
    mod = align_mod

    # 任一 None tokens falsy (L74 if af_tokens and fd_tokens...), 跳到 SYNONYM 检查又因 norm falsy 返回 None
    assert mod._side_hit(None, "Fulham FC") is None
    assert mod._side_hit("Liverpool", None) is None
    assert mod._side_hit(None, None) is None


def test_d123_4_pair_hit_exact_match_returns_token_hit(align_mod):
    """D-123-4: align._pair_hit (L82-89) 双方 tokens 子集匹配 -> "tokens"."""
    mod = align_mod

    out = mod._pair_hit("Fulham", "Chelsea",
                          {"home_name": "Fulham FC", "away_name": "Chelsea FC"},
                          swapped=False)
    # 两侧 _side_hit 都返 "tokens", 合并返 "tokens" (L89 SYNONYM_HIT not in hits else TOKEN_HIT)
    assert out == "tokens"


def test_d123_5_pair_hit_swap_mode(align_mod):
    """D-123-5: align._pair_hit swapped=True, FD home/away 互换后匹配 -> 命中."""
    mod = align_mod

    # swapped: AF home=Away, FD home=af_away -> tokens 等
    out = mod._pair_hit("Arsenal FC", "Chelsea FC",
                          {"home_name": "Chelsea FC", "away_name": "Arsenal FC"},  # fd 是反序
                          swapped=True)
    # swapped 模式下 AF home vs FD away, AF away vs FD home -> 都 token 子集
    assert out in ("tokens", "synonym")


def test_d123_6_pair_hit_no_match_returns_none(align_mod):
    """D-123-6: align._pair_hit 任一侧 _side_hit None -> 整场 None (L87)."""
    mod = align_mod

    out = mod._pair_hit("Arsenal", "Chelsea",
                          {"home_name": "Liverpool", "away_name": "ManU"})
    assert out is None
