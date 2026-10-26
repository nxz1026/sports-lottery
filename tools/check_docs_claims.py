"""R46: 文档声明校验器 —— 把 README/AGENTS/SKILL 里**可验证的声明**逐条实测对照。

为什么要有这个：R43/R44/R45 连续三轮出现"文档听起来合理但与代码矛盾"的声明
（timer 09:00 vs 09:30、13 处 vs 14 处、pytest 须 PYTHONPATH vs 实际不需要）。
本脚本把其中可机械校验的部分固化下来，改文档后重跑即可发现新的漂移。

用法：python tools/check_docs_claims.py
退出码 0 = 全部一致；1 = 有 FAIL。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# 本脚本输出含中文断言名；Windows 下管道默认走本地 codepage(gbk)，终端会显示乱码，
# 而 pre-push hook 正是把它当门禁输出给用户看的。强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results: list[tuple[str, str, str]] = []   # (状态, 声明, 实测)


def check(name: str, expected, actual) -> None:
    """登记一条断言。

    ``expected`` = 从**代码/仓库**实测出来的真值；``actual`` = 从**文档**里读出来的声明值。
    失败时按「声明 vs 实际」打印 ⇒ 声明在前、实测在后（R49 修：原先两个标签印反了，
    排障时会照着反的提示去改错地方）。
    """
    ok = expected == actual
    results.append(("PASS" if ok else "FAIL", name, f"声明={actual!r} 实际={expected!r}"))


def check_rc(name: str, expected_rc: int, actual_rc: int) -> None:
    """登记一条**代码侧**二值断言（没有「文档声明」可对照时用）。

    别套用 check() 的「声明 vs 实际」措辞：那边 expected=实测真值、actual=文档声明，
    而这里两个数**都是代码侧** ⇒ 直接写「期望退出码 / 实测退出码」。
    （R49 修：ruff 那条曾把 实测 rc=1 印成「声明=1 实际=0」，排障时会被引到错的方向。）
    """
    ok = expected_rc == actual_rc
    results.append(("PASS" if ok else "FAIL", name,
                    f"期望退出码={expected_rc!r} 实测退出码={actual_rc!r}"))


def skip(name: str, why: str) -> None:
    """登记一条「本次未检查」。

    环境缺工具 **≠** 检查不通过：生产机 venv 里没装 ruff 时，「没检查」不能印成「ruff 不绿」
    （R49 修：Oracle 上因此平白报 1 项 FAIL）。
    """
    results.append(("SKIP", name, why))


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# 订正/否定标记：带这些词的行是在**记录**旧写法或否定旧说法，不是陈述现状。
# 文档天然会大量引用自己的历史（R43-R47 各段都引用旧写法），所以「全文不得出现
# 该串」这种朴素规则会把订正记录本身判为违规（R46 的 09:00 规则已踩过这个坑）。
CORRECTION_MARKERS = (
    "订正", "误写", "此前", "旧写法", "原写法", "原文", "错误写法", "不成立",
    "已修复", "已恢复", "已移除", "已不存在", "漏改", "不走", "不涉及",
    "无此单元", "真名", "错记录", "不再", "负向精确串",
    "没有可用登录入口", "缺少可用的登录入口", "缺少可用登录入口",
)


def stale_lines(text: str, phrases: list[str]) -> list[int]:
    """返回「命中错误写法 **且** 该行无任何订正/否定标记」的行号。

    这样既挡住"文档把旧写法当现状陈述"，又不会误伤"订正记录里引用旧写法"。
    """
    out: list[int] = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(p in line for p in phrases) and not any(m in line for m in CORRECTION_MARKERS):
            out.append(i)
    return out


# ── AST 工具：守门断言必须**精确判定代码行为**，不能靠字符串匹配 ──────────
# 为什么：源码里的 docstring / 注释会逐字引用旧写法（订正记录、教学价值），
#        朴素字符串匹配会命中自己的说明 ⇒ 假失败（R46 的 09:00 规则已踩过）。
#        「函数里出现了某个字符串」≠「函数会用那个字符串」—— 要看的是
#        **代码**（可执行语句）里的常量，不是文档里的。

def _docstring_ids(tree: ast.AST) -> set[int]:
    """收集该模块里所有 docstring 常量节点的 id（用来把它们从「代码字符串」里排除）。"""
    ids: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(n, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def code_strings(p: Path) -> set[str]:
    """返回该文件**代码**（不含 docstring）里用到的字符串常量集合。"""
    tree = ast.parse(read(p))
    doc_ids = _docstring_ids(tree)
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            out.add(n.value)
    return out


def find_fn(p: Path, name: str):
    """按名字找函数定义节点（找不到返回 None）。"""
    tree = ast.parse(read(p))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def fn_code_strings(p: Path, name: str) -> set[str]:
    """函数体**代码**里的字符串常量（排除它自己的 docstring）。"""
    fn = find_fn(p, name)
    if fn is None:
        return set()
    doc_ids = _docstring_ids(fn)
    out: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_ids:
            out.add(n.value)
    return out


def fn_imports(p: Path, name: str) -> set[str]:
    """函数体里 import 的模块名集合（``from X import Y`` ⇒ X；``import X`` ⇒ X）。"""
    fn = find_fn(p, name)
    if fn is None:
        return set()
    mods: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
        elif isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name)
    return mods


def top_level_imports(p: Path) -> set[str]:
    """**模块顶层**（不在函数体内）的 import 模块名集合。"""
    tree = ast.parse(read(p))
    mods: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
        elif isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name)
    return mods


def env_setdefaults(p: Path) -> set[tuple[str, str]]:
    """返回 ``os.environ.setdefault("VAR", "VALUE")`` 的 (VAR, VALUE) 集合。

    ⚠️ 为什么要专门写这个：`setdefault("LEAGUE_PROCESS_NAME", "web")` 的
    两个参数是**两个独立的字符串常量** ⇒ 用 `code_strings()` 查
    「有没有同时含 VAR 和 VALUE 的那个串」永远查不到 ⇒ 断言会**永远红**。
    """
    out: set[tuple[str, str]] = set()
    tree = ast.parse(read(p))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        if n.func.attr != "setdefault":
            continue
        target = n.func.value
        if not (isinstance(target, ast.Attribute) and target.attr == "environ"):
            continue
        if len(n.args) >= 2 and all(isinstance(a, ast.Constant) and isinstance(a.value, str)
                                    for a in n.args[:2]):
            out.add((n.args[0].value, n.args[1].value))
    return out


def touches_sys_path(p: Path) -> bool:
    """文件里是否碰过 ``sys.path``（属性访问，不是字符串常量）。"""
    tree = ast.parse(read(p))
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == "path" \
                and isinstance(n.value, ast.Name) and n.value.id == "sys":
            return True
    return False


def sql_statements(sql_text: str) -> list[str]:
    """按分号切 SQL 语句（调用方先把整行注释剥掉）。

    ⚠️ 为什么需要：psql 脚本里**报告查询**与**真正的 DELETE** 常常含**同样的子串**
    （例：``WHERE ok AND processed_at`` 两边都有）⇒ 只做「全文有没有这个串」的
    判定 ⇒ 把 DELETE 里的 ``ok AND`` 删掉也照样绿（审计实测：13 个变异漏网之一）。
    ⇒ 必须**定位到那一条语句**再判它自己的谓词。
    """
    return [s.strip() for s in sql_text.split(";") if s.strip()]


def fn_calls(p: Path, name: str) -> set[str]:
    """函数体里**调用**的函数名（点号全名，如 ``uuid.uuid4`` / ``os.replace``）。

    ⚠️ 为什么需要：断言「_write_job 必须用唯一 tmp 名」时，要判定的是
    **调用了** ``uuid.uuid4()``，而不是「文件里出现过 uuid 这个字样」
    （docstring 里写一遍就假绿了 —— 审计实测的 13 个假阴性之一）。
    """
    fn = find_fn(p, name)
    if fn is None:
        return set()
    out: set[str] = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            parts: list[str] = []
            cur: ast.AST = f
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            out.add(".".join(reversed(parts)))
        elif isinstance(f, ast.Name):
            out.add(f.id)
    return out


def fn_handles(p: Path, name: str, exc_name: str) -> bool:
    """函数体里是否有 ``except <exc_name>``（AST 判定，免疫注释/docstring）。"""
    fn = find_fn(p, name)
    if fn is None:
        return False
    for n in ast.walk(fn):
        if isinstance(n, ast.ExceptHandler) and n.type is not None:
            t = n.type
            if isinstance(t, ast.Name) and t.id == exc_name:
                return True
            if isinstance(t, ast.Tuple) and any(
                    isinstance(x, ast.Name) and x.id == exc_name for x in t.elts):
                return True
    return False


def fn_names(p: Path, name: str) -> set[str]:
    """函数体里引用到的**名字**（如 ``STATUS_FAILED`` / ``executor``）。"""
    fn = find_fn(p, name)
    if fn is None:
        return set()
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}


def js_fn(js_text: str, name: str) -> str:
    """从 JS 源码里抠出某个函数的**函数体**（按花括号配平）。

    ⚠️ 为什么需要：看板的「未登录分支」要判定的是**那段代码**，
    而不是「整个文件里有没有出现过 location.href=LOGIN」
    （后者会被别的函数/注释顶替 ⇒ 假绿）。
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", js_text)
    if not m:
        return ""
    start = js_text.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(js_text)):
        if js_text[i] == "{":
            depth += 1
        elif js_text[i] == "}":
            depth -= 1
            if depth == 0:
                return js_text[start:i + 1]
    return ""



def js_code(html_text: str) -> str:
    """抽出页面里**最后一个内联 <script>**，并**去掉注释**。

    ⚠️ 为什么需要：看板的修复说明都写在 ``//`` 注释里 ⇒ 直接对 HTML 做
    「有没有这个串」的判定 ⇒ **注释里写一遍就假绿**（审计实测：13 个假阴性之一）。
    ⇒ 判定前必须把注释剥掉，只看**真正会执行**的代码。
    """
    blocks = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)</script>", html_text)
    if not blocks:
        return ""
    code = blocks[-1]
    code = re.sub(r"/\*[\s\S]*?\*/", " ", code)     # 块注释
    code = re.sub(r"(?m)^\s*//.*$", " ", code)        # 整行行注释
    return code



def js_set_interval_ms(js_text: str) -> int | None:
    """解析 ``setInterval(...)`` 的**周期实参**（毫秒）。

    ⚠️ 为什么不能直接上正则 ``setInterval\\(...,\\s*(\\d+)\\)``：
    回调体里**含分号**（``if(document.hidden)return;``）⇒ ``[^;]`` 量不过去
    ⇒ 永远 MISSING（审计实测：这条断言写出来就是红的，**从来没绿过**）。
    ⇒ 改成**按括号配平**抠出整个调用实参，再取最后一个逗号后的数字。
    """
    for m in re.finditer(r"setInterval\s*\(", js_text):
        start = m.end() - 1
        depth = 0
        for i in range(start, len(js_text)):
            ch = js_text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    args = js_text[start + 1:i]
                    cut = -1
                    d2 = 0
                    for k, c2 in enumerate(args):
                        if c2 in "([{":
                            d2 += 1
                        elif c2 in ")]}":
                            d2 -= 1
                        elif c2 == "," and d2 == 0:
                            cut = k
                    tail = args[cut + 1:] if cut >= 0 else args
                    mm = re.search(r"(\d+)\s*$", tail.strip())
                    if mm:
                        return int(mm.group(1))
                    break
    return None


def main() -> int:
    readme = read(ROOT / "README.md")
    agents = read(ROOT / "AGENTS.md")
    skill = read(ROOT / "SKILL.md")

    # ---- 1. 版本号三方一致 ----
    init = read(SCRIPTS / "core" / "__init__.py")
    ver = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    skill_ver = re.search(r"^version:\s*(\S+)", skill, re.M).group(1)
    check("SKILL.md version == core.__version__", ver, skill_ver)
    check("README 声明的引擎版本 == core.__version__", ver,
          ver if f'`{ver}`' in readme or f'"{ver}"' in readme else "MISSING")

    # ---- 2. timer 触发时刻 ----
    timer = read(ROOT / "ops" / "league-daily-predict.timer")
    m = re.search(r"OnCalendar=\*-\*-\*\s+(\d{2}:\d{2}):\d{2}\s+(\S+)", timer)
    check("timer OnCalendar 时刻", "09:30", m.group(1))
    check("timer 时区", "Asia/Shanghai", m.group(2))
    # README 不得把 09:00 描述为**现行**触发时刻。
    # 放行三类行：
    #   1) 同行同时出现 09:30 —— 该行必然是在做"09:00 → 09:30"的对比/订正记录
    #      （含订正表格行、审计记录里 "timer 09:00 vs 09:30" 的措辞），而不是陈述现行值；
    #   2) 旧 apscheduler 故障复盘（next_run / 对照实验 / CronTrigger / BackgroundScheduler）；
    #   3) 带订正/否定标记的行（与其他"已知错误写法"检查同一把尺，见 CORRECTION_MARKERS）。
    # 早期版本靠逐个追加关键词（"为 **09:30**""实为"…），R46 自己新写的段落就被误报了一次，
    # 改为「同行有 09:30 即放行」+ 行级订正标记两条规则。
    bad = []
    for i, l in enumerate(readme.splitlines(), 1):
        if "09:00" not in l or "09:30" in l:
            continue
        if any(k in l for k in ("next_run", "对照实验", "CronTrigger", "BackgroundScheduler")):
            continue          # 旧实现故障复盘
        if any(m in l for m in CORRECTION_MARKERS):
            continue          # 订正/否定语境
        bad.append(i)
    check("README 无残留 09:00 现行触发声明", [], bad)

    # ---- 3. systemd 单元名 ----
    check("ops/ 下存在 league-web.service", True, (ROOT / "ops" / "league-web.service").is_file())
    stale = stale_lines(readme, ["league-dashboard.service"])
    check("README 无残留 league-dashboard.service 现行引用", [], stale)

    # ---- 4. 部署路径 ----
    svc = read(ROOT / "ops" / "league-daily-predict.service")
    env_path = re.search(r"EnvironmentFile=(\S+)", svc).group(1)
    check("README 部署路径与 unit EnvironmentFile 一致",
          env_path, env_path if env_path in readme else f"README 未提及 {env_path}")

    # ---- 5. 测试基线 ----
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                         cwd=ROOT, capture_output=True, text=True, timeout=600).stdout
    collected = int(re.search(r"(\d+) tests? collected", out).group(1))
    for label, text in (("README", readme), ("AGENTS", agents)):
        m2 = re.search(r"(\d+) collected", text)
        check(f"{label} 声明 collected 数 == 实测", collected,
              int(m2.group(1)) if m2 else "MISSING")

    # ---- 6. ruff 门禁 ----
    # ruff 没装（生产机 venv / Oracle）⇒ 本项**未检查**，不是「检查不通过」⇒ 记 SKIP，不阻断。
    if importlib.util.find_spec("ruff") is None:
        skip("ruff check . 退出码为 0（门禁已绿）", "当前环境未安装 ruff ⇒ 本项未检查（非失败）")
    else:
        rc = subprocess.run([sys.executable, "-m", "ruff", "check", "."],
                            cwd=ROOT, capture_output=True, text=True).returncode
        check_rc("ruff check . 退出码为 0（门禁已绿）", 0, rc)
    check("AGENTS.md 不再声称 ruff 非全绿", 0, agents.count("勿期待全绿") + agents.count("当前基线非 0"))

    # ---- 7. 前端 API 基址行号 ----
    dash = read(ROOT / "static" / "dashboard.html").splitlines()
    real_line = next((i + 1 for i, l in enumerate(dash) if "const API=" in l), None)
    claimed = re.search(r"dashboard\.html:(\d+)", readme)
    check("README 引用的 dashboard.html 行号正确", real_line,
          int(claimed.group(1)) if claimed else "MISSING")

    # ---- 8. 常量声明 ----
    consts = read(SCRIPTS / "core" / "constants.py")
    n_sim = re.search(r"DEFAULT_N_SIMULATIONS:\s*int\s*=\s*(\d+)", consts).group(1)
    check("README 提到的 10k 次模拟 == DEFAULT_N_SIMULATIONS", "10000", n_sim)
    past = re.search(r"DEFAULT_PAST_DAYS:\s*int\s*=\s*(\d+)", consts).group(1)
    check("README 提到的 past-days 默认 30", "30", past)

    # ---- 9. ML 特征维数 ----
    tree = ast.parse(read(SCRIPTS / "core" / "model" / "features.py"))
    dims = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "FEATURE_COLUMNS":
            dims = len(node.value.elts)
    check("README 声明的 26 维特征 == FEATURE_COLUMNS 长度", 26, dims)

    # ---- 10. 凭据不得入库 ----
    # 本脚本自身包含这些字面量（作为待查模式），故排除自身。
    # 只扫"人写的文件"：模型权重 json（scripts/references/*.json）里出现 4 位数字
    # 属正常浮点，不是凭据（实测 ml_model_seriea.json 命中 "9659"）。
    self_path = Path(__file__).resolve()
    leaks = []
    for pat in ("ndjack", "AUTH_PASSWORD=123", "3db1", "9659"):
        for f in ROOT.rglob("*"):
            if f.resolve() == self_path:
                continue
            if not f.is_file() or f.suffix not in (".md", ".py", ".sh", ".txt", ".html"):
                continue
            if "references" in f.parts:      # 模型/参考数据，非人写
                continue
            try:
                if pat in f.read_text(encoding="utf-8", errors="ignore"):
                    leaks.append(f"{f.relative_to(ROOT)}:{pat}")
            except OSError:
                pass
    check("仓库无明文凭据残留", [], leaks)

    # ---- 11. R47 新增：公网入口与鉴权的实测口径 ----
    # 2026-10-19 生产实测（Oracle nginx dsh-web + 8077 后端）：
    #   /health              → 200   （真实路径，**不在 /api/ 下**）
    #   /api/v1/health      → 404   （旧文档写法，从未存在）
    #   /jc/       → 200   （auth_basic off，免凭据）
    #   /jc/api/v1/me → 401（应用层 session 拒绝，**不是** Basic Auth）
    #   /dashboard/          → 401   （这才是 Nginx Basic Auth）
    #   /jc/login  → 404   （登录页 2026-10-18 下线，nginx 仍指向它）
    # 以下断言是"禁止出现已知错误写法"（负向、精确字符串），不依赖措辞风格。
    deploy = read(ROOT / "docs" / "web" / "DASHBOARD_DEPLOY.md")
    ops_readme = read(ROOT / "ops" / "README.md")
    for label, text in (("README", readme), ("DASHBOARD_DEPLOY", deploy),
                        ("ops/README", ops_readme)):
        check(f"{label} 不含已废弃的 /api/v1/health 写法",
              [], stale_lines(text, ["/api/v1/health"]))
    check("README 不再声称 /jc/ 走 Nginx Basic Auth",
          [], stale_lines(readme, ["走 Nginx Basic Auth"]))
    check("README 不再声称公网入口触发 Basic（401 / Basic 触发）",
          [], stale_lines(readme, ["401 / Basic 触发"]))
    check("DASHBOARD_DEPLOY 不再声称公网入口提供 Basic Auth",
          [], stale_lines(deploy, ["提供 HTTPS 和 Basic Auth"]))
    check("文档不再引用已退役的 league-dashboard.service",
          [],
          stale_lines(readme, ["league-dashboard.service"])
          + stale_lines(deploy, ["league-dashboard.service"])
          + stale_lines(ops_readme, ["league-dashboard.service"]))
    # 健康检查的真实路径必须被正确写出（正向，防"删错写成别处"）。
    check("ops/README 写明真实健康检查路径 /health", True, "/health" in ops_readme)

    # ---- 12. R47 新增：登录入口已恢复（缺陷修复的守门）----
    # 2026-10-18（093dafa）删掉独立登录页，前提「Nginx Basic Auth 把关」在生产不成立
    # （nginx 对 /jc/ 是 auth_basic off；require_auth 只认 lp_session cookie）。
    # 用户已选定方案：恢复独立登录页。以下断言防"再被删一次"。
    check("独立登录页已恢复（static/dashboard-login.html 存在）", True,
          (ROOT / "static" / "dashboard-login.html").is_file())
    check("直连登录页已恢复（static/login.html 存在）", True,
          (ROOT / "static" / "login.html").is_file())
    api_src = read(ROOT / "web" / "api.py")
    check("web/api.py 恢复了 /login 路由", True, '@app.get("/login")' in api_src)
    check("web/api.py 恢复了未登录跳登录页", True,
          'RedirectResponse(url="/login"' in api_src)
    dash_html = read(ROOT / "static" / "dashboard.html")
    check("前端 401 跳回登录页（dashboard.html）", True,
          'LOGIN="/jc/login"' in dash_html and "location.href=LOGIN" in dash_html)
    check("前端 401 提示不再宣称由 Nginx Basic Auth 把关", 0,
          dash_html.count("Nginx Basic Auth 把关"))
    check("README 记录了登录入口缺陷与修复", True,
          "没有可用登录入口" in readme and "已修复" in readme)

    # ---- 12.5 编码损坏（U+FFFD）扫描 ----
    # 实测踩坑：2026-10-21 发现路线图里「怎么做」三个字被写成了三个 U+FFFD 替换字符
    # （commit 1e4fdac 引入）—— 中文文档里一个字变成方块，肉眼很容易漏过，
    # 但读者看到的就是乱码。⇒ 扫全部受版本控制的文本文件，有 U+FFFD 即 FAIL。
    # （3 个 U+FFFD 替换字符，commit 1e4fdac 引入）—— 中文文档里一个字变成方块，
    # 肉眼看「像字」很容易漏过，但读者看到的就是乱码。
    # ⇒ 扫全部受版本控制的文本文件，有 U+FFFD 即 FAIL。
    _TEXT_SUFFIXES = (".md", ".py", ".sql", ".html", ".json", ".txt")
    _SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
                  "stress_runs", "dist", "build", ".pytest_cache"}
    _corrupted: list[str] = []
    for _p in ROOT.rglob("*"):
        if not _p.is_file() or _p.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in _p.parts):
            continue
        try:
            _text = _p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "\ufffd" in _text:
            _corrupted.append(f"{_p.relative_to(ROOT)}（{_text.count(chr(0xfffd))} 处）")
    check("受版本控制的文本文件无 U+FFFD 编码损坏", [], _corrupted)

    # ---- 12.6 UTF-8 BOM 扫描 ----
    # 实测踩坑（2026-10-21）：把脚本传到生产机时，管道给文件加了个 **UTF-8 BOM**
    # ⇒ `#!/usr/bin/env bash` 变成 `﻿#!/usr/bin/env bash`
    # ⇒ bash 报 "No such file or directory"（虽然 `bash <script>` 仍能跑，但每轮都报错）。
    # BOM 在 .html（首行标签前多一个字符）/ .sql（psql 可能报错）里同样是坑 ⇒ 一并扫掉。
    _bom_hits: list[str] = []
    for _p in ROOT.rglob("*"):
        if not _p.is_file() or _p.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in _p.parts):
            continue
        try:
            _head = _p.read_bytes()[:3]
        except OSError:
            continue
        if _head == b"\xef\xbb\xbf":
            _bom_hits.append(str(_p.relative_to(ROOT)))
    check("受版本控制的文本文件无 UTF-8 BOM", [], _bom_hits)

    # ---- 13. R48b 追加：产品路线图存在且被 README/AGENTS 引用 ----
    # 路线图是竞彩链路的总纲（诊断 + 度量基线 + 硬约束）。它若被删而 README/AGENTS
    # 还在指路，读者会点到一个不存在的文件；反之只留一份孤儿文件也没人看。
    # 断言只查「存在 + 被引用」这种稳定事实，**不查里面的数字**（那些是生产实测值，
    # 会随运营变化，由路线图自己的「度量基线」表负责逐次更新）。
    roadmap_name = "产品路线图-竞彩闭环-20261020.md"
    roadmap_path = ROOT / "docs" / "project" / roadmap_name
    check("产品路线图文件存在（docs/project/）", True, roadmap_path.is_file())
    check("README 引用了产品路线图", True, roadmap_name in readme)
    check("AGENTS 引用了产品路线图", True, roadmap_name in read(ROOT / "AGENTS.md"))

    # ---- 14. 建议 2 的待审清单存在且被路线图引用 ----
    # v2 清单是建议 2 的落地依据（396 支六档 + 逐支人工判定）。路线图 §6.2 与
    # 《建议 2》两处都指向它；只查「存在 + 被引用」，不查里面的队数/比例
    # （那是逐次实测更新的生产数字，由清单自己的度量表负责）。
    jc_alias_v2 = "待审-竞彩球队对照-v2-20261020.md"
    jc_e_p01 = "待审-竞彩E档建行-P0P1-20261020.md"
    roadmap_text = read(roadmap_path)
    check("竞彩球队对照 v2 清单存在（docs/project/）", True,
          (ROOT / "docs" / "project" / jc_alias_v2).is_file())
    check("产品路线图引用了竞彩球队对照 v2 清单", True, jc_alias_v2 in roadmap_text)
    # v2 清单里 A+B+C+D 已落库、E 档未落库 —— 两段状态都要写明，
    # 否则下一个人会照着「一行未入库」重新执行已做过的落库。
    v2_text = read(ROOT / "docs" / "project" / jc_alias_v2)
    check("v2 清单记录了 A+B+C+D 的落库结果", True,
          "已按本清单落库" in v2_text and "待审-竞彩E档建行-P0P1" in v2_text)
    check("E 档待审清单存在（docs/project/）", True,
          (ROOT / "docs" / "project" / jc_e_p01).is_file())
    check("E 档清单记录了落库结果与「一行未入库」的生成态", True,
          "已落库" in read(ROOT / "docs" / "project" / jc_e_p01)
          and "一行未入库" in read(ROOT / "docs" / "project" / jc_e_p01))
    check("产品路线图引用了 E 档待审清单", True, jc_e_p01 in roadmap_text)

    # ---- 15. 建议 1（模型补欧冠）的队名链与落地状态 ----
    # 竞彩↔模型按**中文名** join ⇒ 模型产出的中文名必须逐字等于竞彩官方叫法。
    # 人工核对表是这条契约的载体；它若被删或被 LLM 缓存冲掉，join 立刻静默失效
    # （症状是 sfc 的 n_with_probs 归零、错位榜只剩五大联赛那几行）。
    # 只查「存在 + 被引用 + 规模」，不查表里的具体队名（那是逐次人工核对的产物）。
    jc_names_doc = "待审-竞彩官方队名表-20261020.md"
    official_json = SCRIPTS / "references" / "team_cn_official.json"
    check("竞彩官方队名表文件存在（scripts/references/）", True, official_json.is_file())
    check("竞彩官方队名表被 core/i18n.py 引用", True,
          "team_cn_official.json" in read(SCRIPTS / "core" / "i18n.py"))
    check("竞彩官方队名表清单存在（docs/project/）", True,
          (ROOT / "docs" / "project" / jc_names_doc).is_file())
    check("产品路线图引用了竞彩官方队名表清单", True, jc_names_doc in roadmap_text)
    if official_json.is_file():
        import json as _json
        _raw = _json.loads(official_json.read_text(encoding="utf-8"))
        _teams = {k: v for k, v in _raw.items() if not k.startswith("_")}
        check("竞彩官方队名表条目数 ≥ 100（防清空/误删）", True, len(_teams) >= 100)
        check("竞彩官方队名表取值都是非空字符串", [],
              [k for k, v in _teams.items() if not isinstance(v, str) or not v.strip()])
        # 文档写「109 条」而 JSON 有 112 个 key（3 个是 _comment/_source/_generated 元数据）
        # ⇒ 清单文档与数据表的条目数必须对得上，否则下一个人会以为少了两条。
        check("官方队名表清单与数据表条目数一致（109）", True,
              f"{len(_teams)} 条" in read(ROOT / "docs" / "project" / jc_names_doc))
    # 路线图必须写明建议 1 的落地状态（代码侧 + DB 侧都要写明），
    # 否则下一个人会以为训练集回填还没做（重复花配额），或以为做了却其实没做。
    check("路线图记录了建议 1 的落地状态", True,
          "建议 1（模型补欧冠）代码落地" in roadmap_text)
    check("路线图写明建议 1 的 DB 侧已落库", True,
          "建议 1 的 DB 侧已落库" in roadmap_text)
    # DB 侧落库的关键数字必须落在路线图里（§3 基线表 + §6.4），否则下一轮回归无从对照。
    check("路线图记录了 DB 侧落库后的 fact.fixture 总数（6048）与 ucl 行数（707）", True,
          "6048" in roadmap_text and "707" in roadmap_text)
    check("路线图记录了 DB 侧落库后的 ref.team 支数（267）", True, "267" in roadmap_text)
    check("路线图记录了新建 ref.team 行数（88）", True, "88" in roadmap_text)
    # 队名链契约：ref.team.name_cn 不参与任何 join —— 这条必须写明，
    # 否则下一个人看到「曼城 / Inter / 阿贾克斯」这些旧名会想去"修" name_cn，
    # 而那既不提升映射率（join 键是 aliases->>'sporttery'）、也不改展示（走 jc_match.home_cn）。
    check("路线图写明 ref.team.name_cn 不参与任何 join", True,
          "不参与任何 join" in roadmap_text)
    contract_text = read(ROOT / "docs" / "web" / "DATA_CONTRACT.md")
    check("DATA_CONTRACT 写明 name_cn 不参与 join（§8.1.1）", True,
          "8.1.1" in contract_text and "不参与任何 join" in contract_text)
    check("DATA_CONTRACT 写明 ref.league 是联赛真相源且含 ucl 行", True,
          "ref.league.ids" in contract_text and "ucl" in contract_text)
    # ref.league 增补脚本必须在仓库里（DB 侧落地时直接用，不手搓 SQL）
    check("ref.league 增补 ucl 行的 SQL 存在（docs/db/）", True,
          (ROOT / "docs" / "db" / "ucl_league_seed.sql").is_file())
    # 物化入口的警告必须留在文档里：**回填后仍要显式跑一次物化**（不是自动的）。
    # ⚠️ 口径已从「没有 CLI」变成「有 CLI 但手动触发」（§6.10.2 补的 `scripts/store/materialize.py`）——
    #    守的是**意图**（「别以为回填完就自动进 fact.fixture」），不是那句已经过期的话。
    check("路线图记录了物化入口需手动触发（回填后要显式跑一次）", True,
          "每次回填后仍要显式跑一次物化" in roadmap_text)
    check("路线图记录了物化 CLI 的用法（scripts/store/materialize.py）", True,
          "scripts/store/materialize.py" in roadmap_text)
    # ⚠️ 反向守卫：**不再**许诺「生产没有 CLI 入口」（§6.10.2 之后这句话就是假的了）。
    #    用 stale_lines：命中该串**且**行内无「订正/此前/已修复」等标记的行才算违规
    #    （§6.4.2 那条历史记录带订正标记 ⇒ 不算违规）。
    check("路线图不再残留『生产没有 CLI 入口』这句过期说法", True,
          not stale_lines(roadmap_text, ["生产没有 CLI 入口"]))

    # ---- 16. 建议 3（分层命中率 + CLV 管线）的结构守卫 ----
    # 建议 3 新增了一条「每天自动长一行」的管线：预测归档 → 收盘快照 → CLV 落库 → 看板。
    # 这条链路的每个环节都是**静默失效**的：删一个文件、改一个函数名、让某次 join 匹配不上，
    # 都不会报错，只会在某天看板空了才被发现。所以对**结构事实**设守卫。
    s3_sql = ROOT / "docs" / "db" / "suggestion3_schema.sql"
    check("建议 3 的建表 SQL 存在（docs/db/）", True, s3_sql.is_file())
    if s3_sql.is_file():
        _sql3 = s3_sql.read_text(encoding="utf-8")
        for _tbl in ("model.pred_archive", "model.pred_archive_match",
                     "analysis.clv_match", "analysis.clv_daily"):
            check(f"建议 3 建表 SQL 含 {_tbl}", True, _tbl in _sql3)
    # 管线的每一段代码都必须在仓库里（缺一段 = 看板永远空白）
    for _rel in ("scripts/core/bjt.py", "scripts/core/team_alias.py",
                 "scripts/model/archive.py", "scripts/model/archive_backfill.py",
                 "scripts/market/clv_calc.py", "web/services/clv.py"):
        check(f"建议 3 新增文件存在（{_rel}）", True, (ROOT / _rel).is_file())
    # 两个**增量**端点
    _pred_router = read(ROOT / "web" / "routers" / "predictions.py")
    check("web 新增 /accuracy/stratified 端点", True,
          '@router.get("/accuracy/stratified")' in _pred_router)
    check("web 新增 /clv 端点", True, '@router.get("/clv")' in _pred_router)
    # 老端点的契约不得被悄悄改掉（建议 3 对外承诺的是「增量」）
    _acc_src = read(ROOT / "web" / "services" / "accuracy.py")
    check("老接口 league_accuracy_web 仍在（未被删/改名）", True,
          "def league_accuracy_web(" in _acc_src)
    check("新接口 league_accuracy_stratified 在", True,
          "def league_accuracy_stratified(" in _acc_src)
    # 核心不变量：p_model 只能是**模型自己的输出**，绝不能是市场概率。
    #   顶层 `home_true_prob`/`draw_true_prob`/`away_true_prob` 是 predictor.py 从 `match`
    #   **透传的市场先验**（ESPN 赔率去水），不是模型输出 ⇒ 拿它当 p_model 就是循环论证。
    #   `*_implied` 是市场隐含概率，更不能要。
    # ⇒ 查「代码（不含说明文字）里有没有出现这些键」，而不是查有没有 `_true_prob` 后缀。
    _archive_src = read(ROOT / "scripts" / "model" / "archive.py")
    try:
        _atree = ast.parse(_archive_src)
    except SyntaxError:
        _atree = ast.Module(body=[], type_ignores=[])
    _doc_ids: set[int] = set()
    for _n in ast.walk(_atree):
        if isinstance(_n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _body = getattr(_n, "body", [])
            if _body and isinstance(_body[0], ast.Expr) \
                    and isinstance(_body[0].value, ast.Constant) \
                    and isinstance(_body[0].value.value, str):
                _doc_ids.add(id(_body[0].value))
    _prob_keys: list[str] = []
    _market_keys: list[str] = []
    for _n in ast.walk(_atree):
        if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name) and _n.func.id == "_prob":
            for _a in _n.args:
                if isinstance(_a, ast.Constant) and isinstance(_a.value, str):
                    _prob_keys.append(_a.value)
        if isinstance(_n, ast.Constant) and isinstance(_n.value, str) \
                and id(_n) not in _doc_ids:
            if "true_prob" in _n.value or "implied" in _n.value:
                _market_keys.append(_n.value)
    check("归档取用的概率键只有 h/d/a", [], [k for k in _prob_keys if k not in ("h", "d", "a")])
    check("归档代码不碰市场先验/隐含概率（无 true_prob / implied）", [], _market_keys)
    # 归档失败不得打断主预测流程（否则一次 DB 抖动就废掉当天全部预测）
    _pred_src = read(ROOT / "scripts" / "predict.py")
    try:
        _tree3 = ast.parse(_pred_src)
    except SyntaxError:
        _tree3 = ast.Module(body=[], type_ignores=[])
    _arch_fn = next((n for n in ast.walk(_tree3)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "_archive_league_output"), None)
    check("predict.py 有 _archive_league_output", True, _arch_fn is not None)
    if _arch_fn is not None:
        check("归档函数内部有 except 兜底", True,
              any(isinstance(n, ast.ExceptHandler) for n in ast.walk(_arch_fn)))
    _call_in_try = False
    for _node in ast.walk(_tree3):
        if isinstance(_node, ast.Try):
            for _sub in ast.walk(_node):
                if isinstance(_sub, ast.Call) and isinstance(_sub.func, ast.Name) \
                        and _sub.func.id == "_archive_league_output":
                    _call_in_try = True
    check("归档调用点在 try 内（失败只 warning，不打断预测）", True, _call_in_try)
    # web 层不得顶层 import scripts.*（既有约定：模块级 sys.path 自举 + 函数内延迟 import）
    for _rel in ("web/services/clv.py", "web/routers/predictions.py"):
        try:
            _t3 = ast.parse(read(ROOT / _rel))
        except SyntaxError:
            _t3 = ast.Module(body=[], type_ignores=[])
        _bad_import = []
        for _node in _t3.body:              # 只看模块顶层，不看函数体
            if isinstance(_node, ast.Import):
                for _a in _node.names:
                    if _a.name.split(".")[0] == "scripts":
                        _bad_import.append(f"import {_a.name}")
            elif isinstance(_node, ast.ImportFrom):
                _mod = (_node.module or "")
                if _mod.split(".")[0] == "scripts":
                    _bad_import.append(f"from {_mod} import ...")
        check(f"{_rel} 顶层未 import scripts.*", [], _bad_import)
    # 端点自带的口径说明不能还在讲「市场先验」——它是给使用者看的第一手定义，
    # 写错一次就等于把循环论证的口径发出去。
    check("/api/v1/clv 的口径说明写明 p_model 取 probs.h/d/a", True,
          "probs.h/d/a" in _pred_router and "循环论证" in _pred_router)
    # CLI 入口不能少：少了 `if __name__ == "__main__"`，脚本被当模块执行 ⇒
    # 静默什么都不做（退出码 0、零输出、零写入），看起来像「跑了但没数据」（2026-10-21 踩过）。
    for _rel in ("scripts/market/clv_calc.py", "scripts/model/archive_backfill.py"):
        check(f"{_rel} 有 __main__ 入口", True,
              '__name__ == "__main__"' in read(ROOT / _rel))
    # 文档侧：字段口径与端点必须写进契约文档，否则下一个人会继续用 data_window 当比赛日
    check("DATA_CONTRACT 记录了 match_date 字段（§2.2）", True,
          "match_date" in contract_text)
    check("DATA_CONTRACT 写明 data_window 是取数区间", True,
          "取数区间" in contract_text)
    _plan = read(ROOT / "docs" / "web" / "PLAN.md")
    check("PLAN 端点表登记了 /api/v1/accuracy/stratified", True,
          "/api/v1/accuracy/stratified" in _plan)
    check("PLAN 端点表登记了 /api/v1/clv", True, "/api/v1/clv" in _plan)
    # 路线图必须写明建议 3 的落地状态，以及「存量永远算不出 CLV」这条限制
    check("路线图记录了建议 3 的落地状态", True,
          "建议 3 代码落地" in roadmap_text)
    check("路线图写明存量 backtest_market 永远算不出 CLV", True,
          "永远算不出 CLV" in roadmap_text)
    check("路线图写明 n 不足不显示均值的口径", True,
          "n 不足" in roadmap_text or "insufficient" in roadmap_text)
    # 生产落库结果与「首跑暴露的 5 个 bug」必须留在文档里 ——
    # 不写下来，下一个人会以为这条管线是「一次就写对了」，也会重犯同样的静默失败。
    check("路线图记录了建议 3 的生产落库结果（§6.5.6）", True, "6.5.6" in roadmap_text)
    check("路线图记录了生产首跑暴露的 5 个真 bug（§6.5.7）", True,
          "6.5.7" in roadmap_text and "静默失败" in roadmap_text)

    # ---- 18. 建议 4（决策层：今日推荐单 + Kelly + 竞彩官方赔率结算）----
    # 这条链路的每个环节都是「静默失效」：定价口径错、结算用错价、样本不足还给 ROI，
    # 都不会报错，只会让看板给出**误导性的建议**。⇒ 对结构与口径设守卫。
    s4_sql = ROOT / "docs" / "db" / "suggestion4_schema.sql"
    check("建议 4 的建表 SQL 存在（docs/db/）", True, s4_sql.is_file())
    if s4_sql.is_file():
        _s4 = s4_sql.read_text(encoding="utf-8")
        check("建议 4 DDL 含 analysis.rec_settle", True, "analysis.rec_settle" in _s4)
        check("建议 4 DDL 给 ev_report 加了可空列（match_id/option_code/sp_decide）", True,
              all(k in _s4 for k in ("match_id", "option_code", "sp_decide")))
        check("建议 4 DDL 有幂等唯一键（match_id, option_code, BJT 日）", True,
              "ev_report_pick_uq" in _s4 and "Asia/Shanghai" in _s4)
    for _rel in ("scripts/market/kelly.py", "scripts/decision/recommend.py",
                 "scripts/decision/settle.py", "web/services/recommend.py"):
        check(f"建议 4 新增文件存在（{_rel}）", True, (ROOT / _rel).is_file())
    # Kelly 公式唯一真源：web 层 combo 必须**委托** scripts 层，不得自己再写一份
    _combo_src = read(ROOT / "web" / "services" / "combo.py")
    check("web/services/combo.py 委托 scripts/market/kelly.py（Kelly 唯一真源）", True,
          "market.kelly" in _combo_src)
    # 定价口径：推荐单必须用**竞彩官方 SP**（fact.jc_offer.options），不是 ESPN 赔率
    _rec_src = read(ROOT / "scripts" / "decision" / "recommend.py")
    check("推荐单定价取竞彩官方 SP（fact.jc_offer 的 options）", True,
          "fact.jc_offer" in _rec_src and "options" in _rec_src)
    check("推荐单不使用 ESPN 赔率定价（无 decimal_odds）", True, "decimal_odds" not in _rec_src)
    # 结算口径：必须用 fact.jc_result 的**官方结算 SP** + win_flag
    _set_src = read(ROOT / "scripts" / "decision" / "settle.py")
    check("结算用竞彩官方结算 SP（sp_home/sp_draw/sp_away）", True,
          all(k in _set_src for k in ("sp_home", "sp_draw", "sp_away")))
    check("结算命中判定用 win_flag（官方标记）", True, "win_flag" in _set_src)
    check("战绩 n 不足不给 ROI（insufficient 门槛）", True,
          "MIN_N_SETTLED" in _set_src and "insufficient" in _set_src)
    # web 端点
    check("web 新增 /recommend 端点", True, '@router.get("/recommend")' in _pred_router)
    _plan4 = read(ROOT / "docs" / "web" / "PLAN.md")
    check("PLAN 端点表登记了 /api/v1/recommend", True, "/api/v1/recommend" in _plan4)
    # 路线图必须写明建议 4 的落地状态与「EV 含跨市场定价差」这条诚实标注
    check("路线图记录了建议 4 的落地状态（§6.6）", True, "6.6" in roadmap_text)
    check("路线图写明 EV 含跨市场定价差（不许直接进 EV/Kelly）", True,
          "跨市场定价差" in roadmap_text)
    check("README 记录了建议 4 的 v4.0 段", True, "v4.0" in readme)
    # 今日口径：made_at 是 timestamptz，「今天」必须按 BJT 算（与 §7 第 12 条同款坑）
    _rec_web_path = ROOT / "web" / "services" / "recommend.py"
    _rec_web = read(_rec_web_path)
    # ⚠️ **P1（2026-10-23）**：这条断言原来查的是**整个文件**的子串
    #    ⇒ `recommend.py` 的**模块 docstring 第 7 行逐字写着**这个串
    #    ⇒ 把真 SQL 改成 `'UTC'`，docstring 仍然满足断言（审计实测：守门 359/359 全绿）。
    # ⇒ 改成 **AST 判定**：只看**代码**里的字符串常量（docstring 被排除）。
    _tz_sql = [s for s in code_strings(_rec_web_path) if "AT TIME ZONE" in s]
    check("推荐单「今天」按 BJT 判定（SQL 常量里的时区 == Asia/Shanghai）", True,
          bool(_tz_sql) and all("Asia/Shanghai" in s for s in _tz_sql))
    check("推荐单的 SQL 常量里不许出现 UTC 口径（会话时区是 UTC ⇒ 会差一天）", [],
          [s for s in _tz_sql if "UTC" in s])
    # web 层顶层不得 import scripts.*（新增的两个 web 模块）
    for _rel in ("web/services/recommend.py", "web/services/combo.py"):
        try:
            _t4 = ast.parse(read(ROOT / _rel))
        except SyntaxError:
            _t4 = ast.Module(body=[], type_ignores=[])
        _bad4: list[str] = []
        for _node in _t4.body:              # 只看模块顶层
            if isinstance(_node, ast.Import):
                for _a in _node.names:
                    if _a.name.split(".")[0] == "scripts":
                        _bad4.append(f"import {_a.name}")
            elif isinstance(_node, ast.ImportFrom):
                _mod = (_node.module or "")
                if _mod.split(".")[0] == "scripts":
                    _bad4.append(f"from {_mod} import ...")
        check(f"{_rel} 顶层未 import scripts.*", [], _bad4)

    # ---- 18. 建议 4 补：DB 行读取形态（生产首跑真 bug 的回归锁）----
    # psycopg3 默认 `tuple_row` ⇒ `conn.execute()` 返回的行是**普通 tuple**，
    # `dict(row)` 会抛 `TypeError: object is not iterable`（生产首跑实测：
    # 「今日推荐单」因此一直是空的，且失败被 warning 吞掉 ⇒ 没人发现）。
    # 判定口径：**同一函数内既有 fetchall() 又有 dict(<单个变量>)** ⇒ 疑似把 DB 行当 dict；
    # 只有**显式设了 `cur.row_factory = dict_row`** 的文件才放行。
    _dbrow_bad: list[str] = []
    for _base in ("scripts", "web"):
        for _f in sorted((ROOT / _base).rglob("*.py")):
            try:
                _txt = _f.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - 读不到就跳过，不误伤
                continue
            if "row_factory" in _txt:      # 显式设了 row_factory ⇒ dict(row) 安全
                continue
            try:
                _tree = ast.parse(_txt)
            except SyntaxError:
                continue
            for _fn in ast.walk(_tree):
                if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                _has_fetch = any(
                    isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
                    and _n.func.attr in ("fetchall", "fetchmany")
                    for _n in ast.walk(_fn))
                if not _has_fetch:
                    continue
                for _n in ast.walk(_fn):
                    if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name)
                            and _n.func.id == "dict" and len(_n.args) == 1
                            and isinstance(_n.args[0], ast.Name)):
                        _dbrow_bad.append(f"{_f.relative_to(ROOT)}:{_n.lineno} "
                                          f"dict({_n.args[0].id})")
    check("scripts/web 不得把 DB 行直接 dict()（psycopg3 默认 tuple 行）", [], _dbrow_bad)

    # 今日模式的读取入口必须走位置索引（与回溯模式同款写法）
    _sale_dict_calls: list[str] = ["<load_sale_matches 不存在>"]
    try:
        _rec_tree = ast.parse(_rec_src)
    except SyntaxError:
        _rec_tree = ast.Module(body=[], type_ignores=[])
    for _fn in ast.walk(_rec_tree):
        if isinstance(_fn, ast.FunctionDef) and _fn.name == "load_sale_matches":
            _sale_dict_calls = [
                f"dict({_a.args[0].id})"
                for _a in ast.walk(_fn)
                if isinstance(_a, ast.Call) and isinstance(_a.func, ast.Name)
                and _a.func.id == "dict" and len(_a.args) == 1
                and isinstance(_a.args[0], ast.Name)
            ]
            break
    check("load_sale_matches 用位置索引取列（不用 dict(row)）", [], _sale_dict_calls)

    # 决策层失败必须**可见**：warning 会被吞掉（生产首跑就是这么丢的）
    _predict_src = read(ROOT / "scripts" / "predict.py")
    check("决策层失败必须可见（logger.error + log_error 落 events.jsonl）", True,
          'context="predict.main.recommend"' in _predict_src
          and "今日推荐单生成失败" in _predict_src
          and "logger.error" in _predict_src)

    # 建议 2 映射待办的**订正**必须留在文档里 —— 否则下一个人会去给「维戈塞尔塔」
    # 新建一行（而它早就通过 #2339 映射好了 ⇒ 新建 = 多余行 + 撞 partial unique 风险）
    check("路线图记录了建议 2 映射待办的订正（维戈塞尔塔原判断作废）", True,
          "原判断作废" in roadmap_text and "维戈塞尔塔" in roadmap_text)

    # ---- 19. 建议 5（滚动重算 + 赔率异动告警）----
    # 建议 5 的原文有个**前提是错的**（「复用既有告警 webhook」实际不存在），
    # 且它的告警口径若按「首末差」会命中 77%（纯噪音）⇒ 这三条订正必须留在文档里，
    # 否则下一个人会照原文实现一个「每次都告警」的噪音通道。
    check("路线图记录了建议 5 的实测订正（§6.7）", True, "6.7" in roadmap_text)
    check("建议 5 记录了告警口径=相邻跳变（实测命中率）而非首末差", True,
          "相邻" in roadmap_text and "0.40%" in roadmap_text and "首末差" in roadmap_text)
    check("建议 5 记录了「既有告警 webhook 不存在」这条订正", True,
          "无 webhook" in roadmap_text or "既有告警 webhook" in roadmap_text)
    check("建议 5 记录了 movement 开盘价取错的订正（第一版完整 had）", True,
          "第一版完整" in roadmap_text or "第一版**完整" in roadmap_text)

    # ---- 19.1 建议 5 的代码纪律（32 条）----
    _jc_router = read(ROOT / "web" / "routers" / "jc.py")
    _jc_ops_router = read(ROOT / "web" / "routers" / "jc_ops.py")
    _om_src = read(ROOT / "scripts" / "market" / "odds_move.py")
    _gc_src = read(ROOT / "scripts" / "market" / "gap_calc.py")
    _nt_src = read(ROOT / "scripts" / "market" / "notify.py")
    _s5_ddl = read(ROOT / "docs" / "db" / "suggestion5_schema.sql")
    _tm_src = read(ROOT / "web" / "services" / "team_match.py")

    # ① 看板内联 JS 必须语法合法 —— **本轮实测踩到**：往 dashboard.html 插函数时
    #    少写一个大括号 ⇒ 整个内联脚本解析失败 ⇒ 看板所有交互全挂
    #    （页面还"能打开"，只是什么都不响应 ⇒ 典型静默失败）。
    #    浏览器测试在无 playwright 的环境里是 skip ⇒ 靠这条守门兜底。
    #    node 不在 ⇒ SKIP（不阻断，与 ruff 同款纪律）。
    _dash_html = read(ROOT / "static" / "dashboard.html")
    _m_js = re.search(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", _dash_html)
    if _m_js is None:
        check("看板内联 JS 语法合法（node --check）", True, "无内联 script")
    elif shutil.which("node") is None:
        skip("看板内联 JS 语法合法（node --check）",
             "当前环境未安装 node ⇒ 本项未检查（非失败）")
    else:
        _js_path = Path(tempfile.gettempdir()) / "sports_lottery_dashboard_inline_check.js"
        _js_path.write_text(_m_js.group(1), encoding="utf-8")
        _rc_js = subprocess.run(["node", "--check", str(_js_path)],
                                capture_output=True, text=True).returncode
        check_rc("看板内联 JS 语法合法（node --check）", 0, _rc_js)

    # ①' 内联 JS **作用域**合法 —— 2026-10-21 生产事故：dashboard.html 里
    #     `const num=v=>...` 定义在 loadAccuracy() 内部，却被 loadClv()/loadRecommend() 使用
    #     ⇒ 运行时 ReferenceError ⇒ 看板「CLV」「今日推荐单」两个面板显示
    #     「暂不可用：num is not defined」。页面不白屏、接口 200 ⇒ **纯静默失效**。
    #     node --check 只查语法、查不出作用域错误 ⇒ 这里用 tools/check_js_scope.py 兜底。
    #     不用 tsc：它是 npm 全局包，生产机未必装得上（与 ruff 同款纪律：缺工具记 SKIP 不阻断）；
    #     本脚本纯标准库 ⇒ 本地与生产机都能跑。判定规则已与 tsc --checkJs 的 TS2304 **逐条对拍**
    #     （dashboard.html 10 处一致、其余 4 个静态页 0 处一致）。
    try:
        _js_scope_spec = importlib.util.spec_from_file_location(
            "_guard_js_scope", ROOT / "tools" / "check_js_scope.py")
        _js_scope_mod = importlib.util.module_from_spec(_js_scope_spec)
        _js_scope_spec.loader.exec_module(_js_scope_mod)
        _scope_hits: list[str] = []
        for _html in sorted((ROOT / "static").glob("*.html")):
            for _f in _js_scope_mod.check_html(_html):
                _scope_hits.append(f"{_html.name}: {_f}")
    except Exception as _exc:  # noqa: BLE001 - 读不到就判失败，不误伤
        _scope_hits = [f"检查器加载失败：{_exc!r}"]
    check("静态页内联 JS 无跨作用域引用（tools/check_js_scope.py）", [], _scope_hits)

    # ①''' 看板内联 JS **运行时**冒烟 —— 语法/作用域都合法还不够：
    #     生产事故里接口 200、页面不白屏，**只有面板内容是「暂不可用」**。
    #     ⇒ 真跑一次 loadClv()/loadRecommend()/loadAccuracy()，断言三个面板都渲染出真内容。
    #     工具：tools/dashboard_smoke.js（node + vm 桩，无浏览器依赖）。
    #     node 不在 ⇒ SKIP（不阻断，与 ruff 同款纪律）。
    _smoke_js = ROOT / "tools" / "dashboard_smoke.js"
    if not _smoke_js.is_file():
        check("看板运行时冒烟（面板真的渲染出内容）", True, "缺 tools/dashboard_smoke.js")
    elif shutil.which("node") is None:
        skip("看板运行时冒烟（面板真的渲染出内容）",
             "当前环境未安装 node ⇒ 本项未检查（非失败）")
    else:
        _rc_smoke = subprocess.run(["node", str(_smoke_js)], cwd=ROOT,
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace").returncode
        check_rc("看板运行时冒烟（面板真的渲染出内容）", 0, _rc_smoke)

    # ①'' 看板分区 header 必须跨全宽 —— 2026-10-21 用户报「今日推荐 · 21 场
    #      直接占用了一个卡片位置」：header 作为独立节点进渐进渲染队列 ⇒ 在 2 列 grid 里占一格。
    _sec_rule = next((l for l in _dash_html.splitlines() if l.startswith(".section-h3{")), "")
    check("看板分区 header 跨全宽（不独占卡片格）", True,
          "grid-column:1/-1" in _sec_rule)

    # ② 凭证纪律：飞书 webhook URL **只允许**从环境变量读
    #    （谁拿到就能往那个群发消息）⇒ 仓库任何文件里都不得出现 hook URL。
    _hook_re = re.compile(r"open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9\-]+")
    _hook_hits: list[str] = []
    for _p in ROOT.rglob("*"):
        if not _p.is_file() or _p.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in _p.parts):
            continue
        try:
            _t = _p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _hook_re.search(_t):
            _hook_hits.append(str(_p.relative_to(ROOT)))
    check("仓库任何文件都不得含飞书 webhook URL（只走 env）", [], _hook_hits)
    check("飞书推送只从环境变量读 URL（不硬编码）", True,
          "os.environ.get(ENV_WEBHOOK)" in _nt_src
          and 'ENV_WEBHOOK = "FEISHU_WEBHOOK_URL"' in _nt_src)

    # ③ 未配 webhook 时**不得谎报已推送**
    check("未配 webhook 时返回 not_configured（不谎报已推）", True,
          '"ok": False, "reason": "not_configured"' in _nt_src)
    check("analysis.odds_move 的 pushed 默认 false（没配 webhook 恒为没推）", True,
          re.search(r"pushed\s+boolean\s+NOT NULL DEFAULT false", _s5_ddl) is not None)
    check("异动推送失败不阻断落库（pushed 保持 false）", True,
          "pushed" in _om_src and "push_failed" in _om_src)

    # 推送标记必须**逐条精确**（场次+侧+前后两版）——
    # 原实现用 `match_id = ANY(...) AND curr_ts = ANY(...)` ⇒ **笛卡尔积**
    # ⇒ 会把没被推送的行也标成 pushed=true ⇒ 正是「绝不谎报已推」要防的事。
    _mp_ok = False
    try:
        _mp_tree = ast.parse(_om_src)
        for _fn in ast.walk(_mp_tree):
            if not (isinstance(_fn, ast.FunctionDef) and _fn.name == "mark_pushed"):
                continue
            _sqls = [n.value for n in ast.walk(_fn)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and n.value.strip().upper().startswith("UPDATE")]
            _mp_ok = bool(_sqls) and all("ANY(" not in s for s in _sqls) and any(
                all(k in s for k in ("option_code = %s", "prev_ts = %s", "curr_ts = %s"))
                for s in _sqls)
            break
    except SyntaxError:
        _mp_ok = False
    check("推送标记逐条精确（不许 ANY 笛卡尔积 ⇒ 会谎报已推）", True, _mp_ok)

    # 推送必须**去重**：检测窗口 24h ⇒ 同一异动每轮都会重新检出
    # ⇒ 不去重就会每 10 分钟重推同一条 ⇒ 刷屏。
    check("异动推送去重（只推没推过的；--repush 才重发）", True,
          "already_pushed" in _om_src and "--repush" in _om_src)

    # 落表 SQL 必须与 **DDL 对账** —— 生产首跑实测：INSERT 写了 `RETURNING move_id`，
    # 而表里**根本没有这列** ⇒ 每次落表都抛 UndefinedColumn ⇒ 异动一条都进不去。
    # ⚠️ 单测用的是**假连接**（不执行真 SQL）⇒ 单测全绿也验不到这层 ⇒ 守门独立复核。
    _s5_cols: set[str] = set()
    try:
        _s5_ddl = read(ROOT / "docs" / "db" / "suggestion5_schema.sql")
        _m_tbl = re.search(r"CREATE TABLE IF NOT EXISTS analysis\.odds_move\s*\((.*?)\n\);",
                           _s5_ddl, re.S)
        if _m_tbl:
            for _line in _m_tbl.group(1).split("\n"):
                _line = _line.strip().rstrip(",")
                if not _line or _line.upper().startswith(
                        ("CONSTRAINT", "PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")):
                    continue
                _s5_cols.add(_line.split()[0])
    except Exception:  # noqa: BLE001 - 读不到就判失败，不误伤
        _s5_cols = set()
    _m_ins = re.search(r"INSERT INTO analysis\.odds_move\s*\(([^)]*)\)", _om_src, re.S)
    _ins_cols = {c.strip() for c in _m_ins.group(1).split(",") if c.strip()} if _m_ins else set()
    check("异动落表 SQL 的列都在 DDL 里（假连接验不到这层）", True,
          bool(_s5_cols) and bool(_ins_cols) and not (_ins_cols - _s5_cols))
    _m_ret = re.search(r"RETURNING\s+(\w+)", _om_src)
    check("异动落表 RETURNING 只引用真实列（实测曾引用不存在的 move_id）", True,
          _m_ret is None or _m_ret.group(1) in _s5_cols)

    # 告警文案的窗口标签必须**从常量推导** —— 窗口从 540~660 改成 240~1200 秒后，
    # 写死的「相邻 ~10 分钟跳变」就与真实口径脱节了。
    check("告警文案的窗口标签从常量推导（不许写死旧口径）", True,
          "def gap_label" in _om_src and "window_label=gap_label()" in _om_src)

    # ④ 告警文案必须带「异动是信息不是信号」（§6.6.5.1 的水位结论）
    check("告警文案带「异动是信息不是信号」的硬约束", True,
          "不是下注理由" in _om_src and "信息" in _om_src)
    check("页面/接口文案带同样的硬约束", True,
          "不是下注理由" in _jc_router and "不是下注理由" in read(ROOT / "web" / "services" / "odds_move.py"))

    # ⑤ 异动落表才是资产：幂等唯一键 + 前后两版可复算
    check("analysis.odds_move 有幂等唯一键（场,玩法,侧,前后版本）", True,
          "PRIMARY KEY (match_id, play_type, option_code, prev_ts, curr_ts)" in _s5_ddl)
    check("异动事件存了前后两版 snap_ts 与 SP（可复算）", True,
          all(k in _s5_ddl for k in ("prev_ts", "curr_ts", "sp_prev", "sp_curr", "move_pct")))

    # ⑥ 决策时盘口必须取「第一版**完整** had」，不是「第一版快照」
    check("gap_calc 取决策时盘口用『三项齐全』过滤（第一版完整 had）", True,
          all(k in _gc_src for k in ("options->>'h'", "options->>'d'", "options->>'a'")))
    check("gap_calc 的决策时盘口取 snap_ts <= 归档 run_at 的最后一版", True,
          "snap_ts <= src.run_at" in _gc_src)

    # ⑦ 错位榜必须是**增量**：老字段一字未动
    check("错位榜老字段（gap/official/model）仍在 jc_gap 输出里", True,
          all(k in _tm_src for k in ('"gap": round(gap, 3)', '"official":', '"model":')))
    check("错位榜新增三分量字段（gap_decide/gap_now/drift_pp）", True,
          all(k in read(ROOT / "web" / "services" / "gap.py")
              for k in ("gap_decide", "gap_now", "drift_pp"))
          and "attach_triples" in _jc_router)

    # ⑧ web 层顶层不得 import scripts.*（建议 5 新增的 3 个 web 模块）
    for _rel in ("web/services/gap.py", "web/services/odds_move.py", "web/services/notify.py"):
        try:
            _t5 = ast.parse(read(ROOT / _rel))
        except SyntaxError:
            _t5 = ast.Module(body=[], type_ignores=[])
        _bad5: list[str] = []
        for _node in _t5.body:              # 只看模块顶层
            if isinstance(_node, ast.Import):
                for _a in _node.names:
                    if _a.name.split(".")[0] == "scripts":
                        _bad5.append(f"import {_a.name}")
            elif isinstance(_node, ast.ImportFrom):
                _mod = (_node.module or "")
                if _mod.split(".")[0] == "scripts":
                    _bad5.append(f"from {_mod} import ...")
        check(f"{_rel} 顶层未 import scripts.*", [], _bad5)

    # ⑨ 建议 5 的文件级落地清单必须齐（§6.7.4）
    for _rel in ("scripts/market/odds_move.py", "scripts/market/gap_calc.py",
                 "scripts/market/notify.py", "docs/db/suggestion5_schema.sql",
                 "web/services/gap.py", "web/services/odds_move.py", "web/services/notify.py",
                 "tests/test_suggestion5_odds_move.py"):
        check(f"建议 5 新增文件存在（{_rel}）", True, (ROOT / _rel).is_file())

    # ⑩ web 端点齐了
    check("web 新增 /api/jc/odds-move 端点", True, '@router.get("/odds-move")' in _jc_router)
    check("web 新增 /api/jc/ops/odds-move 端点", True,
          '@router.get("/ops/odds-move")' in _jc_ops_router)

    # ⑪ 看板必须有异动事件表 + 错位榜三分量列（§6.7.4 的 static/dashboard.html 落点）
    check("看板有异动事件表容器与列定义", True,
          'id="jcMoveBody"' in _dash_html and "JC_MOVE_COLS" in _dash_html
          and "move_pct" in _dash_html and "pushed" in _dash_html)
    check("看板错位榜有三分量列（gap_decide/gap_now/drift_pp）", True,
          all(k in _dash_html for k in ("gap_decide", "gap_now", "drift_pp")))

    # ⑫ 异动检测的相邻对口径必须是「~10 分钟间隔」窗口（跨批次自然间隔不算）
    check("异动检测只取 4~20 分钟的相邻对（240~1200 秒）", True,
          "MIN_GAP_SEC = 240" in _om_src and "MAX_GAP_SEC = 1200" in _om_src)
    check("异动检测默认阈值 5%（实测命中 0.40%）", True,
          "DEFAULT_THRESHOLD_PCT = 5.0" in _om_src)

    # ⑬ 告警是信息不是信号 ⇒ web 层不得出现「按异动直接出推荐」的端点
    _bad_signal_routes: list[str] = []
    for _rf in sorted((ROOT / "web" / "routers").glob("*.py")):
        for _i, _l in enumerate(read(_rf).splitlines(), 1):
            _m_route = re.search(r'@(\w+\.)?get\("([^"]+)"\)', _l)
            if not _m_route:
                continue
            _path = _m_route.group(2).lower()
            if "odds" in _path and "recommend" in _path:
                _bad_signal_routes.append(f"{_rf.relative_to(ROOT)}:{_i} {_path}")
    check("web 层没有『按异动直接给推荐』的端点（告警≠信号）", [], _bad_signal_routes)

    # ---- 19.2 建议 6 的代码纪律（体彩侧独立小产品）----
    _lot_svc = read(ROOT / "web" / "services" / "lottery.py")
    _lot_api = read(ROOT / "web" / "routers" / "lottery_api.py")
    _roadmap = read(ROOT / "docs" / "project" / "产品路线图-竞彩闭环-20261020.md")

    # ① 文件级落地清单（§6.8.5）
    check("建议 6 新增文件存在（tests/test_suggestion6_lottery_panel.py）", True,
          (ROOT / "tests" / "test_suggestion6_lottery_panel.py").is_file())

    # ② 两个新端点齐了（老端点另有一组「老字段未动」断言）
    check("web 新增 /api/v1/lottery/omission 端点", True,
          '@router.get("/lottery/omission")' in _lot_api)
    check("web 新增 /api/v1/lottery/splits 端点", True,
          '@router.get("/lottery/splits")' in _lot_api)

    # ③ 遗漏值口径必须写死：单位是「期」不是「天」，四个字段齐
    check("遗漏值单位是『期』不是『天』", True,
          '"unit": "期"' in _lot_svc)
    check("遗漏值四字段口径齐（cur/count/avg/max）", True,
          all(k in _lot_svc for k in ("OMISSION_DEFINITION", '"cur"', '"avg"', '"max"', '"count')))
    # max 必须是「连续未出现期数」= 索引差 - 1（曾错用索引差，与 cur 口径差 1）
    check("最大遗漏用『连续未出现期数』（索引差-1），不是索引差", True,
          "idx - last[num] - 1" in _lot_svc)

    # ④ GAME_SPEC 的 pool 语义不一致：front_pool/back_pool 是**上界**，
    #    pool/special_pool 是**个数** ⇒ 特别号上界必须减 1，
    #    否则会把不存在的 15 号算进遗漏值（单测已抓到过这个真 bug）。
    check("特别号上界用 special_pool-1（不把不存在的 15 号算进去）", True,
          'g["special_pool"] - 1' in _lot_svc)

    # ⑤ 复式注数口径 = C(m,5)×C(n,2)（**不是 m×n**）
    check("复式注数按 C(m,front)×C(n,back) 算（不是 m×n）", True,
          'C(m, g["front"]) * C(n, g["back"])' in _lot_svc)
    check("拆法对比含『出票张数』维度（拆单式与复式的真实差别）", True,
          '"ticket_count"' in _lot_svc and '"coverage_ratio"' in _lot_svc)

    # ⑥ 诚实提示规范句必须**逐字**出现在三处：服务层常量 / 看板面板 / 路线图 §6.8.6
    #    规范句本身在这里**钉死**（拆成两段字面量，源码里就是隐式拼接的两段）。
    _DISC_PARTS = ("本页只做历史统计呈现与组合数学计算，不构成投注建议；",
                   "热号/冷号/遗漏值只描述已发生的历史事实，实测选号能力与纯随机无法区分。")
    _disc_expected = "".join(_DISC_PARTS)
    _disc_runtime = ""
    try:
        _spec = importlib.util.spec_from_file_location(
            "_guard_lottery_svc", ROOT / "web" / "services" / "lottery.py")
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _disc_runtime = getattr(_mod, "DISCLAIMER", "")
    except Exception:  # noqa: BLE001 - 读不到就判失败，不误伤
        _disc_runtime = ""
    check("诚实提示规范句的服务层常量 == 规范句（运行时真值）", _disc_expected, _disc_runtime)
    check("诚实提示规范句在服务层源码里逐字声明", True,
          all(p in _lot_svc for p in _DISC_PARTS))
    check("诚实提示规范句逐字出现在看板面板顶部", True,
          _disc_expected in _dash_html)
    check("诚实提示规范句逐字出现在路线图 §6.8.6", True,
          _disc_expected in _roadmap)

    # ⑦ 新面板不得出现「选号推荐」承诺词（把统计呈现说成选号建议 = 违规）
    _panel = ""
    _i0 = _dash_html.find("体彩统计面板")
    if _i0 >= 0:
        _i1 = _dash_html.find('id="lotPanelSplits"', _i0)
        _panel = _dash_html[_i0:(_i1 + 400 if _i1 > 0 else _i0 + 2000)]
    check("体彩统计面板存在（容器 + 两个按钮）", True,
          all(k in _dash_html for k in ('id="lotPanelOmission"', 'id="lotPanelSplits"',
                                       'id="lotPanelOmissionBtn"', 'id="lotPanelSplitsBtn"',
                                       "lotPanelOmission()", "lotPanelSplits()")))
    check("体彩面板不含选号承诺词（必中/稳赚/回本/包中/盈利/推荐号码）", [],
          [w for w in ("必中", "稳赚", "回本", "包中", "盈利", "推荐号码") if w in _panel])

    # ⑧ 老端点与老字段**一字未动**（增量，不许借机改语义）
    check("老端点 /lottery/{prize,cost,stats,dantuo} 仍在", True,
          all(f'@router.get("/lottery/{p}")' in _lot_api
              for p in ("prize", "cost", "stats", "dantuo")))
    check("stats() 老字段未动（count/unique/most/odd_even/sum_min/sum_max/avg）", True,
          all(f'"{k}"' in _lot_svc
              for k in ("count", "unique", "most", "odd_even", "sum_min", "sum_max", "avg")))

    # ⑨ 路线图 §6.8 必须写清实测口径与关键数字（防止只改代码不改文档）
    check("路线图有 §6.8 建议 6 章节", True,
          "### 6.8 建议 6 的实测订正与落地版" in _roadmap)
    # §6.8.4 的零假设检验数字**按段**校验（不能用全文匹配：变更记录里
    # 会合法引用旧口径数字做「订正前 → 订正后」对照）。
    _i684 = _roadmap.find("#### 6.8.4")
    _i685 = _roadmap.find("#### 6.8.5")
    _sec684 = _roadmap[_i684:_i685] if 0 <= _i684 < _i685 else ""
    check("路线图 §6.8.4 记录了选号能力零假设检验的关键数字（新口径）", True,
          bool(_sec684) and all(t in _sec684 for t in (
              "0.273", "0.323", "0.200", "99.750%", "Bonferroni", "12 项方向相反")))
    check("路线图 §6.8.4 不再残留旧口径 p 值/门槛", [],
          [t for t in ("0.348", "0.411", "0.278", "99.667%") if t in _sec684])
    check("路线图 §6.8 记录了复式注数口径订正（C(m,5)×C(n,2)）", True,
          "C(m,5)×C(n,2)" in _roadmap)
    check("路线图 §6.8 记录了遗漏值口径（按期不按天）", True,
          "按「期」计" in _roadmap and "max" in _roadmap)

    # ---- 19.3 顺手修：两处「无数据被当成 0 / 条件恒假」型缺陷 ----
    _calib_src = read(ROOT / "scripts" / "core" / "calibration.py")
    _predict_src = read(ROOT / "scripts" / "predict.py")
    _dash_py = read(ROOT / "scripts" / "core" / "dashboard.py")
    _dailyimage_html = read(ROOT / "static" / "dailyimage.html")

    # ① 无赔率样本 ⇒ odds_accuracy 必须是 None（不是 0）+ odds_note 标记
    check("无赔率样本时 odds_accuracy 是 None（不是 0%）", True,
          'if total_odds_based else None' in _calib_src
          and '"odds_note"' in _calib_src)
    check("校准里有 odds_note 标记（no_odds_samples）", True,
          '"no_odds_samples"' in _calib_src)

    # ② predict.py 必须在无样本时印 N/A，**不许**印 "0% (0/0)"
    check("predict.py 无赔率样本时印 N/A（不印 0% (0/0)）", True,
          "Odds favorite accuracy: N/A" in _predict_src
          and "no odds samples in history" in _predict_src)

    # ③ dashboard.py 模板不能直接格式化 None（{:.1%} 遇 None 会抛 TypeError）
    check("dashboard 模板对 odds_accuracy=None 走 N/A（不直接格式化 None）", True,
          "odds_acc_txt" in _dash_py and "{odds_acc:.1%}" not in _dash_py
          and '"N/A" if _odds_acc is None' in _dash_py)

    # ④ daily-image 提示条条件必须看 temp_rows（rows 就是 main_rows ⇒ 旧条件恒假）
    _note_lines = [ln.strip() for ln in _dailyimage_html.splitlines()
                   if "d.main_rows.length===0" in ln and ln.strip().startswith("if(")]
    check("daily-image 提示条条件存在且用 temp_rows（不看 rows）", True,
          bool(_note_lines)
          and all("d.temp_rows" in ln for ln in _note_lines)
          and all("(d.rows||[]).length" not in ln for ln in _note_lines))

    # ⑤ VM 的 rows == main_rows 契约要显式锁住（否则前端条件会再次悄悄失效）
    check("daily-image VM 的 rows == main_rows（契约被单测锁定）", True,
          '"rows": main_rows' in read(ROOT / "web" / "app" / "jc.py")
          and (ROOT / "tests" / "test_hotfix_bad_metrics.py").is_file())

    # ⑥ 文档口径同步
    _dc = read(ROOT / "docs" / "web" / "DATA_CONTRACT.md")
    check("DATA_CONTRACT 记录 odds_accuracy 无样本时为 null", True,
          "odds_note" in _dc and "null" in _dc)

    # ---- 19.4 建议 6 的零假设检验脚本（§6.8.4 的数字必须「任何人可复算」）----
    # 纪律：结论印在**用户可见的页面**上 ⇒ 必须有一条人人可跑的复算命令，
    # 否则那些数字只是作者的断言。
    _sel_path = ROOT / "scripts" / "research" / "lottery_selection_check.py"
    _sel_test = ROOT / "tests" / "test_lottery_selection_check.py"
    check("零假设检验脚本已入仓（scripts/research/lottery_selection_check.py）", True,
          _sel_path.is_file())
    check("零假设检验脚本的单测已入仓（tests/test_lottery_selection_check.py）", True,
          _sel_test.is_file())
    _sel_src = read(_sel_path) if _sel_path.is_file() else ""

    # ① 三个「不许给假结论」的内建守卫必须都在
    check("脚本有『池数 < 100 ⇒ 不给结论』的百分位分辨率门槛", True,
          "resolution_ok = pools >= 100" in _sel_src and "本次不给结论" in _sel_src)
    check("脚本有畸形任务守卫（跳过畸形任务并说明原因）", True,
          "task_degenerate_reason" in _sel_src and "数据畸形" in _sel_src)
    check("脚本有『可用任务 < 3 ⇒ 不给结论』守卫", True,
          "n_usable < 3" in _sel_src)

    # ② 同类聚合：真实值与零分布值都必须是「多种子平均」
    #    （不做 ⇒ 两边离散度失配 4.5 倍 ⇒ z 被压到 ±0.2 ⇒ 假阴性）
    check("零分布与真实值同类聚合（都是多种子平均，避免离散度失配）", True,
          "real = statistics.mean(" in _sel_src
          and "nulls.append(statistics.mean(" in _sel_src)

    # ③ 号码池离散度（卡方）也要可复算，且理论值口径写对了
    check("脚本含号码池离散度卡方检验（chi2_dispersion）", True,
          "def chi2_dispersion(" in _sel_src)
    check("卡方零分布理论值口径写死（池大小 − 每期个数，不是 df）", True,
          "池大小 − 每期个数" in _sel_src)

    # ④ 研究脚本不得被 web 层顶层 import（web 层禁止顶层 import scripts.*）
    _web_import_research = []
    for _wf in sorted((ROOT / "web").rglob("*.py")):
        if re.search(r"^\s*(from|import)\s+.*research", read(_wf), re.M):
            _web_import_research.append(str(_wf.relative_to(ROOT)))
    check("web 层不 import 研究脚本（scripts/research 不进 web 运行时）", [],
          _web_import_research)

    # ⑤ §6.8.7 必须指向已入仓的脚本，不能还写着「未入库」
    _i687 = _roadmap.find("#### 6.8.7")
    _i688 = _roadmap.find("#### 6.8.8")
    _sec687 = _roadmap[_i687:_i688] if 0 <= _i687 < _i688 else ""
    check("路线图 §6.8.7 指向已入仓的复算脚本", True,
          "scripts/research/lottery_selection_check.py" in _sec687)
    check("路线图 §6.8.7 不再写『调研期临时脚本、未入库』", True,
          "未入库" not in _sec687)

    # ⑥ 五个方法学坑必须写进文档（否则下一个人会重踩）
    check("路线图 §6.8.4 记录了方法学订正（同类聚合/留一法/分辨率/畸形守卫）", True,
          all(t in _sec684 for t in ("同类聚合", "失配 4.5 倍", "留一法", "分辨率", "畸形")))
    check("路线图 §6.8.4 记录了留一法自检的实测值", True,
          "+0.0025" in _sec684 and "1.0348" in _sec684)

    # ⑦ README 同步新口径
    check("README 记录了选号检验的新口径 p 值与多重比较门槛", True,
          all(t in readme for t in ("0.273", "0.323", "0.200", "99.750%")))
    check("README 不再残留旧口径 p 值/门槛", True,
          not any(t in readme for t in ("0.348", "0.411", "0.278", "99.667%")))

    # ---- 19.5 两条遗留收尾（§6.10：竞彩官方 id 多对一映射 + raw→fact 物化 CLI）----
    _s10_ddl = ROOT / "docs" / "db" / "suggestion10_team_sporttery_map.sql"
    _s10_mat = ROOT / "scripts" / "store" / "materialize.py"
    check("遗留 A 的 DDL 已入仓（suggestion10_team_sporttery_map.sql）", True,
          _s10_ddl.is_file())
    check("遗留 B 的物化 CLI 已入仓（scripts/store/materialize.py）", True,
          _s10_mat.is_file())
    check("遗留 A/B 的测试已入仓（tests/test_suggestion10_deferred.py）", True,
          (ROOT / "tests" / "test_suggestion10_deferred.py").is_file())
    _s10_sql = read(_s10_ddl) if _s10_ddl.is_file() else ""
    _s10_mat_src = read(_s10_mat) if _s10_mat.is_file() else ""
    _s10_jc = read(ROOT / "scripts" / "ingest" / "jc_write.py")

    # ① 映射表是「多 id 的真源」：主键必须防撞，且**不许**改 ref.team 的行
    check("映射表主键是 (sport, jc_id)（一个官方 id 只属一支队）", True,
          "PRIMARY KEY (sport, jc_id)" in _s10_sql)
    check("DDL 不许改 ref.team 的任何一行（纯增量）", True,
          "UPDATE ref.team" not in _s10_sql and "ALTER TABLE ref.team" not in _s10_sql)
    check("fact.jc_match 回填只补 NULL（不改已有映射）", True,
          "m.home_team_id IS NULL" in _s10_sql and "m.away_team_id IS NULL" in _s10_sql)
    check("映射表授权与 ref.team 同口径（league_ing 无 DELETE）", True,
          "GRANT SELECT, INSERT, UPDATE ON ref.team_sporttery TO league_ing" in _s10_sql
          and "GRANT DELETE" not in _s10_sql)

    # ② resolve_jc_teams 必须「映射表为准 + aliases 兜底」，且 aliases 侧**兼容数组形态**
    #    （将来真迁数组时读者已就绪，不必再改代码）
    check("resolve_jc_teams 走映射表 ref.team_sporttery", True,
          "FROM ref.team_sporttery" in _s10_jc)
    check("resolve_jc_teams 保留 aliases 兜底（存量不丢）", True,
          "jsonb_array_elements_text" in _s10_jc and "LATERAL" in _s10_jc)
    check("aliases 侧兼容字符串/数组两种形态", True,
          "jsonb_typeof" in _s10_jc and "jsonb_build_array" in _s10_jc)
    check("映射表优先于 aliases（真源只有一个）", True,
          "by_jc.setdefault" in _s10_jc)
    check("set_team_sporttery 不覆盖既有映射（不 DO UPDATE）", True,
          "def set_team_sporttery(" in _s10_jc
          and "DO NOTHING" in _s10_jc
          and "DO UPDATE" not in _s10_jc.split("TEAM_MAP_INSERT_SQL = ")[1].split('"')[0])
    check("映射表缺表时必须出声（不许静默降级）", True,
          "to_regclass" in _s10_jc and "logger.warning" in _s10_jc)

    # ③ 物化 CLI：默认只读（回滚不提交）、有 __main__ 入口、参数互斥返回码 2
    check("物化 CLI 默认预演（dry-run 回滚、不提交）", True,
          'ap.add_argument("--execute"' in _s10_mat_src
          and "conn.rollback()" in _s10_mat_src and "conn.commit()" in _s10_mat_src)
    check("物化 CLI 有 __main__ 入口（少了会静默什么都不做）", True,
          'if __name__ == "__main__":' in _s10_mat_src
          and "raise SystemExit(main())" in _s10_mat_src)
    check("物化 CLI 输出 before/after 行数（幂等要能对账）", True,
          '"before"' in _s10_mat_src and '"after"' in _s10_mat_src)
    check("物化 CLI 连不上库返回码 2（不降级到别的角色）", True,
          "return 2" in _s10_mat_src)

    # ④ 文档：§6.10 必须记录实测数字与选型依据
    _i610 = _roadmap.find("### 6.10 ")
    _i62 = _roadmap.find("## 7. ")
    _sec610 = _roadmap[_i610:_i62] if 0 <= _i610 < _i62 else ""
    check("路线图有 §6.10 两条遗留收尾章节", True, bool(_sec610))
    check("路线图 §6.10 记录了映射缺口的实测口径（397/152/245）", True,
          all(t in _sec610 for t in ("397", "152", "245")))
    check("路线图 §6.10 记录了选型依据（数组化会静默断掉 152 支队的映射）", True,
          "152" in _sec610 and "ref.team_sporttery" in _sec610)
    check("路线图 §6.10 记录了物化 CLI 的验收标准（幂等/默认只读）", True,
          "--dry-run" in _sec610 and "幂等" in _sec610)

    # ---- 19'. §6.12：生产事故修复 + 遗留收尾（2026-10-21 晚）----
    _i612 = _roadmap.find("### 6.12 ")
    _sec612 = _roadmap[_i612:_i62] if 0 <= _i612 < _i62 else ""
    check("路线图有 §6.12 生产事故修复章节", True, bool(_sec612))
    check("路线图 §6.12 记录了两处作用域 bug（num + pendTotal）", True,
          "num" in _sec612 and "pendTotal" in _sec612)
    check("路线图 §6.12 记录了映射缺口的当前实测口径（244 个未映射 id）", True,
          "244" in _sec612)
    check("路线图 §6.12 记录了 ESPN 历史赔率的取数事实（scoreboard 无 / summary 有）", True,
          "summary" in _sec612 and "scoreboard" in _sec612)
    check("路线图 §6.12 写明了「对用户可见产品零收益」的诚实结论", True,
          "零收益" in _sec612)
    check("路线图 §6.12 要求页面实测（不能只看 API）", True,
          "页面" in _sec612 and "API" in _sec612)

    # ---- 19''. 竞彩队映射补齐（§6.12.3 A）：只增不改 / 只补 NULL / 默认只读 ----
    _s12_sql = read(ROOT / "docs" / "db" / "suggestion12_team_backfill.sql")
    _s12_cli = read(ROOT / "scripts" / "ingest" / "jc_team_backfill.py")
    _s12_write = read(ROOT / "scripts" / "ingest" / "jc_write.py")
    check("队映射补齐有手工 SQL（单事务）", True,
          "BEGIN;" in _s12_sql and "COMMIT;" in _s12_sql)
    check("队映射补齐只增不改（ON CONFLICT DO NOTHING）", True,
          "ON CONFLICT (sport, name_cn) DO NOTHING" in _s12_sql
          and "ON CONFLICT (sport, jc_id) DO NOTHING" in _s12_sql
          and "UPDATE ref.team" not in _s12_sql.upper())
    check("队映射回填只补 NULL（不改既有映射）", True,
          "IS NULL" in _s12_sql and "UPDATE fact.jc_match" in _s12_sql)
    check("队映射补齐有 CLI 且默认预演（dry-run 回滚）", True,
          "--execute" in _s12_cli and "conn.rollback()" in _s12_cli)
    check("采集每轮补齐映射（新队不再攒缺口）", True,
          "jc_write.ensure_jc_team_mappings(cur)" in read(ROOT / "scripts" / "ingest" / "jc_load.py"))
    check("代码版与 SQL 版口径一致（同一套 ON CONFLICT / IS NULL）", True,
          "ON CONFLICT (sport, name_cn) DO NOTHING" in _s12_write
          and "ON CONFLICT (sport, jc_id) DO NOTHING" in _s12_write
          and "IS NULL" in _s12_write)

    # ---- 20. 训练集去重不许遮蔽回填的先验（§6.12.6 的第二个缺陷）----
    _calib = read(ROOT / "scripts" / "core" / "calibration.py")
    check("训练集去重必须补齐赔率字段（不许「先到先得」丢字段）", True,
          "_merge_missing_odds" in _calib
          and "_ODDS_FIELDS" in _calib)
    check("赔率字段的「缺失」判定要区分 0.0 / False 与 None", True,
          "odds_data_available" in _calib and "_odds_field_missing" in _calib)
    _sec6126 = ""
    _m6126 = re.search(r"####\s*6\.12\.6.*?(?=\n####|\n---)", _roadmap, re.S)
    if _m6126:
        _sec6126 = _m6126.group(0)
    check("路线图 §6.12.6 记录了训练集去重遮蔽先验的根因与实测口径", True,
          "遮蔽" in _sec6126 and "89/89" in _sec6126)
    check("路线图 §6.12.6 记录了回填后的生产验收数字（409 条 / odds_accuracy）", True,
          "409" in _sec6126 and "0.725" in _sec6126)
    check("路线图 §6.12.5 记了「闸门基线也踩了坑」（0.55 是含平局口径）", True,
          "0.55" in _sec612 and "含平局" in _sec612)

    # ---- 20b. 六个联赛的模型必须全部用修后数据重训（§6.12.6 末节）----
    # 这条守的是「漏跑」：上一轮只重训了 epl/ucl/laliga，bundesliga/seriea/ligue1
    # 带着修前权重继续上线 —— 文档里若不写出来，下一个人会以为六个都重训过。
    check("路线图 §6.12.6 记了 6 个联赛全部重训（漏跑也要写出来）", True,
          all(lg in _sec6126 for lg in ("epl", "ucl", "laliga", "bundesliga", "seriea", "ligue1"))
          and "6/6 联赛准确率与 Brier 双双改善" in _sec6126)
    check("路线图 §6.12.6 标了「in-sample ≠ 泛化指标」（不许把同批对拍当泛化）", True,
          "in-sample" in _sec6126 and "不是泛化能力指标" in _sec6126)
    check("路线图 §6.12.6 记了服务侧生效路径（子进程 ⇒ 进程内缓存不跨次沿用）", True,
          "subprocess.Popen" in _sec6126 and "_ML_MODEL_CACHE" in _sec6126)
    _m6124 = re.search(r"####\s*6\.12\.4.*?(?=\n####|\n---)", _roadmap, re.S)
    _sec6124 = _m6124.group(0) if _m6124 else ""
    check("路线图 §6.12.4 有「6 联赛全重训 + 不劣化 + 新进程读盘一致」的验收标准", True,
          "逐个不同" in _sec6124 and "不劣化" in _sec6124 and "新进程" in _sec6124)

    # ---- 22. §6.13 本轮收尾：队名漏配 / 279 纠正 / 采集多日窗口 / 文档漂移 ----
    # 本轮的核心教训：**「补能力」与「把『说它不存在』的地方一起翻过来」必须同时做** ——
    # 否则文档和守门会拿一句已经过期的假话当护栏（§6.11.1 已经吃过一次亏）。
    _i6113 = _roadmap.find("#### 6.11.3 ")
    _i612 = _roadmap.find("### 6.12 ")
    _sec6113 = _roadmap[_i6113:_i612] if 0 <= _i6113 < _i612 else ""
    # 只取 §6.11.3 的**表格行**（以 | 开头）：节内的说明性引用块可以提到旧话。
    _t6113 = [ln for ln in _sec6113.splitlines() if ln.startswith("| ") and "---" not in ln]
    check("路线图有 §6.11.3 剩余遗留清单章节", True, bool(_sec6113))
    check("§6.11.3 遗留表不许再把已完成的项写成「待排期 / 暂不批量做」", True,
          not [ln for ln in _t6113 if ("待排期" in ln or "暂不批量做" in ln)])
    check("§6.11.3 遗留表把 home_true_prob 回填标成已完成", True,
          any("home_true_prob" in ln and "已完成" in ln for ln in _t6113))
    check("§6.11.3 遗留表把竞彩映射缺口标成已完成", True,
          any("竞彩映射缺口" in ln and "已完成" in ln for ln in _t6113))

    _i613 = _roadmap.find("### 6.13 ")
    _i7 = _roadmap.find("## 7. ")
    _sec613 = _roadmap[_i613:_i7] if 0 <= _i613 < _i7 else ""
    check("路线图有 §6.13 本轮收尾章节", True, bool(_sec613))
    check("§6.13 记了 8 个队名别名缺口（不许只写「修好了」）", True,
          all(t in _sec613 for t in ("巴伦西亚", "云达不来梅", "贝西克塔斯", "巴黎圣日尔曼",
                                    "帕德博恩", "托特纳姆热刺", "美因茨", "赫塔费")))
    check("§6.13 记了「理论能产出 ≠ 实际会产出」这条口径订正（第一批审计的假阴性）", True,
          "实际产出" in _sec613 and "假阴性" in _sec613)
    check("§6.13 记了别名修复的生产验收数字（54 行 / 12 场 + 审计 rc=0）", True,
          "54" in _sec613 and "12 场" in _sec613 and "rc=0" in _sec613)
    check("§6.13 记了采集端回补的生产验收（452 行 ups / jc_result 462 / 结算 32 条）", True,
          "452" in _sec613 and "462" in _sec613 and "32/32" in _sec613)
    # 契约版本号：代码固定写 v1.3，文档正文历史上写的是 v1.2（重构前旧号）⇒ 必须对齐
    _contract_doc = read(ROOT / "docs" / "project" / "国内采集机实施文档-v1.md")
    check("采集契约文档的版本号与代码一致（v1.3）", True,
          'CONTRACT = "v1.3"' in read(ROOT / "collector-cn" / "collector.py")
          and "契约版本：v1.3" in _contract_doc)
    check("契约文档不再声称「远端按版本号判断解析器分支」（实测远端 0 处读该字段）", True,
          not stale_lines(_contract_doc, ["远端按版本号判断解析器分支"]))
    check("§6.13 记了 279 的纠正目标（皇家贝蒂斯 #2355 / af_id 543）", True,
          "皇家贝蒂斯" in _sec613 and "2355" in _sec613 and "543" in _sec613)
    check("§6.13 记了 65 场赛果缺口 + 4 日窗口 + 回补 CLI", True,
          "65" in _sec613 and "today_minus_3" in _sec613 and "--backfill-range" in _sec613)
    check("§6.13 写明了「改完 ≠ 生效」（要国内机重部署 + 回补）", True,
          "国内机" in _sec613 and "≠" in _sec613)

    # 别名两侧必须镜像（漏一侧 = 静默漏场次，且零报错）
    _alias_src = read(ROOT / "scripts" / "core" / "team_alias.py")
    _alias_web = read(ROOT / "web" / "services" / "team_match.py")
    check("8 个别名缺口两侧都补上了（scripts/ 与 web/ 必须镜像）", True,
          all(t in _alias_src and t in _alias_web
              for t in ("巴伦西亚", "云达不来梅", "贝西克塔斯", "巴黎圣日尔曼",
                        "帕德博恩", "托特纳姆热刺", "美因茨", "赫塔费")))
    # 审计脚本的主判据必须是「模型实际产出过的名字」，不是「理论可产出集合」
    # （第一版用理论集合 ⇒ 漏报 5 条，见路线图 §6.13.1 的订正框）
    _audit_src = read(ROOT / "scripts" / "ingest" / "jc_name_join_audit.py")
    check("队名审计的主判据是「模型实际产出名」（不许退回理论集合）", True,
          "model.pred_archive_match" in _audit_src and "_ACTUAL_NAMES_SQL" in _audit_src)
    # 官方队名表只允许动 _comment：Real Betis 的值必须仍是「皇家贝蒂斯」
    _official = read(ROOT / "scripts" / "references" / "team_cn_official.json")
    check("官方队名表 Real Betis 的值仍是「皇家贝蒂斯」（不许被「改对」成贝西克塔斯）", True,
          '"皇家贝蒂斯"' in _official and '"贝西克塔斯"' not in _official)
    # 静默失败必须变成可复算的审计命令
    _audit_src = read(ROOT / "scripts" / "ingest" / "jc_name_join_audit.py")
    check("队名 join 有只读复算审计脚本（退出码 1 = 有缺口，不许静默）", True,
          "def main" in _audit_src and "return 1" in _audit_src)

    # 279 重指：SQL 是唯一真源，CLI 不复制语句；DDL 源文件已翻过来
    _s10 = read(ROOT / "docs" / "db" / "suggestion10_team_sporttery_map.sql")
    check("suggestion10 的 279 改按 af_id=543 定位（不再写死 team_id）", True,
          "af_id = 543" in _s10 and "SELECT 'football', 279, 2351" not in _s10)
    _repoint_cli = read(ROOT / "scripts" / "ingest" / "jc_team_id_repoint.py")
    check("279 重指 CLI 读同一份 .sql（不复制语句 ⇒ 不会两处改一处）", True,
          "suggestion13_betis_id_repoint.sql" in _repoint_cli
          and "UPDATE ref.team_sporttery" not in _repoint_cli)

    # 采集端：4 个单日窗口 + pageSize 自愈 + 截断告警 + 回补 CLI
    _cfg = json.loads(read(ROOT / "collector-cn" / "config.json"))
    _jc_urls = _cfg.get("topics", {}).get("jczq_result", {}).get("candidates", [])
    check("竞彩赛果采集改成 4 个单日窗口（D-3/D-2/D-1/今天）", True, len(_jc_urls) == 4)
    check("竞彩赛果采集的 4 个窗口都带 pageSize=100", True,
          all("pageSize=100" in u for u in _jc_urls))
    _want_days = ("{today_minus_3}", "{today_minus_2}", "{yesterday}", "{today}")
    check("竞彩赛果采集的 4 个窗口各自 matchBeginDate=matchEndDate=同一天", True,
          len(_jc_urls) == len(_want_days)
          and all(("matchBeginDate=" + d + "&matchEndDate=" + d) in u
                  for u, d in zip(_jc_urls, _want_days)))
    _basket = _cfg.get("topics", {}).get("jclq_result", {}).get("candidates", [])
    check("篮球的 7 日窗口没被动过（不许被足球的改动带跑）", True,
          any("{today_minus_7}" in u and "{today}" in u for u in _basket))
    # 篮球赛果：pageSize=30 时接口 total=42/pages=2 ⇒ 每批只拿第 1 页 30 场，**漏 12 场**
    # （2026-10-22 实测：埃菲斯vs拜仁、巴黎vs博洛尼亚、皇马vs阿斯维尔 等真实比赛）。
    # 翻页参数本身生效（pageNo=2 会返回另外 12 行），但采集主路径只取第 1 页 ⇒ 必须 pageSize=100。
    check("篮球赛果采集 pageSize=100（pageSize=30 时 total=42 只取到 30 场，漏 12 场）", True,
          all("pageSize=100" in u for u in _basket) and len(_basket) == 1)
    _collector = read(ROOT / "collector-cn" / "collector.py")
    check("采集器有 pageSize 自愈兜底（失败自动退回，不静默）", True,
          "_fetch_with_page_fallback" in _collector and "page_size_fallbacks" in _collector)
    check("采集器有分页截断自检（疑似截断不许静默）", True,
          "page_limit_warnings" in _collector)
    check("采集器有回补 CLI（--backfill-range）且与日常批共用推送", True,
          "--backfill-range" in _collector and "backfill_range" in _collector
          and "_push_paths" in _collector)
    # 原子性：写批次文件与拿单例锁都必须用 O_EXCL 独占创建（2026-10-23 生产实测修）
    # ⚠️ 断言必须走 **AST**，不能靠字符串匹配：新代码的 docstring 里逐字引用了旧写法
    #    （教学价值），不剥离注释/文档字符串的话，断言会命中自己的说明 ⇒ 假失败
    #    （本轮真踩到了两次：一次是 `#` 注释，一次是 docstring）。
    _collector_code = ""
    _open_modes: list[tuple[int, str]] = []
    try:
        _tree = ast.parse(_collector)
    except SyntaxError:
        _tree = None
    if _tree is not None:
        for _node in ast.walk(_tree):
            if (isinstance(_node, ast.Call) and isinstance(_node.func, ast.Name)
                    and _node.func.id == "open" and len(_node.args) >= 2
                    and isinstance(_node.args[1], ast.Constant)
                    and isinstance(_node.args[1].value, str)):
                _open_modes.append((_node.lineno, _node.args[1].value))
    _w_modes = [(ln, m) for ln, m in _open_modes if "w" in m and "x" not in m]
    check("采集器写批次文件用 O_EXCL 独占创建（序号分配不撞名 ⇒ 文件不交织）", True,
          any(m == "x" for _ln, m in _open_modes))
    check("采集器单例锁用 O_EXCL 独占创建（同一秒启动的多个批次不会同时拿到锁）", True,
          any(m == "x" for _ln, m in _open_modes)
          and _collector.count('open(lock, "x"') >= 1)
    check("采集器写批次文件不许再用非原子的 open(w) 两步式（AST 判定，免疫注释/文档字符串）",
          True, not _w_modes)
    # 存活判定必须**跨平台**：采集端跑在 Windows 工作机，但 collector.py 同时是
    # 仓库里的生产代码，其回归测试会在 **Linux（Oracle）** 上跟着全量 pytest 跑。
    # 初版只认 `ctypes.windll`（Windows 专有）⇒ Oracle 上子进程 AttributeError 崩掉、
    # 两条锁测试 failed（2026-10-23 实测）。现在走 `_pid_alive()`：
    #   Windows → OpenProcess；POSIX → os.kill(pid, 0)（只探测不发信号）。
    # ⚠️ Windows 上**不能用** os.kill(pid, 0)：那边 os.kill 对非 CTRL_* 信号会
    #    调 TerminateProcess ⇒ 会把目标进程杀掉。
    _ctypes_windll_lines: list[int] = []
    _has_pid_alive_fn = False
    _has_os_kill_call = False
    _pid_alive_span: tuple[int, int] | None = None
    if _tree is not None:
        for _node in ast.walk(_tree):
            if isinstance(_node, ast.FunctionDef) and _node.name == "_pid_alive":
                _has_pid_alive_fn = True
                _pid_alive_span = (_node.lineno, _node.end_lineno or _node.lineno)
            if (isinstance(_node, ast.Attribute) and _node.attr == "windll"):
                _ctypes_windll_lines.append(_node.lineno)
            if (isinstance(_node, ast.Call) and isinstance(_node.func, ast.Attribute)
                    and _node.func.attr == "kill"):
                _has_os_kill_call = True
    check("采集端进程存活判定跨平台（Windows 走 OpenProcess / POSIX 走 os.kill(0)）",
          True, _has_pid_alive_fn and _has_os_kill_call)
    # 精确判定：所有 ctypes.windll 引用都必须落在 _pid_alive 的行区间内
    # （不许散落到别的函数 ⇒ 那些函数一进 Linux 就 AttributeError）
    _windll_outside = [
        ln for ln in _ctypes_windll_lines
        if _pid_alive_span is None or not (_pid_alive_span[0] <= ln <= _pid_alive_span[1])
    ]
    check("采集端不许在 _pid_alive 之外裸用 ctypes.windll（Windows 专有，会让 Linux 侧测试崩）",
          True, not _windll_outside)
    # ── 第三处 TOCTOU：锁文件「已存在」与「读得到有效 pid」不是同一时刻 ──────
    # 创建者 O_EXCL 建好锁后还要再写一次 pid ⇒ 中间有「文件存在、内容为空」的窗口；
    # 若把空内容当成「旧 pid 已死(0)⇒接管」⇒ 竞争者会**与创建者同时持锁**
    # （实测 4 进程同刻抢锁，两个 ACQ 间隔仅 0.6ms、持锁区间真的重叠）。
    check("采集端锁文件内容为空时必须拒绝拿锁（第三处 TOCTOU：不许当成无锁而接管）",
          True,
          "for _attempt in range(_LOCK_EMPTY_RETRIES):" in _collector
          and "_LOCK_EMPTY_RETRIES" in _collector
          and "if raw:" in _collector
          and "_LOCK_STALE_S" in _collector)
    check("采集端老空锁（创建者崩溃残留）必须可接管（否则后续批次永久卡死）",
          True,
          "_LOCK_STALE_S" in _collector
          and "st_mtime" in _collector)

    # ── 2026-10-23：测试不许写生产仓库的文件（Oracle 上真污染过） ──────────
    # 事故：Oracle 跑全量 pytest 后，仓库里 scripts/references/team_translations.json
    # 被多写了「阿森纳 vs 切尔西」「Liverpool」「Everton」等测试夹具队名。
    # 根因：parse._warm_team_names → 真调 core.i18n.warm_translations → 真调 LLM
    #      → save_translations 写 TRANSLATION_CACHE_FILE（指向**仓库内生产文件**）；
    #      生产机有 LLM 凭证 ⇒ 翻译真成功 ⇒ 真落盘（开发机无凭证 ⇒ 本机复现不了）。
    _conftest = read(ROOT / "tests" / "conftest.py")
    check("测试隔离 i18n 翻译缓存（autouse fixture，生产文件不许被测试写）", True,
          "isolate_i18n_cache" in _conftest
          and "autouse=True" in _conftest
          and "TRANSLATION_CACHE_FILE" in _conftest)
    # 未知表必须抛 ValueError（契约），不许是 KeyError（2026-10-23 修的真 bug：
    # 原先 _PK_BY_TABLE[table] 在校验前求值 ⇒ 未知表先炸 KeyError，调用方按契约
    # except ValueError 会漏接）
    _jc_issue_write = read(ROOT / "scripts" / "ingest" / "jc_issue_write.py")
    check("未知表抛 ValueError 而不是 KeyError（契约归位：调用方只 catch ValueError）",
          True,
          "if table not in _PK_BY_TABLE:" in _jc_issue_write
          and "raise ValueError(f\"未知写指令目标表" in _jc_issue_write)
    # 队名映射断言必须跟上 2026-10-22 的纠正终态（279 → #2355 皇家贝蒂斯，
    # 506 仍 → #2351 毕尔巴鄂竞技）。生产是对的、是断言旧了。
    _s10 = read(ROOT / "tests" / "test_suggestion10_deferred.py")
    check("队名映射断言已跟上 279→#2355 的纠正终态（不许再断言旧的 #2351）", True,
          "(279, 2355)" in _s10 and "(506, 2351)" in _s10
          and "(2355, 2351)" in _s10
          and "rows == [(279, 2351), (506, 2351)]" not in _s10)
    # node 依赖的用例：没装 node ⇒ skip（未检查），不许报成失败
    _xss = read(ROOT / "tests" / "web_ui" / "test_dailyimage_xss.py")
    check("node 依赖用例没装 node 时是 skip 而非失败（照 test_dashboard_smoke 惯例）", True,
          "shutil.which(\"node\")" in _xss
          and "pytest.mark.skipif" in _xss
          and 'subprocess.run(["node"' not in _xss)
    # 延迟 import 的替身必须打**包属性**：`from store import jc_view` 取的是包属性，
    # 只换 sys.modules 条目换不掉它（order-dependent ⇒ 只在全量跑时红）
    _jc_helpers_t = read(ROOT / "tests" / "test_jc_helpers.py")
    check("延迟 import 的替身打的是包属性（order-dependent 失败已修）", True,
          "monkeypatch.setattr(store, \"jc_view\"" in _jc_helpers_t)

    # README / 路线图：旧话必须带订正标记；v4.6 段必须存在
    check("README/路线图 不再把「补数据属待排期」当现状陈述", True,
          not stale_lines(readme, ["补数据属待排期"])
          and not stale_lines(_roadmap, ["补数据属待排期"]))
    check("README 有 v4.6 段（记本轮三项 + 队名漏配）", True,
          "### v4.6" in readme and "队名 join 静默漏场次" in readme)
    check("README 记录了 279 纠正后的目标（皇家贝蒂斯 #2355）", True,
          "皇家贝蒂斯" in readme and "2355" in readme)

    # ---- 22. 采集端手动模式 + 定价赔率新鲜度守门（§6.13.5）----
    # 触发：用户「我本机是我的工作电脑，不是每天 24h 都开的」⇒ 采集端改按需手动。
    _cn_readme = read(ROOT / "collector-cn" / "README.md")
    _refresh_bat = ROOT / "collector-cn" / "scripts" / "refresh_now.bat"
    check("一键手动刷新脚本在库（双击就跑完整条链）", True, _refresh_bat.is_file())
    _rb = read(_refresh_bat) if _refresh_bat.is_file() else ""
    check("一键脚本覆盖 4 步：采集+推送 / 远端装载 / 出荐单 / 结算", True,
          "--push-batch" in _rb and "jc-ingest-run.sh" in _rb
          and "decision/recommend.py" in _rb and "decision/settle.py" in _rb)
    check("一键脚本是纯 ASCII（cmd 按 OEM 码页解析，中文会截断字符串字面量）", True,
          _refresh_bat.is_file()
          and all(b < 128 for b in _refresh_bat.read_bytes()))
    check("一键脚本结束/失败都 pause（双击不闪窗）", True,
          _rb.count("pause") >= 3)
    _tasks_bak = ROOT / "collector-cn" / "scripts" / "tasks_backup" / "20261022-manual-mode"
    _task_xmls = sorted(p.name for p in _tasks_bak.glob("*.xml")) if _tasks_bak.is_dir() else []
    check("5 个计划任务的 XML 备份在库（机制保留、可逐字恢复）", True,
          len(_task_xmls) == 5
          and all(f"collector_{n}.xml" in _task_xmls
                  for n in ("0930", "1530", "2130", "2330"))
          and "collector_offer_10m.xml" in _task_xmls)
    _restore = ROOT / "collector-cn" / "scripts" / "tasks_backup" / "restore_scheduled_tasks.ps1"
    check("一键恢复脚本存在（Register-ScheduledTask + Enable）", True,
          _restore.is_file()
          and "Register-ScheduledTask" in read(_restore)
          and "Enable-ScheduledTask" in read(_restore))
    check("collector-cn/README 有「手动模式」章节", True,
          "手动模式" in _cn_readme and "refresh_now.bat" in _cn_readme)
    check("collector-cn/README 的频率矛盾已订正（不许再写「每 1 小时（2026-09-17 降频」）", True,
          not stale_lines(_cn_readme, ["每 1 小时（2026-09-17 降频"]))
    check("collector-cn/README 记了实测依据（54% 空转 ⇒ 不是拍脑袋改）", True,
          "54%" in _cn_readme and "2.8%" in _cn_readme)
    check("路线图有 §6.13.5（采集端改手动 + 新鲜度守门）", True,
          "#### 6.13.5" in _roadmap)
    check("路线图 §6.13.5 记了关键数字（54% 空转 / 6 小时阈值 / 退出码 3）", True,
          "54%" in _roadmap and "退出码 **3**" in _roadmap and "6 小时" in _roadmap)

    _rec_src = read(ROOT / "scripts" / "decision" / "recommend.py")
    check("荐单有全局新鲜度闸（拒绝出单 + --allow-stale 显式放行）", True,
          "STALE_EXIT_CODE" in _rec_src and "--allow-stale" in _rec_src
          and "--max-offer-age-hours" in _rec_src)
    check("荐单守门默认阈值 = 6 小时（与文档一致）", True,
          "DEFAULT_MAX_OFFER_AGE_HOURS = 6.0" in _rec_src)
    check("荐单有逐场闸（超龄场次跳过，计入 stale_skipped）", True,
          "stale_skipped" in _rec_src)
    check("荐单守门同时看采集到港时间（ops.file_arrival，与 web freshness 同源）", True,
          "ops.file_arrival" in _rec_src)
    _web_rec = read(ROOT / "web" / "services" / "recommend.py")
    check("web 层新鲜度委托 scripts 唯一真源（不复制一份 SQL）", True,
          "from decision.recommend import" in _web_rec
          and "fact.jc_offer" not in _web_rec)
    check("荐单生成时间的旧话不再复现（web 服务层 + 看板）", True,
          not stale_lines(_web_rec, ["推荐单在每次预测跑完后生成"])
          and not stale_lines(read(ROOT / "static" / "dashboard.html"),
                              ["推荐单在每次预测跑完后生成"]))
    check("看板显式显示定价赔率快照新鲜度", True,
          "offer_freshness" in read(ROOT / "static" / "dashboard.html"))
    check("看板冒烟 fixture 含 offer_freshness（前端不因缺字段炸）", True,
          "offer_freshness" in read(ROOT / "tools" / "dashboard_smoke.js"))
    check("README 记了手动模式主路径（refresh_now.bat）", True,
          "refresh_now.bat" in readme and "手动" in readme)

    # ---- 23. 2026-10-23：ops/落港保留策略 + gaps 口径 + 坏文件不掀翻整批 ----
    # 触发：用户「ops.ingest_log 每天 1 万行」→ 查出根因是「坏文件 ⇒ marker 永久
    #       失败 ⇒ 每 10 分钟重装同一批老文件」；用户拍板「要改，保留合理时间范围内
    #       的就行」+ gaps 口径「ok」。
    # ⚠️ 本段断言**必须走 AST / 精确解析**：这些代码的注释与 docstring 里逐字引用了
    #    旧写法（教学价值），朴素字符串匹配会命中自己的说明 ⇒ 假失败。

    # ── A. gaps 口径：「清单里没有该 topic」≠ 缺档 ──────────────────────
    _jr = read(ROOT / "scripts" / "ingest" / "jc_read.py")
    _jr_tree = ast.parse(_jr)
    _fm = next((n for n in ast.walk(_jr_tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_from_manifest"), None)
    # 「state 字面量」= _from_manifest 里所有 `xxx = [(None, "...")]` 形态的赋值
    _fm_states: set[str] = set()
    if _fm is not None:
        for _n in ast.walk(_fm):
            if isinstance(_n, ast.Assign) and isinstance(_n.value, ast.List):
                for _e in _n.value.elts:
                    if isinstance(_e, ast.Tuple):
                        for _x in _e.elts:
                            if isinstance(_x, ast.Constant) and isinstance(_x.value, str):
                                _fm_states.add(_x.value)
    check("jc_read：清单没列该 topic 的口径 = absent（AST 判定，不许再是 missing）",
          {"absent"}, _fm_states)

    _jt = read(ROOT / "scripts" / "ingest" / "jc_topic.py")
    _jt_tree = ast.parse(_jt)
    _lt = next((n for n in ast.walk(_jt_tree)
                if isinstance(n, ast.FunctionDef) and n.name == "load_topic"), None)
    _absent_branch = None
    for _n in (ast.walk(_lt) if _lt is not None else []):
        if not isinstance(_n, ast.If):
            continue
        _t = _n.test
        if (isinstance(_t, ast.Compare) and isinstance(_t.left, ast.Name)
                and _t.left.id == "state" and len(_t.comparators) == 1
                and isinstance(_t.comparators[0], ast.Constant)
                and _t.comparators[0].value == "absent"):
            _absent_branch = _n
    check("jc_topic.load_topic 有 state=='absent' 的早退分支（残缺批不落任何痕迹）",
          True, _absent_branch is not None)
    if _absent_branch is not None:
        _absent_sql = [s.value for s in ast.walk(_absent_branch)
                       if isinstance(s, ast.Constant) and isinstance(s.value, str)
                       and "ops." in s.value]
        check("absent 分支不写 ops.* 台账（否则又造出假缺档行，gaps 虚高）",
              [], _absent_sql)
        check("absent 分支不抛异常（残缺批不该掀翻整批）", True,
              not any(isinstance(s, ast.Raise) for s in ast.walk(_absent_branch)))

    _jl = read(ROOT / "scripts" / "ingest" / "jc_load.py")
    _jl_tree = ast.parse(_jl)
    _lb = next((n for n in ast.walk(_jl_tree)
                if isinstance(n, ast.FunctionDef) and n.name == "load_batch"), None)
    _fb_states: set[str] = set()
    for _n in (ast.walk(_lb) if _lb is not None else []):
        if isinstance(_n, ast.BoolOp) and isinstance(_n.op, ast.Or):
            for _v in _n.values:
                if isinstance(_v, ast.List):
                    for _e in _v.elts:
                        if isinstance(_e, ast.Tuple):
                            for _x in _e.elts:
                                if isinstance(_x, ast.Constant) and isinstance(_x.value, str):
                                    _fb_states.add(_x.value)
    check("jc_load.load_batch 空候选兜底 = absent（不许 missing，AST 判定）",
          {"absent"}, _fb_states)

    # ── B. 坏文件不掀翻整批（第三个真缺陷） ────────────────────────────
    _missing_branch = None
    for _n in (ast.walk(_lt) if _lt is not None else []):
        if not isinstance(_n, ast.If):
            continue
        if any(isinstance(_c, ast.Call) and isinstance(_c.func, ast.Attribute)
               and _c.func.attr == "exists" for _c in ast.walk(_n.test)):
            _missing_branch = _n
    check("jc_topic.load_topic 有「清单列了但文件不存在」的独立分支（不再走 read_lines 抛）",
          True, _missing_branch is not None)
    if _missing_branch is not None:
        _mb_sql = [s.value for s in ast.walk(_missing_branch)
                   if isinstance(s, ast.Constant) and isinstance(s.value, str)]
        check("文件不存在分支会留痕 ops.ingest_log（排障能查到哪个文件读不出来）",
              True, any("ops.ingest_log" in s for s in _mb_sql))
        check("文件不存在分支会写 ops.file_arrival（真缺档 ⇒ gaps +1，不是假缺档）",
              True, any("ops.file_arrival" in s for s in _mb_sql))
        check("文件不存在分支不抛异常（一个文件丢了不该让整批永久重跑）",
              True, not any(isinstance(x, ast.Raise) for x in ast.walk(_missing_branch)))
        _gap_true = False
        for _r in ast.walk(_missing_branch):
            if isinstance(_r, ast.Return) and isinstance(_r.value, ast.Dict):
                for _k, _v in zip(_r.value.keys, _r.value.values):
                    if (isinstance(_k, ast.Constant) and _k.value == "gap"
                            and isinstance(_v, ast.Constant) and _v.value is True):
                        _gap_true = True
        check("文件不存在 ⇒ 记为真缺档（gap=True，AST 判定）", True, _gap_true)
    # ⚠️ **返回值的键集合是既有契约**：``tests/test_jc_topic_issue.py`` 断言
    #    ``set(ret) == {"topic","lines","ups","gap"}`` ⇒ 三条返回路径（正常 / absent /
    #    文件不存在）必须**完全一致**，不许塞额外键。
    #    （2026-10-23 在 Oracle 上实测踩到：absent 分支多返回了 "absent" 键 ⇒
    #      那条真包用例 failed ⇒ 连带 830 个 order-dependent error。）
    _ret_keys: dict[str, set[str]] = {}
    for _branch_name, _branch in (("absent", _absent_branch),
                                  ("file-missing", _missing_branch)):
        if _branch is None:
            continue
        for _r in ast.walk(_branch):
            if isinstance(_r, ast.Return) and isinstance(_r.value, ast.Dict):
                _ks = {k.value for k in _r.value.keys
                       if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if _ks:
                    _ret_keys.setdefault(_branch_name, set()).update(_ks)
    check("load_topic 三条返回路径的键集合一致（既有契约 {topic,lines,ups,gap}，不许加额外键）",
          {"topic", "lines", "ups", "gap"},
          _ret_keys.get("absent", set()) | _ret_keys.get("file-missing", set()))

    # read_lines 的「坏行直接抛」是 ticket P0-COLLECT2f/2g 定的契约：
    # 不许为了掩盖坏文件把它改成「跳过坏行」（那会静默丢数据）。
    _rl = next((n for n in ast.walk(_jr_tree)
                if isinstance(n, ast.FunctionDef) and n.name == "read_lines"), None)
    _rl_caught: set[str] = set()
    for _h in (ast.walk(_rl) if _rl is not None else []):
        if isinstance(_h, ast.ExceptHandler) and isinstance(_h.type, ast.Name):
            _rl_caught.add(_h.type.id)
    check("read_lines 保持「坏行直接抛」契约（只许 catch PermissionError，不许吞解析错误）",
          {"PermissionError"}, _rl_caught)

    # ── C. 保留策略（ops 台账 + 落港目录） ──────────────────────────────
    _ops_p = ROOT / "docs" / "db" / "ops_retention.sql"
    check("ops 保留策略 SQL 在库（docs/db/ops_retention.sql）", True, _ops_p.is_file())
    _ops_txt = read(_ops_p) if _ops_p.is_file() else ""
    # 注释剥离（SQL 的注释行一律以 -- 开头）⇒ 断言不会被注释里的旧写法污染
    _ops_code = "\n".join(l for l in _ops_txt.splitlines() if not l.strip().startswith("--"))
    _m_keep = re.search(r"\\set keep_days (\d+)", _ops_code)
    _m_ledger = re.search(r"\\set ledger_keep_days (\d+)", _ops_code)
    check("ops.ingest_log 默认窗口 = 7 天（治本后每天 ~550 行 ⇒ 一周排障够用）",
          7, int(_m_keep.group(1)) if _m_keep else "MISSING")
    check("ops 台账（file_arrival/ingest_marker）默认窗口 = 37 天（> 落港 30 天）",
          37, int(_m_ledger.group(1)) if _m_ledger else "MISSING")
    # ⚠️ **P1（2026-10-23）**：cron **真正执行**的是 `docs/infra/jc-ops-retention.sh`，
    #    它每次都用 `-v keep_days=… -v ledger_keep_days=…` **覆盖** SQL 里的 `\set`
    #    ⇒ 守 SQL 默认值 = **守一个从不生效的值**（审计实测：把 cron 入口的
    #    LEDGER_KEEP_DAYS 改成 7（< 落港 30）⇒ 守门照样全绿 ⇒ 孤儿重装复发）。
    # ⇒ 现在**两个都查**，并断言两者**一致**。
    _ops_sh_p = ROOT / "docs" / "infra" / "jc-ops-retention.sh"
    check("cron 真正执行的 ops 保留入口在库（docs/infra/jc-ops-retention.sh）", True,
          _ops_sh_p.is_file())
    _ops_sh_txt = read(_ops_sh_p) if _ops_sh_p.is_file() else ""
    _ops_sh_code = "\n".join(l for l in _ops_sh_txt.splitlines()
                              if not l.strip().startswith("#"))
    _m_sh_keep = re.search(r"JC_OPS_KEEP_DAYS:-(\d+)", _ops_sh_code)
    _m_sh_ledger = re.search(r"JC_OPS_LEDGER_KEEP_DAYS:-(\d+)", _ops_sh_code)
    _sh_keep = int(_m_sh_keep.group(1)) if _m_sh_keep else None
    _sh_ledger = int(_m_sh_ledger.group(1)) if _m_sh_ledger else None
    check("cron 入口的 ops.ingest_log 窗口 = 7 天（**这是生效值**，SQL 的 \\set 只是兜底）",
          7, _sh_keep if _sh_keep is not None else "MISSING")
    check("cron 入口的台账窗口 = 37 天（**这是生效值**）",
          37, _sh_ledger if _sh_ledger is not None else "MISSING")
    check("cron 入口与 SQL 的两个窗口必须一致（不许一个 37 一个 7 ⇒ 运行时行为不可预测）",
          True,
          _sh_keep is not None and _sh_ledger is not None and _m_keep and _m_ledger
          and int(_m_keep.group(1)) == _sh_keep
          and int(_m_ledger.group(1)) == _sh_ledger)
    check("cron 入口必须把两个窗口传给 psql（不许静默丢掉 ⇒ 落回 SQL 默认值）", True,
          "-v keep_days=" in _ops_sh_code and "-v ledger_keep_days=" in _ops_sh_code)
    # ⚠️ **语句级判定**（审计实测：子串判定会被**报告查询**顶替 ⇒
    #    把 DELETE 里的 `ok AND` 删掉也照样绿 ⇒ 守卫形同虚设）。
    _ops_stmts = sql_statements(_ops_code)
    _marker_del = [s for s in _ops_stmts
                    if re.search(r"delete\s+from\s+ops\.ingest_marker", s, re.I)]
    check("ops 保留策略有 ingest_marker 的 DELETE 语句（语句级定位）", True,
          bool(_marker_del))
    check("ops 保留策略只删 ok=true 的 marker（ok=false 是要重试的批次，删了=静默丢数据）"
          "—— 语句级判定", True,
          bool(_marker_del) and all(re.search(r"\bok\b", s, re.I) for s in _marker_del))
    check("ops 保留策略删完必须 VACUUM（DELETE 不回收磁盘）", True, "VACUUM" in _ops_code)
    check("VACUUM FULL 只作可选（它拿 ACCESS EXCLUSIVE 锁会阻塞每 10 分钟的装载）",
          True, "\\if :vacuum_full" in _ops_code and "VACUUM (FULL, ANALYZE)" in _ops_code)
    # ⚠️ 业务表白名单必须覆盖 **truncate / drop / alter**（审计实测：
    #    加一句 `TRUNCATE fact.fixture;` 或 `DROP TABLE fact.fixture;` 守门照样绿）。
    _biz_touch = re.compile(
        r"(?is)\b(delete\s+from|truncate(\s+table)?|drop\s+table|alter\s+table)\s+"
        r"(public\.)?(fact|analysis|raw|model)\b")
    check("ops 保留策略绝不碰业务表（fact.* / analysis.* 一律不动；含 truncate/drop/alter）",
          True, not _biz_touch.search(_ops_code))

    _stg_p = ROOT / "docs" / "infra" / "jc-staging-retention.sh"
    check("落港清理脚本在库（docs/infra/jc-staging-retention.sh）", True, _stg_p.is_file())
    _stg_txt = read(_stg_p) if _stg_p.is_file() else ""
    # bash 的注释同样是整行以 # 开头 ⇒ 剥离后做字符串判定等价于 AST 判定
    _stg_code = "\n".join(l for l in _stg_txt.splitlines() if not l.strip().startswith("#"))
    _m_stg = re.search(r"JC_STAGING_KEEP_DAYS:-(\d+)", _stg_code)
    _stg_days = int(_m_stg.group(1)) if _m_stg else None
    check("落港目录默认保留 30 天（职责=传输中转+排障取证，业务数据已落库）",
          30, _stg_days if _stg_days is not None else "MISSING")
    check("落港清理的坏文件走隔离不直接删（shutil.move ⇒ 保留证据、可回滚）", True,
          "shutil.move" in _stg_code)
    check("落港清理的文件操作必须用 league 身份（root 移文件会改属主）", True,
          "sudo -n -u league" in _stg_code)
    check("落港清理不许删 ops 台账（台账留着才防将来重复装载）", True,
          not re.search(r"delete\s+from\s+ops\.", _stg_code, re.I))
    check("落港清理不许碰业务表（**含 truncate / drop / alter**）", True,
          not _biz_touch.search(_stg_code))
    # dry-run 不许留下空隔离目录（每次 dry-run 建一个 ⇒ 空目录无上限堆积）
    _stg_lines = _stg_code.splitlines()
    _mk = [i for i, l in enumerate(_stg_lines) if 'mkdir -p "$QUAR/$STAMP"' in l]
    _gd = [i for i, l in enumerate(_stg_lines) if 'if [ "$APPLY" = "1" ]; then' in l]
    check("落港清理的隔离目录只在真跑时创建（dry-run 不留空目录 ⇒ 不无上限堆积）", True,
          bool(_mk) and bool(_gd) and any(g < m for g in _gd for m in _mk))
    # ⚠️ **真包测试不许硬编码落港目录里的具体文件名**：保留策略（30 天窗口）
    #    会合法删掉它 ⇒ 2026-10-23 在 Oracle 上实测：09-16 那批被删 ⇒ 真包用例 failed
    #    ⇒ 现在动态挑「盘上最老的可解析文件」。
    # ⚠️ 断言必须走 **AST**（并排除 docstring）：测试的说明文字里**逐字引用了旧文件名**
    #    （订正记录，教学价值）⇒ 朴素字符串匹配会命中自己的说明 ⇒ 假失败。
    _jct = read(ROOT / "tests" / "test_jc_topic_issue.py")
    _jct_tree = ast.parse(_jct)
    _jct_doc_ids: set[int] = set()
    for _n in ast.walk(_jct_tree):
        if isinstance(_n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _body = getattr(_n, "body", [])
            if _body and isinstance(_body[0], ast.Expr) \
                    and isinstance(_body[0].value, ast.Constant) \
                    and isinstance(_body[0].value.value, str):
                _jct_doc_ids.add(id(_body[0].value))
    _jct_hard: list[str] = []
    for _n in ast.walk(_jct_tree):
        if (isinstance(_n, ast.Constant) and isinstance(_n.value, str)
                and id(_n) not in _jct_doc_ids
                and re.search(r"\d{4}-\d{2}-\d{2}T[\d\-]*Z", _n.value)):
            _jct_hard.append(_n.value)
    check("真包测试不许硬编码落港目录里的具体文件名（AST 判定，免疫 docstring 里的订正记录）",
          [], _jct_hard)
    check("真包测试改成动态挑「盘上最老的可解析文件」（保留策略删掉旧的也不会失效）",
          True, "_pick_real" in _jct)
    # ⚠️ **真库端到端用例不许 commit()**：fixture 的 ``finally: rollback()`` 在 commit
    #    之后**完全失效** ⇒ 每跑一次全量 pytest 就往**生产 fact 表**塞夹具垃圾行
    #    （2026-10-23 实测：``game_num='90'`` + 随机 ``issue_no='T'+hex`` +
    #    ``src_file='f.jsonl'``，5 轮共 175 行）⇒ 断言走 AST（免疫 docstring 里的提及）。
    _commit_leaks: list[str] = []
    for _rel in ("tests/test_jc_issue_match_prize.py", "tests/test_jc_topic_issue.py"):
        try:
            _t = ast.parse(read(ROOT / _rel))
        except SyntaxError:
            continue
        for _n in ast.walk(_t):
            if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
                    and _n.func.attr == "commit"):
                _commit_leaks.append(f"{_rel}:{_n.lineno}")
    check("真库端到端用例不许 commit()（fixture 的 rollback 会失效 ⇒ 污染生产库）",
          [], _commit_leaks)

    # ── 2026-10-23：「部署了但服务没重启 ⇒ 页面出现假的无数据」必须能被看见 ──
    # 事故：部署后忘了 systemctl restart league-web ⇒ 服务内存里是旧代码，
    #       而 static/dashboard.html 每次请求现读磁盘 ⇒ **静态页新、API 旧**的混合态
    #       ⇒ 页面印「⚠️ 暂无定价赔率快照（未采集）/ 暂无推荐单」，
    #       而库里明明有 1.6 小时前的新鲜快照 + 3 条当日荐单（实测真实数据没问题）。
    _health_src = read(ROOT / "web" / "services" / "health.py")
    check("/health 必须暴露运行中代码版本 code_sha（漏重启时一眼可辨）", True,
          "code_sha" in _health_src and "_code_sha" in _health_src)
    # ⚠️ **P1（2026-10-23）**：原来只查「源码里有没有 code_sha 这个串」⇒
    #    把它改成硬编码 `None`（`"code_sha": None,`）守门照样绿（审计实测）。
    #    ⇒ 断言它**真的去 git rev-parse**（AST 判定，免疫 docstring）。
    check("/health 的 code_sha 必须真去 git rev-parse（不许硬编码 None）", True,
          "rev-parse" in code_strings(ROOT / "web" / "services" / "health.py"))
    check("部署后自检脚本在库（tools/check_service_freshness.py）", True,
          (ROOT / "tools" / "check_service_freshness.py").is_file())
    _sf = read(ROOT / "tools" / "check_service_freshness.py")
    check("部署后自检脚本会比对 /health 的 code_sha 与仓库 HEAD（不一致 ⇒ 退出码 1）", True,
          "code_sha" in _sf and "rev-parse" in _sf and "systemctl restart league-web" in _sf)
    # ⚠️ **P1（2026-10-23）**：自检脚本的三态退出码 ——
    #    「服务不可达 / 响应不是 JSON」原来也返回 0 ⇒ shell 门禁里「未检查」与
    #    「通过」同码 ⇒ 每次部署自检都可能假绿（审计实测：起一个返回 HTML 的假
    #    /health，脚本印「未检查」却 exit 0）。
    # ⇒ 现在必须是**三态**：0=一致 / 1=落后或响应异常（fail-closed）/ 2=未检查。
    check("部署后自检脚本必须三态退出（0=一致 / 1=落后或响应异常 / 2=未检查）", True,
          all(k in _sf for k in ("EXIT_OK", "EXIT_STALE", "EXIT_UNCHECKED")))
    _dash = read(ROOT / "static" / "dashboard.html")
    check("看板推荐单面板必须印「数据截至」（页面停在旧状态时用户能看出来）", True,
          "数据截至" in _dash)
    # ⚠️ **P1（2026-10-23）**：原来只查「有没有 setInterval」⇒ 把它从 60000 改成
    #    3600000（慢 60 倍）守门照样绿，而页面文案仍印「每 60 秒自动刷新」⇒
    #    文档 / UI / 代码三方一起说谎（审计实测）。
    # ⇒ 判定**实际周期**（毫秒实参 == 60000）+ 与页面文案里的「60 秒」互校。
    _interval_ms = js_set_interval_ms(js_code(_dash))
    check("看板推荐单面板的自动刷新周期 = 60000 毫秒（60 秒）", 60000,
          _interval_ms if _interval_ms is not None else "MISSING")
    check("看板页面文案里的刷新周期与代码一致（不许文档/UI/代码三方各说各话）", True,
          "60 秒" in _dash and _interval_ms == 60000)
    _smoke = read(ROOT / "tools" / "dashboard_smoke.js")
    # ⚠️ **P1（2026-10-23）**：这条断言原来查「整个冒烟脚本里有没有『数据截至』和
    #    『must2』」⇒ **两个串都在错误信息文案里** ⇒ 把 PANELS 里的
    #    `must2: '数据截至'` 删掉、甚至把整段 must2 判定代码删掉，守门照样绿
    #    （审计实测：13 个变异漏网之一）⇒ **冒烟已经不再断言「数据截至」了**。
    # ⇒ 改成**解析 PANELS 数组**（只看那几行），错误文案里的同名字串不算数。
    _m_panels = re.search(r"const PANELS\s*=\s*\[(.*?)\n\];", _smoke, re.S)
    _panels_block = _m_panels.group(1) if _m_panels else ""
    check("看板冒烟的 recommendPanel 必须带 must2: '数据截至'（**解析 PANELS 数组**）", True,
          "must2" in _panels_block and "数据截至" in _panels_block
          and "recommendPanel" in _panels_block)
    check("看板冒烟必须断言 P0/P1 口径（唯一真源 / 降级态）", True,
          "P0_CHECKS" in _smoke and "P1_CHECKS" in _smoke)

    # ⚠️⚠️ **硬不变量**：台账窗口必须 > 落港窗口。
    #    否则台账比文件先消失 ⇒ 装载器把老文件判成「孤儿(未装载)」⇒ 每轮全部重装
    #    ⇒ 又开始写重复 ingest_log（2026-10-23 实测：keep_days=7 时下一轮 cron
    #    重装了 4295 个文件）。这条断言**解析实际生效的默认值**来判定，
    #    不是靠注释里写一句「必须大于」就算数。
    #    ⚠️ **P1（2026-10-23）**：比较的是 **cron 入口**（生效值），不是 SQL 的 `\set`
    #    （被 `-v` 覆盖 ⇒ 从不生效）。
    check("⚠️ 台账窗口 > 落港窗口（否则老文件被判孤儿 ⇒ 每轮重装，2026-10-23 实测 4295 个）",
          True,
          _sh_ledger is not None and _stg_days is not None
          and _sh_ledger > _stg_days)

    _inst_p = ROOT / "docs" / "infra" / "install-jc-retention-cron.sh"
    check("保留策略 cron 安装器在库（部署物进仓库，别靠手敲 crontab）", True, _inst_p.is_file())
    _inst_txt = read(_inst_p) if _inst_p.is_file() else ""
    _inst_code = "\n".join(l for l in _inst_txt.splitlines() if not l.strip().startswith("#"))
    check("cron 安装器装了两条每日任务（落港清理 + ops 保留）", True,
          "jc-staging-retention.sh" in _inst_code and "jc-ops-retention.sh" in _inst_code
          and _inst_code.count("* * *") >= 2)
    check("cron 安装器是幂等的（标记块替换式，重复装不叠加条目）", True,
          "MARK_BEGIN" in _inst_code and "MARK_END" in _inst_code
          and "crontab -" in _inst_code)
    check("ops 保留的运行入口在库（docs/infra/jc-ops-retention.sh）", True,
          (ROOT / "docs" / "infra" / "jc-ops-retention.sh").is_file())

    # ── D. 文档同步 ───────────────────────────────────────────────────
    check("README 记了保留窗口（落港 30 天 / 日志 7 天 / 台账 37 天）", True,
          "30 天" in readme and "7 天" in readme and "37 天" in readme)
    check("路线图有 §6.13.6（保留策略 + gaps 口径 + 坏文件隔离）", True,
          "#### 6.13.6" in _roadmap)
    check("路线图 §6.13.6 记了根因（每 10 分钟重装 ⇒ 每天 1 万行）与实测数字", True,
          "1208" in _roadmap or "1 万行" in _roadmap)

    # ── P0：模型概率**唯一真源**（2026-10-23 三路审计发现的口径塌方）────────
    # 事故（生产实测，斯图加特 vs 门兴格拉德巴赫 / 德甲 / 2026-10-23）：
    #   probs            = {h:0.6466, d:0.1405, a:0.2129}  ← 决策层（推荐单）用的
    #   ml_proba         = [0.9778, 0.0081, 0.0141]         ← 卡片概率条/edge/EV 用的
    #   confidence_score = 0.793（=(p−0.25)×2 的**把握度评分**）  ← 卡片标成「这条的赢面」
    #   reasoning_factors.draw_true_prob = 0.137（**市场先验**）  ← 串关 EV 用的
    # ⇒ 同一页面上 4 个「赢面」互相矛盾：卡片说主胜「每投 1 元多赚 0.33 元」，
    #    决策层用**同一个模型**算出主胜 EV = −0.159 ⇒ 卡片让买主胜、推荐单让买客胜。
    # 口径纪律（唯一真源 scripts/core/model_probs.py 的 docstring 里是权威版）：
    #   模型概率 = probs.{h,d,a}（融合后最终值）；绝不用 ml_proba（分类器原始输出）、
    #   绝不用 *_true_prob（ESPN 市场先验）。
    _mp_path = SCRIPTS / "core" / "model_probs.py"
    check("模型概率唯一真源模块在库（scripts/core/model_probs.py）", True, _mp_path.is_file())
    _mp_code = code_strings(_mp_path)
    check("唯一真源不许读 ml_proba（ML 分类器原始输出，未与规则模型融合）", False,
          any("ml_proba" in s for s in _mp_code))
    check("唯一真源不许读 *_true_prob（ESPN 去水市场先验，不是模型输出）", False,
          any(re.search(r"\b\w+_true_prob\b", s) for s in _mp_code))
    _mp_tol = None
    for _n in ast.parse(read(_mp_path)).body:
        if isinstance(_n, ast.Assign) and any(getattr(t, "id", "") == "DEFAULT_SUM_TOL"
                                              for t in _n.targets) \
                and isinstance(_n.value, ast.Constant) and isinstance(_n.value.value, (int, float)):
            _mp_tol = float(_n.value.value)
    check("模型概率求和容差 ≥ 1e-3（融合后按 4 位小数四舍五入，误差上界 ≈1.5e-4；"
          "旧容差 1e-6 把 99/162 真实场次误判为非法）", True,
          _mp_tol is not None and _mp_tol >= 1e-3)

    _arch_path = SCRIPTS / "model" / "archive.py"
    _arch_fns = {n.name for n in ast.walk(ast.parse(read(_arch_path)))
                  if isinstance(n, ast.FunctionDef)}
    check("归档层不许自带第二份概率校验实现（_as_prob 必须消失 ⇒ 口径只有一份）", False,
          "_as_prob" in _arch_fns)
    check("归档层不许自带第二份 probs→键前缀映射（_PROBS_PREFIX 必须消失）", False,
          any(isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_PROBS_PREFIX"
                                                for t in n.targets)
              for n in ast.parse(read(_arch_path)).body))
    check("归档层 _prob 必须委托唯一真源 core.model_probs", True,
          "core.model_probs" in fn_imports(_arch_path, "_prob"))

    _pred_path = ROOT / "web" / "routers" / "predictions.py"
    check("web API 校验层不许再读 ml_proba（AST 判定，免疫 docstring 里的订正记录）", False,
          any("ml_proba" in s for s in fn_code_strings(_pred_path, "_validated_model_probs")))
    check("web API 校验层必须走唯一真源 core.model_probs", True,
          "core.model_probs" in fn_imports(_pred_path, "_validated_model_probs"))
    check("预测摘要必须透出 probs（页面要能直接看到模型真值，不必反推 edge_prob）", True,
          "probs" in fn_code_strings(_pred_path, "_prediction_summary"))

    _combo_path = ROOT / "web" / "routers" / "combo.py"
    check("串关不许读 reasoning_factors 的 *_ml_true_prob（draw_ml_true_prob 键**根本不存在**"
          " ⇒ 平局腿实际取到市场先验）", [],
          sorted(s for s in code_strings(_combo_path) if "_ml_true_prob" in s))
    check("串关必须走唯一真源 core.model_probs", True,
          "core.model_probs" in fn_imports(_combo_path, "_resolve_model_probs"))

    _tick_path = ROOT / "web" / "routers" / "ticket.py"
    check("票面三向概率不许回退 ml_proba（分类器原始输出，强弱悬殊场次会塌方）", False,
          any("ml_proba" in s for s in fn_code_strings(_tick_path, "_model_three")))
    check("票面必须走唯一真源 core.model_probs", True,
          "core.model_probs" in fn_imports(_tick_path, "_model_three"))

    _web_toplevel: list[str] = []
    for _rel in ("web/api.py", "web/routers/predictions.py", "web/routers/combo.py",
                 "web/routers/ticket.py"):
        for _m in top_level_imports(ROOT / _rel):
            if _m == "scripts" or _m.startswith("scripts."):
                _web_toplevel.append(f"{_rel}:{_m}")
    check("web 层禁止顶层 import scripts.*（只能在函数内延迟 import）", [], _web_toplevel)
    check("看板冒烟必须断言 P0 口径（模型概率唯一真源 / 赢面≠把握度 / 赔率来源标注）", True,
          "p0_modelProbs_noMlFallback" in _smoke and "p0_confBlock" in _smoke
          and "p0_marketLabel_source" in _smoke)

    # ── P1（2026-10-23）：「读失败」与「本来就没有」必须**分开表达** ─────────
    # 事故：数据库读失败时，页面把「连不上库」印成
    #   「你还没跑脚本」/「已结算 0 注」/「暂无 CLV 数据…成对样本不存在」/「24 小时无异动」
    # ⇒ 四种情况都在说谎，而且「你还没跑脚本」这个原因会**让人白跑一趟**。
    # 修法（照抄仓库现成范式 `recommend.offer_freshness()` 的 `degraded` 标志）：
    # 后端读失败返回 degraded=True，页面据此印「⚠️ 读取失败」。
    _rec_web = ROOT / "web" / "services" / "recommend.py"
    _clv_web = ROOT / "web" / "services" / "clv.py"
    _om_web = ROOT / "web" / "services" / "odds_move.py"
    check("推荐单读失败必须有 degraded 标志（today_picks_status 存在）", True,
          find_fn(_rec_web, "today_picks_status") is not None)
    check("推荐单快照必须透出 picks_degraded（否则页面分不出「读失败」与「没荐单」）", True,
          "picks_degraded" in fn_code_strings(_rec_web, "snapshot"))
    check("推荐单战绩必须带 degraded 标志（否则「读失败」被印成「已结算 0 注」）", True,
          "degraded" in fn_code_strings(_rec_web, "record"))
    check("CLV 读失败必须有 degraded 标志（daily_rows_status 存在）", True,
          find_fn(_clv_web, "daily_rows_status") is not None)
    check("CLV 摘要必须透出 degraded 标志", True,
          "degraded" in fn_code_strings(_clv_web, "summary"))
    check("赔率异动读失败必须有 degraded 标志（recent_status 存在）", True,
          find_fn(_om_web, "recent_status") is not None)
    check("赔率异动运维视图必须带 degraded 标志", True,
          "degraded" in fn_code_strings(_om_web, "ops"))
    check("CLV 端点必须把 degraded 透出（路由层不许把标志吃掉）", True,
          "degraded" in fn_code_strings(ROOT / "web" / "routers" / "predictions.py", "clv_daily"))
    check("赔率异动端点必须把 degraded 透出", True,
          "degraded" in fn_code_strings(ROOT / "web" / "routers" / "jc.py", "odds_move"))
    check("看板冒烟必须断言 P1 降级态（读失败不许被印成「没有数据」）", True,
          "p1_clv_degraded" in _smoke and "p1_recommend_degraded" in _smoke
          and "p1_record_degraded" in _smoke and "p1_odds_move_degraded" in _smoke)

    # ── P1（2026-10-23）：观测进程名隔离 + 子进程自举 ───────────────────────
    # 事故①：`core/log.py` 把 process_name **写死** "predict"，而 `core/__init__.py`
    #   → `core.predictor` → `core.log` 是**第一个**执行 `observability.setup()` 的地方
    #   ⇒ web 进程先装成 "predict"，随后 `web/api.py` 想装成 "web" ⇒ **重入守门抛
    #   RuntimeError** ⇒ 被 except 吞掉 ⇒ **生产上从来没有 events.web.jsonl /
    #   runtime.web.log**，web 事件写进了 predict 子进程的文件（H9 隔离失效），
    #   而 `web/api.py` 的注释却在宣称「web 进程独立写」—— **注释在撒谎**。
    _core_log = SCRIPTS / "core" / "log.py"
    check("观测进程名必须由 env 驱动（core/log.py 不得写死 process_name）", True,
          "LEAGUE_PROCESS_NAME" in code_strings(_core_log))
    check("core/log.py 不得再把 process_name 写死成 'predict'（AST 判定，免疫 docstring）", False,
          any("predict" in s and "process_name" in s
              for s in code_strings(_core_log)))
    _api_src = ROOT / "web" / "api.py"
    check("web 进程必须把观测进程名定成 'web'（AST 判定 setdefault 的实参）", True,
          ("LEAGUE_PROCESS_NAME", "web") in env_setdefaults(_api_src))
    _ai_an = ROOT / "web" / "services" / "ai_analyze.py"
    check("ai_analyze 子进程必须有自己的观测进程名", True,
          ("LEAGUE_PROCESS_NAME", "ai_analyze") in env_setdefaults(_ai_an))
    _enrich = ROOT / "web" / "enrich.py"
    check("ai_enrich 子进程必须有自己的观测进程名", True,
          ("LEAGUE_PROCESS_NAME", "ai_enrich") in env_setdefaults(_enrich))
    # 事故②：ai_analyze 子进程（`python -m web.services.ai_analyze`）的 sys.path[0]
    #   只有仓库根 ⇒ `core` / `store` 都 import 不到 ⇒ 生产实测
    #   `ModuleNotFoundError: No module named 'core'` ⇒ **exit 1**，
    #   而看板 AI 面板却显示「尚未生成。先点『生成当日分析』」（面板也在撒谎）。
    check("ai_analyze 子进程必须自举 scripts/（否则生产 exit 1：No module named 'core'）", True,
          touches_sys_path(_ai_an)
          and any("scripts" in s for s in code_strings(_ai_an)))
    check("ai_analyze 的模型概率必须走唯一真源（不许读 *_ml_true_prob / *_true_prob）", False,
          any(re.search(r"\b\w+_true_prob\b", s) for s in code_strings(_ai_an)))
    check("ai_analyze 必须用唯一真源 core.model_probs 取模型概率", True,
          "core.model_probs" in fn_imports(_ai_an, "_class_per_match"))

    # ── P1-G（2026-10-23）：未登录首屏不许停在「假 loading」，必须跳登录页 ──────
    # 事故：`_probeAuthAndBoot()` 探测到 401 时只改一句 liveText 就 return ⇒
    #   首屏那批占位符（「正在加载今日预测…」/「正在加载冠军概率…」/「正在读取 AI 状态…」）
    #   **永远停在 loading**（看起来像一直在加载），真相是「没登录、压根没数据」；
    #   而且这一路**不跳登录页**（与 api() 的 401 处理不一致）⇒ 用户面对一个转不出来的页面。
    # ⇒ 判定一律基于**剥掉注释后的 JS 代码**（注释里写一遍就假绿 —— 审计实测的 13 个假阴性之一）。
    _dash_js = js_code(dash_html)
    _probe_js = js_fn(_dash_js, "_probeAuthAndBoot")
    _mark_js = js_fn(_dash_js, "markUnlogged")
    check("看板启动探测必须认未登录（401 / 403 两个码都要处理）", True,
          "401" in _probe_js and "403" in _probe_js)
    check("看板启动探测的未登录分支必须调用 markUnlogged()（不许只改 liveText 就走人）", True,
          "markUnlogged()" in _probe_js)
    check("看板启动探测的未登录分支必须跳登录页（location.href=LOGIN）", True,
          "location.href=LOGIN" in _probe_js.replace(" ", ""))
    check("markUnlogged 必须存在且把占位符文案改成含「未登录」", True,
          bool(_mark_js) and "未登录" in _mark_js)
    check("markUnlogged 必须摘掉 loading 动画类（不许一边说未登录一边还在转圈）", True,
          "remove(\"loading\")" in _mark_js.replace(" ", "")
          or "remove('loading')" in _mark_js.replace(" ", ""))
    check("看板冒烟必须断言 P1-G（未登录 ⇒ 跳登录页 + 不许停在假 loading）", True,
          "p1g_redirect_to_login" in _smoke and "p1g_no_fake_loading" in _smoke
          and "p1g_loading_class_removed" in _smoke)

    # ── P1-H（2026-10-23）：状态文件写入并发安全 + 异常不许被静默吞掉 ─────────
    # 事故①：`_write_job` 用**固定** tmp 名（`<jid>.tmp`）+ 单次 `os.replace`：
    #   · 同 jid 的并发写者共用同一个 tmp ⇒ 状态文件内容互相污染；
    #   · Windows 上 `os.replace` 在目标文件正被读时瞬时抛 PermissionError(WinError 5)
    #     ⇒ 整条状态迁移炸掉。**实测**：全量 pytest 里 `test_job_poll_to_terminal`
    #     与 ai_enrich 两个用例随机 flake，根因就是它。
    # 事故②：`submit_job` **丢掉了 Future** ⇒ `run_job` 抛的异常被静默吞掉
    #   ⇒ 任务永远停在 queued，页面显示「排队中」，且**没有任何日志**。
    _jobs_py = ROOT / "web" / "services" / "jobs.py"
    check("jobs._write_job 必须用唯一 tmp 名（固定名 ⇒ 同 jid 并发写互相污染）", True,
          "uuid.uuid4" in fn_calls(_jobs_py, "_write_job"))
    check("jobs._write_job 必须对 os.replace 的 PermissionError 重试（Windows 瞬时文件锁）", True,
          "os.replace" in fn_calls(_jobs_py, "_write_job")
          and fn_handles(_jobs_py, "_write_job", "PermissionError"))
    check("jobs.submit_job 必须挂 done_callback（否则 run_job 异常被 Future 静默吞掉）", True,
          any(c.split(".")[-1] == "add_done_callback"
              for c in fn_calls(_jobs_py, "submit_job")))
    check("jobs 必须有 _on_job_done 兜底（异常 ⇒ 落日志 + 任务标 failed，不许停在 queued）", True,
          find_fn(_jobs_py, "_on_job_done") is not None
          and "STATUS_FAILED" in fn_names(_jobs_py, "_on_job_done")
          and "_spin_state" in fn_calls(_jobs_py, "_on_job_done"))
    # ── P1-H2（2026-10-23）：同进程**活锁**不许被 stale 判定偷删 ─────────────────
    # 事故：`_drop_stale_lock` 对「同进程（pid == self）」用「持锁 age ≥ 5s ⇒ 强删」的
    #   粗暴规则来清「线程崩死留下的残留锁」⇒ 实际把**同进程另一个线程正在合法持有
    #   的锁**一起删了。实测（修复前，持锁线程仍存活）：锁文件被删 ⇒ 第三个写者
    #   0.01s 内进入临界区 ⇒ **互斥彻底失效** ⇒ 并发写互相污染 jobs/<id>.json
    #   （正是 P1-H1「tmp 名冲突」要消灭的那类 bug 换了个马甲）。
    # ⇒ 修法：进程内锁登记表（拿到锁登记 / 释放注销）；`_drop_stale_lock` 判定
    #   stale 前**先查表** ⇒ 登记在册（活人持锁）就立即 return。
    # 判定口径：**AST + 语句顺序**（守卫必须早于 unlink），免疫注释/docstring 污染。
    _dsl = find_fn(_jobs_py, "_drop_stale_lock")
    _guard_idx = -1
    _unlink_idx = -1
    if _dsl is not None:
        for _i, _st in enumerate(_dsl.body):
            if isinstance(_st, ast.If) and _guard_idx < 0:
                _called: set[str] = set()
                for _c in ast.walk(_st.test):
                    if isinstance(_c, ast.Call):
                        _f = _c.func
                        if isinstance(_f, ast.Name):
                            _called.add(_f.id)
                        elif isinstance(_f, ast.Attribute):
                            _called.add(_f.attr)
                if "_lock_held_in_process" in _called and any(
                        isinstance(_x, ast.Return) for _x in ast.walk(_st)):
                    _guard_idx = _i
            if _unlink_idx < 0:
                for _c in ast.walk(_st):
                    if not isinstance(_c, ast.Call):
                        continue
                    _f = _c.func
                    _nm = _f.attr if isinstance(_f, ast.Attribute) else (
                        _f.id if isinstance(_f, ast.Name) else "")
                    if _nm == "unlink":
                        _unlink_idx = _i
                        break
    check("jobs._drop_stale_lock 必须先查进程内锁登记表（同进程活锁不许被偷删）", True,
          _guard_idx >= 0)
    check("jobs._drop_stale_lock 的登记表守卫必须早于 unlink（顺序错了等于没守卫）", True,
          _guard_idx >= 0 and _unlink_idx >= 0 and _guard_idx < _unlink_idx)
    check("jobs._exclusive_lock 必须登记/注销进程内锁（拿到锁⇒登记，释放⇒注销）", True,
          "_register_lock" in fn_calls(_jobs_py, "_exclusive_lock")
          and "_unregister_lock" in fn_calls(_jobs_py, "_exclusive_lock"))
    check("jobs 必须有进程内锁登记表三件套（登记 / 注销 / 查询）", True,
          all(find_fn(_jobs_py, _n) is not None for _n in
              ("_register_lock", "_unregister_lock", "_lock_held_in_process")))
    # 回归测试必须在（AST 取函数名，不许子串匹配）
    _tlock_fns: set[str] = set()
    _tlock = ROOT / "tests" / "test_jobs_lock_helpers.py"
    try:
        for _n in ast.walk(ast.parse(read(_tlock))):
            if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _tlock_fns.add(_n.name)
    except (SyntaxError, OSError):
        pass
    check("必须有「活锁不许被偷删 / 残留锁仍要清理 / 登记表必注销」的回归测试", True,
          "test_live_in_process_lock_cannot_be_stolen" in _tlock_fns
          and "test_drop_stale_lock_keeps_registered_lock_regardless_of_age" in _tlock_fns
          and "test_unregistered_old_lock_is_still_cleaned_up" in _tlock_fns
          and "test_lock_registry_cleared_on_release_and_on_error" in _tlock_fns)
    # ── P1-H3（2026-10-23）：AI 分析端点不许把服务器绝对路径甩给用户 ──────────
    # 事故：`/api/v1/ai/analyze` 的读取失败分支原文案是 `f"read_failed: {e}"`
    #   ⇒ 英文 + **异常对象里带服务器绝对路径**（PermissionError / IsADirectoryError
    #   的 str() 含完整路径）⇒ 把部署目录布局甩给终端用户，与本模块 P2-C 的
    #   脱敏策略自相矛盾。
    # 判定口径（AST）：note 的值**必须是字符串常量**（不许 f-string 拼接异常）。
    _ai_py = ROOT / "web" / "routers" / "ai.py"
    _ai_fn = find_fn(_ai_py, "ai_analyze_get")
    _bad_notes: list[str] = []
    _ai_keys: set[str] = set()
    if _ai_fn is not None:
        for _n in ast.walk(_ai_fn):
            if not isinstance(_n, ast.Dict):
                continue
            for _k, _v in zip(_n.keys, _n.values):
                if not (isinstance(_k, ast.Constant) and isinstance(_k.value, str)):
                    continue
                _ai_keys.add(_k.value)
                if _k.value == "note" and not (
                        isinstance(_v, ast.Constant) and isinstance(_v.value, str)):
                    _bad_notes.append(ast.dump(_v)[:120])
    check("AI 分析端点的 note 不许拼接异常/路径（必须中文常量文案）", True, not _bad_notes)
    check("AI 分析端点的降级分支必须带 reason 键（机器可读原因，不许只靠 note 文案）", True,
          "reason" in _ai_keys)
    check("AI 分析端点必须把读取失败细节写进服务端日志（不许只丢给用户或直接丢）", True,
          "_logger.warning" in fn_calls(_ai_py, "ai_analyze_get"))
    # 部署自检的三态退出码（不可达 = 未检查(2)，绝不许与「通过」(0) 同码 ⇒ 假绿）
    _csf = ROOT / "tools" / "check_service_freshness.py"
    _exit_consts: dict[str, object] = {}
    for _n in ast.parse(read(_csf)).body:
        if isinstance(_n, ast.Assign):
            for _t in _n.targets:
                if getattr(_t, "id", "") in ("EXIT_OK", "EXIT_STALE", "EXIT_UNCHECKED") \
                        and isinstance(_n.value, ast.Constant):
                    _exit_consts[_t.id] = _n.value.value
    check("部署自检必须是三态退出码 0/1/2（不可达=未检查不许与通过同码）", True,
          _exit_consts.get("EXIT_OK") == 0 and _exit_consts.get("EXIT_STALE") == 1
          and _exit_consts.get("EXIT_UNCHECKED") == 2)
    check("部署自检的不可达分支必须返回「未检查」退出码（不许返回 0）", True,
          "EXIT_UNCHECKED" in fn_names(_csf, "main"))
    # 子进程自带 env 时也不许写仓库 logs 目录（PYTEST_CURRENT_TEST 是 pytest 写进
    # os.environ 的，子进程继承 ⇒ 可当兜底判据）
    _obs = SCRIPTS / "core" / "observability.py"
    check("观测日志目录必须有 pytest 兜底（子进程自带 env 时也不许写仓库 logs）", True,
          "PYTEST_CURRENT_TEST" in fn_code_strings(_obs, "_default_logs_dir"))
    # ── P1-H 续：测试不许在**仓库根**凭空写出文件 ──────────────────────────
    # 事故：`tests/test_bball_elo_save_ratings.py` 用 `sys.modules.setdefault`
    #   把假模块 `bball.config_bball` 装进**全局** sys.modules（整个 session 生效），
    #   其中 `DATA_DIR = Path("unused")` 是**相对路径**且没被 fixture 重定向
    #   ⇒ **后续别的测试**（test_bball_run.py）把它写到 CWD = **仓库根**
    #   ⇒ 仓库根凭空多出 `unused`（实测 108 字节）⇒ 被 .gitignore 覆盖 ⇒ 完全隐形。
    _conftest_src = read(ROOT / "tests" / "conftest.py")
    check("测试哨兵必须守住「仓库根不许凭空多出文件」（相对路径写盘的兜底）", True,
          "_repo_root_files" in _conftest_src)
    # ⇒ 静态侧再堵一道：**模块属性**被赋成单段相对路径 ⇒ FAIL（AST 判定）。
    _rel_placeholder: list[str] = []
    for _t in sorted((ROOT / "tests").rglob("*.py")):
        try:
            _tree = ast.parse(read(_t))
        except (SyntaxError, OSError):
            continue
        for _n in ast.walk(_tree):
            if not isinstance(_n, ast.Assign) or not isinstance(_n.value, ast.Call):
                continue
            _f = _n.value.func
            if not (isinstance(_f, ast.Name) and _f.id in ("Path", "PurePath")):
                continue
            if not _n.value.args or not isinstance(_n.value.args[0], ast.Constant):
                continue
            _s = _n.value.args[0].value
            if not isinstance(_s, str) or not _s:
                continue
            if "/" in _s or "\\" in _s:      # 含分隔符 ⇒ 多段路径，不是这一类
                continue
            for _tgt in _n.targets:
                if isinstance(_tgt, ast.Attribute):
                    _rel_placeholder.append(
                        f"{_t.relative_to(ROOT)}:{_n.lineno} -> {_tgt.attr} = Path({_s!r})")
    check("测试不许把**单段相对路径**赋给模块属性（落点 = CWD = 仓库根 ⇒ 凭空多出文件）",
          [], _rel_placeholder)

    # ── P2（2026-10-23）：24 条口径类缺陷的门禁 ──────────────────────────
    # 三路审计的 P2 = 「口径说错」：把「没有」说成 0、把 UTC 说成 BJT、
    # 英文/内部路径暴露给用户、文案与代码各说各话。
    # ⇒ 判定一律基于**剥掉注释后的代码**（`js_code` / `code_strings`），
    #    否则「在注释里写一遍」就假绿（审计实测的 13 个假阴性之一）。
    _dash_js2 = js_code(dash_html)
    _jc_html = read(ROOT / "static" / "jc.html")
    _jc_js2 = js_code(_jc_html)

    # ── P2-A 前端（10 条）────────────────────────────────────────────────
    _pct_js = js_fn(_dash_js2, "pct")
    check("看板 pct() 不得把「没有值」印成 0.0%（null/空串 ⇒ 「—」）", True,
          bool(_pct_js) and "const n=Number(v)" not in _pct_js
          and "—" in _pct_js and "return" in _pct_js)
    check("看板不得出现硬编码浅黄 #ffe082（暗色模式未归一）", False,
          "#ffe082" in _dash_js2)
    check("看板不得把内部接口路径印给用户（POST /api/v1/...）", False,
          "POST /api/v1/" in _dash_js2)
    check("看板不得直接渲染后端英文 note（AI 分析面板）", False,
          "no analysis yet" in _dash_js2)
    check("static/jc.html 不得写死根路径 API（生产永久「请先登录」）", False,
          'fetch("/api/jc/' in _jc_js2)
    check("static/jc.html 必须按当前 URL 推导 API 基址", True,
          "location.pathname" in _jc_js2)
    _p2a_keys = ("p2a_pct_null_is_dash", "p2a_basketball_missing_winprob_unknown",
                  "p2a_stamp_is_bjt", "p2a_snapshot_stamp_is_bjt",
                  "p2a_quota_label_is_bjt_day", "p2a_history_date_is_bjt",
                  "p2a_live_partial_refresh_label", "p2a_live_full_sync_label",
                  "p2a_ai_analyze_no_internal_path", "p2a_ranklist_empty_state",
                  "p2a_temp_leagues_no_hardcoded_color", "p2a_jc_api_base_from_url")
    check("看板冒烟必须断言 P2-A 的 12 条前端口径（逐条 key）", [],
          [k for k in _p2a_keys if k not in _smoke])

    # ── P2-B 后端数字/时区/荐单口径（7 条）───────────────────────────────
    _clv_src = read(ROOT / "web" / "services" / "clv.py")
    check("CLV summary：n_days 必须是去重日期数（不许拿行数当天数）、n_rows 才是行数", True,
          '"n_days": len(days)' in _clv_src and '"n_rows": len(rows)' in _clv_src
          and '"n_days": len(rows)' not in _clv_src)
    _rec_path = ROOT / "scripts" / "decision" / "recommend.py"
    _rec_code = code_strings(_rec_path)
    check("荐单候选池 = business_date >= 今日业务日（不是 max(business_date)）", True,
          not any("max(business_date)" in s for s in _rec_code)
          and any("m.business_date >= %s::date" in s for s in _rec_code))
    check("荐单 made_at = 生成时刻（不许再取归档 run_at 的 decided_at）", True,
          'made_at = p.get("made_at") or now_bjt()' in _rec_src)
    # ⚠️ **P2-L（2026-10-23）阈值政策变更（用户批准）**：此前这条守门写死
    #   「阈值必须是政策值 0.05、不许偷换成实测水位 0.129」⇒ **用户 2026-10-23
    #   明确批准**把阈值提到实测水位 ⇒ 口径**反过来**：
    #   ① 阈值必须 == 实测水位（真源里两者相等，见下面 P2-L 区块）；
    #   ② 模块里必须**写着**「扛水 = EV > 0」那段口径 —— 否则下一个改阈值的人
    #      会以为「阈值 = 水位」是扛水门槛的**数学等价**（它不是：EV ≥ 12.9%
    #      等价 p/q ≥ m² ≈ 1.275 ⇒ **比扛水更严一道**）。
    check("荐单 EV 阈值必须对齐实测水位（用户 2026-10-23 批准）且模块写着「EV>0 / 比扛水更严」口径",
          True,
          "betting_policy" in _rec_src and "EV > 0" in _rec_src
          and "比扛水更严" in _rec_src)
    _note_src = read(ROOT / "web" / "services" / "recommend.py")
    # ⚠️ P2-I（2026-10-23）：文案里的数字改成**从真源派生** ⇒ 这里不再查字面量，
    #    改查「有没有手写数字」+「有没有从真源导入」；数字对不对由 UVC 工具**渲染后**判。
    check("用户可见水位文案不许再出现旧错值「25~33%」", True, "25~33%" not in _note_src)
    # ── P2-H4（2026-10-23）：用户可见文案不许宣称「阈值 = 水位」或「EV 必须 ≥ 水位」──
    # 事故：**生产浏览器验收时当场抓到**。`pricing_note` 的最后一句写的是
    #   「单边投注要先扛掉这笔水 ⇒ EV 必须**不低于水位**才谈得上盈利
    #     （推荐端阈值已对齐为 EV ≥ 12.9%）」——
    #   而本轮已经**否决**了抬阈值的改动（阈值维持 0.05）⇒ 这句话是**假话**；
    #   且它本身代数上也不对（EV > 0 ⟺ p/q > m，「扛水」= EV > 0，不是 EV ≥ 12.9%）。
    # ⇒ 用户能直接看到荐单面板上 ⇒ **必须**守死。
    # 判定口径：`code_strings`（**AST 级、排除 docstring**）⇒ 注释里的订正记录不会假绿，
    #   但**会真正渲染给用户**的字符串常量一旦写回假话 ⇒ 立刻 FAIL。
    _note_codes = code_strings(ROOT / "web" / "services" / "recommend.py")
    # ⚠️ P2-L（2026-10-23）：阈值 = 水位之后，「阈值是政策值，不是水位」
    #   「不需要再拿水位当门槛」这两句**变成自相矛盾**（阈值本身就是水位）
    #   ⇒ 一并列为禁语（防止 P2-I 的旧文案被改回来）。
    _forbidden_note = ("不低于水位", "阈值已对齐", "政策值，不是水位", "不需要再拿水位当门槛")
    _bad_note = [s[:80] for s in _note_codes
                 if any(f in s for f in _forbidden_note)]
    check("用户可见荐单文案不许宣称「EV 必须 ≥ 水位」或「阈值已对齐为 12.9%」", True,
          not _bad_note)
    check("用户可见荐单文案必须写清正确口径（阈值=实测水位 + 比扛水更严一道）", True,
          any("当前实测水位" in s for s in _note_codes)
          and any("比「扛水」更严一道" in s for s in _note_codes))
    # ── P2-I（2026-10-23）：口径数字的**唯一真源** + 用户可见文案守门工具 ──────
    # 事故（P2-H4）：文案里的数字是**手打**的 ⇒ 与代码各写各的 ⇒ 漂移成假话。
    # ⇒ 修法：① 口径数字搬进唯一真源 `scripts/core/betting_policy.py`；
    #   ② 用户可见文案**从真源派生**（不许手写）；
    #   ③ 新增 `tools/check_user_visible_claims.py`（UVC）：
    #      枚举「会渲染给用户的文本」⇒ 查泄漏 / 查手写口径数字 / **渲染后**核对数字。
    _bp_path = SCRIPTS / "core" / "betting_policy.py"
    check("口径数字必须有唯一真源（scripts/core/betting_policy.py 在库）", True,
          _bp_path.is_file())
    _bp_consts: dict[str, float] = {}
    if _bp_path.is_file():
        for _n in ast.parse(read(_bp_path)).body:
            if isinstance(_n, ast.Assign):
                for _t in _n.targets:
                    if isinstance(_t, ast.Name) and isinstance(_n.value, ast.Constant) \
                            and isinstance(_n.value.value, (int, float)) \
                            and not isinstance(_n.value.value, bool):
                        _bp_consts[_t.id] = float(_n.value.value)
    # ⚠️ **P2-L（2026-10-23）阈值政策变更（用户批准）**：口径**反过来**——
    #   阈值不再是不许等于水位的「政策值 0.05」，而是**实测水位 12.9%**
    #   （用户 2026-10-23 明确批准；8 天 39 条 → 25 条实测取证）。
    #   ⇒ 守门要盯的是「阈值 == 水位」这个**恒等式**（重测水位必须显式重审阈值）。
    check("口径真源的 MIN_EV 必须 == 实测水位 0.129（用户 2026-10-23 批准的政策变更）", True,
          _bp_consts.get("MIN_EV") == 0.129
          and _bp_consts.get("WATER_LEVEL_MEASURED") == 0.129)
    check("口径真源的水位必须是实测 0.129（生产实测 2026-10-23）", True,
          abs(_bp_consts.get("WATER_LEVEL_MEASURED", -1) - 0.129) < 1e-9)
    check("荐单面板与荐单 note 都必须从口径真源派生（不许自己写数字）", True,
          "betting_policy" in _note_src and "betting_policy" in _rec_src)
    _uvc = ROOT / "tools" / "check_user_visible_claims.py"
    check("用户可见文案守门工具必须在库（tools/check_user_visible_claims.py）", True,
          _uvc.is_file())
    if _uvc.is_file():
        _uvc_rc = subprocess.run([sys.executable, str(_uvc)], cwd=ROOT,
                                 capture_output=True, text=True, timeout=600)
        check("用户可见文案守门必须全绿（泄漏 / 手写口径数字 / 渲染数字与真源不一致 / Markdown 标记泄漏）",
              0, _uvc_rc.returncode)

    # ── P2-J（2026-10-23）：Markdown 标记不许漏到页面（4 条）──────────────
    # 事故（**生产浏览器验收时当场抓到**）：荐单面板实际印着
    #   「…竞彩 had 三侧含水（**生产实测 … − 1 = 12.9%**；…）… ⇒ **本单 EV 已把水算进去了**…」
    #   「推荐单是**手动生成**的（scripts/decision/recommend.py）：…」
    # ⇒ 用户看到的是**一串星号**。根因：前端**不解析 Markdown**（全仓无 marked/showdown）。
    # ⇒ 守门 UVC 原来也看不见它：`_pricing_note()` 是**函数返回**的文案，
    #    而 UVC 的后端枚举只认「dict 里直接写的字面量」⇒ **盲区**。
    check("前端不许有 Markdown 渲染器（否则「**」会被解析、判定④口径要重审）", True,
          not any(re.search(r"new\s+marked\b|marked\s*\.\s*parse|showdown\s*\.|markdownit\s*\(",
                            read(ROOT / "static" / rel), re.I)
                  for rel in ("dashboard.html", "jc.html")))
    # 判定口径：AST 级（`fn_code_strings` 排除 docstring，注释天然不在 AST 里）
    #   ⇒ 注释/订正记录里的星号不会假绿，但**会真正渲染给用户**的字符串常量
    #   一旦写回 Markdown 标记 ⇒ 立刻 FAIL。
    # ⚠️ 只能圈**真正会渲染给用户**的那几段：全文件扫 `code_strings` 会误伤
    #   argparse 的 help 文案（那是终端输出，不是页面文案）。
    _md_bad: list[str] = []
    for _s in fn_code_strings(ROOT / "web" / "services" / "recommend.py", "_pricing_note"):
        if "**" in _s or "`" in _s:
            _md_bad.append(f"recommend.py::_pricing_note: {_s[:60]}")
    _cal = [s for s in code_strings(ROOT / "web" / "services" / "recommend.py")
            if "手动生成" in s]
    if not _cal:
        _md_bad.append("recommend.py: 找不到 calendar_note 文案（锚点「手动生成」不见了）")
    for _s in _cal:
        if "**" in _s or "`" in _s:
            _md_bad.append(f"recommend.py::calendar_note: {_s[:60]}")
    check("荐单面板的用户可见文案里不许出现 Markdown 标记（** / 反引号）", True, not _md_bad)
    # 前端：用 UVC 自己的枚举（它会剥掉 JS 注释 ⇒ 注释里的星号不算）
    _uvc_src = read(_uvc)
    _uvc_mod = None
    if _uvc.is_file():
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("uvc_mod", _uvc)
        if _spec and _spec.loader:
            _uvc_mod = _ilu.module_from_spec(_spec)
            sys.modules["uvc_mod"] = _uvc_mod
            _spec.loader.exec_module(_uvc_mod)
    if _uvc_mod is not None:
        _fe_md = [s for _w, s in _uvc_mod.frontend_user_visible_strings()
                  if "**" in s or "`" in s]
        check("前端用户可见文案里不许出现 Markdown 标记（** / 反引号）", True, not _fe_md)
    check("UVC 必须覆盖「函数返回」的用户可见文案（P2-J 补的盲区）", True,
          "ast.Call" in _uvc_src and "_pricing_note" in _uvc_src)
    check("UVC 必须有判定④「Markdown 标记泄漏」（不许被悄悄摘掉）", True,
          "find_markdown_markers" in _uvc_src and "MARKDOWN_MARKER" in _uvc_src)

    # ── P2-L（2026-10-23）：EV 阈值政策变更（用户批准提到实测水位 12.9%）────────
    # ⚠️ **背景**：阈值政策在 2026-10-23 由**用户明确批准**变更（5% → 实测水位 12.9%）。
    #   这是**代码真改**（与 P2-H4「文案写 12.9% 而代码是 5%」那种**假话**相反）：
    #   代码与文案一致，守门跟着真源联动 ⇒ 阈值再改，守门自动跟着改。
    # ⇒ 但**代数上必须说清楚**：单边「扛水」的数学门槛仍是 **EV > 0**（p/q > m ≈ 1.129）；
    #   EV ≥ 12.9% 等价 **p/q ≥ m² ≈ 1.275** ⇒ **比扛水更严一道**
    #   （= 一道保守筛选，**不是**扛水门槛的数学等价）⇒ 用户可见文案必须这么写。
    _bp_mod = None
    try:
        import importlib.util as _ilu3
        _spec3 = _ilu3.spec_from_file_location("betting_policy_mod", _bp_path)
        if _spec3 and _spec3.loader:
            _bp_mod = _ilu3.module_from_spec(_spec3)
            sys.modules["betting_policy_mod"] = _bp_mod
            _spec3.loader.exec_module(_bp_mod)
    except Exception:  # noqa: BLE001 - 导入失败必须显式失败，不许静默跳过
        _bp_mod = None
    _pc_errs = (_bp_mod.policy_consistency_errors() if _bp_mod is not None
                else ["betting_policy 导入失败"])
    check("口径真源的政策一致性自检必须为空（阈值必须 == 实测水位；重测水位必须显式重审阈值）",
          True, not _pc_errs)
    _rec_codes_l = code_strings(ROOT / "scripts" / "decision" / "recommend.py")
    check("荐单 note 必须同时说清两件事（扛水门槛=EV>0 / 阈值比扛水更严一道）", True,
          any("比扛水更严一道" in s for s in _rec_codes_l)
          and any("扛水" in s for s in _rec_codes_l))
    check("UVC 必须覆盖「模块级文案常量」（P2-L 补的盲区：_NOTE_TMPL 原先完全没被枚举）", True,
          "USER_VISIBLE_CONSTS" in _uvc_src and "module_level_strings" in _uvc_src
          and "unregistered_module_constants" in _uvc_src)
    if _uvc_mod is not None:
        try:
            _rendered_l = _uvc_mod.render_pricing_note()
        except Exception as _exc_l:  # noqa: BLE001 - 渲染失败必须显式失败
            _rendered_l = f"<渲染失败: {_exc_l}>"
        check("荐单面板文案（渲染后）必须同时说清「扛水门槛=EV为正」与「阈值比扛水更严一道」", True,
              "EV 为正" in _rendered_l and "比「扛水」更严一道" in _rendered_l)
        check("荐单面板文案（渲染后）必须写明阈值 = 实测水位且有批准记录", True,
              "当前实测水位" in _rendered_l and "批准" in _rendered_l)
        check("荐单面板文案（渲染后）不许再出现「政策值，不是水位」这类旧口径", True,
              "政策值" not in _rendered_l and "不需要再拿水位当门槛" not in _rendered_l)

    # ── P2-K（2026-10-23）：首屏不许永远停在「连接中…」（5 条）──────────────
    # 事故（**生产浏览器验收时当场撞上**）：整块页面停在「连接中…」，所有面板停在
    #   「正在加载…」，顶部时间戳不变，用户**看不到任何错误提示、也没有重试入口**。
    # nginx 侧证据：那一轮 8 个 API **全部 200 且响应体完整发出**，随后客户端自己
    #   断开（499）⇒ 服务端没拖住；但页面代码**确实有真缺口**：
    #   `Promise.all([...])` 里**任一** loader 的 fetch **永不 settle**
    #   ⇒ Promise.all **永不 resolve** ⇒ `.finally` 不执行 ⇒ 首屏永远「连接中…」。
    _dash_html = read(ROOT / "static" / "dashboard.html")
    _m = re.search(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", _dash_html)
    _dash_js = re.sub(r"/\*.*?\*/", "", _m.group(1), flags=re.S) if _m else ""
    _dash_js = re.sub(r"(?m)^[ \t]*//[^\n]*$", "", _dash_js)
    check("看板请求必须带超时（api()/jcApi() 走 fetchTo + AbortController）", True,
          "function fetchTo(" in _dash_js and "AbortController" in _dash_js
          and "API_TIMEOUT_MS" in _dash_js
          and "fetchTo(API+path" in _dash_js and "fetchTo(JCAPI+path" in _dash_js)
    _boot_i = _dash_js.find("async function _probeAuthAndBoot(")
    _boot_js = _dash_js[_boot_i:_boot_i + 2000] if _boot_i >= 0 else ""
    check("首屏批量加载必须用 Promise.allSettled（一个面板挂掉不许拖死其它）", True,
          "Promise.allSettled" in _boot_js and "Promise.all([loadToday()" not in _boot_js)
    check("首屏有数据没取到时，顶部必须如实写「N 项未取到」（不许假装最近同步）", True,
          "未取到" in _boot_js)
    _smoke_src = read(ROOT / "tools" / "dashboard_smoke.js")
    check("看板冒烟必须有 P2-J/P2-K 的运行时断言（页面级 bug 必须有可自动跑的验证）", True,
          all(k in _smoke_src for k in ("p2j_normal_panels_no_markdown_marker",
                                        "p2j_degraded_panels_no_markdown_marker",
                                        "p2k_hanging_request_times_out",
                                        "p2k_live_not_claimed_synced_when_partial_failure")))
    check("看板冒烟必须能断言「请求永远挂着 ⇒ 超时失败」（不许 promise 永不 settle）", True,
          "REJECTED:请求超时" in _smoke_src and "HUNG" in _smoke_src)
    # ── P2-M（2026-10-23）：首屏健壮性补漏（两个真缺口）────────────────────
    # ① P2-K 的兜底**只保护到响应头**：`fetch()` 一返回响应头就把超时定时器清了
    #    ⇒ 响应体卡住时 `await r.json()` 永不 settle ⇒ 首屏仍会永久停在「连接中…」。
    # ⇒ 现在响应体在**同一个 AbortController 保护下**读完（text），超时一样会 abort。
    check("看板请求的超时必须覆盖响应体读取（不许只保护到响应头）", True,
          "const text=await r.text();" in _dash_js
          and "json:()=>{try{return JSON.parse(text)" in _dash_js)
    # ② 点「每日一图」「分享」（只有 data-action、没有 data-view）⇒ 旧实现调
    #    switchView(undefined) ⇒ 所有 view 的 active 被去掉 ⇒ **主区整块空白**。
    check("导航栏只给带 data-view 的按钮挂视图切换（点「每日一图」不许清空主区）", True,
          '.nav button[data-view]' in _dash_js
          # ⚠️ 只查**监听那一处**：`switchView` 内部还有一句
          # `document.querySelectorAll(".nav button").forEach(b=>b.classList.toggle("active",…))`
          # —— 那是**高亮当前导航项**，合法；不许拿它当「监听挂太宽」的证据。
          and '.nav button").forEach(b=>b.addEventListener("click",()=>switchView(' not in _dash_js)
    check("switchView 对未知视图名必须原样保留当前视图（不许把主区清空）", True,
          'if(!name||!document.getElementById("view-"+name))return;' in _dash_js)
    check("看板冒烟必须有 P2-M 的运行时断言（响应体卡住 / 未知视图名）", True,
          all(k in _smoke_src for k in ("p2m_body_never_completes_times_out",
                                        "p2m_switchview_unknown_name_keeps_current_view")))
    for _p in (ROOT / "web" / "services" / "recommend.py", ROOT / "web" / "services" / "clv.py",
               ROOT / "scripts" / "decision" / "recommend.py"):
        _tz2 = [s for s in code_strings(_p) if "AT TIME ZONE" in s]
        check(f"{_p.name} 的 SQL 时区常量 == Asia/Shanghai（不许 UTC）", True,
              all("Asia/Shanghai" in s for s in _tz2) and "UTC" not in _tz2[0] if _tz2 else True)
    check("准确率端点必须透出 overall 聚合（不许只给单联赛分片）", True,
          "overall" in code_strings(ROOT / "web" / "routers" / "predictions.py"))

    # ── P2-N（2026-10-23）：荐单门槛口径标注（页面不许说假话）──────────────
    # 背景：阈值 2026-10-23 起 = EV ≥ 12.9%（用户批准），但生成端**只做 upsert、
    #   从不清理**同一比赛日早先批次留下的行 ⇒ 库里混着旧批次的行 ⇒
    #   页面照旧把它们当「荐单」展示 ⇒ 与面板上写的门槛**自相矛盾**。
    # ⇒ 只标注、不删历史行（历史记录不可篡改）。
    _web_rec = ROOT / "web" / "services" / "recommend.py"
    _gen_rec = ROOT / "scripts" / "decision" / "recommend.py"
    _policy_src = ROOT / "scripts" / "core" / "betting_policy.py"

    def _module_float(p: Path, name: str):
        """模块级 float 常量的值（AST，不执行代码）。找不到返回 None。"""
        tree = ast.parse(read(p))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name \
                        and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, (int, float)):
                    return float(node.value.value)
        return None

    def _fn_body(p: Path, name: str):
        """函数节点（找不到返回 None）。"""
        tree = ast.parse(read(p))
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return n
        return None

    def _calls(fn, attr: str) -> bool:
        """函数体里有没有 ``<x>.<attr>(...)`` 形式的调用（AST 判定）。"""
        if fn is None:
            return False
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == attr:
                return True
        return False

    def _calls_name(fn, name: str) -> bool:
        """函数体里有没有直接调用同名函数（``f(...)`` 形式，AST 判定）。"""
        if fn is None:
            return False
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == name:
                return True
        return False

    def _module_str(p: Path, name: str) -> str:
        """模块级字符串常量的值（AST，不执行代码）。找不到返回 ""。"""
        tree = ast.parse(read(p))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name \
                        and isinstance(node.value, ast.Constant) \
                        and isinstance(node.value.value, str):
                    return node.value.value
        return ""

    def _names(fn) -> set[str]:
        """函数体里引用到的名字（AST 判定；不含属性名）。"""
        if fn is None:
            return set()
        return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}

    def _compares_ge(fn) -> bool:
        """函数体里有没有 ``x >= y`` 这种比较（AST 判定）。"""
        if fn is None:
            return False
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and any(
                    isinstance(op, ast.GtE) for op in n.ops):
                return True
        return False

    # ① 真源必须记着旧门槛（历史口径，只用于标注）
    check("真源必须记着旧门槛 EV ≥ 5%（历史口径，只用于标注不许筛选）", 0.05,
          _module_float(_policy_src, "EV_PREV_MIN_EV"))
    check("真源的现行门槛必须仍是实测水位 12.9%（P2-L 政策不得被静默改回）", 0.129,
          _module_float(_policy_src, "MIN_EV"))
    # ② 读层必须逐条打标注，且判定必须**从真源派生**（不许写死 true）
    _read_picks_fn = _fn_body(_web_rec, "_read_picks")
    _read_picks_keys = fn_code_strings(_web_rec, "_read_picks")
    check("web 读层必须给每条荐单打 meets_threshold 标注", True,
          "meets_threshold" in _read_picks_keys and "threshold_label" in _read_picks_keys)
    _meets_fn = _fn_body(_web_rec, "_meets_threshold")
    check("门槛判定必须从真源派生（不许写死阈值、不许写死 true）", True,
          "MIN_EV" in _names(_meets_fn) and _compares_ge(_meets_fn)
          and "core.betting_policy" in fn_imports(_web_rec, "_meets_threshold"))
    # ③ 战绩必须标出混合口径（跨了两套门槛 ⇒ 命中率不许被当成现行门槛的表现）
    _record_keys = fn_code_strings(_web_rec, "record")
    check("战绩必须给出「不满足现行门槛」的条数与混合口径说明", True,
          "n_below_threshold" in _record_keys and "threshold_note" in _record_keys)
    # ④ 前端必须真的印出来（页面级 bug 必须有可见效果）
    check("前端必须渲染逐条标注 + 面板级说明 + 战绩口径说明", True,
          "p.meets_threshold===false" in _dash_js
          and "threshold_label" in _dash_js
          and "picks_threshold_note" in _dash_js
          and "rec.threshold_note" in _dash_js)
    # ⑤ 生成端必须自检 + 显式告警（静默失败比报错更危险）
    _cnt_fn = _fn_body(_gen_rec, "count_below_threshold")
    _main_fn = _fn_body(_gen_rec, "main")
    _below_sql = _module_str(_gen_rec, "_BELOW_MIN_EV_SQL")
    check("生成端必须有当日残留行自检（按 BJT 日 + EV < 现行门槛）", True,
          _cnt_fn is not None
          and "AT TIME ZONE 'Asia/Shanghai'" in _below_sql
          and "ev < %s" in _below_sql
          and "MIN_EV" in _names(_cnt_fn))
    check("生成端跑批后必须调用残留行自检并显式告警（不许静默）", True,
          _calls_name(_main_fn, "count_below_threshold") and _calls(_main_fn, "warning"))
    # ⑥ 页面级 bug 必须有可自动跑的运行时验证
    check("看板冒烟必须有 P2-N 的运行时断言（逐条标注 / 混合口径说明）", True,
          all(k in _smoke_src for k in ("p2n_below_threshold_pick_is_labeled",
                                        "p2n_threshold_notes_are_rendered")))

    # ── P2-C 会话与运维（9 条）──────────────────────────────────────────
    _auth_src = read(ROOT / "web" / "auth.py")
    check("会话 cookie 的 Secure 必须跟请求协议走（不许只看 config.USE_HTTPS）", True,
          "_cookie_secure" in _auth_src and "x-forwarded-proto" in _auth_src.lower())
    _sess_src = read(ROOT / "web" / "session_store.py")
    _vt = fn_code_strings(ROOT / "web" / "session_store.py", "validate_token")
    check("validate_token 必须是只读（鉴权路径不许写库 ⇒ 写锁竞争会打成 500）", True,
          not any(re.search(r"(?i)\b(delete|insert|update)\b", s) for s in _vt))
    check("登录限速计数必须落盘（不许只在进程内存 ⇒ 重启即清零）", True,
          "record_login_failure" in _auth_src and "login_failures" in _sess_src)
    check("/health 的 code_sha 必须有进程内缓存（不许每次请求都 spawn git）", True,
          "CODE_SHA_TTL_SECONDS" in _health_src and "_code_sha" in _health_src)
    check("/health 不许回传服务器绝对路径（会话库路径要脱敏）", True,
          "_safe_db_label" in _health_src)
    _ai_src = read(ROOT / "web" / "routers" / "ai.py")
    check("/api/v1/ai/* 不许回传服务器绝对路径", True, "_sanitize_paths" in _ai_src)
    check("AI 分析空态 note 必须是中文且不许出现内部 API 路径", True,
          "POST /api/v1/jobs/ai-analyze" not in _ai_src and "尚未生成分析" in _ai_src)
    _jt_src = read(ROOT / "scripts" / "ingest" / "jc_topic.py")
    check("rows_in/rows_ups 的口径必须写进 jc_topic.py（读入/落库/幂等跳过/设计跳过）", True,
          all(s in _jt_src for s in ("读入行数", "落库行数", "幂等跳过", "设计跳过")))
    _jr_src = read(ROOT / "scripts" / "ingest" / "jc_read.py")
    check("异形文件名不许掀翻整批（必须有可区分异常 + 文件级隔离）", True,
          "InvalidFileNameError" in _jr_src and "badname" in _jr_src)
    check("隔离事件必须入台账且幂等（ok=false + not exists 去重）", True,
          "not exists" in _jt_src and "record_quarantine" in _jt_src)
    _idx_sql = ROOT / "docs" / "db" / "infra_p2_ingest_log_index.sql"
    check("ingest_log 的 (at) 索引迁移文件在库且用 CONCURRENTLY", True,
          _idx_sql.is_file()
          and "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in read(_idx_sql)
          and "ops.ingest_log" in read(_idx_sql))
    _ret_sh = read(ROOT / "docs" / "infra" / "jc-staging-retention.sh")
    check("保留策略隔离动作必须写 ops 台账（不许零痕迹）", True,
          "--record-quarantine" in _ret_sh)
    check("配额「当日」读写两侧都必须是 BJT 自然日（不许一个 UTC 一个 BJT）", True,
          "AT TIME ZONE 'Asia/Shanghai'" in read(ROOT / "scripts" / "store" / "jc_ops_view.py")
          and "current_date" not in read(ROOT / "scripts" / "store" / "jc_ops_view.py")
          and "now_bjt().date()" in read(ROOT / "scripts" / "ingest" / "api_get.py"))

    # ---- 21. 守门项数自检（防「README 说的项数」与实际漂移）----
    # README 会写「守门 N 项」；N 变了却没人改 README ⇒ 那个数字就在漂。
    # 这里把 README 声明的 N 与**实际登记的项数**对齐（含本条自身 ⇒ 固定点）。
    _guard_total = len(results) + 1
    _m_guard = re.search(r"check_docs_claims\.py`[，,]\s*(\d+)\s*项", readme)
    check("README 声明的守门项数 == 实测", _guard_total,
          int(_m_guard.group(1)) if _m_guard else "MISSING")

    # ---- 输出 ----
    width = 78
    print("=" * width)
    print("文档声明校验（R46）")
    print("=" * width)
    marks = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP"}
    for status, name, detail in results:
        print(f"[{marks[status]}] {name}")
        if status in ("FAIL", "SKIP"):
            print(f"        {detail}")
    fails = [r for r in results if r[0] == "FAIL"]
    skips = [r for r in results if r[0] == "SKIP"]
    print("-" * width)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(fails) - len(skips)}，"
          f"跳过 {len(skips)}，失败 {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
