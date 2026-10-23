"""tools/check_user_visible_claims —— 用户可见文案的**一致性守门**（2026-10-23 新增）。

⚠️ **为什么要有这个工具**（P2-H4 事故，生产浏览器验收时当场抓到）：

用户可见文案里的数字以前是**手打**的。荐单面板上实际印着：

    「…单边投注要先扛掉这笔水 ⇒ EV 必须不低于水位才谈得上盈利
      （推荐端阈值已对齐为 EV ≥ 12.9%）」

而代码里的真实阈值是 ``MIN_EV = 0.05``（**5%**）⇒ **两处各写各的**
⇒ 文案漂移成**假话**：用户被告知一个**不存在的策略**。
而**静态审计 / 单测 / 守门全都没抓到** —— 因为守门只查了
「文案里有没有 12.9%」「有没有 25~33%」，**没查「12.9% 是不是那个阈值」**。

⇒ 本工具把「用户可见文案」当成**一等公民**来查五件事：

1. **不许泄漏**：用户可见文本不得含服务器绝对路径 / 内部接口路径 / 凭证；
2. **不许手写口径数字**：EV 阈值 / 水位这类数字必须**从唯一真源**
   （``scripts/core/betting_policy.py``）**派生** —— 手写就漂移；
3. **派生后必须对**：把文案**真的渲染出来**（行为级），核对里面的
   EV 阈值 / 水位数字 == 真源常量；
4. **不许有 Markdown 标记**（P2-J，2026-10-23）：前端**不解析 Markdown**
   （全仓没有 marked/showdown）⇒ 文案里写 ``**粗体**`` 只会**原样**漏给用户；
5. **模块级文案常量必须显式登记**（P2-L，2026-10-23）：`_NOTE_TMPL` 这类
   **模块级常量**原先**完全不在枚举里** ⇒ 新增含中文的模块级常量时
   必须二选一登记（用户可见 / 不是用户可见）⇒ 不许静默漏网。

⚠️ **2026-10-23 订正（P2-L：阈值政策变更）**：本文档开头写的
「代码里真正的阈值是 ``MIN_EV = 0.05``」是**事故当时**的事实（文案手打 12.9%
而代码是 5%）。⇒ **2026-10-23 用户已批准把阈值提到实测水位 12.9%**，
现在 ``MIN_EV == 0.129 == WATER_LEVEL_MEASURED``（见 ``scripts/core/betting_policy.py``）。
⇒ 本工具的判定**与真源联动**（阈值改了多少它就查多少）⇒ 不受这次变更影响。

判定口径说明（为什么这样才不是「空断言」）：
- 文案集合 = 后端「会渲染给用户的字段」的字符串常量与 f-string 字面量
  + **函数返回的文案**（P2-J 补的盲区）
  + 前端含中文 / 百分比 / 比较符 / 内部接口路径的字符串字面量 + HTML 文本节点
  + **真的渲染出来的**荐单面板文案（行为级）；
- **纯代码 token**（如 ``/api/v1``、``match_num``）**不算**用户可见文本
  （否则每条断言都要维护白名单，迟早假绿）；
- 第 3 条是**行为级**：真的 import 目标模块并调用渲染函数，
  不是在源码里找字符串 ⇒ 改坏实现必然被抓到。

用法：``python tools/check_user_visible_claims.py``（退出码 0 = 全过，1 = 有问题）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── 判据正则 ──────────────────────────────────────────────────────────────
# 服务器绝对路径（POSIX 部署目录 / Windows 盘符）
ABS_PATH = re.compile(
    r"(?<![\w])(?:/home/|/srv/|/var/|/opt/|/etc/|/tmp/|/root/|/Users/|/data/)"
    r"|(?:[A-Za-z]:[\\/])")
# 内部接口路径
INTERNAL_API = re.compile(r"(?:GET|POST|PUT|DELETE|PATCH)\s+/api/|/api/(?:v1|jc)/")
# 凭证类
CREDENTIAL = re.compile(
    r"(?:postgresql://|mysql://|mongodb://|redis://)"
    r"|(?:\bpassword\s*=|\bpwd\s*=|\bsecret\s*=|\btoken\s*=|\bapi[_-]?key\s*=)",
    re.I)
# 含中文 ⇒ 用户可见文案
CJK = re.compile(r"[\u4e00-\u9fff]")
# 纯代码 token（ASCII、无空格、无特殊符号）
CODE_TOKEN = re.compile(r"^[A-Za-z0-9_.:/\-]+$")

# 后端「会渲染给用户的字段」键名（前端会直接印出来）
USER_VISIBLE_KEYS = frozenset({
    "sql_note", "note", "pricing_note", "calendar_note", "triple_note",
    "warning", "message", "detail", "scope", "summary", "hint", "reason",
    "title", "desc", "advice", "comment", "remark",
})

# 前端渲染 sink（用于识别「这段文本会被印出来」）
FRONTEND_FILES = ("static/dashboard.html", "static/jc.html",
                  "static/dailyimage.html", "static/login.html",
                  "static/dashboard-login.html")
BACKEND_GLOBS = ("web", "scripts/decision")

# ── 模块级「用户可见文案常量」（P2-L，2026-10-23 补的盲区）──────────────────
# ⚠️ **为什么要有**：原来的后端枚举只认「dict 里直接写的字面量」⇒
#   **模块级常量**根本没被看见 —— 而这几段恰恰是**会印给用户**的：
#     `scripts/decision/recommend.py::_NOTE_TMPL` → 写进 `analysis.ev_report.note`
#         → 经 `/api/v1/recommend` 的 `picks[].note` 返回给前端；
#     `web/routers/jc.py::_CALIBER_NOTE` → `"note"` 字段；
#     `web/routers/jc_ops.py::_NOTE` → `"sql_note"` 字段；
#     `web/services/lottery.py::DISCLAIMER` → `"note"` / `"disclaimer"` 字段。
#   ⇒ 在它们上面写 Markdown 标记或手写口径数字，守门**全是绿的**。
# ⚠️ **为什么用显式登记，而不是「全扫含中文的模块级常量」**：
#   全扫会把 **SQL / DDL** 也当文案（实测：`web/session_store.py::_SCHEMA` 的建表
#   注释里带 `**限速闸门**` ⇒ 会被判定④误报成「Markdown 泄漏」）。
#   ⇒ 改成**两张显式表**：用户可见的登记进 `USER_VISIBLE_CONSTS`，
#     不是用户可见的登记进 `NON_USER_VISIBLE_CONSTS`；
#   **任何新的含中文模块级常量都必须二选一登记**（否则守门 FAIL）⇒ 不会静默漏网。
USER_VISIBLE_CONSTS: dict[str, tuple[str, ...]] = {
    "scripts/decision/recommend.py": ("_NOTE_TMPL",),
    "web/routers/jc.py": ("_CALIBER_NOTE",),
    "web/routers/jc_ops.py": ("_NOTE",),
    "web/services/lottery.py": ("DISCLAIMER",),
}
NON_USER_VISIBLE_CONSTS: dict[str, tuple[str, ...]] = {
    "web/session_store.py": ("_SCHEMA",),
}


def module_level_strings(root: Path = ROOT) -> list[tuple[str, int, str, str]]:
    """模块级字符串常量 → ``(相对路径, 行号, 常量名, 文本)``。

    ⚠️ **P2-L（2026-10-23）补的盲区**：``_NOTE_TMPL``（荐单 note）、
    ``_CALIBER_NOTE`` / ``_NOTE`` / ``DISCLAIMER`` 这些**模块级常量**
    原先**完全不在枚举里** ⇒ 它们上面写什么都抓不到。
    """
    out: list[tuple[str, int, str, str]] = []
    for base in BACKEND_GLOBS:
        for p in sorted((root / base).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not (isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    continue
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out.append((rel, node.lineno, t.id, node.value.value))
    return out


def unregistered_module_constants(root: Path = ROOT) -> list[str]:
    """含中文（或含口径数字）的模块级常量里，**没登记**的。

    ⇒ 守门要求返回空列表：新增含中文的模块级常量时，
    **必须**二选一登记进 ``USER_VISIBLE_CONSTS`` / ``NON_USER_VISIBLE_CONSTS``
    ⇒ 否则它（可能）是一段没人守的用户可见文案 ⇒ 静默漏网。
    """
    bad: list[str] = []
    for rel, ln, name, s in module_level_strings(root):
        if name in USER_VISIBLE_CONSTS.get(rel, ()):
            continue
        if name in NON_USER_VISIBLE_CONSTS.get(rel, ()):
            continue
        if not (CJK.search(s) or HANDWRITTEN_EV.search(s) or HANDWRITTEN_WATER.search(s)):
            continue
        bad.append(f"{rel}:{ln} {name} = {s[:80]}")
    return bad


# ── 枚举：后端用户可见字符串 ──────────────────────────────────────────────
def backend_user_visible_strings(root: Path = ROOT) -> list[tuple[str, int, str]]:
    """后端「用户可见字段」里的字符串常量 + f-string 的**字面量部分**。

    返回 ``(相对路径, 行号, 文本)``。

    ⚠️ **P2-J（2026-10-23）补盲区**：原来只认「dict 里直接写字面量」，
    **函数返回的文案抓不到** —— 而最要命的那段（``_pricing_note()``，
    荐单面板定价说明）恰恰是**函数返回** ⇒ 静态枚举等于没看见它
    ⇒ 页面上漏出 ``**粗体**`` 的 Markdown 星号时，守门是绿的。
    ⇒ 现在：遇到 ``ast.Call`` 就**解析同模块函数的 return 文本**。
    """
    out: list[tuple[str, int, str]] = []
    for base in BACKEND_GLOBS:
        for p in sorted((root / base).rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            funcs = {n.name: n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for k, v in zip(node.keys, node.values):
                    if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                        continue
                    if k.value not in USER_VISIBLE_KEYS:
                        continue
                    for piece in _string_pieces(v, funcs, depth=0):
                        out.append((rel, node.lineno, piece))
    # ⚠️ **P2-L（2026-10-23）**：补上「**模块级常量**」这一类（原先完全看不见）。
    #   `_NOTE_TMPL` / `_CALIBER_NOTE` / `_NOTE` / `DISCLAIMER` 都是**会印给用户**的
    #   字符串常量，但不是 dict 里的字面量 ⇒ 旧枚举全漏了。
    for rel, ln, name, text in module_level_strings(root):
        if name in USER_VISIBLE_CONSTS.get(rel, ()):
            out.append((rel, ln, text))
    return out


def _string_pieces(value: ast.AST, funcs: dict | None = None, depth: int = 0) -> list[str]:
    """取出表达式里的**字面量文本片段**（常量 / f-string 的常量段 / 拼接段）。

    ``depth`` 限制函数调用链的展开层数（防自递归爆掉）。
    """
    pieces: list[str] = []
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        pieces.append(value.value)
    elif isinstance(value, ast.JoinedStr):
        for part in value.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                pieces.append(part.value)
    elif isinstance(value, ast.BinOp):
        pieces.extend(_string_pieces(value.left, funcs, depth))
        pieces.extend(_string_pieces(value.right, funcs, depth))
    elif isinstance(value, ast.Call) and funcs and depth < 3:
        # ⚠️ P2-J：函数返回的文案**也是**用户可见文案（`_pricing_note()` 就是）。
        name = value.func.id if isinstance(value.func, ast.Name) else None
        fn = funcs.get(name) if name else None
        if fn is not None:
            for ret in ast.walk(fn):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    pieces.extend(_string_pieces(ret.value, funcs, depth + 1))
    return pieces


# ── 枚举：前端用户可见字符串 ──────────────────────────────────────────────
def frontend_user_visible_strings(root: Path = ROOT) -> list[tuple[str, str]]:
    """前端会被渲染出来的文本（JS 字面量 + HTML 文本节点）。"""
    out: list[tuple[str, str]] = []
    for rel in FRONTEND_FILES:
        p = root / rel
        if not p.is_file():
            continue
        html = p.read_text(encoding="utf-8")
        code = _last_inline_script(html)
        for s in _js_strings(code):
            if _is_user_visible_js(s):
                out.append((rel, s))
        for t in _html_text_nodes(html):
            out.append((rel, t))
    return out


def _last_inline_script(html: str) -> str:
    blocks = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)</script>", html)
    code = blocks[-1] if blocks else ""
    code = re.sub(r"/\*[\s\S]*?\*/", " ", code)
    code = re.sub(r"(?m)^\s*//.*$", " ", code)
    return code


def _js_strings(code: str) -> list[str]:
    pat = re.compile(
        r'"((?:[^"\\\n]|\\.)*)"'
        r"|'((?:[^'\\\n]|\\.)*)'"
        r"|`((?:[^`\\]|\\.)*?)`",
        re.S,
    )
    out: list[str] = []
    for m in pat.finditer(code):
        s = next(g for g in m.groups() if g is not None)
        s = re.sub(r"\$\{[^}]*\}", "", s)
        out.append(s)
    return out


def _is_user_visible_js(s: str) -> bool:
    t = s.strip()
    if not t:
        return False
    if CJK.search(t):
        return True
    if "%" in t or "≥" in t or "≤" in t:
        return True
    if CODE_TOKEN.match(t):
        # 纯代码 token（API 基址 "/api/jc/"、字段名、类名…）⇒ **不是**用户可见文本。
        # ⚠️ 必须排在 INTERNAL_API 之前：否则 ``var API_BASE = "/api/jc/"`` 会被
        #    当成「把内部接口路径甩给用户」而误报（实测过：这是**假阳性**）。
        return False
    if INTERNAL_API.search(t):
        # 含内部接口路径**且不是**纯 token ⇒ 一定是「混在文案里的接口路径」⇒ 泄漏。
        return True
    return True


def _html_text_nodes(html: str) -> list[str]:
    s = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    s = re.sub(r"(?is)<style\b.*?</style>", " ", s)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    out = []
    for m in re.finditer(r">([^<>]+)<", s):
        t = m.group(1).strip()
        if t:
            out.append(t)
    return out


# ── 判定 1：泄漏 ──────────────────────────────────────────────────────────
def find_leaks(strings: list[tuple[str, str]]) -> list[str]:
    bad: list[str] = []
    for where, s in strings:
        for pat, label in ((ABS_PATH, "绝对路径"), (INTERNAL_API, "内部接口路径"),
                           (CREDENTIAL, "凭证")):
            if pat.search(s):
                bad.append(f"{where}: {label} :: {s[:90]}")
                break
    return bad


# ── 判定 2：手写的口径数字（必须派生）─────────────────────────────────────
# EV 阈值：``EV ≥ 12.9%`` / ``EV>=30%`` / ``单位 EV ≥ 5%``
HANDWRITTEN_EV = re.compile(r"EV\s*[≥>=]\s*\d")
# 水位：``水位 12.9%`` / ``水位 ≈ 11~13%``（数字紧跟「水位」）
HANDWRITTEN_WATER = re.compile(r"水位[^0-9%]{0,12}\d+\.?\d*\s*%?")


def find_handwritten_policy_numbers(strings: list[tuple[str, str]]) -> list[str]:
    """用户可见文案里**手写**的 EV 阈值 / 水位数字（必须改成从真源派生）。"""
    bad: list[str] = []
    for where, s in strings:
        if HANDWRITTEN_EV.search(s) or HANDWRITTEN_WATER.search(s):
            bad.append(f"{where}: 手写口径数字 :: {s[:90]}")
    return bad


# ── 判定 4：Markdown 标记泄漏（P2-J）──────────────────────────────────────
# 前端**不解析 Markdown**（全仓没有 marked / showdown / markdown-it）⇒
# 用户可见文本里写 ``**粗体**`` 只会**原样**显示给用户（生产浏览器验收时当场抓到：
# 荐单面板印着「（**生产实测 … 12.9%**）」「推荐单是**手动生成**的」）。
MARKDOWN_MARKER = re.compile(r"\*\*|`")


def find_markdown_markers(strings: list[tuple[str, str]]) -> list[str]:
    """用户可见文案里不许出现 Markdown 标记（不会渲染 ⇒ 只会原样显示）。"""
    bad: list[str] = []
    for where, s in strings:
        if MARKDOWN_MARKER.search(s):
            bad.append(f"{where}: Markdown 标记（前端不渲染，只会原样显示）:: {s[:90]}")
    return bad


# ── 判定 3：渲染后的数字必须与真源一致（行为级）──────────────────────────
def policy_constants() -> dict[str, float]:
    """从唯一真源读常量（AST，不执行代码）。"""
    src = (ROOT / "scripts" / "core" / "betting_policy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, float] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, (int, float)):
                out[t.id] = float(node.value.value)
    return out


def render_pricing_note() -> str:
    """真的渲染出荐单面板的定价说明（行为级）。"""
    for p in (str(ROOT), str(ROOT / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from web.services import recommend  # noqa: PLC0415 - 延迟导入（要 sys.path 先就位）

    return recommend._pricing_note()


def check_rendered_numbers() -> list[str]:
    """渲染后的文案里，EV 阈值 / 水位数字必须 == 真源常量。"""
    consts = policy_constants()
    ev_pct = consts["MIN_EV"] * 100
    water_pct = consts["WATER_LEVEL_MEASURED"] * 100
    text = render_pricing_note()
    bad: list[str] = []

    for m in re.finditer(r"EV\s*[≥>=]\s*(\d+(?:\.\d+)?)\s*%", text):
        if abs(float(m.group(1)) - ev_pct) > 1e-6:
            bad.append(f"渲染文案里的 EV 阈值 {m.group(1)}% != 真源 {ev_pct:g}%")
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[~～]\s*(\d+(?:\.\d+)?)\s*%", text):
        lo, hi = float(m.group(1)), float(m.group(2))
        if not (lo - 1e-9 <= water_pct <= hi + 1e-9):
            bad.append(f"渲染文案里的水位区间 {lo}~{hi}% 不含实测水位 {water_pct:g}%")
    for m in re.finditer(r"=(\s*)(\d+(?:\.\d+)?)\s*%", text):
        if abs(float(m.group(2)) - water_pct) > 1e-6:
            bad.append(f"渲染文案里的水位 {m.group(2)}% != 真源 {water_pct:g}%")
    return bad


# ── 主流程 ────────────────────────────────────────────────────────────────
def main() -> int:
    backend = [(rel, s) for rel, _ln, s in backend_user_visible_strings()]
    frontend = frontend_user_visible_strings()
    # ⚠️ P2-J：荐单面板那段文案是**函数返回**的 ⇒ 静态枚举看不见它
    # ⇒ 这里**真的渲染一次**并纳入文案集合（行为级，也顺带补上 ④ 的盲区）。
    try:
        rendered_note = render_pricing_note()
    except Exception as exc:  # noqa: BLE001 - 渲染失败必须显式失败，不许静默跳过
        print("=" * 78)
        print("用户可见文案一致性守门（UVC）")
        print("=" * 78)
        print(f"[FAIL] 渲染荐单面板文案失败（不许跳过）：{exc}")
        return 1
    rendered_pieces = [("web/services/recommend.py::_pricing_note()", rendered_note)]
    all_uv = backend + frontend + rendered_pieces
    # ⚠️ 判定②查的是「**源码里手写**的口径数字」⇒ 只看源码集合。
    #    渲染结果里的数字是**从真源派生**的（如「水位 ≈ 11~13%」来自常量）
    #    ⇒ 拿它去判「手写」是**假阳性**。
    source_uv = backend + frontend

    leaks = find_leaks(all_uv)
    handwritten = find_handwritten_policy_numbers(source_uv)
    markdown = find_markdown_markers(all_uv)
    rendered = check_rendered_numbers()
    # ⚠️ P2-L：模块级文案常量必须**显式登记**（用户可见 / 不是用户可见）⇒ 不许静默漏网
    unregistered = unregistered_module_constants()

    print("=" * 78)
    print("用户可见文案一致性守门（UVC）")
    print("=" * 78)
    print(f"用户可见文本：后端 {len(backend)} 段 + 前端 {len(frontend)} 段 "
          f"+ 渲染 {len(rendered_pieces)} 段 = {len(all_uv)} 段")

    failures = 0
    for title, items in (("① 泄漏（绝对路径 / 内部接口 / 凭证）", leaks),
                         ("② 手写的口径数字（必须从真源派生）", handwritten),
                         ("③ 渲染后的数字与真源不一致", rendered),
                         ("④ Markdown 标记泄漏（前端不渲染，只会原样显示）", markdown),
                         ("⑤ 含中文的模块级常量未登记（可能漏守的用户可见文案）", unregistered)):
        print(f"\n--- {title} ---")
        if not items:
            print("  （无）")
            continue
        failures += len(items)
        for it in items:
            print(f"  [FAIL] {it}")
    print()
    if failures:
        print(f"合计：{failures} 处问题")
        return 1
    print("合计：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
