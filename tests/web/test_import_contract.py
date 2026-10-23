"""R47 回归守门：web 应用必须在**干净环境**下可导入。

背景（生产事故，已实测复现并修复）：
- `web/routers/admin.py` 在模块级 `from core import observability`（core 在 scripts/ 下）。
- 此前只有 `web/routers/jc.py` 会在模块级把 `scripts/` 加进 `sys.path`，
  而 `admin` 恰好排在 `jc` **之后** → 它的可导入性依赖导入顺序。
- R46 的 isort 自动排序把 `admin` 按字母序排到了 `jc` **之前** →
  `import web.api` 直接 `ModuleNotFoundError: No module named 'core'`，
  生产 `league-web.service` 无限重启（本地测试因 pytest 进程已带 scripts/ 路径而全绿）。

本测试用**全新子进程 + 清空 PYTHONPATH** 复现生产启动条件：
`python -c "import web.api"` 必须成功。
这样任何一次 import 重排 / 路径注入位置变动都会在 CI/本地被立刻抓到，
而不是等到生产重启才暴露。

同时直接测 `web.routers.admin` 单独可导入（自举后应与顺序无关）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _clean_env() -> dict:
    """构造不含 PYTHONPATH 的环境（生产 unit 里就没有 PYTHONPATH）。"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    # ⚠️ **P1（2026-10-23）**：子进程**不许**写生产 logs 目录。
    #    `web/api.py` 会装挂 observability（进程名 web）⇒ 默认目录 = FOOTBALL_DIR/logs
    #    = 生产目录 ⇒ 每跑一次这条用例就往生产 events.web.jsonl 里写几行
    #    （而它被 .gitignore 覆盖 ⇒ 污染隐形）。
    #    ⇒ LP_OUTPUT_DIR 决定 core.constants.FOOTBALL_DIR ⇒ 把它指到临时目录。
    out_root = Path(tempfile.gettempdir()) / "sports-lottery-test-root" / "import-contract"
    out_root.mkdir(parents=True, exist_ok=True)
    env["LP_OUTPUT_DIR"] = str(out_root)
    # 确保 cwd 是仓库根（生产的 WorkingDirectory）
    return env


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_import_web_api_in_clean_env():
    """干净环境下 `import web.api` 必须成功（生产 league-web.service 的启动条件）。"""
    r = _run("import web.api; print('OK')")
    assert r.returncode == 0, (
        "干净环境下 import web.api 失败 —— 生产 league-web.service 会无限重启：\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )
    assert "OK" in r.stdout


def test_import_admin_router_in_clean_env():
    """`web.routers.admin` 单独可导入（自举后与导入顺序无关）。"""
    r = _run("import web.routers.admin; print('OK')")
    assert r.returncode == 0, (
        "web.routers.admin 单独导入失败（依赖导入顺序）：\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )
    assert "OK" in r.stdout


def test_core_importable_after_web_api_import():
    """import web.api 之后 core 必须已在 sys.path（供 admin 等模块使用）。"""
    r = _run("import web.api, sys; print('scripts' in ' '.join(sys.path))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("True"), f"scripts/ 未进入 sys.path：{r.stdout!r}"
