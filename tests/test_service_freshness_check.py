"""部署后自检脚本 tools/check_service_freshness.py 的行为级测试。

背景（2026-10-23 生产实测）：部署后忘了 ``systemctl restart league-web``
⇒ 服务内存里仍是旧代码，而 ``static/dashboard.html`` 每次请求现读磁盘
⇒ 页面变成「静态页新 / API 旧」的混合态 ⇒ 用户看到「⚠️ 暂无定价赔率快照（未采集）、
暂无推荐单」，而库里明明有 1.6 小时前的新鲜快照 + 3 条当日荐单。

⇒ 这个脚本就是用来**当场发现**「服务落后于仓库」的。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check_service_freshness.py"


def _load_mod():
    spec = importlib.util.spec_from_file_location("check_service_freshness", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_service_freshness"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()


def test_local_head_is_the_repo_short_sha():
    """本地 HEAD 必须读得到（否则自检无从比对）。"""
    sha = mod._local_head()
    assert sha and len(sha) >= 7


def test_mismatch_exits_1_and_prints_restart_hint(monkeypatch, capsys):
    """服务落后于仓库 ⇒ **退出码 1** + 打印修复命令（否则部署后没人会发现）。"""
    monkeypatch.setattr(mod, "_local_head", lambda: "aaa1111")
    monkeypatch.setattr(mod, "_remote_sha", lambda url: ("bbb2222", None, None))
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 1, "服务落后 ⇒ 必须非零退出码"
    assert "systemctl restart league-web" in out, "必须打印修复命令"
    assert "aaa1111" in out and "bbb2222" in out, "必须同时报出两边版本"


def test_match_exits_0(monkeypatch, capsys):
    """版本一致 ⇒ 退出码 0。"""
    monkeypatch.setattr(mod, "_local_head", lambda: "aaa1111")
    monkeypatch.setattr(mod, "_remote_sha", lambda url: ("aaa1111", None, None))
    assert mod.main([]) == 0
    assert "一致" in capsys.readouterr().out


def test_unreachable_is_not_treated_as_pass(monkeypatch, capsys):
    """服务不可达 ⇒ **未检查**（退出码 2），**不是** 0。

    ⚠️ 关键（**P1，2026-10-23**）：旧实现这里返回 **0** ⇒ shell 门禁里
    「未检查」与「通过」**同码** ⇒ 每次部署自检都可能**假绿**
    （审计实测：起一个返回 HTML 的假 /health，脚本印「未检查」却 exit 0）。
    ⇒ 现在不可达 = 退出码 2（未检查），与「一致」(0) 严格区分。
    """
    monkeypatch.setattr(mod, "_local_head", lambda: "aaa1111")
    monkeypatch.setattr(mod, "_remote_sha",
                        lambda url: (None, "ConnectionRefusedError: boom", "unreachable"))
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 2, "不可达 = 未检查 ⇒ 必须退出码 2，不许与「通过」(0) 同码"
    assert rc != 0, "未检查绝不许当成通过"
    assert "未检查" in out, "必须明说是「未检查」，不能冒充通过"
    # ⚠️ 不能用「输出里没有『一致』两个字」来判 —— 那句提示本身写着「不是『一致』」
    #    ⇒ 改成判**成功标记**（✓）不存在。
    assert "✓" not in out, "未检查时不许打勾（打勾=冒充通过）"


def test_bad_response_is_fail_closed(monkeypatch, capsys):
    """连上了但响应**不是预期 JSON** ⇒ 退出码 1（fail-closed），不是「未检查」。

    事故：反代 502 页 / 登录挑战页 ⇒ 服务活着但答非所问 ⇒ 这是**问题**，
    归成「未检查」会把人引向「服务没跑」的错误方向。
    """
    monkeypatch.setattr(mod, "_local_head", lambda: "aaa1111")
    monkeypatch.setattr(mod, "_remote_sha",
                        lambda url: (None, "响应不是合法 JSON", "bad_response"))
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 1, "活着但答非所问 ⇒ fail-closed（1），不许退化成「未检查」(2)"
    assert "不是预期 JSON" in out
    assert "未检查" not in out


def test_local_head_unreadable_is_unchecked(monkeypatch, capsys):
    """读不到本地 HEAD ⇒ **未检查**（退出码 2），不许当成一致。"""
    monkeypatch.setattr(mod, "_local_head", lambda: None)
    monkeypatch.setattr(mod, "_remote_sha", lambda url: ("aaa1111", None, None))
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "未检查" in out
    assert "✓" not in out


def test_exit_codes_are_distinct_constants():
    """三态退出码必须互不相同（0 一致 / 1 落后或响应异常 / 2 未检查）。"""
    assert len({mod.EXIT_OK, mod.EXIT_STALE, mod.EXIT_UNCHECKED}) == 3
    assert (mod.EXIT_OK, mod.EXIT_STALE, mod.EXIT_UNCHECKED) == (0, 1, 2)


def test_remote_without_code_sha_is_stale(monkeypatch, capsys):
    """响应里没有 code_sha ⇒ 说明服务是**旧版本**（那个字段是后加的）⇒ 退出码 1。"""
    monkeypatch.setattr(mod, "_local_head", lambda: "aaa1111")
    monkeypatch.setattr(mod, "_remote_sha", lambda url: (None, None, None))
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "systemctl restart league-web" in out


def test_health_endpoint_exposes_code_sha():
    """/health 必须暴露 code_sha（服务版本可见 ⇒ 漏重启能被发现）。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from web.services import health as health_mod

    out = health_mod.check()
    assert "code_sha" in out
    assert out["code_sha"] is None or isinstance(out["code_sha"], str)
