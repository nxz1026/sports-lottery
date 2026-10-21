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
    _rec_web = read(ROOT / "web" / "services" / "recommend.py")
    check("推荐单「今天」按 BJT 判定（AT TIME ZONE 'Asia/Shanghai'）", True,
          "AT TIME ZONE 'Asia/Shanghai'" in _rec_web)
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

    # ---- 19. 守门项数自检（防「README 说的项数」与实际漂移）----
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
