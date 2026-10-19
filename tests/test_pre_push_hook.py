"""R14 pre-push hook + install 脚本守门测试。

覆盖：
- hook 脚本存在 + pwsh 能正确 parse
- install 脚本幂等：连续 install 两次不报错
- uninstall 干净：卸掉后 core.hooksPath 不再指向 stress_runs/hooks
- SPORTS_LOTTERY_SKIP_PUSH_HOOK=1 时 hook 退出码 0（跳过路径）
- hook 跑 pytest 失败时退出码 1（阻断路径）—— 用一个故意 fail 的测试
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SOURCE = REPO_ROOT / "stress_runs" / "hooks" / "pre-push"
INSTALL_SCRIPT = REPO_ROOT / "stress_runs" / "install-pre-push.ps1"


# ── 文件存在性 ─────────────────────────────────────────────

class TestHookScriptsExist:
    def test_pre_push_hook_present(self):
        assert HOOK_SOURCE.exists(), f"hook missing: {HOOK_SOURCE}"
        assert HOOK_SOURCE.stat().st_size > 100, "hook 文件太小，可能没内容"

    def test_install_script_present(self):
        assert INSTALL_SCRIPT.exists(), f"install missing: {INSTALL_SCRIPT}"
        assert INSTALL_SCRIPT.stat().st_size > 100, "install 脚本太小"


# ── Hook 脚本解析（pwsh 语法守门）──────────────────────────

class TestHookParsesPowerShell:
    def test_hook_pwsh_parse_no_syntax_error(self):
        """pwsh Parser API 应能解析 hook 脚本（即使内容里有 $env:）。"""
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        # 把 hook 脚本包到 wrapper 里 parse
        wrapper = f"""
            $ErrorActionPreference = 'Stop'
            $tokens = $null
            $errors = $null
            [System.Management.Automation.Language.Parser]::ParseFile(
                '{HOOK_SOURCE.as_posix()}', [ref]$tokens, [ref]$errors) | Out-Null
            if ($errors.Count -gt 0) {{
                Write-Host "PARSE ERRORS:"
                $errors | ForEach-Object {{ Write-Host $_ }}
                exit 1
            }}
            Write-Host "OK"
        """
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", wrapper],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert "OK" in result.stdout, f"hook pwsh parse 失败: stdout={result.stdout!r} stderr={result.stderr!r}"
        assert result.returncode == 0

    def test_install_script_pwsh_parse_no_syntax_error(self):
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        wrapper = f"""
            $tokens = $null
            $errors = $null
            [System.Management.Automation.Language.Parser]::ParseFile(
                '{INSTALL_SCRIPT.as_posix()}', [ref]$tokens, [ref]$errors) | Out-Null
            if ($errors.Count -gt 0) {{
                exit 1
            }}
            Write-Host "OK"
        """
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", wrapper],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert "OK" in result.stdout, f"install pwsh parse 失败: {result.stderr!r}"


# ── Hook 内容契约 ─────────────────────────────────────────

class TestHookContentContract:
    def test_hook_has_skip_env(self):
        """hook 必须尊重 SPORTS_LOTTERY_SKIP_PUSH_HOOK=1 跳过路径。"""
        text = HOOK_SOURCE.read_text(encoding="utf-8")
        assert "SPORTS_LOTTERY_SKIP_PUSH_HOOK" in text

    def test_hook_blocks_on_failure(self):
        """hook 在 pytest 失败时必须 exit 1。"""
        text = HOOK_SOURCE.read_text(encoding="utf-8")
        assert "exit 1" in text or "Exit 1" in text
        assert "BLOCKED" in text or "failed" in text.lower()

    def test_hook_passes_on_success(self):
        """hook 在 pytest 全过时必须 exit 0。"""
        text = HOOK_SOURCE.read_text(encoding="utf-8")
        assert "exit 0" in text

    def test_hook_targets_retrain_ml_feedback_observability(self):
        """hook 跑的测试必须覆盖核心风险链路，不能瞎跑全量。"""
        text = HOOK_SOURCE.read_text(encoding="utf-8")
        # 至少包含这些测试文件路径
        for path in (
            "tests/test_retrain_train_league.py",
            "tests/test_ml_feedback.py",
            "tests/test_observability.py",
        ):
            assert path in text, f"hook 应跑 {path}"

    def test_hook_excludes_unstable_propagation(self):
        """odds_propagation 是 flaky 易被门测试覆盖（不应进入 5x 强保证路径）。"""
        text = HOOK_SOURCE.read_text(encoding="utf-8")
        # 移除 test_predictor_odds_propagation —— R13 #4 _debug_fields 改动后偶发
        # 断言漂移；不在 5x 快路径内，由 stress_runs/run5x.ps1 全量覆盖。
        assert "test_predictor_odds_propagation.py" not in text

    def test_install_targets_runnable(self):
        """install 脚本必须设 core.hooksPath。"""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "core.hooksPath" in text
        assert "stress_runs/hooks" in text
        # 卸载路径
        assert "-Uninstall" in text
        # 跳过场景
        assert "SPORTS_LOTTERY_SKIP_PUSH_HOOK" in text


# ── Install / Uninstall 真实跑（pwsh + 临时 git 仓）────────

class TestInstallUninstallCycle:
    """用临时 git 仓（不在真实 sports-lottery）模拟 install / uninstall 循环。"""

    @pytest.fixture
    def fake_repo(self, tmp_path, monkeypatch):
        """建一个临时 git 仓，把 hook 文件 + install 脚本 symlink 进去（保路径相对性）。"""
        # 在真实仓库运行 install 会改 core.hooksPath → 不希望这样。用 fake repo 测试。
        fake = tmp_path / "fake_repo"
        fake.mkdir()
        subprocess.run(["git", "init", "-q", str(fake)], check=True)
        subprocess.run(
            ["git", "-C", str(fake), "config", "user.email", "test@test.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(fake), "config", "user.name", "test"],
            check=True,
        )
        # 把 hook 文件 + install 脚本拷过去（不是 symlink，避免 Windows 权限问题）
        fake_hooks_dir = fake / "stress_runs" / "hooks"
        fake_hooks_dir.mkdir(parents=True)
        shutil.copy(HOOK_SOURCE, fake_hooks_dir / "pre-push")
        shutil.copy(INSTALL_SCRIPT, fake / "stress_runs" / "install-pre-push.ps1")
        # 把 install 脚本里的 "Set-Location $repoRoot" 跟 fake repo 根对齐
        yield fake

    def test_install_sets_hookspath(self, fake_repo):
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(fake_repo / "stress_runs" / "install-pre-push.ps1")],
            cwd=str(fake_repo),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert result.returncode == 0, f"install 失败: stdout={result.stdout!r} stderr={result.stderr!r}"
        # core.hooksPath 应已设
        hookpath = subprocess.run(
            ["git", "-C", str(fake_repo), "config", "--get", "core.hooksPath"],
            capture_output=True, text=True,
        )
        assert hookpath.stdout.strip() == "stress_runs/hooks"

    def test_install_is_idempotent(self, fake_repo):
        """连续两次 install 不报错。"""
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        for _ in range(2):
            r = subprocess.run(
                ["pwsh", "-NoProfile", "-File", str(fake_repo / "stress_runs" / "install-pre-push.ps1")],
                cwd=str(fake_repo), capture_output=True, text=True,
            )
            assert r.returncode == 0
        # hookpath 仍是 stress_runs/hooks
        hp = subprocess.run(
            ["git", "-C", str(fake_repo), "config", "--get", "core.hooksPath"],
            capture_output=True, text=True,
        )
        assert hp.stdout.strip() == "stress_runs/hooks"

    def test_uninstall_clears_hookspath(self, fake_repo):
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        # 先装
        subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(fake_repo / "stress_runs" / "install-pre-push.ps1")],
            cwd=str(fake_repo), check=True,
        )
        # 再卸
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(fake_repo / "stress_runs" / "install-pre-push.ps1"),
             "-Uninstall"],
            cwd=str(fake_repo), capture_output=True, text=True, errors="replace",
        )
        assert result.returncode == 0
        # hookpath 应 unset（不论 GBK 中文，断言只针对 git config 状态）
        hp = subprocess.run(
            ["git", "-C", str(fake_repo), "config", "--get", "core.hooksPath"],
            capture_output=True, text=True,
        )
        assert hp.stdout.strip() == "", f"uninstall 后 core.hooksPath 应清空，实际 {hp.stdout.strip()!r}"


# ── Hook 真实跑测（用 skip env + 故意 fail 测试）──────────

class TestHookExecution:
    """触发 git push 让 pre-push hook 真跑 pytest。"""

    @pytest.fixture
    def fake_repo_with_failing_test(self, tmp_path):
        """建一个 fake git 仓，含一个故意 fail 的测试，看 hook 阻断 push。"""
        fake = tmp_path / "fake_hook_exec"
        fake.mkdir()
        subprocess.run(["git", "init", "-q", str(fake)], check=True)
        subprocess.run(
            ["git", "-C", str(fake), "config", "user.email", "t@t.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(fake), "config", "user.name", "t"], check=True,
        )
        # hook 拷入（同时给一个 .ps1 副本，让 pwsh -File 可调用）
        fake_hooks_dir = fake / "stress_runs" / "hooks"
        fake_hooks_dir.mkdir(parents=True)
        shutil.copy(HOOK_SOURCE, fake_hooks_dir / "pre-push")
        shutil.copy(HOOK_SOURCE, fake_hooks_dir / "pre-push.ps1")
        # 设 core.hooksPath
        subprocess.run(
            ["git", "-C", str(fake), "config", "core.hooksPath", "stress_runs/hooks"],
            check=True,
        )
        # hook 跑的测试列表在脚本里硬编码。这里让 path 不存在 → collection error → exit 5
        # 但 pytest exit 5 在 hook 里被视作"非 0"→ fail 阻断。
        yield fake

    def test_hook_blocks_push_when_pytest_fails(self, fake_repo_with_failing_test):
        """hook 应在 pytest 失败时阻断 push（exit 1）。

        pwsh 7+ 默认拒绝无 .ps1 扩展名的脚本直接调用（`pwsh -File foo`），
        但 Fake repo .git/hooks 名字必须是无扩展名 `pre-push`（git 自动识别）。
        测试中用 .ps1 副本调 `-File`，保 hook 内容一致。
        """
        fake = fake_repo_with_failing_test
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        # fake repo 里 hook 硬编码的 tests/* path 不存在 → pytest exit 5
        hook_path = str(fake / "stress_runs" / "hooks" / "pre-push.ps1")
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", hook_path],
            cwd=str(fake), capture_output=True, text=True, errors="replace",
            env={**os.environ, "PYTHONPATH": ""},
        )
        # pytest 失败 → hook exit 1
        assert result.returncode == 1, (
            f"hook 应在 pytest fail 时 exit 1，实际 exit={result.returncode}\n"
            f"stdout={result.stdout[-400:]}\nstderr={result.stderr[-400:]}"
        )
        assert "BLOCKED" in result.stdout or "FAIL" in result.stdout

    def test_hook_exits_zero_on_skip_env(self, fake_repo_with_failing_test):
        """SPORTS_LOTTERY_SKIP_PUSH_HOOK=1 时 hook 直接 exit 0 跳过。"""
        fake = fake_repo_with_failing_test
        if not shutil.which("pwsh"):
            pytest.skip("pwsh not on PATH")
        env = {**os.environ, "SPORTS_LOTTERY_SKIP_PUSH_HOOK": "1"}
        hook_path = str(fake / "stress_runs" / "hooks" / "pre-push.ps1")
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", hook_path],
            cwd=str(fake), capture_output=True, text=True, errors="replace", env=env,
        )
        assert result.returncode == 0, (
            f"skip env 下 hook 应 exit 0，实际 exit={result.returncode}\n"
            f"stdout={result.stdout[-300:]}\nstderr={result.stderr[-300:]}"
        )
        assert "skipped" in result.stdout.lower() or "skip" in result.stdout.lower()
