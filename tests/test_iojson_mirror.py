"""P2-17 scripts ↔ web JSON loader 编码兜底镜像对账。

子任务审计 #2：宽容 JSON 加载器三处独立实现：
  - ``scripts/core/calibration._try_load_json``（utf-8/gbk/gb18030）
  - ``scripts/core/backtest`` 内联（旧版只 utf-8/gbk，漏 gb18030 —— 漂移）
  - ``web/services/store._json_load``（utf-8/gbk/gb18030 + warning log）

P2-17 把 scripts 端抽到 ``scripts/core/_iojson.py``（含 gb18030），
calibration / backtest 改 delegate；web 端保留自己的 _json_load
（语义不同：log warning）但编码兜底集合与 scripts 端镜像测试守住。

本测试断言：

1. ``scripts/core/_iojson.ENCODINGS == ("utf-8", "gbk", "gb18030")``
2. web 端 _json_load 也走同样三编码（防止 web 端漏补 encoding）
3. try_load_json 在坏 JSON / 不存在文件 / 编码不能识别 → 返 None
4. try_load_json 对 gbk 编码的文件能成功读（兼容性边界）
5. list 顶层 + dict 顶层两种结构都支持（calibration 的 historical 是 list）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_iojson_encodings_are_known_set():
    """scripts/core/_iojson.ENCODINGS = (utf-8, gbk, gb18030)。"""
    from core._iojson import ENCODINGS

    assert tuple(ENCODINGS) == ("utf-8", "gbk", "gb18030")


def test_web_store_json_load_uses_same_three_encodings():
    """web/services/store._json_load 兜底集合与 scripts 端镜像（utf-8/gbk/gb18030）。"""
    from web.services import store
    # 从 web 端 _json_load 源码字面量提取（避免直接 import private）
    import inspect
    src = inspect.getsource(store._json_load)
    for enc in ("utf-8", "gbk", "gb18030"):
        assert f'"{enc}"' in src, (
            f"web._json_load 缺 {enc} 编码兜底；scripts/core/_iojson 已含，"
            f"web 漏掉会让同一坏 JSON 文件在两端解析结果不一致"
        )


def test_try_load_json_returns_dict_for_dict_top_level(tmp_path):
    """dict 顶层 JSON 正常解析。"""
    from core._iojson import try_load_json

    p = tmp_path / "test.json"
    p.write_text(json.dumps({"a": 1, "b": [2, 3]}), encoding="utf-8")
    data = try_load_json(p)
    assert data == {"a": 1, "b": [2, 3]}


def test_try_load_json_returns_list_for_list_top_level(tmp_path):
    """list 顶层 JSON 正常解析（calibration 的 historical_past_matches 顶层是 list）。"""
    from core._iojson import try_load_json

    p = tmp_path / "test.json"
    p.write_text(json.dumps([{"a": 1}, {"b": 2}]), encoding="utf-8")
    data = try_load_json(p)
    assert data == [{"a": 1}, {"b": 2}]


def test_try_load_json_returns_none_for_missing_file(tmp_path):
    """文件不存在 → None（不抛穿）。"""
    from core._iojson import try_load_json

    assert try_load_json(tmp_path / "nope.json") is None


def test_try_load_json_returns_none_for_corrupt_json(tmp_path):
    """坏 JSON → None（不抛穿）。"""
    from core._iojson import try_load_json

    p = tmp_path / "bad.json"
    p.write_bytes(b'{"a": ')  # 截断
    assert try_load_json(p) is None


def test_try_load_json_returns_none_for_non_utf8_with_no_gbk(tmp_path):
    """乱码（既非 utf-8 也非 gbk 也非 gb18030）→ None。"""
    from core._iojson import try_load_json

    p = tmp_path / "binary.json"
    p.write_bytes(b"\xff\xfe\x00\x01\x02\x03")  # random bytes
    assert try_load_json(p) is None


def test_try_load_json_handles_gbk_encoded_file(tmp_path):
    """gbk 编码的 JSON 文件能成功解析。"""
    from core._iojson import try_load_json

    p = tmp_path / "gbk.json"
    # {"name": "测试"}  gbk 编码
    p.write_bytes(b'{"name": "\xb2\xe2\xca\xd4"}')
    data = try_load_json(p)
    assert data == {"name": "测试"}


def test_calibration_try_load_json_returns_object_not_strict_dict(tmp_path):
    """calibration._try_load_json 接受 list 顶层（historical_past_matches.json）。"""
    from core.calibration import _try_load_json

    p = tmp_path / "hist.json"
    p.write_text(json.dumps([{"m": 1}, {"m": 2}]), encoding="utf-8")
    data = _try_load_json(p)
    assert data == [{"m": 1}, {"m": 2}]


def test_backtest_uses_try_load_json_with_gb18030_fallback(tmp_path, monkeypatch):
    """scripts/core/backtest 的 _bk_load_recent_predictions 走
    _try_load_json + 包含 gb18030 兜底（旧版漏 gb18030 的漂移已修复）。
    """
    import inspect
    from core import backtest

    # 不直接 import _bk_load_recent_predictions（私有），从源码确认
    src = inspect.getsource(backtest._bk_load_recent_predictions)
    assert "_try_load_json" in src, "backtest 还在内联 try-load，应已 delegate"
    # 同时确保不再有内联的 utf-8/gbk 双编码 try-load（修复点）
    assert '"utf-8"' not in src or "open(f, encoding=" not in src, (
        "backtest 仍有内联 utf-8/gbk 双 try-load，gb18030 兜底漂移未修复"
    )
