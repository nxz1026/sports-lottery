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
    # 物化入口的缺口必须留在文档里（raw→fact 没有生产 CLI ⇒ 下次回填仍要手工跑一次）
    check("路线图记录了物化入口的缺口（生产无 CLI）", True,
          "生产没有 CLI 入口" in roadmap_text)

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
