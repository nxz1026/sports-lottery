"""``scripts/ingest/jc_name_join_audit.py`` 的纯函数契约测试（不连库）。

为什么要有这个脚本：竞彩场次与模型预测**按中文名 join**，若某支队的竞彩叫法
模型永远产不出来 ⇒ 那一场**静默出局**（荐单/CLV 拿不到概率，页面上看不出异常）。
2026-10-22 生产实测因此丢了 10 场（巴伦西亚 / 云达不来梅 / 贝西克塔斯）。

这个脚本把该检查变成**人人可跑的复算命令**（退出码 1 = 有缺口，不许静默）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.team_alias import alias_table  # noqa: E402


def _load_mod():
    spec = importlib.util.spec_from_file_location(
        "jc_name_join_audit_test", REPO_ROOT / "scripts" / "ingest" / "jc_name_join_audit.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_unreachable_flags_name_model_cannot_produce():
    mod = _load_mod()
    bad = mod.unreachable({"贝西克塔斯": 3, "曼城": 5}, {"曼城", "曼彻斯特城"})
    assert [n for n, _ in bad] == ["贝西克塔斯"]
    assert dict(bad)["贝西克塔斯"] == 3


def test_unreachable_is_empty_when_all_names_reachable():
    mod = _load_mod()
    assert mod.unreachable({"曼城": 5, "阿森纳": 2}, {"曼城", "曼彻斯特城", "阿森纳"}) == []


def test_unreachable_respects_alias_bridge():
    """别名表桥接过的名字算可达（这正是加别名的目的）。"""
    mod = _load_mod()
    assert "贝西克塔斯" in alias_table()
    assert mod.unreachable({"贝西克塔斯": 1}, {"皇家贝蒂斯"}) == []


def test_model_side_names_contains_official_table_and_alias_targets():
    mod = _load_mod()
    side = mod.model_side_names()
    assert "皇家贝蒂斯" in side          # 官方队名表的值
    assert "瓦伦西亚" in side            # COUNTRY_CN / LLM 缓存
    assert "比利亚雷亚尔" in side        # 官方队名表
    for targets in alias_table().values():
        for t in targets:
            assert t in side


def test_audit_module_has_main_entrypoint():
    """少了 __main__ 入口 ⇒ `python scripts/ingest/jc_name_join_audit.py` 会**静默什么都不做**。"""
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_name_join_audit.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    assert "raise SystemExit(main())" in src


def test_audit_sql_only_reads():
    """审计必须只读：SQL 里不许出现 INSERT/UPDATE/DELETE。"""
    src = (REPO_ROOT / "scripts" / "ingest" / "jc_name_join_audit.py").read_text(encoding="utf-8")
    upper = src.upper()
    for kw in ("INSERT ", "UPDATE ", "DELETE "):
        assert kw not in upper, kw
    assert "read_conn" in src


# ── 映射自检（一个队行挂了多个 id，但源名不属于它 ⇒ id 挂错了队）─────────

def test_mapping_violations_accepts_same_club_two_names():
    """合法形态：#2355「皇家贝蒂斯」挂着 505「皇家贝蒂斯」+ 279「贝西克塔斯」
    （同一家俱乐部，竞彩给了两个 id 两个叫法；别名表把「贝西克塔斯」桥接过来）。"""
    mod = _load_mod()
    rows = [(2355, "皇家贝蒂斯", 505, "皇家贝蒂斯"),
            (2355, "皇家贝蒂斯", 279, "贝西克塔斯")]
    assert mod.mapping_violations(rows) == []


def test_mapping_violations_flags_id_pointed_at_wrong_club():
    """错配形态：279「贝西克塔斯」挂在「毕尔巴鄂竞技」行上（2026-10-22 纠正前的状态）。"""
    mod = _load_mod()
    rows = [(2351, "毕尔巴鄂竞技", 506, "毕尔巴鄂竞技"),
            (2351, "毕尔巴鄂竞技", 279, "贝西克塔斯")]
    bad = mod.mapping_violations(rows)
    assert len(bad) == 1
    assert bad[0][2] == 279
    assert bad[0][3] == "贝西克塔斯"
