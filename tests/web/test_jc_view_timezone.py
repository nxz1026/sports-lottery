"""竞彩看板时区口径回归（2026-09-17 验收修复）。

背景：fact.jc_match.kickoff_bj 等列是 `timestamp without time zone`，**列里存的
已经是北京时间**。旧代码写 `kickoff_bj at time zone 'Asia/Shanghai'`，等于把该墙上
时间先当成北京时间转成 timestamptz，再由 to_char 按会话时区（本机 Etc/UTC）渲染，
结果早 8 小时——实测 kickoff_bj 2026-09-18 18:30 被渲染成 "09-18 10:30"，影响全部
51 场。前端 static/jc.html 又对"看起来像时间戳"的字符串一律 +8h，与后端方向相反，
使同一页面上 fixtures 与 issues 的偏移互相矛盾且依浏览器时区而异。

这两处都极易被后人"顺手改回去"，故在此加静态守卫。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _jc_view_sql() -> str:
    from store import jc_view
    return jc_view._FIX_SQL


def test_fixtures_sql_does_not_apply_at_time_zone_to_naive_bj_column():
    """kickoff_bj 已是北京时间，绝不能再用 at time zone 二次换算。"""
    sql = _jc_view_sql()
    assert "at time zone" not in sql.lower(), (
        "kickoff_bj 是 timestamp without time zone 且已存北京时间；"
        "`at time zone 'Asia/Shanghai'` 会按会话时区(UTC)渲染成早 8 小时。"
    )


def test_fixtures_sql_formats_kickoff_bj_directly():
    sql = _jc_view_sql()
    assert re.search(r"to_char\(\s*m\.kickoff_bj\s*,\s*'MM-DD HH24:MI'\s*\)", sql), \
        "kickoff_bj 应直接 to_char 输出 MM-DD HH24:MI"


def test_frontend_only_shifts_timestamps_carrying_an_offset():
    """前端只应对带时区标识（Z / ±HH:MM）的 UTC 瞬时值 +8h。

    不带时区的字符串（sale_begin/sale_end/draw_at/kickoff_bj/odds_update）本就是
    北京时间，原样显示即可；旧代码一律 +8h，在 UTC 浏览器下把
    "2026-09-16 20:00 北京" 显示成 "2026-09-17 04:00 北京"。
    """
    js = (REPO_ROOT / "static" / "jc.html").read_text(encoding="utf-8")
    assert "库内一律 UTC" not in js, "该注释是错误假设，已由实测推翻"
    # 必须存在"检测字符串尾部时区标识"的判定
    assert re.search(r"Z\|\[\+-\]\\d\{2\}", js), \
        "cell() 需要先判断时间戳是否自带时区标识，再决定是否 +8h"
