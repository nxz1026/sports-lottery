"""R46: 文档声明校验器 —— 把 README/AGENTS/SKILL 里**可验证的声明**逐条实测对照。

为什么要有这个：R43/R44/R45 连续三轮出现"文档听起来合理但与代码矛盾"的声明
（timer 09:00 vs 09:30、13 处 vs 14 处、pytest 须 PYTHONPATH vs 实际不需要）。
本脚本把其中可机械校验的部分固化下来，改文档后重跑即可发现新的漂移。

用法：python tools/check_docs_claims.py
退出码 0 = 全部一致；1 = 有 FAIL。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# 本脚本输出含中文断言名；Windows 下管道默认走本地 codepage(gbk)，终端会显示乱码，
# 而 pre-push hook 正是把它当门禁输出给用户看的。强制 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

results: list[tuple[str, str, str]] = []   # (状态, 声明, 实测)


def check(name: str, expected, actual) -> None:
    ok = expected == actual
    results.append(("PASS" if ok else "FAIL", name, f"声明={expected!r} 实际={actual!r}"))


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
    rc = subprocess.run([sys.executable, "-m", "ruff", "check", "."],
                        cwd=ROOT, capture_output=True, text=True).returncode
    check("ruff check . 退出码为 0（门禁已绿）", 0, rc)
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
    #   /dashboard/jc/       → 200   （auth_basic off，免凭据）
    #   /dashboard/jc/api/v1/me → 401（应用层 session 拒绝，**不是** Basic Auth）
    #   /dashboard/          → 401   （这才是 Nginx Basic Auth）
    #   /dashboard/jc/login  → 404   （登录页 2026-10-18 下线，nginx 仍指向它）
    # 以下断言是"禁止出现已知错误写法"（负向、精确字符串），不依赖措辞风格。
    deploy = read(ROOT / "docs" / "web" / "DASHBOARD_DEPLOY.md")
    ops_readme = read(ROOT / "ops" / "README.md")
    for label, text in (("README", readme), ("DASHBOARD_DEPLOY", deploy),
                        ("ops/README", ops_readme)):
        check(f"{label} 不含已废弃的 /api/v1/health 写法",
              [], stale_lines(text, ["/api/v1/health"]))
    check("README 不再声称 /dashboard/jc/ 走 Nginx Basic Auth",
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
    # （nginx 对 /dashboard/jc/ 是 auth_basic off；require_auth 只认 lp_session cookie）。
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
          'LOGIN="/dashboard/jc/login"' in dash_html and "location.href=LOGIN" in dash_html)
    check("前端 401 提示不再宣称由 Nginx Basic Auth 把关", 0,
          dash_html.count("Nginx Basic Auth 把关"))
    check("README 记录了登录入口缺陷与修复", True,
          "没有可用登录入口" in readme and "已修复" in readme)

    # ---- 输出 ----
    width = 78
    print("=" * width)
    print("文档声明校验（R46）")
    print("=" * width)
    for status, name, detail in results:
        mark = "OK  " if status == "PASS" else "FAIL"
        print(f"[{mark}] {name}")
        if status == "FAIL":
            print(f"        {detail}")
    fails = [r for r in results if r[0] == "FAIL"]
    print("-" * width)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(fails)}，失败 {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
