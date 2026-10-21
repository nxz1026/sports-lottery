"""内联 JS 作用域检查器 —— 抓「跨作用域引用」这一类**静默失效**的 bug。

为什么需要它（2026-10-21 生产事故）：
    static/dashboard.html 里 ``const num=v=>...`` 定义在 ``loadAccuracy()`` **函数内部**，
    却被 ``loadClv()`` / ``loadRecommend()`` 使用 ⇒ 运行时 ``ReferenceError``
    ⇒ 页面把「CLV」「今日推荐单」两个面板显示成「暂不可用：num is not defined」。
    页面不白屏、控制台也不显眼 ⇒ **纯静默失效**：只有肉眼点开面板才发现。

    同一文件里还有**第二处同型 bug**（tsc 实测一起挖出来的）：
    ``const pendTotal`` 定义在 ``try`` 块里，``catch`` 块却引用它
    ⇒ 一旦 loadAccuracy 抛错，catch 自己先抛 ReferenceError ⇒ **掩盖真正的错误信息**。

为什么自己写而不用 ``tsc --checkJs``：
    tsc 实测能查出来（TS2304 "Cannot find name"），但它是 npm 全局包，
    生产机 / 守门环境未必装得上（ruff 就是这么被 skip 的）。
    本脚本**只用 Python 标准库** ⇒ 本地与生产机都能跑，守门不漂移。

判定规则（只报「一定会 ReferenceError」的那一类）：
    名字 N 在作用域 S 里被声明；N 在作用域 T 里被引用；
    若 S 不在 T 的祖先链上、且 T 自己也没声明 N ⇒ N 在 T 处不可见 ⇒ 必然 ReferenceError。
    从未被本文件任何作用域声明过的名字一律放行（浏览器全局 API：document/fetch/…）。

刻意**不报**的两类（避免误伤，宁可漏报也不误报）：
    ① 暂时性死区（TDZ）：「同作用域内先用后声明」本工具判为可见（运行时才炸）；
    ② 三元表达式的 ``a ? b : c`` 里的 ``b``（跟在 ``:`` 后面会被当成对象键跳过）。

用法：python tools/check_js_scope.py [文件或目录...]
退出码：0 = 无跨作用域引用；1 = 有；2 = 参数/IO 错误。
"""
from __future__ import annotations

import bisect
import re
import sys
from collections import defaultdict
from pathlib import Path

# 保留字/字面量：它们不是「引用」，跳过。
KEYWORDS = {
    "var", "let", "const", "function", "class", "extends", "super", "new", "delete",
    "typeof", "instanceof", "in", "of", "this", "null", "true", "false", "undefined",
    "void", "return", "if", "else", "for", "while", "do", "switch", "case", "default",
    "break", "continue", "throw", "try", "catch", "finally", "yield", "await", "async",
    "static", "get", "set", "import", "export", "from", "as", "with", "debugger",
    "NaN", "Infinity", "arguments",
}

PUNCT = sorted(
    [
        "=>", "===", "!==", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "**",
        "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<", ">>", ">>>",
        "{", "}", "(", ")", "[", "]", ";", ",", "<", ">", "+", "-", "*", "/", "%",
        "&", "|", "^", "!", "~", "?", ":", "=", "...", ".",
    ],
    key=len,
    reverse=True,
)

IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
NUM_RE = re.compile(r"(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?\d+)?)n?")


class LexError(Exception):
    pass


class Tok:
    __slots__ = ("kind", "val", "line")

    def __init__(self, kind: str, val: str, line: int):
        self.kind = kind
        self.val = val
        self.line = line

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Tok({self.kind},{self.val!r},L{self.line})"


def _line_index(src: str) -> list[int]:
    """每行起始 offset，用于 O(log n) 把 offset 换成行号。"""
    offs = [0]
    for m in re.finditer("\n", src):
        offs.append(m.end())
    return offs


def _line_of(offs: list[int], offset: int) -> int:
    return bisect.bisect_right(offs, offset)


def _skip_quoted(src: str, i: int) -> int:
    """``src[i]`` 是引号，返回结束引号之后的下标。"""
    q = src[i]
    i += 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1
        i += 1
    return n


def _match_brace(src: str, i: int) -> int:
    """``src[i]`` 是 '{'，返回配对 '}' 的下标。跳过字符串与嵌套模板。"""
    depth = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            i = _skip_quoted(src, i)
            continue
        if c == "`":
            i, _ = _scan_template(src, i)
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise LexError("花括号不配对")


def _scan_template(src: str, i: int) -> tuple[int, list[tuple[int, int]]]:
    """扫描模板字符串。``src[i]`` 是反引号。

    返回 (结束下标, [(插值表达式起, 插值表达式止)])。表达式里可能再嵌模板，递归处理。
    """
    n = len(src)
    i += 1
    exprs: list[tuple[int, int]] = []
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1, exprs
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            end = _match_brace(src, i + 1)          # 指向 '{' 本身
            exprs.append((i + 2, end))
            i = end + 1
            continue
        i += 1
    return i, exprs


def lex(src: str, base_line: int = 1) -> list[Tok]:
    """把 JS 源码切成 token 列表。

    字符串/模板正文不产 token；**模板插值表达式会产 token**（那里面的变量是真引用）。
    """
    toks: list[Tok] = []
    offs = _line_index(src)
    i = 0
    n = len(src)

    def prev_significant() -> Tok | None:
        return toks[-1] if toks else None

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'":
            line = base_line + _line_of(offs, i) - 1
            end = _skip_quoted(src, i)
            toks.append(Tok("str", src[i:end], line))
            i = end
            continue
        if c == "`":
            line = base_line + _line_of(offs, i) - 1
            end, exprs = _scan_template(src, i)
            toks.append(Tok("tmpl", "`", line))
            for s, e in exprs:
                toks.extend(lex(src[s:e], base_line + _line_of(offs, s) - 1))
            toks.append(Tok("tmpl_end", "`", line))
            i = end
            continue
        if c == "/":
            p = prev_significant()
            is_regex = True
            if p is not None:
                if p.kind in ("ident", "num"):
                    is_regex = False
                elif p.kind == "punct" and p.val in (")", "]", "}"):
                    is_regex = False
            if is_regex:
                line = base_line + _line_of(offs, i) - 1
                j = i + 1
                in_class = False
                while j < n:
                    d = src[j]
                    if d == "\\":
                        j += 2
                        continue
                    if d == "[":
                        in_class = True
                    elif d == "]":
                        in_class = False
                    elif d == "/" and not in_class:
                        break
                    elif d == "\n":
                        break
                    j += 1
                if j < n and src[j] == "/":
                    j += 1
                    while j < n and (src[j].isalpha() or src[j].isdigit()):
                        j += 1
                    toks.append(Tok("regex", src[i:j], line))
                    i = j
                    continue
            toks.append(Tok("punct", "/", base_line + _line_of(offs, i) - 1))
            i += 1
            continue
        m = NUM_RE.match(src, i)
        if m and (c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit())):
            toks.append(Tok("num", m.group(0), base_line + _line_of(offs, i) - 1))
            i = m.end()
            continue
        m = IDENT_RE.match(src, i)
        if m:
            toks.append(Tok("ident", m.group(0), base_line + _line_of(offs, i) - 1))
            i = m.end()
            continue
        for p in PUNCT:
            if src.startswith(p, i):
                toks.append(Tok("punct", p, base_line + _line_of(offs, i) - 1))
                i += len(p)
                break
        else:
            i += 1        # 不认识的字符：跳过，不影响作用域判定
    return toks


class Scope:
    __slots__ = ("kind", "parent", "name", "decls")

    def __init__(self, kind: str, parent: "Scope | None", name: str):
        self.kind = kind          # global | function | block
        self.parent = parent
        self.name = name
        self.decls: set[str] = set()

    def chain(self) -> list["Scope"]:
        out: list[Scope] = []
        s: Scope | None = self
        while s is not None:
            out.append(s)
            s = s.parent
        return out

    def func_scope(self) -> "Scope":
        s = self
        while s.kind == "block" and s.parent is not None:
            s = s.parent
        return s

    def label(self) -> str:
        if self.kind == "global":
            return "<脚本顶层>"
        if self.kind == "function":
            return f"函数 {self.name}"
        return "块"


class Finding:
    __slots__ = ("name", "lines", "where", "declared_in", "declared_line")

    def __init__(self, name: str, where: str, declared_in: str, declared_line: int):
        self.name = name
        self.lines: list[int] = []
        self.where = where
        self.declared_in = declared_in
        self.declared_line = declared_line

    def __str__(self) -> str:
        ls = ",".join(f"L{x}" for x in self.lines[:6])
        more = f" 等 {len(self.lines)} 处" if len(self.lines) > 6 else ""
        return (f"名字 {self.name!r} 在这些位置不可见（{ls}{more}）：{self.where}\n"
                f"        它声明在 {self.declared_in}（L{self.declared_line}）"
                f" ⇒ 运行时会抛 ReferenceError")


def _collect_pattern(toks: list[Tok], start: int) -> tuple[list[tuple[int, str]], int]:
    """解构模式：返回 ( [(token 下标, 名字)], 模式结束后的下标 )。"""
    names: list[tuple[int, str]] = []
    depth = 0
    i = start
    while i < len(toks):
        t = toks[i]
        if t.kind == "punct" and t.val in "{[":
            depth += 1
            i += 1
            continue
        if t.kind == "punct" and t.val in "}]":
            depth -= 1
            i += 1
            if depth == 0:
                return names, i
            continue
        if t.kind == "ident" and t.val not in KEYWORDS:
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if not (nxt and nxt.kind == "punct" and nxt.val == ":"):
                names.append((i, t.val))
        i += 1
    return names, start


def _params_between(toks: list[Tok], open_idx: int) -> tuple[list[tuple[int, str]], int]:
    """收集 ``(`` … ``)`` 之间的参数名。返回 ([(token 下标, 名字)], 右括号之后的下标)。"""
    names: list[tuple[int, str]] = []
    depth = 0
    i = open_idx
    while i < len(toks):
        t = toks[i]
        if t.kind == "punct" and t.val == "(":
            depth += 1
            i += 1
            continue
        if t.kind == "punct" and t.val == ")":
            depth -= 1
            i += 1
            if depth == 0:
                return names, i
            continue
        if t.kind == "ident" and t.val not in KEYWORDS:
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if not (nxt and nxt.kind == "punct" and nxt.val == ":"):
                names.append((i, t.val))
        i += 1
    return names, open_idx


def _match_paren_backwards(toks: list[Tok], close_idx: int) -> int:
    """``toks[close_idx]`` 是 ')'，返回配对 '(' 的下标；找不到返回 -1。"""
    depth = 1
    i = close_idx - 1
    while i >= 0:
        t = toks[i]
        if t.kind == "punct":
            if t.val == ")":
                depth += 1
            elif t.val == "(":
                depth -= 1
                if depth == 0:
                    return i
        i -= 1
    return -1


def _arrow_params(toks: list[Tok], arrow_idx: int) -> list[tuple[int, str]]:
    """取箭头函数的参数。

    精确判定（不用「往回找最近的左括号」那种模糊办法 —— 那会把外层调用的实参
    误当成箭头参数）：
      · ``=>`` 前一个 token 是 ')' ⇒ 参数列表就是那对括号；
      · ``=>`` 前一个 token 是标识符 ⇒ 单参数简写 ``p=>…``。
    """
    prev = toks[arrow_idx - 1] if arrow_idx > 0 else None
    if prev is None:
        return []
    if prev.kind == "ident" and prev.val not in KEYWORDS:
        return [(arrow_idx - 1, prev.val)]
    if prev.kind == "punct" and prev.val == ")":
        op = _match_paren_backwards(toks, arrow_idx - 1)
        if op >= 0:
            names, _ = _params_between(toks, op)
            return names
    return []


def _arrow_expr_end(toks: list[Tok], start: int) -> int:
    """表达式体箭头：返回箭头表达式的结束 token 下标（不含）。

    表达式延伸到**深度 0** 的 ``,`` / ``;`` / ``)`` / ``]`` / ``}`` 为止。
    """
    depth = 0
    j = start
    n = len(toks)
    while j < n:
        t = toks[j]
        if t.kind == "punct":
            if t.val in ("(", "[", "{"):
                depth += 1
            elif t.val in (")", "]", "}"):
                if depth == 0:
                    break
                depth -= 1
            elif t.val in (",", ";") and depth == 0:
                break
        j += 1
    return j


def _skip_initializer(toks: list[Tok], i: int) -> int:
    """跳过声明的初始化表达式，返回停在 ``,`` / ``;`` / ``)`` / ``of`` / ``in`` 的下标。"""
    depth = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.kind == "ident" and depth == 0 and t.val in ("of", "in"):
            return i
        if t.kind == "punct":
            if t.val in ("(", "[", "{"):
                depth += 1
            elif t.val in (")", "]", "}"):
                if depth == 0:
                    return i
                depth -= 1
            elif t.val in (",", ";") and depth == 0:
                return i
        i += 1
    return i


def _declarators(toks: list[Tok], i: int) -> list[tuple[int, str]]:
    """从 ``const`` / ``let`` / ``var`` 处解析**整条声明语句**的变量名。

    必须处理逗号分隔的多声明符（``const a=1, b=2``）—— 只看第一个会把后面的
    名字当成「别处的变量」⇒ 误报。
    """
    n = len(toks)
    names: list[tuple[int, str]] = []
    j = i + 1
    while j < n:
        t = toks[j]
        if t.kind == "ident" and t.val not in KEYWORDS:
            names.append((j, t.val))
            j += 1
        elif t.kind == "punct" and t.val in "{[":
            pat, j = _collect_pattern(toks, j)
            names.extend(pat)
        else:
            break
        j = _skip_initializer(toks, j)
        if j < n and toks[j].kind == "punct" and toks[j].val == ",":
            j += 1
            continue
        break
    return names


class Analysis:
    """作用域树 + 声明表 + 逐 token 的作用域归属。"""

    def __init__(self) -> None:
        self.scope_of_tok: list[Scope] = []
        self.declared_at: dict[str, list[Scope]] = defaultdict(list)
        self.decl_line: dict[str, int] = {}
        self.consumed: set[int] = set()
        self.soft: set[str] = set()      # 表达式体箭头的参数（不可判定 ⇒ 不参与可见性检查）


def build(toks: list[Tok]) -> Analysis:
    """走一遍 token 流，建作用域树并收集声明。**只走一遍**，保证后续查询与建树一致。"""
    an = Analysis()
    global_scope = Scope("global", None, "<global>")
    stack: list[Scope] = [global_scope]
    an.scope_of_tok = [global_scope] * len(toks)

    def fill(a: int, b: int, sc: Scope) -> None:
        """把 [a, b) 区间的 token 归属标成 sc（跳过的 token 也要有归属，否则查表错位）。"""
        for k in range(a, min(b, len(an.scope_of_tok))):
            an.scope_of_tok[k] = sc

    # pending：下一个 '{' 若是函数体，则把 pending_params 声明进该函数作用域
    pending_params: list[tuple[int, str]] = []
    pending_name = "<anonymous>"
    pending_kind = "block"
    arrow_pops: dict[int, list[Scope]] = {}

    n = len(toks)
    i = 0
    while i < n:
        if i in arrow_pops:
            for sc in arrow_pops.pop(i):
                if stack and stack[-1] is sc:
                    stack.pop()
        t = toks[i]
        an.scope_of_tok[i] = stack[-1]
        if t.kind == "punct" and t.val == "{":
            sc = Scope(pending_kind, stack[-1], pending_name)
            for idx, nm in pending_params:
                sc.decls.add(nm)
                an.declared_at[nm].append(sc)
                an.decl_line.setdefault(nm, toks[idx].line)
                an.consumed.add(idx)
            pending_params = []
            pending_name = "<anonymous>"
            pending_kind = "block"
            stack.append(sc)
            i += 1
            continue
        if t.kind == "punct" and t.val == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        if t.kind == "punct" and t.val == "=>":
            # 箭头函数：
            #   body 是 { }      → 参数进该函数作用域（严格）
            #   body 是表达式    → 开一个**真实作用域**覆盖表达式 token 区间，
            #                       否则参数名会被当成「别处声明的同名变量」⇒ 误报。
            pnames = _arrow_params(toks, i)
            nxt = toks[i + 1] if i + 1 < n else None
            if nxt is not None and nxt.kind == "punct" and nxt.val == "{":
                pending_params = pnames
                pending_name = "<arrow>"
                pending_kind = "function"
            else:
                end = _arrow_expr_end(toks, i + 1)
                sc = Scope("function", stack[-1], "<arrow>")
                for idx, nm in pnames:
                    sc.decls.add(nm)
                    an.declared_at[nm].append(sc)
                    an.decl_line.setdefault(nm, toks[idx].line)
                    an.consumed.add(idx)
                stack.append(sc)
                arrow_pops.setdefault(end, []).append(sc)
            i += 1
            continue

        if t.kind != "ident":
            i += 1
            continue

        v = t.val

        if v == "function":
            j = i + 1
            fname = "<anonymous>"
            if j < n and toks[j].kind == "ident" and toks[j].val not in KEYWORDS:
                fname = toks[j].val
                stack[-1].decls.add(fname)
                an.declared_at[fname].append(stack[-1])
                an.decl_line.setdefault(fname, toks[j].line)
                an.consumed.add(j)
                j += 1
            if j < n and toks[j].kind == "punct" and toks[j].val == "(":
                pnames, _ = _params_between(toks, j)
                pending_params = pnames
                pending_name = fname
                pending_kind = "function"
            i += 1
            continue

        if v in ("var", "let", "const"):
            # 整条声明语句的变量名一次性收齐（含逗号分隔的多声明符）
            names = _declarators(toks, i)
            target = stack[-1].func_scope() if v == "var" else stack[-1]
            for idx, nm in names:
                target.decls.add(nm)
                an.declared_at[nm].append(target)
                an.decl_line.setdefault(nm, toks[idx].line)
                an.consumed.add(idx)
            i += 1
            continue

        if v == "class":
            nxt = toks[i + 1] if i + 1 < n else None
            if nxt is not None and nxt.kind == "ident":
                stack[-1].decls.add(nxt.val)
                an.declared_at[nxt.val].append(stack[-1])
                an.decl_line.setdefault(nxt.val, nxt.line)
                an.consumed.add(i + 1)
            i += 1
            continue

        if v == "catch":
            j = i + 1
            if j < n and toks[j].kind == "punct" and toks[j].val == "(":
                pnames, _ = _params_between(toks, j)
                pending_params = pnames
                pending_name = "catch"
                pending_kind = "block"
            i += 1
            continue

        i += 1
    return an


def find_bad_refs(toks: list[Tok], an: Analysis) -> list[Finding]:
    """第二遍：查引用可见性。作用域归属直接查表（不再重走一遍 ⇒ 不会错位）。"""
    findings: list[Finding] = []
    seen: dict[tuple[str, int], Finding] = {}
    n = len(toks)
    for i, t in enumerate(toks):
        if t.kind != "ident" or t.val in KEYWORDS or i in an.consumed:
            continue
        prev = toks[i - 1] if i > 0 else None
        nxt = toks[i + 1] if i + 1 < n else None
        if prev is not None and prev.kind == "punct" and prev.val in (".", "?."):
            continue                      # 属性访问 obj.x
        if nxt is not None and nxt.kind == "punct" and nxt.val == ":":
            continue                      # 对象键 / 标签 / 三元分支
        scopes = an.declared_at.get(t.val)
        if not scopes:
            continue                      # 从未声明过 ⇒ 浏览器全局 API，放行
        here = an.scope_of_tok[i]
        if any(sc in here.chain() for sc in scopes):
            continue                      # 可见
        decl_sc = scopes[0]
        key = (t.val, id(decl_sc))
        f = seen.get(key)
        if f is None:
            f = Finding(t.val, here.label(), decl_sc.label(), an.decl_line.get(t.val, 0))
            seen[key] = f
            findings.append(f)
        f.lines.append(t.line)
    return findings


SCRIPT_RE = re.compile(r"(?s)<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>")


def check_html(path: Path) -> list[Finding]:
    html = path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    for m in SCRIPT_RE.finditer(html):
        js = m.group(1)
        base_line = html[: m.start(1)].count("\n") + 1
        toks = lex(js)
        an = build(toks)
        for f in find_bad_refs(toks, an):
            f.lines = [x + base_line - 1 for x in f.lines]
            findings.append(f)
    return findings


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args:
        args = [str(Path(__file__).resolve().parents[1] / "static")]
    targets: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            targets.extend(sorted(p.glob("*.html")))
        elif p.is_file():
            targets.append(p)
        else:
            print(f"路径不存在：{a}", file=sys.stderr)
            return 2
    total = 0
    for p in targets:
        findings = check_html(p)
        total += len(findings)
        if findings:
            print(f"[FAIL] {p}")
            for f in findings:
                print(f"       {f}")
    print(f"跨作用域引用：{total} 处（扫描 {len(targets)} 个文件）")
    return 1 if total else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv))
