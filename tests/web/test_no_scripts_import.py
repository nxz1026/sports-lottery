"""契约 §5 静态守护：web 层**顶层**绝不容许 `from scripts.*` / `from ai.*`。

- 函数体内 `try/except` 包裹的 lazy import 是文档化例外（WO-M6R.md §1），
  本守护不查函数体 / try 块内节点，只在模块顶层 Import / ImportFrom 上 fail。
- 已知例外：`web/enrich.py`（enrich job CLI 是文档化允许路径）。

新增 web/ 模块时若必须 `from scripts.*`：
  1) 先尝试把被依赖函数移到 `web/services/*`（参考已有 `web/services/ai._score_key` 副本模式）；
  2) 实在无法迁移 → 在本文件 ALLOWLIST 中加白，并请同步更新 README 契约 §5 的例外清单。
"""
from __future__ import annotations

import ast
from pathlib import Path

# 仓库根 + web/ 子目录
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"

# 文档化例外：lazy import 在 web 模块内是被显式允许的，本守护只挡顶层。
# 这里列的是**整个文件**允许顶层 import scripts/ai 的白名单（当前空）。
ALLOWLIST: set[str] = set()

# 顶层禁止出现的模块名前缀
FORBIDDEN_TOP_LEVEL_PREFIXES = ("scripts", "ai")


def _module_top_level_violations(py_path: Path) -> list[str]:
    """返回该 .py 文件**模块顶层** Import/ImportFrom 中所有违反契约的模块名。"""
    src = py_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(py_path))
    except SyntaxError as exc:
        # 语法错误不在本守护管辖范围（其它测试 / CI 会捕）；明示跳过。
        return [f"<SyntaxError: {exc}>"]
    violations: list[str] = []
    # 仅遍历模块 body 的直接子节点；函数体 / try 块 / if 块内的 import 不算"顶层"
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_and_append(violations, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # 顶层 relative import（from .foo import ...）不算 scripts/ai 违规，跳过
            if node.level:
                continue
            _check_and_append(violations, module)
    return violations


def _check_and_append(out: list[str], module: str) -> None:
    """若 module 命中禁前缀，加进去（截到前缀 + 1 段，便于定位）。"""
    if not module:
        return
    head = module.split(".", 1)[0]
    if head in FORBIDDEN_TOP_LEVEL_PREFIXES:
        out.append(module)


def _iter_web_py() -> list[Path]:
    """递归 web/ 下所有 .py，排除 __pycache__。"""
    return sorted(p for p in WEB_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_top_level_scripts_or_ai_import_in_web() -> None:
    """web/ 每个 .py 文件的**模块顶层**都不应出现 scripts.* / ai.* import。"""
    offenders: list[str] = []
    for py in _iter_web_py():
        rel = py.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWLIST:
            continue
        for mod in _module_top_level_violations(py):
            offenders.append(f"{rel}: {mod}")
    assert not offenders, (
        "契约 §5 违规：web 层**顶层**出现 scripts/ai import。"
        " 函数体内的 lazy-in-try 是文档化例外（WO-M6R.md），不在本守护范围。"
        " 修复方法：① 把依赖函数搬到 web/services/*；② 改函数体 lazy import + try/except；"
        " ③ 实在不行在 ALLOWLIST 加白并更新 README §5。\n  - " + "\n  - ".join(offenders)
    )


def test_allowlist_is_current() -> None:
    """白名单注释若与文件实际状态不符，pytest 也能看到——白名单为空即预期。"""
    # 当前已知例外 web/enrich.py 是 lazy import（不在顶层），所以 ALLOWLIST 为空。
    # 保留此断言以备未来新增白名单时主动确认。
    assert ALLOWLIST == set(), (
        "ALLOWLIST 非空：请同步更新 README §5 的文档化例外清单与本测试 docstring。"
    )
