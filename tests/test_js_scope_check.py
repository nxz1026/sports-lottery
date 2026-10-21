"""``tools/check_js_scope.py`` 的单测 —— 锁住「跨作用域引用」这一类静默失效。

背景（2026-10-21 生产事故）：``static/dashboard.html`` 里 ``const num=v=>...`` 定义在
``loadAccuracy()`` **函数体内**，却被 ``loadClv()`` / ``loadRecommend()`` 使用
⇒ 运行时 ``ReferenceError`` ⇒ 看板两个面板显示「暂不可用：num is not defined」。
页面不白屏、接口 200 ⇒ **纯静默失效** ⇒ 只有肉眼点开面板才发现。

本文件同时承担**回归锁**作用：仓库里 ``static/`` 的内联 JS 必须 **0 命中**
（与 ``tsc --checkJs`` 的 TS2304 逐条对拍过：dashboard.html 10 处一致、其余 4 页 0 处一致）。
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_check_js_scope", ROOT / "tools" / "check_js_scope.py")
cjs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cjs)


def findings(js: str) -> list[str]:
    """对一段 JS 跑作用域分析，返回形如 ``name@行号`` 的清单（每个引用点一条）。"""
    toks = cjs.lex(js)
    an = cjs.build(toks)
    return [f"{f.name}@{ln}" for f in cjs.find_bad_refs(toks, an) for ln in f.lines]


class TestCrossScopeDetection(unittest.TestCase):
    """必须被抓出来的跨作用域引用。"""

    def test_const_inside_function_used_by_other_function(self):
        """本轮生产事故的原样复现：num 定义在 loadAccuracy 内、被 loadClv 用。"""
        js = """
        async function loadAccuracy(){const num=v=>v===null?"—":"n="+v;return num(1)}
        async function loadClv(){return num(2)}
        """
        self.assertEqual(findings(js), ["num@3"])

    def test_const_in_try_used_in_catch(self):
        """第二处同型 bug：try 块里声明的变量在 catch 块里被引用。"""
        js = """
        async function f(){try{const pendTotal=1;throw new Error("x")}catch(e){return pendTotal}}
        """
        self.assertEqual(findings(js), ["pendTotal@2"])

    def test_block_local_used_outside_its_block(self):
        """块作用域里的变量在块外被引用 ⇒ 必然 ReferenceError。"""
        js = """
        function f(){if(true){const q=1;}return q}
        """
        self.assertEqual(findings(js), ["q@2"])

    def test_reports_all_reference_sites(self):
        """同一处定义被引用多次 ⇒ 每个位置都要报出来（不能只报第一处）。"""
        js = """
        function a(){const z=1;return z}
        function b(){return z}
        function c(){return z+z}
        """
        hits = findings(js)
        self.assertEqual(len(hits), 3, hits)
        self.assertTrue(all(h.startswith("z@") for h in hits), hits)


class TestNoFalsePositive(unittest.TestCase):
    """**不能**误报的情形（宁可漏报也不误报）。"""

    def test_global_helper_used_by_many_functions(self):
        js = """
        const num=v=>v===null?"—":"n="+v;
        function a(){return num(1)}
        function b(){return num(2)}
        """
        self.assertEqual(findings(js), [])

    def test_shadowing_in_sibling_scopes(self):
        """两个兄弟作用域各自声明同名变量 ⇒ 各自可见，不是错误。"""
        js = """
        function a(){const v=1;return v}
        function b(){const v=2;return v}
        """
        self.assertEqual(findings(js), [])

    def test_property_access_is_not_a_reference(self):
        """``p.home`` 不是对 ``home`` 的引用（曾经漏掉 '.' 导致大量误报）。"""
        js = """
        function f(p){return [p.id,p.home,p.away].join("|")}
        """
        self.assertEqual(findings(js), [])

    def test_object_literal_keys_are_not_references(self):
        js = """
        function f(){return {home:1, away:2}}
        """
        self.assertEqual(findings(js), [])

    def test_multi_declarator_const(self):
        """``const a=1, b=2`` 的两个名字都要算声明（曾经只看第一个 ⇒ 误报）。"""
        js = """
        function f(){const o=1,p=2;return o+p}
        """
        self.assertEqual(findings(js), [])

    def test_arrow_single_ident_param_expression_body(self):
        """``c=>c+1`` 这种单参数箭头：参数是箭头自己的。"""
        js = """
        const f=c=>c+1;
        const g=c=>{const d=c*2;return d};
        """
        self.assertEqual(findings(js), [])

    def test_arrow_expression_body_scoped_param(self):
        """表达式体箭头的参数只在箭头内可见 ⇒ 箭头外引用它是错误。"""
        js = """
        const f=c=>c+1;
        function g(){return c}
        """
        self.assertEqual(findings(js), ["c@3"])

    def test_destructured_arrow_param(self):
        js = """
        function flat(d){return Object.entries(d).flatMap(([k,v])=>[k,v])}
        """
        self.assertEqual(findings(js), [])

    def test_template_interpolation_reference_is_visible(self):
        """模板插值里的引用要参与作用域判定（``${p(...)}`` 里的 p 是真引用）。"""
        js = """
        const p=n=>n;
        function f(){return `${p(1)}`}
        """
        self.assertEqual(findings(js), [])

    def test_template_interpolation_catches_missing_name(self):
        """模板插值里的引用同样要判可见性（插值里的 t 在别的作用域声明 ⇒ 不可见）。"""
        js = """
        function a(){const t=1;return t}
        function b(){return `${t}`}
        """
        self.assertEqual(findings(js), ["t@3"])

    def test_never_declared_name_is_treated_as_global(self):
        """从未声明过的名字（document/fetch/…）放行。"""
        js = """
        function f(){return document.querySelector("#x").textContent}
        """
        self.assertEqual(findings(js), [])

    def test_catch_param_is_visible_in_catch_block(self):
        js = """
        try{throw new Error("x")}catch(e){return e.message}
        """
        self.assertEqual(findings(js), [])

    def test_class_and_function_declarations(self):
        js = """
        class A{constructor(){this.x=1}}
        function f(){const a=new A();return a.x}
        """
        self.assertEqual(findings(js), [])


class TestRepoStaticPages(unittest.TestCase):
    """仓库内联 JS 的回归锁：必须 0 命中。"""

    def test_static_pages_have_no_cross_scope_reference(self):
        static_dir = ROOT / "static"
        total = []
        for p in sorted(static_dir.glob("*.html")):
            for f in cjs.check_html(p):
                total.append(f"{p.name}: {f}")
        self.assertEqual(total, [], "静态页出现跨作用域引用：\n" + "\n".join(total))

    def test_dashboard_defines_num_in_global_scope(self):
        """``num`` 必须在**顶层**定义（不在任何函数体内）—— 事故的直接根因。"""
        html = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        toks = cjs.lex(cjs.SCRIPT_RE.search(html).group(1))
        an = cjs.build(toks)
        global_scope = an.scope_of_tok[0]
        self.assertIn("num", global_scope.decls,
                      "num 不在脚本顶层作用域声明 ⇒ loadClv/loadRecommend 用不了它")

    def test_dashboard_pend_total_declared_outside_try(self):
        """``pendTotal`` 必须声明在 try 之前（catch 才拿得到）。"""
        html = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        js = cjs.SCRIPT_RE.search(html).group(1)
        decl = js.index("pendTotal=")
        try_at = js.index("try{const d=await api(\"/accuracy/breakdown\")")
        self.assertLess(decl, try_at,
                        "pendTotal 声明在 try 块里 ⇒ catch 块引用它会抛 ReferenceError")

    def test_section_header_spans_full_row(self):
        """分区 header 必须跨全宽（grid-column:1/-1）⇒ 不再独占一个卡片格。"""
        html = (ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
        rule = next(l for l in html.splitlines() if l.startswith(".section-h3{"))
        self.assertIn("grid-column:1/-1", rule)


class TestToolCli(unittest.TestCase):
    """工具自身的 CLI 契约。"""

    def test_main_returns_1_on_findings(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.html"
            f.write_text("<script>function a(){const k=1;return k}\n"
                         "function b(){return k}</script>", encoding="utf-8")
            self.assertEqual(cjs.main(["check_js_scope", str(f)]), 1)

    def test_main_returns_0_when_clean(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "y.html"
            f.write_text("<script>const k=1;\nfunction b(){return k}</script>", encoding="utf-8")
            self.assertEqual(cjs.main(["check_js_scope", str(f)]), 0)

    def test_main_returns_2_on_missing_path(self):
        self.assertEqual(cjs.main(["check_js_scope", "/nonexistent/zzz.html"]), 2)


if __name__ == "__main__":
    unittest.main()
