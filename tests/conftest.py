"""pytest 根级 conftest —— 收敛跨测试复用的 fixture（+ 两条 autouse 安全边界）。

R17 #5: 之前 tests/test_jc_view_aggregations.py:29 + tests/test_jc_ops_view.py:27
两处重复实现 ``fake_pg`` fixture（jc_view / jc_ops_view 用的 fake read_conn
工厂，行为基本相同）。提取到本 conftest.py 让两测试共享，其它测试按需引用。

注意：本 conftest 的 ``fake_pg`` **不起 autouse**。``reset_obs`` / 临时目录隔离等
autouse fixture 仍由各测试文件本地持有（避免引入跨文件隐式依赖）。

⚠️ **两条 autouse 都是「安全边界」，不是「测试便利」**：
① ``isolate_i18n_cache``（2026-10-23 加）—— i18n 翻译缓存。
② ``isolate_production_writes``（2026-10-23 加）—— **测试绝不允许写生产仓库的文件**。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# scripts/（core / store / decision …）在 sys.path 上 —— 根 conftest 要 import
# `core.observability` 做写盘隔离。追加到**末尾**（不前置），避免遮蔽同名的
# 顶层包（与 web/api.py 的做法一致）。各测试文件自己的 `sys.path.insert(0, ...)`
# 不受影响（它们会先插到最前）。
_SCRIPTS_DIR = str(REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.append(_SCRIPTS_DIR)


# ── 模块级：早于任何测试模块 import ─────────────────────────────────────
# `core.log` 在 **import 时**就装挂 observability，写的是**生产** logs 目录
# （`FOOTBALL_DIR/logs/`）。conftest 先于测试模块被 import ⇒ 在这里就把
# 观测目录挪到临时目录，避免「收集阶段」就已经往生产文件里写东西。
# 事故复盘见下面 `isolate_production_writes` 的 docstring。
_TEST_ROOT = Path(tempfile.gettempdir()) / "sports-lottery-test-root"
_TEST_LOGS_DIR = _TEST_ROOT / "logs"


def _redirect_observability_to_temp() -> None:
    """把 observability 的**默认**目录从生产挪到临时目录。

    ⚠️ **P1（2026-10-23）改过的口径**：这里**不再**去改
    ``core.constants.FOOTBALL_DIR``（它由 ``LP_OUTPUT_DIR`` 决定，而那个同时决定
    「预测产物目录」⇒ 改了就读不到真预测文件 ⇒ 测试会假失败）。
    现在是**两层保险**：
      ① ``os.environ["LEAGUE_LOGS_DIR"]``（模块级，见下）⇒ 父进程与**继承 env 的子进程**
         都落临时目录；
      ② ``core.observability._default_logs_dir()`` 里的 ``PYTEST_CURRENT_TEST`` 兜底
         ⇒ **自带 env 的子进程**（实测：``subprocess.run(env={"PYTHONPATH": ...})``）
         也落临时目录（pytest 把 ``PYTEST_CURRENT_TEST`` 写进 os.environ ⇒ 子进程继承）。
    这里再显式 ``setup(dir=...)`` 一次，是为了让**本进程**的 handler 指向临时目录
    （不依赖任何 import 顺序）。
    """
    try:
        from core import observability as obs
    except Exception:  # noqa: BLE001
        return
    try:
        obs.reset_for_test()
        obs.setup(
            dir=_TEST_LOGS_DIR,
            process_name=os.environ.get("LEAGUE_PROCESS_NAME") or "predict",
            events_max_bytes=10 * 1024 * 1024, events_backup_count=5)
    except Exception:  # noqa: BLE001 - 隔离失败不该阻断测试收集
        pass


# ⚠️ **观测日志目录（进程级 env，子进程也继承）**：
#    `LEAGUE_LOGS_DIR` 只挪**观测日志**，**不动** `LP_OUTPUT_DIR`（那个决定预测产物目录，
#    改了就读不到真预测文件了）。
#    ⇒ 测试进程与它拉起的**所有子进程**的日志都落临时目录 ⇒ 生产 logs 目录零写入。
#    （实测：子进程（如 `python -m market.clv_calc`）也 import core.log ⇒ 也往生产写。）
os.environ["LEAGUE_LOGS_DIR"] = str(_TEST_LOGS_DIR)


# ⚠️ **进程名在测试进程里必须钉死**：web 进程入口（`web/api.py`）会
#    `os.environ.setdefault("LEAGUE_PROCESS_NAME", "web")` ⇒ 之后
#    `importlib.reload(core.log)` 算出的进程名会**漂移**（predict ⇄ web）⇒
#    观测文件名在会话中途换名 ⇒ 守门哨兵被自己的重装挂触发假失败。
#    ⇒ 测试进程统一用 "web"（与 web/api.py 的 setup 参数一致 ⇒ 不产生「装挂失败」噪音）。
os.environ.setdefault("LEAGUE_PROCESS_NAME", "web")

_redirect_observability_to_temp()


# 生产路径「不许被测试改写」的哨兵：跑一次全量 pytest 前后逐个比对
# (size, mtime_ns) ⇒ 变了就 **FAIL**（而不是让它悄悄写进去）。
# 实测事故（2026-10-23，Oracle 上跑一次全量后）：7 个生产文件被改 ——
#   scripts/logs/events.jsonl / events.predict.jsonl / runtime.log / runtime.predict.log
#   scripts/references/.calibration_state.json（**生产校准系数被测试值覆盖**）
#   web/.data/sessions.db（**生产会话库**）
#   仓库根的 `unused`（相对路径写盘）
# ⇒ 全部被 .gitignore 覆盖 ⇒ `git status` 恒为干净 ⇒ **污染完全隐形**。
_WATCH_FILES = (
    REPO_ROOT / "scripts" / "logs" / "events.jsonl",
    REPO_ROOT / "scripts" / "logs" / "events.predict.jsonl",
    REPO_ROOT / "scripts" / "logs" / "runtime.log",
    REPO_ROOT / "scripts" / "logs" / "runtime.predict.log",
    REPO_ROOT / "scripts" / "logs" / "events.web.jsonl",
    REPO_ROOT / "scripts" / "logs" / "runtime.web.log",
    REPO_ROOT / "scripts" / "logs" / "events.test.jsonl",
    REPO_ROOT / "scripts" / "logs" / "runtime.test.log",
    REPO_ROOT / "scripts" / "references" / ".calibration_state.json",
    REPO_ROOT / "web" / ".data" / "sessions.db",
    REPO_ROOT / "unused",
)


def _snapshot(paths) -> dict:
    out = {}
    for p in paths:
        try:
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            out[str(p)] = None
    return out


def _repo_root_files() -> set[str]:
    """仓库根的**文件**名集合（不含目录）。

    ⚠️ 为什么要它：**相对路径**写盘（``Path("unused")`` 这类占位）落点就是
    **CWD = 仓库根** ⇒ 仓库根凭空多出文件 ⇒ 被 .gitignore 覆盖 ⇒ 完全隐形。
    实测（2026-10-23）：`tests/test_bball_elo_save_ratings.py` 用
    ``sys.modules.setdefault("bball.config_bball", ...)`` 把假模块装进**全局**
    sys.modules（整个 session 生效），其中 ``DATA_DIR = Path("unused")``
    没有被 fixture 重定向 ⇒ **后续别的测试**（``test_bball_run.py``）把它写到了
    仓库根 ⇒ 哨兵抓到 `sports-lottery/unused`（108 字节）被改。
    ⇒ 守门从「盯已知路径」升级为「**仓库根不许凭空多出文件**」（不管它叫什么）。
    """
    out: set[str] = set()
    try:
        for p in REPO_ROOT.iterdir():
            if p.is_file():
                out.add(p.name)
    except OSError:
        pass
    return out


# ── 共享模块属性泄漏检测（用 pytest 钩子，不用 fixture）─────────────────
# ⚠️ **为什么不用 fixture**：autouse fixture 的 teardown 跑在 `monkeypatch` 的
#    还原**之前**（monkeypatch 是 isolate_production_writes 的依赖 ⇒ 先建后拆）
#    ⇒ 用 fixture 判「有没有还原」会**永远误报**。
#    ⇒ 改用钩子：`pytest_runtest_setup`（trylast ⇒ 所有夹具装完之后）拍快照，
#    `pytest_runtest_teardown`（trylast ⇒ 所有夹具拆完之后）比对 ⇒ 判得准。
#
# ⚠️ **2026-10-23 实测**：`tests/web/test_predictions_router_helpers.py` 里写了
# `ws.covers_date = lambda ...` / `ws.latest_by_league = lambda ...`
# —— **不走 monkeypatch** ⇒ 替身在**整个 pytest session 里一直生效** ⇒
# 后续所有用到这两个函数的用例测的都是 lambda、不是生产代码（**假阴性源**）。
# 默认字母序下受害者先跑 ⇒ 污染**完全隐形**；一旦换顺序（逆序/乱序）就冒出
# **15 个 order-dependent 失败**。
#
# 只盯**函数型属性**（`covers_date` 这类）—— 状态型属性（observability 的
# `_INSTALLED` 等）由各 fixture 自己管理；且 `importlib.reload()` 会重建函数
# 对象（值相同但身份不同）⇒ 一并排除。
_LEAK_WATCH_MODULES = (
    "web.services.store", "web.services.recommend", "web.services.clv",
    "web.services.odds_move", "web.routers.predictions",
    "web.routers.combo", "web.routers.ticket", "decision.recommend",
)
_LEAK_BEFORE: dict = {}


def _snapshot_module_funcs() -> dict:
    import sys
    import types as _types

    out: dict = {}
    for name in _LEAK_WATCH_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        out[name] = {k: v for k, v in vars(mod).items()
                     if isinstance(v, (_types.FunctionType, _types.BuiltinFunctionType))}
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    # ⚠️ 必须是 **tryfirst**（在 fixture 装好**之前**拍快照）：
    #    有些 fixture 自己就 `monkeypatch.setattr(ws, "latest_by_league", ...)`
    #    ⇒ 快照若在 fixture 之后拍，就把「替身」当成基线 ⇒ 还原后反而报泄漏（假阳性）。
    global _LEAK_BEFORE
    _LEAK_BEFORE = _snapshot_module_funcs()


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    after = _snapshot_module_funcs()
    leaked: list[str] = []
    for name, snap in _LEAK_BEFORE.items():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in snap.items():
            if getattr(mod, k, None) is not v:
                leaked.append(f"{name}.{k}")
    if leaked:
        pytest.fail(
            "测试把共享生产模块的函数换成了替身且没有还原"
            "（⇒ 替身会在整个 session 里生效，污染后续用例）：\n  " + "\n  ".join(leaked))


@pytest.fixture(autouse=True)
def isolate_production_writes(tmp_path, monkeypatch):
    """**autouse**：把测试的写盘目标从「生产仓库」挪到「临时目录」，并**守门**。

    ⚠️ **2026-10-23 实测事故**：在 Oracle 上跑一次全量 pytest ⇒ **7 个生产文件被改**：
      ① `scripts/logs/events.jsonl` / `events.predict.jsonl`（+523KB / +922KB 测试事件）
         —— 而 `scripts/core/ml_feedback.py:136` 的 `join_predictions_outcomes`
         **正是从 events.jsonl 读 ML 训练样本** ⇒ **测试事件会进训练集**；
      ② `scripts/references/.calibration_state.json` ⇒ **生产校准系数被测试值覆盖**
         （实测被写成 `home_correction: 1.176 / sample_size: 6`）⇒ 后续预测的修正量被改；
      ③ `web/.data/sessions.db` ⇒ **生产会话库被改**；
      ④ 仓库根的 `unused` ⇒ 相对路径写盘。
    ⇒ 7 个路径**全部**被 .gitignore 覆盖 ⇒ `git status` 永远干净 ⇒ 污染隐形。

    本 fixture 做三件事：
      ① observability 事件流 → 挪到 `tmp_path/logs`（core.log 的 import-time 装挂
         指向生产目录，不挪的话每条日志都在写生产文件）；
      ② `core.calibration.FOOTBALL_DIR` → `tmp_path`（校准状态文件不再落生产）；
      ③ `web.config.SESSION_DB_PATH` → `tmp_path`（会话库不再落生产）；
      ④ **守门**：测试前后比对生产路径的 (size, mtime_ns) ⇒ 变了就 FAIL。
         （光靠上面三条重定向只能覆盖已知路径；守门是**兜底**，覆盖未知的漏网路径。）
    """
    from core import observability as obs  # noqa: F401 - 确保 core 可导入

    # ⚠️ **这里刻意「不」重新装挂 observability**：
    #    装挂状态是**进程级**的（`_INSTALLED` / `_PROCESS_NAME_USED`），
    #    每个用例都重装 ⇒ ① 与「自己控制装挂」的观测性用例直接撞
    #    （`obs.setup(dir=...)` 会因 process_name 不一致而 RuntimeError），
    #    ② 还会把观测文件写进用例自己的 tmp_path ⇒ 撞掉别的用例的文件数断言。
    #    ⇒ 观测目录由**模块级**统一兜住（`core.constants.FOOTBALL_DIR` 已指向临时根
    #    ⇒ 任何一次 setup(dir=None) 都落临时目录）；这里只做「万一被卸了也补回来」。

    # ② 校准状态文件 → tmp（core.calibration 用模块级 FOOTBALL_DIR）
    try:
        import core.calibration as calib
        monkeypatch.setattr(calib, "FOOTBALL_DIR", tmp_path)
    except Exception:  # noqa: BLE001
        pass

    # ③ web 会话库 → tmp。
    #    ⚠️ 必须**同时**设 env：很多用例用 `importlib.reload(web.config)` 重载配置模块
    #    （重载会**重建**模块常量 ⇒ 直接 patch 常量会被 reload 覆盖回生产路径）。
    #    设 env ⇒ 任何一次重载读到的都是 tmp 路径。
    try:
        import web.config as web_config
        monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "sessions.db"))
        monkeypatch.setattr(web_config, "SESSION_DB_PATH", str(tmp_path / "sessions.db"))
    except Exception:  # noqa: BLE001
        pass

    # ④ AI 富化 checkpoint → tmp（2026-10-28 起每批落盘；绝不能写进生产
    #    predictions/.ai_enrich_checkpoint.json，否则测试进度会污染当天真实富化）。
    try:
        import web.enrich as enrich_mod
        monkeypatch.setattr(
            enrich_mod, "CHECKPOINT_FILE", tmp_path / ".ai_enrich_checkpoint.json"
        )
    except Exception:  # noqa: BLE001
        pass

    # ⑤ 守门：前后快照
    before = _snapshot(_WATCH_FILES)
    root_before = _repo_root_files()
    yield
    after = _snapshot(_WATCH_FILES)
    changed = [p for p in before if before[p] != after[p]]
    if changed:
        pytest.fail(
            "测试改了生产仓库的文件（安全边界被打破）：\n  " + "\n  ".join(
                f"{p}  {before[p]} -> {after[p]}" for p in changed))
    # ⑤ 仓库根不许凭空多出文件（相对路径写盘的兜底守门，见 _repo_root_files 的说明）
    root_new = sorted(_repo_root_files() - root_before)
    if root_new:
        pytest.fail(
            "测试在仓库根凭空写出了文件（多半是**相对路径**写盘，"
            "落点 = CWD = 仓库根）：\n  " + "\n  ".join(root_new)
            + "\n  ⇒ 请把占位路径改成绝对临时路径（参考 "
              "tests/test_bball_elo_save_ratings.py 的 _UNUSED 写法）。")


@pytest.fixture(autouse=True)
def isolate_i18n_cache(tmp_path, monkeypatch):
    """**autouse**：把 i18n 的 LLM 翻译缓存文件重定向到临时目录（每个用例独立）。

    ⚠️ **2026-10-23 加（真实事故）**：Oracle 上跑全量 pytest 后，
    仓库里的 ``scripts/references/team_translations.json`` **被测试改了**
    （多出「阿森纳 vs 切尔西」「Liverpool」「Everton」等测试夹具队名）。

    根因链：
    1. ``scripts/core/data/parse.py::_warm_team_names`` → 真调
       ``core.i18n.warm_translations`` → ``translate_team_names`` → **真调 LLM** →
       ``save_translations`` → **写 ``TRANSLATION_CACHE_FILE``**；
    2. ``TRANSLATION_CACHE_FILE`` 指向**仓库内的生产文件**；
    3. 生产机（Oracle）**有 LLM 凭证** ⇒ 翻译**真的成功** ⇒ 真的落盘；
       开发机（无凭证）翻译失败 ⇒ 不落盘 ⇒ **本机跑不出这个污染**（环境差异）。
    ⇒ 触发路径不止一处：任何调 ``parse_events`` / ``_warm_team_names`` 的用例都可能中招
    （实测 ``tests/test_ucl_league_support.py`` 是其中一个）。

    ⇒ 本 fixture 在**每个用例**开始时把 ``TRANSLATION_CACHE_FILE`` 指到 ``tmp_path``、
    并清掉模块级 ``_CACHE`` ⇒ **测试永远碰不到生产文件**（无论环境有没有 LLM 凭证）。
    """
    try:
        import core.i18n as i18n_mod
    except Exception:  # noqa: BLE001 - 脚本不在 sys.path 时跳过（不阻断其它用例）
        yield
        return
    orig_file = i18n_mod.TRANSLATION_CACHE_FILE
    orig_cache = i18n_mod._CACHE
    try:
        i18n_mod.TRANSLATION_CACHE_FILE = tmp_path / "team_translations.json"
        i18n_mod._CACHE = None
        yield
    finally:
        i18n_mod.TRANSLATION_CACHE_FILE = orig_file
        i18n_mod._CACHE = orig_cache


@pytest.fixture
def fake_pg():
    """返回 fake_read_conn 工厂：queue 按 cursor.execute 调用次数 pop。

    用法::

        def test_x(fake_pg):
            fake_read_conn = fake_pg([_TEAMS_OK, _TOPICS_OK, ...])
            with patch.object(jc_view, "read_conn", fake_read_conn):
                result = jc_view.ops_snapshot(...)
    """
    def _make(rows: list):
        queue = list(rows)

        def fake_read_conn(role: str = "ro"):
            class _Cursor:
                row_factory = None

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def execute(self_inner, sql, params=None):
                    self_inner._next = queue.pop(0) if queue else []

                def fetchone(self_inner):
                    """R18 #3: 单列查询走 fetchone() 而非 fetchall()。

                    queue 元素 = list[dict]（fetchall）/ list[tuple]（fetchone）
                    / Exception（raise）。list[tuple] 时取 [0]；空 → None。
                    """
                    nxt = getattr(self_inner, "_next", [])
                    if isinstance(nxt, Exception):
                        raise nxt
                    if isinstance(nxt, list) and nxt:
                        return nxt[0]
                    return None

                def fetchall(self_inner):
                    nxt = getattr(self_inner, "_next", [])
                    if isinstance(nxt, Exception):
                        raise nxt
                    return list(nxt)

            class _Conn:
                def cursor(self_inner):
                    return _Cursor()

            class _Ctx:
                def __enter__(self_inner):
                    return self_inner.conn

                def __exit__(self_inner, *exc):
                    return False

            ctx = _Ctx()
            ctx.conn = _Conn()
            return ctx

        return fake_read_conn

    return _make
