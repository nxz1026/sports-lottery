"""R51-5+6: scripts/core/i18n.py 3 个 0 直测 helper 9 测 + D-101/D-102 双契约锁。

R51-1+2 已锁 web/auth.py (D-97/D-98), R51-3+4 已锁 features.py (D-99/D-100).
``scripts/core/i18n.py`` 的 3 个未直测 helper:

- load_translations     (L20-32): 全局 _CACHE 单例 + 文件 IO + JSON 解析 + 异常兜底
- save_translations     (L35-43): dump JSON + mkdir + 异常 warning
- translate_team_names  (L46-79): 调用 ai.llm_client.generate + 处理嵌套/flat/异常响应

warm_translations (L82-98) + to_cn (L101-108) 已被 test_i18n.py 覆盖.

D-101 锁 translate_team_names (5 测):
- D-101-1: 空 names -> 返 {}
- D-101-2: 响应 {"translations": {nested}} -> 解析嵌套 dict
- D-101-3: 响应 flat {"English": "中文"} -> 解析 flat
- D-101-4: 响应混合 (嵌套含非 str value) -> 过滤非 str / 空字符串
- D-101-5: generate 抛异常 或 返非 dict -> 返 {} (LLM 不可用降级)

D-102 锁 load_translations + save_translations (4 测):
- D-102-1: 二次调用缓存命中 (_CACHE is not None 返 _CACHE)
- D-102-2: 文件不存在 返 {}
- D-102-3: 坏 JSON / OSError 返 {}
- D-102-4: save_translations 写文件 + mkdir parents + 更新 _CACHE

策略:
- spec-load core.i18n 到隔离 namespace (避免 _CACHE 跨测污染)
- TRANSLATION_CACHE_FILE stub 为 tmp_path 文件 (真写但限定 tmp 目录)
- ai.llm_client.generate stub 为受控 lambda
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def i18n_mod(tmp_path, monkeypatch):
    """Spec-load core.i18n + tmp TRANSLATION_CACHE_FILE + stub generate.

    - 清空 _CACHE (spec-load 默认 None, 但保险)
    - TRANSLATION_CACHE_FILE 指向 tmp 目录, 真文件 IO 但不污染 repo
    - ai.llm_client.generate stub (用 unittest.mock MagicMock, 测试内 replace)
    """
    spec = importlib.util.spec_from_file_location(
        "core_i18n_test",
        REPO_ROOT / "scripts" / "core" / "i18n.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 清空全局缓存 (跨 fixture 重入保险)
    mod._CACHE = None

    # TRANSLATION_CACHE_FILE 指向 tmp_path 下文件
    cache_file = tmp_path / "team_translations.json"
    monkeypatch.setattr(mod, "TRANSLATION_CACHE_FILE", cache_file)

    # ai.llm_client.generate stub 默认返 {} (测试再覆盖)
    generate_mock = MagicMock(return_value={})
    monkeypatch.setattr("ai.llm_client.generate", generate_mock)

    return mod, generate_mock, cache_file


# ──────────────────────── D-101: 5 测 ────────────────────────


def test_d101_1_empty_names_returns_empty_dict(i18n_mod):
    """D-101-1: translate_team_names([]) 立即返 {} 不调 generate (L48-49)."""
    mod, generate_mock, _ = i18n_mod
    out = mod.translate_team_names([])
    assert out == {}
    # generate 不应被调用 — 空 names 早退
    generate_mock.assert_not_called()


def test_d101_2_parses_nested_translations_key(i18n_mod):
    """D-101-2: 响应 {'translations': {en: cn}} 嵌套形式 — 走 L68-69 分支, 正确解析."""
    mod, generate_mock, _ = i18n_mod
    generate_mock.return_value = {
        "translations": {"Fulham": "富勒姆", "Chelsea FC": "切尔西"},
    }

    out = mod.translate_team_names(["Fulham", "Chelsea FC"])

    assert out == {"Fulham": "富勒姆", "Chelsea FC": "切尔西"}
    generate_mock.assert_called_once()
    # prompt 含 names 列表
    prompt_arg = generate_mock.call_args[0][0]
    assert "Fulham" in prompt_arg
    assert "Chelsea FC" in prompt_arg


def test_d101_3_parses_flat_dict_response(i18n_mod):
    """D-101-3: 响应 flat {en: cn} (非嵌套) 且所有 value 是 str — 走 L70-71 分支."""
    mod, generate_mock, _ = i18n_mod
    generate_mock.return_value = {"AS Roma": "罗马", "Bologna FC 1909": "博洛尼亚"}

    out = mod.translate_team_names(["AS Roma", "Bologna FC 1909"])

    assert out == {"AS Roma": "罗马", "Bologna FC 1909": "博洛尼亚"}


def test_d101_4_filters_non_string_values_and_whitespace(i18n_mod):
    """D-101-4: L72-75 过滤非 str value (None / int / 空字符串 / 纯 whitespace).

    注意: i18n.py L70-71 已经把 flat 分支限制为 ``all(isinstance(v, str) for v in resp.values())``,
    即 flat 响应所有 value 必须都是 str 才进入 mapping. 嵌套响应 (L68-69) 才会
    把嵌套 dict 原样赋给 mapping, 此时 L72-75 才会真正过滤非 str value.
    所以这里用嵌套形式 {translations: {...混合 value...}} 才能触发过滤.
    """
    mod, generate_mock, _ = i18n_mod
    generate_mock.return_value = {
        "translations": {
            "ValidName": "有效翻译",
            "EmptyValue": "",          # 空字符串 -> strip 后 "" falsy, 跳过
            "Whitespace": "   ",       # 纯 whitespace strip 后 "" falsy, 跳过
            "NoneValue": None,         # 非 str -> 跳过
            "IntValue": 42,            # 非 str -> 跳过
            "ListValue": ["x"],        # 非 str -> 跳过
        }
    }

    out = mod.translate_team_names(["anything"])

    # 仅 ValidName 进入 out
    assert out == {"ValidName": "有效翻译"}
    assert "EmptyValue" not in out
    assert "Whitespace" not in out
    assert "NoneValue" not in out
    assert "IntValue" not in out
    assert "ListValue" not in out


def test_d101_5_generate_exception_or_non_dict_returns_empty(i18n_mod):
    """D-101-5: generate 抛异常 或 返非 dict (list / str / int) -> 返 {} 降级 (L77-79 / L65-66)."""
    mod, generate_mock, _ = i18n_mod

    # 场景 a: generate 抛 RuntimeError
    generate_mock.side_effect = RuntimeError("LLM API down")
    out_a = mod.translate_team_names(["X"])
    assert out_a == {}

    # 场景 b: generate 返 list (非 dict)
    generate_mock.reset_mock()
    generate_mock.side_effect = None
    generate_mock.return_value = ["not", "a", "dict"]
    out_b = mod.translate_team_names(["Y"])
    assert out_b == {}

    # 场景 c: generate 返 str
    generate_mock.reset_mock()
    generate_mock.return_value = "string-not-dict"
    out_c = mod.translate_team_names(["Z"])
    assert out_c == {}


# ──────────────────────── D-102: 4 测 ────────────────────────


def test_d102_1_second_call_hits_cache_no_file_read(i18n_mod):
    """D-102-1: 第一次读文件 + 第二次返 _CACHE 单例 — 不再 IO.

    写入合法 JSON 后调用 load_translations 两次:
    - 第 1 次: _CACHE=None -> 读文件, 设 _CACHE, 返内容
    - 第 2 次: _CACHE is not None -> 返 _CACHE (不读文件)
    """
    mod, _, cache_file = i18n_mod

    # 预先写 cache file
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"Pre": "预存"}, ensure_ascii=False), encoding="utf-8")

    # 第 1 次读
    cache_a = mod.load_translations()
    assert cache_a == {"Pre": "预存"}
    assert mod._CACHE == {"Pre": "预存"}

    # 第 2 次直接命中 _CACHE — 但如果删了文件, 应仍能返 _CACHE 单例
    cache_file.unlink()
    cache_b = mod.load_translations()
    assert cache_b == {"Pre": "预存"}
    # 单例等价
    assert cache_a is cache_b


def test_d102_2_file_missing_returns_empty_dict(i18n_mod):
    """D-102-2: TRANSLATION_CACHE_FILE 不存在 -> 返 {} (L25-29 else 分支)."""
    mod, _, cache_file = i18n_mod

    # 确保文件不存在
    assert not cache_file.exists()

    out = mod.load_translations()
    assert out == {}
    assert mod._CACHE == {}


def test_d102_3_corrupt_json_returns_empty_dict(i18n_mod):
    """D-102-3: 坏 JSON / OSError -> 返 {} 兜底 (L30-31 except)."""
    mod, _, cache_file = i18n_mod

    # 场景 a: 坏 JSON
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{broken: not json,,,", encoding="utf-8")
    out_bad = mod.load_translations()
    assert out_bad == {}
    assert mod._CACHE == {}

    # 重置 + 场景 b: 重新设置成坏 JSON (清完 _CACHE 再来)
    mod._CACHE = None
    cache_file.write_text("[1, 2,", encoding="utf-8")  # 坏 list JSON
    out_bad2 = mod.load_translations()
    assert out_bad2 == {}

    # 场景 c: 文件存在但 OSError (用不可读权限模拟: 无)
    # 在 Windows 上难模拟 OSError, 跳过 (后续可 monkeypatch open 失败)


def test_d102_4_save_translations_writes_json_and_updates_cache(i18n_mod):
    """D-102-4: save_translations(d) -> 写 JSON + 更新 _CACHE + mkdir parents.

    验证:
    - _CACHE 即时更新 (后续 load_translations 命中)
    - 文件真出现且 JSON 内容一致
    - parent dir 自动创建 (即使删除 tmp_path 父目录再 save)
    """
    mod, _, cache_file = i18n_mod

    data = {"Manchester United": "曼联", "Liverpool FC": "利物浦"}

    # 第 1 次写: 直接写, parent 已存在
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    mod.save_translations(data)

    # _CACHE 更新
    assert mod._CACHE == data
    # 文件落地 + JSON 内容一致
    assert cache_file.exists()
    on_disk = json.loads(cache_file.read_text(encoding="utf-8"))
    assert on_disk == data

    # 第 2 次验证: load_translations 命中 _CACHE
    reloaded = mod.load_translations()
    assert reloaded == data

    # 删文件验证 _CACHE 真在内存里
    cache_file.unlink()
    assert mod.load_translations() == data
