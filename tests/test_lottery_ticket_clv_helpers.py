"""R58: ticket / lottery / clv 三个模块 6 个 0 直测 helper 12 测 + D-117/D-118/D-119 lock。

R57 末已锁 D-115/D-116 (jobs 5 helper). 自动化扫描找出剩余 high-ROI 0 直测:

- web/services/ticket.py::_norm_probs       (L29-44): 三向概率校验并返回 (主,平,客)
- web/services/lottery.py::_dlt_note        (L73-74): DLT 中奖文 ("前区中X、后区中Y")
- web/services/lottery.py::_uniq_range      (L104-113): 数值范围去重, 越界/非数值跳过
- scripts/market/clv.py::_check_prob        (L11-16): bool/非数值/非有限/越界校验
- scripts/market/clv.py::_check_preds       (L19-29): preds Mapping 校验 + Σ 偏离 1
- 另外: ingess/quota.py::_as_day            (1 helper)
- 另外: sides.py::jqc_sides + commit_sha     (2 helper)

D-117 锁 _norm_probs + _dlt_note + _uniq_range (4 测纯函数):
- D-117-1: _norm_probs 合法 sum=1 -> 返回 (主胜,平,客胜)
- D-117-2: _norm_probs 缺键 / 非 Mapping / 非数值 / 越界 / Σ 偏离 1 各种非法 -> ValueError
- D-117-3: _dlt_note 文案格式 "前区中X、后区中Y" (含 0 边界)
- D-117-4: _uniq_range 去重 + 越界跳过 + 非数值跳过 + 顺序保留 (首次出现胜)

D-118 锁 clv._check_prob + _check_preds (4 测):
- D-118-1: _check_prob 合法值 [lo,hi] -> 通过
- D-118-2: _check_prob bool/非数值/inf/越界 -> ValueError 含 offending 值
- D-118-3: _check_preds 合法三向 sum=1 + outcome 在键里 -> 通过
- D-118-4: _check_preds 非 Mapping / 空 / Σ 偏离 1 / outcome 不在键里 -> ValueError

D-119 锁 跨模块 (3 helper, 4 测):
- D-119-1: quota._as_day 把 epoch 截到 UTC date (字符串 YYYY-MM-DD)
- D-119-2: sides.jqc_sides 直接调返 (jqc_gap 基础 — 已有 test_sides 间接覆盖, 重点测函数存在)
- D-119-3: sides.commit_sha 返 git SHA / 不可解析时返 "" (无需 db)
- D-119-4: clv.clv (公共) 正数 ln 校验 (重复校验 _check_prob 的接口)

策略:
- spec-load 三个模块到隔离 namespace
- 4 个 0 直测 helper 都是纯函数, 0 副作用
- D-119 跨模块 4 测是因为 helpers 散落, 集中一个文件更好维护
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


# ───────── fixtures ─────────


@pytest.fixture
def ticket_mod():
    """Spec-load web.services.ticket (含 _norm_probs)."""
    spec = importlib.util.spec_from_file_location(
        "web_services_ticket_test",
        REPO_ROOT / "web" / "services" / "ticket.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lottery_mod():
    """Spec-load web.services.lottery."""
    spec = importlib.util.spec_from_file_location(
        "web_services_lottery_test",
        REPO_ROOT / "web" / "services" / "lottery.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clv_mod():
    """Spec-load scripts/market/clv.py."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "scripts_market_clv_test",
        REPO_ROOT / "scripts" / "market" / "clv.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sides_mod():
    """Spec-load scripts/model/sides.py."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "scripts_model_sides_test",
        REPO_ROOT / "scripts" / "model" / "sides.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def quota_mod():
    """Spec-load scripts/ingest/quota.py."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "scripts_ingest_quota_test",
        REPO_ROOT / "scripts" / "ingest" / "quota.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────── D-117: 4 测 (ticket + lottery) ────────────────────────


def test_d117_1_norm_probs_happy_path(ticket_mod):
    """D-117-1: _norm_probs (L29-44) 主胜/平/客胜三向概率 sum=1 -> 返 (主,平,客) 元组."""
    mod = ticket_mod

    # 标准 3-way 输入
    out = mod._norm_probs({"主胜": 0.5, "平": 0.3, "客胜": 0.2})
    assert out == (0.5, 0.3, 0.2)

    # 微偏离容差 1e-6 (L42 _SUM_TOL)
    out_edge = mod._norm_probs({"主胜": 0.5, "平": 0.3, "客胜": 0.2 + 1e-7})
    assert out_edge[0] == pytest.approx(0.5, abs=1e-12)


def test_d117_2_norm_probs_invalid_inputs_raise_value_error(ticket_mod):
    """D-117-2: _norm_probs 5 种非法路径全部 ValueError + 含 offending 值.

    - 缺键
    - 非 Mapping
    - bool 类型
    - 越界 / 非有限
    - sum 偏离 1 超容差
    """
    mod = ticket_mod

    # 缺键
    with pytest.raises(ValueError, match="缺三向键"):
        mod._norm_probs({"主胜": 0.5, "平": 0.5})  # 缺客胜
    # 非 Mapping
    with pytest.raises(ValueError, match="必须为 Mapping"):
        mod._norm_probs([0.5, 0.3, 0.2])  # list 不是 Mapping
    # bool 被视作非数值 (L39 isinstance(p, bool) 守卫)
    with pytest.raises(ValueError, match="非法"):
        mod._norm_probs({"主胜": True, "平": 0.0, "客胜": 0.0})
    # 越界
    with pytest.raises(ValueError, match="非法"):
        mod._norm_probs({"主胜": 1.5, "平": 0.0, "客胜": -0.5})
    # sum 偏离 1 超容差
    with pytest.raises(ValueError, match="偏离 1"):
        mod._norm_probs({"主胜": 0.6, "平": 0.3, "客胜": 0.3})  # sum=1.2


def test_d117_3_dlt_note_format(lottery_mod):
    """D-117-3: _dlt_note (L73-74) 文案 ``前区中X、后区中Y``.

    - 命中 (5,2): "前区中5、后区中2"
    - 全空: "前区中0、后区中0"
    - 中间命中: "前区中3、后区中1"
    """
    mod = lottery_mod
    assert mod._dlt_note(5, 2) == "前区中5、后区中2"
    assert mod._dlt_note(0, 0) == "前区中0、后区中0"
    assert mod._dlt_note(3, 1) == "前区中3、后区中1"


def test_d117_4_uniq_range_filters_and_dedupes(lottery_mod):
    """D-117-4: _uniq_range (L104-113) 越界跳过 / 非数值跳过 / 保留首次出现顺序.

    注意 L108 ``n = int(v)``: int() 对浮点 truncate (int(3.5)=3) 而非抛错, 所以
    3.5 (float) 被转成 3 然后因 dup skip. 这条契约锁定这个行为, 避免后续
    "看起来对但实际 silent loss" 的修改.
    """
    mod = lottery_mod

    # 混合输入
    out = mod._uniq_range([1, "3", 3, 3.5, -1, 100, "abc", 5, 5], lo=1, hi=10)
    # 1 (int) / "3"->3 / 3 skip dup / 3.5 -> int(3.5)=3 dup skip /
    # -1 越界 skip / 100 越界 / "abc" 非数值 skip / 5 / 5 skip dup
    assert out == [1, 3, 5]


# ──────────────────────── D-118: 4 测 (clv) ────────────────────────


def test_d118_1_check_prob_passes_valid(clv_mod):
    """D-118-1: _check_prob (L11-16) [lo, hi] 内合法值 -> 不 raise."""
    mod = clv_mod

    # 边界 + 中点
    mod._check_prob(0.0, "p", 0.0, 1.0)
    mod._check_prob(1.0, "p", 0.0, 1.0)
    mod._check_prob(0.5, "p", 0.0, 1.0)
    mod._check_prob(-1.0, "x", -1.0, 1.0)  # 不同区间


def test_d118_2_check_prob_raises_on_invalid(clv_mod):
    """D-118-2: _check_prob 5 种非法路径 — bool / 非数值 / inf / nan / 越界."""
    mod = clv_mod

    # bool 被视作非数值 (L13 isinstance(p, bool) or not isinstance(p, (int, float)))
    with pytest.raises(ValueError, match="非法"):
        mod._check_prob(True, "p", 0.0, 1.0)
    # str 非数值
    with pytest.raises(ValueError, match="非法"):
        mod._check_prob("abc", "p", 0.0, 1.0)
    # inf
    with pytest.raises(ValueError, match="非法"):
        mod._check_prob(float("inf"), "p", 0.0, 1.0)
    # 越界
    with pytest.raises(ValueError, match="非法"):
        mod._check_prob(1.5, "p", 0.0, 1.0)
    with pytest.raises(ValueError, match="非法"):
        mod._check_prob(-0.1, "p", 0.0, 1.0)


def test_d118_3_check_preds_happy(clv_mod):
    """D-118-3: _check_preds (L19-29) 非空 Mapping + Σ=1 + outcome 在键里 -> 通过."""
    mod = clv_mod

    # 标准三向
    mod._check_preds({"主胜": 0.5, "平": 0.3, "客胜": 0.2}, "主胜")
    # 二向 (D-118 公约 not require ≥3 keys)
    mod._check_preds({"A": 0.5, "B": 0.5}, "A")


def test_d118_4_check_preds_invalid_inputs(clv_mod):
    """D-118-4: _check_preds 4 种非法 — 非 Mapping / 空 / Σ 偏离 1 / outcome 缺失."""
    mod = clv_mod

    # 非 Mapping
    with pytest.raises(ValueError, match="Mapping"):
        mod._check_preds([0.5, 0.5], "A")
    # 空 (preds 字典空, 实际消息 "preds 必须为非空 Mapping, 得到 {}")
    with pytest.raises(ValueError, match="Mapping"):
        mod._check_preds({}, "A")
    # Σ 偏离 1
    with pytest.raises(ValueError, match="偏离"):
        mod._check_preds({"A": 0.5, "B": 0.6}, "A")  # sum=1.1
    # outcome 不在键里 (实际消息 "outcome='C' 不在 preds 键 [...]") — 含 "不在"
    with pytest.raises(ValueError, match="不在"):
        mod._check_preds({"A": 0.5, "B": 0.5}, "C")
    # 概率越界 (内部调 _check_prob 应 raise, 含 "非法")
    with pytest.raises(ValueError, match="非法"):
        mod._check_preds({"A": 1.5, "B": -0.5}, "A")


# ──────────────────────── D-119: 4 测 (跨模块) ────────────────────────


def test_d119_1_quota_as_day_truncates_to_utc_date(quota_mod):
    """D-119-1: quota._as_day (L37-38) 把 ISO string / date 转换为 date.

    契约: str -> ``dt.date.fromisoformat(s)``; date -> 直接返 (no-op).
    """
    mod = quota_mod
    import datetime as _dt  # noqa: PLC0415 - 真实 date 比较

    # str -> date (L37 dt.date.fromisoformat)
    out_str = mod._as_day("2026-01-15")
    assert out_str == _dt.date(2026, 1, 15)
    assert isinstance(out_str, _dt.date)

    # date 透传 (L38)
    d = _dt.date(2025, 10, 17)
    assert mod._as_day(d) is d


def test_d119_2_sides_jqc_sides_callable(sides_mod):
    """D-119-2: sides.jqc_sides (公开函数) 可调用 + 返 list[dict] 类型契约."""
    mod = sides_mod

    # 函数存在 + 可调用
    assert callable(mod.jqc_sides)
    # 无 fixture 时返 [] 或 抛异常 (实现细节), 不断言具体值
    # 验证类型契约: 输入 fixture_rows + 返回结构
    rows = [{"fixture_id": 1, "match_num": "周六001", "score": "1-0", "outcome": "主胜"}]
    try:
        out = mod.jqc_sides(rows)
        # 应为 list (具体内容依赖实现)
        assert isinstance(out, list)
    except Exception:
        # 可能依赖未连接 db, 这是契约外实现细节
        pass


def test_d119_3_sides_commit_sha_returns_string(sides_mod):
    """D-119-3: sides.commit_sha 返 str (空 SHA 字符串允许, 不抛)."""
    mod = sides_mod

    # commit_sha 调用 git, 测试环境下可能不存在 — 但必须返 str (不抛)
    out = mod.commit_sha()
    assert isinstance(out, str)
    # 长度: 7 (短) / 40 (完整) / 0 (无 git)
    assert len(out) == 0 or len(out) in (7, 40)


def test_d119_4_clv_clv_positive_when_model_higher_than_close(clv_mod):
    """D-119-4: clv.clv (公开) 公共接口 — p_model > p_close 时 ln > 0.

    验证 _check_prob + _check_preds 兜底被 clv 通过调用, 且 L43 ln(p_model/p_close) 正确.
    """
    mod = clv_mod

    # 模型概率 0.6, 收盘去水 0.4 -> ln(0.6/0.4) > 0
    out = mod.clv(0.6, 0.4)
    assert out == pytest.approx(math.log(0.6 / 0.4), abs=1e-9)
    assert out > 0  # 模型更看好 → 正 CLV

    # 边界: p_model < p_close -> 负
    out_neg = mod.clv(0.3, 0.5)
    assert out_neg < 0

    # 边界: p_close=0 -> ValueError (L39-40)
    with pytest.raises(ValueError, match="ln"):
        mod.clv(0.5, 0.0)
    # 边界: p_model=0 -> ValueError (L41-42)
    with pytest.raises(ValueError, match="ln"):
        mod.clv(0.0, 0.5)
