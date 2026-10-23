"""P1（2026-10-23）：进程名隔离 + 子进程自举。

两件事，都是**生产实测**出来的：

① **观测文件名后缀写死 "predict"** ⇒ web 进程的 `observability.setup(process_name="web")`
   被 `core.log` 先跑的 `setup(process_name="predict")` 顶掉（重入守门 ⇒ RuntimeError）
   ⇒ **生产上从来没有 `events.web.jsonl` / `runtime.web.log`**，
   web 事件写进了 predict 子进程的文件（H9 跨进程隔离失效），
   而 `web/api.py` 的注释却在宣称「web 进程独立写」—— **注释在撒谎**。

② **`ai_analyze` 子进程缺 `scripts/` 自举** ⇒ 生产实测
   `ModuleNotFoundError: No module named 'core'` ⇒ **exit 1**，
   而看板的 AI 面板却显示「尚未生成。先点『生成当日分析』」（面板也在撒谎）。

本文件用**真实子进程**验证两件事（不是字符串匹配）：
- `import core.log` 在不同 `LEAGUE_PROCESS_NAME` 下拿到不同的进程名；
- `import web.services.ai_analyze` 在**只有仓库根**在 sys.path 的干净子进程里也不炸。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(code: str, env_extra: dict | None = None,
         pythonpath: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 关键：默认把 scripts/ 从 PYTHONPATH 里摘掉 ⇒ 子进程只剩仓库根
    # （= 生产实况：`python -m web.services.ai_analyze` 的 sys.path[0] 就是 cwd=仓库根）。
    # 需要「引擎实况」（PYTHONPATH=scripts）时显式传 pythonpath。
    env.pop("PYTHONPATH", None)
    # ⚠️ 子进程**不许**继承测试进程的 LEAGUE_PROCESS_NAME（那是 "test"）⇒
    #    否则「默认进程名是 predict」这条根本测不到。
    env.pop("LEAGUE_PROCESS_NAME", None)
    # ⚠️ 子进程**不许**写生产 logs 目录：LP_OUTPUT_DIR 决定 core.constants.FOOTBALL_DIR
    #    ⇒ 观测文件落临时目录（否则每跑一次这条用例就往生产 events 里写几行）。
    out_root = Path(tempfile.gettempdir()) / "sports-lottery-test-root" / "subproc"
    out_root.mkdir(parents=True, exist_ok=True)
    env["LP_OUTPUT_DIR"] = str(out_root)
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_core_log_process_name_defaults_to_predict():
    """不设 env ⇒ 引擎进程名仍是 "predict"（既有契约不变）。"""
    p = _run("import core.log as l; print(l.PROCESS_NAME)",
             pythonpath=str(REPO / "scripts"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip().splitlines()[-1] == "predict"


def test_core_log_process_name_follows_env():
    """设了 LEAGUE_PROCESS_NAME ⇒ 用它（这正是 web / ai_analyze 的隔离手段）。"""
    p = _run("import core.log as l; print(l.PROCESS_NAME)",
             {"LEAGUE_PROCESS_NAME": "web"}, pythonpath=str(REPO / "scripts"))
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip().splitlines()[-1] == "web"


def test_ai_analyze_module_imports_without_scripts_on_syspath():
    """`import web.services.ai_analyze` 在干净子进程里**不许** ModuleNotFoundError。

    这正是生产那条 exit 1 的根因：子进程的 sys.path[0] 只有仓库根，
    而 `core` / `store` 都在 `scripts/` 下面 ⇒ 模块必须**自己**自举。
    """
    p = _run("import web.services.ai_analyze as m; print('IMPORT_OK', m.OUTPUT_DIR.name)")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "IMPORT_OK" in p.stdout
    assert "ModuleNotFoundError" not in (p.stdout + p.stderr)


def test_ai_analyze_bootstrap_puts_scripts_on_syspath():
    """自举后 `scripts/` 必须在 sys.path 上（否则只是碰巧没炸）。"""
    p = _run(
        "import sys, web.services.ai_analyze as m; "
        "print(any(pp.replace('\\\\','/').endswith('/scripts') for pp in sys.path))")
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip().splitlines()[-1] == "True"


def test_ai_analyze_sets_its_own_process_name():
    """ai_analyze 子进程必须用自己的观测文件名（不与 predict 抢）。"""
    p = _run("import os, web.services.ai_analyze as m; print(os.environ['LEAGUE_PROCESS_NAME'])")
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip().splitlines()[-1] == "ai_analyze"


def test_ai_enrich_sets_its_own_process_name():
    """ai_enrich 子进程同理。"""
    p = _run("import os, web.enrich as m; print(os.environ['LEAGUE_PROCESS_NAME'])")
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.strip().splitlines()[-1] == "ai_enrich"


def test_web_api_sets_its_own_process_name():
    """web 进程入口必须把进程名定成 "web"（且在任何 core 导入之前）。"""
    src = (REPO / "web" / "api.py").read_text(encoding="utf-8")
    assert 'LEAGUE_PROCESS_NAME' in src
    idx_env = src.index("LEAGUE_PROCESS_NAME")
    # 必须在第一个 `from web.` / `from core` 导入之前
    first_import = min(
        [i for i in (src.find("from web."), src.find("from core")) if i >= 0] or [10 ** 9])
    assert idx_env < first_import, "LEAGUE_PROCESS_NAME 设得太晚 ⇒ core.log 已经装成 predict 了"
