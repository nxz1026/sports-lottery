"""scripts/core/_iojson.py — 宽容 JSON 加载（单一真源，scripts 端）。

之前散在三处：
  - ``scripts/core/calibration.py:_try_load_json`` （utf-8/gbk/gb18030）
  - ``scripts/core/backtest.py:38-49`` 内联（utf-8/gbk，**漏 gb18030** —— DRIFT）
  - ``web/services/store.py:_json_load`` （utf-8/gbk/gb18030 + warning log）

本模块提供 ``try_load_json`` 给 scripts 端复用，编码兜底集合（utf-8 → gbk →
gb18030）通过 ``ENCODINGS`` 元组暴露，方便 web↔scripts 镜像测试守住"三端
编码集合必须一致"。

差异守门（vs web ``_json_load``）：
- web 端失败会 ``logger.warning``（运营审计需要）；scripts 端静默 None
  （predictions / calibration 是 best-effort，警告噪声大）
- web 端读 ``path.read_bytes()`` 一次再试解码（避免重复 open）；scripts 端
  按 encoding 顺序 ``open()``（更简单，无 read_bytes 抽象）
- 返回值：web 端要求 ``isinstance(data, dict) and data``；scripts 端
  只校验 ``json.load`` 成功（predictions 顶层是 dict，calibration 是 dict，
  historical 是 list —— 形态差异下放到 caller 校验）
"""
from __future__ import annotations

import json
from collections.abc import Iterable

# 编码兜底顺序：先 utf-8（绝大多数），失败后 gbk（Windows 旧文件），
# 再 gb18030（gbk 超集，覆盖 GBK 未涵盖的扩展字符）。
ENCODINGS: tuple[str, ...] = ("utf-8", "gbk", "gb18030")


def try_load_json(path, encodings: Iterable[str] = ENCODINGS) -> object | None:
    """宽容加载 JSON：依次尝试 encodings，任何一种成功即返回解析结果。

    返回类型 ``object | None``（dict / list 都可能，predictions/calibration
    是 dict，historical 是 list）。失败 → None。

    异常吞掉范围：``UnicodeDecodeError``（编码不对）+ ``ValueError``
    （JSON 格式错）+ ``OSError``（文件不可读）。
    """
    for enc in encodings:
        try:
            with open(path, encoding=enc) as fh:
                return json.load(fh)
        except (UnicodeDecodeError, ValueError, OSError):
            continue
    return None
