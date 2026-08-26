"""同步层测试 — 情景 #22~45。"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from uv_mgr.sync import (
    should_sync_after_uv,
    scan_venv_packages,
    sync_venv,
    sync_all,
    run_uv_passthrough,
)


# ── #22~26 should_sync_after_uv ────────────────────────────────────

class TestShouldSyncAfterUv:
    def test_normal_install(self, unset_uv_sync_after):
        """#22 pip install 应触发 sync。"""
        assert should_sync_after_uv(["pip", "install", "requests"]) is True

    def test_self_update_skipped(self, unset_uv_sync_after):
        """#23 uv self 不透传不应 sync。"""
        assert should_sync_after_uv(["self", "update"]) is False

    def test_cache_skipped(self, unset_uv_sync_after):
        """#24 uv cache 不透传不应 sync。"""
        assert should_sync_after_uv(["cache", "clean"]) is False

    def test_help_version_skipped(self, unset_uv_sync_after):
        """#25 help/version 不透传不应 sync。"""
        assert should_sync_after_uv(["help"]) is False
        assert should_sync_after_uv(["version"]) is False

    def test_env_var_disables(self):
        """#26 UV_MGR_SYNC_AFTER=0 跳过 sync。"""
        with patch.dict(os.environ, {"UV_MGR_SYNC_AFTER": "0"}):
            assert should_sync_after_uv(["pip", "install", "x"]) is False

    def test_deprecated_env_var_still_works(self, unset_uv_sync_after, capsys):
        """旧名 UV_SYNC_AFTER=0 仍能跳过 sync，但打印弃用警告。"""
        with patch.dict(os.environ, {"UV_SYNC_AFTER": "0"}):
            assert should_sync_after_uv(["pip", "install", "x"]) is False
        err = capsys.readouterr().err
        assert "UV_SYNC_AFTER" in err and "弃用" in err

    def test_empty_args(self, unset_uv_sync_after):
        """空参数返回 False。"""
        assert should_sync_after_uv([]) is False


class TestUvVersion:
    @patch("subprocess.run")
    def test_supported(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="uv 0.4.7", stderr="")
        from uv_mgr.sync import check_uv_version
        assert check_uv_version()[0] is True

    @patch("subprocess.run")
    def test_too_old(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="uv 0.3.9", stderr="")
        from uv_mgr.sync import check_uv_version
        assert check_uv_version()[0] is False

    @patch("subprocess.run")
    def test_windows_uses_resolved_uv_executable(self, mock_run, monkeypatch):
        from uv_mgr.sync import check_uv_version

        monkeypatch.setattr("uv_mgr.config.sys.platform", "win32")
        monkeypatch.setattr("uv_mgr.config.shutil.which", lambda _name: r"C:\\Tools\\uv.exe")
        mock_run.return_value = MagicMock(returncode=0, stdout="uv 0.4.7", stderr="")
        assert check_uv_version()[0] is True
        assert mock_run.call_args.args[0][0] == r"C:\\Tools\\uv.exe"


# ── #27~31 scan_venv_packages ──────────────────────────────────────

class TestScanVenvPackages:
    @patch("subprocess.run")
    def test_normal_scan(self, mock_run):
        """#27 正常解析 uv pip list JSON。"""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {"name": "pip", "version": "26.1.2"},
                {"name": "requests", "version": "2.31.0"},
            ]),
            stderr="",
        )
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            result = scan_venv_packages("/tmp/venv/bin/python")
        assert result == ([("pip", "26.1.2"), ("requests", "2.31.0")], True)

    @patch("subprocess.run")
    def test_empty_venv(self, mock_run):
        """#28 空 venv 返回空列表。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            result = scan_venv_packages("/tmp/venv/bin/python")
        assert result == ([], True)

    @patch("subprocess.run")
    def test_uv_error(self, mock_run):
        """#29 uv 返回非 0 时返回空列表。"""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            result = scan_venv_packages("/tmp/venv/bin/python")
        assert result == ([], False)

    @patch("subprocess.run")
    def test_invalid_json(self, mock_run):
        """#30 非法 JSON 返回空列表。"""
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            result = scan_venv_packages("/tmp/venv/bin/python")
        assert result == ([], False)

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_uv_not_found(self, mock_run):
        """#31 uv 不存在返回空列表。"""
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            result = scan_venv_packages("/tmp/venv/bin/python")
        assert result == ([], False)

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="uv", timeout=60))
    def test_timeout(self, mock_run):
        """超时返回空列表。"""
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            result = scan_venv_packages("/tmp/venv/bin/python")
        assert result == ([], False)


# ── #32~37 sync_venv ───────────────────────────────────────────────

class TestSyncVenv:
    def test_windows_scripts_python_exe_is_recognized(self, tmp_path, monkeypatch):
        from uv_mgr.sync import venv_python_path

        monkeypatch.setattr("uv_mgr.config.sys.platform", "win32")
        venv = tmp_path / "venv"
        python = venv / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        assert venv_python_path(str(venv)) == python
    def test_sync_records_snapshot_and_failure_audit(self, conn, monkeypatch):
        from uv_mgr.db import add_venv, get_operations, get_snapshots

        add_venv(conn, "/tmp/test-venv")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        with patch("uv_mgr.sync.scan_venv_packages", return_value=(
            [("example-pkg", "1.0")], True,
        )):
            assert sync_venv(conn, "/tmp/test-venv", check_uv=False) is True
        with patch("uv_mgr.sync.scan_venv_packages", return_value=([], False)):
            assert sync_venv(conn, "/tmp/test-venv", check_uv=False) is False

        assert len(get_snapshots(conn, venv_path="/tmp/test-venv")) == 1
        operations = get_operations(conn, venv_path="/tmp/test-venv", limit=10)
        assert any(row["operation_type"] == "sync" and row["success"] for row in operations)
        assert any(row["operation_type"] == "sync" and not row["success"] for row in operations)

    def test_successful_sync_prunes_old_version_records(self, conn, monkeypatch):
        from uv_mgr.db import add_venv, ensure_package, get_orphan_packages

        add_venv(conn, "/tmp/test-venv")
        old = ensure_package(conn, "example-pkg", "1.0")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        with patch("uv_mgr.sync.scan_venv_packages", return_value=(
            [("Example_Pkg", "2.0")], True,
        )):
            sync_venv(conn, "/tmp/test-venv", check_uv=False)

        assert old not in {row["id"] for row in get_orphan_packages(conn)}
        rows = conn.execute("SELECT name, version FROM packages").fetchall()
        assert [(row["name"], row["version"]) for row in rows] == [
            ("example-pkg", "2.0"),
        ]

    def test_failed_sync_does_not_prune_old_version_records(self, conn, monkeypatch):
        from uv_mgr.db import add_venv, ensure_package

        add_venv(conn, "/tmp/test-venv")
        old = ensure_package(conn, "example-pkg", "1.0")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        with patch("uv_mgr.sync.scan_venv_packages", return_value=([], False)):
            assert sync_venv(conn, "/tmp/test-venv", check_uv=False) is False

        assert conn.execute(
            "SELECT COUNT(*) FROM packages WHERE id = ?", (old,)
        ).fetchone()[0] == 1

    def test_normal_sync(self, conn, monkeypatch, mock_uv_run_success):
        """#32 已注册 venv，目录存在，正常同步。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/test-venv")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        # Mock subprocess.run for python version check to succeed
        original_run = subprocess.run
        def mock_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and cmd[1] == "--version":
                mock = MagicMock(returncode=0, stdout="Python 3.11.15", stderr="")
                return mock
            return mock_uv_run_success(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            sync_venv(conn, "/tmp/test-venv")

        from uv_mgr.db import get_venv_packages, get_venv_by_path
        pkgs = get_venv_packages(conn, add_venv(conn, "/tmp/test-venv"))
        assert pkgs == []
        v = get_venv_by_path(conn, "/tmp/test-venv")
        assert v["python_version"] == "3.11.15"

    def test_auto_register(self, conn, monkeypatch, mock_uv_run_success):
        """#33 未注册 + auto_register=True → 自动注册。"""
        from uv_mgr.db import get_venv_by_path

        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        sync_venv(conn, "/tmp/auto-venv", auto_register=True)
        assert get_venv_by_path(conn, "/tmp/auto-venv") is not None

    def test_no_auto_register(self, conn, capsys, mock_uv_run_success):
        """#34 未注册 + auto_register=False → 打印提示。"""
        sync_venv(conn, "/tmp/not-registered", auto_register=False)
        captured = capsys.readouterr()
        assert "未注册" in captured.out

    def test_deleted_venv_dir(self, conn, monkeypatch, capsys):
        """#35 venv 目录不存在 → 打印警告。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/deleted-venv")
        monkeypatch.setattr(os.path, "isdir", lambda p: False)

        sync_venv(conn, "/tmp/deleted-venv")
        captured = capsys.readouterr()
        assert "目录不存在" in captured.err

    def test_no_python_interpreter(self, conn, monkeypatch, capsys):
        """#36 找不到 python 解释器 → 打印警告。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/no-python")
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        monkeypatch.setattr(Path, "exists", lambda self: False)

        sync_venv(conn, "/tmp/no-python")
        captured = capsys.readouterr()
        assert "Python 解释器" in captured.err


# ── #88~90 verbose 参数 ────────────────────────────────────────────

class TestSyncVerbose:
    def test_sync_quiet_by_default(self, conn, monkeypatch, capsys,
                                   mock_uv_run_success):
        """#88 默认不打印"已同步"详情。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/quiet-venv")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        def mock_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and cmd[1] == "--version":
                return MagicMock(returncode=0, stdout="Python 3.11.15", stderr="")
            return mock_uv_run_success(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            sync_venv(conn, "/tmp/quiet-venv")

        captured = capsys.readouterr()
        assert "已同步" not in captured.out

    def test_sync_verbose_shows_details(self, conn, monkeypatch, capsys,
                                        mock_uv_run_success):
        """#89 verbose=True 时打印"已同步"详情。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/verbose-venv")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        def mock_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and cmd[1] == "--version":
                return MagicMock(returncode=0, stdout="Python 3.11.15", stderr="")
            return mock_uv_run_success(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            sync_venv(conn, "/tmp/verbose-venv", verbose=True)

        captured = capsys.readouterr()
        assert "已同步" in captured.out

    def test_sync_all_verbose_forwarded(self, conn, monkeypatch, capsys,
                                        mock_uv_run_success):
        """#90 sync_all verbose 传递到 sync_venv。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/forward-venv")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        def mock_run(cmd, *args, **kwargs):
            if len(cmd) > 1 and cmd[1] == "--version":
                return MagicMock(returncode=0, stdout="Python 3.11.15", stderr="")
            return mock_uv_run_success(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_run):
            sync_all(conn, auto_discover=False, verbose=True)

        captured = capsys.readouterr()
        assert "已同步: /tmp/forward-venv" in captured.out


# ── #38~42 sync_all ────────────────────────────────────────────────

class TestSyncAll:
    def test_missing_cwd_skips_discovery_and_continues(self, conn, monkeypatch, capsys):
        """失效当前目录不应阻断已注册环境和 tool 的同步。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/registered")
        monkeypatch.setattr(Path, "cwd", lambda: (_ for _ in ()).throw(FileNotFoundError()))
        monkeypatch.setattr("uv_mgr.sync.discover_tool_venvs", lambda: [])
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "1.0")), \
             patch("uv_mgr.sync.sync_venv", return_value=True) as sync:
            assert sync_all(conn, auto_discover=True) is True
        sync.assert_called_once()
        assert "当前工作目录已不存在" in capsys.readouterr().err

    def test_prune_missing_venv_is_successful(self, conn, capsys):
        """index gc 清理失效记录后不应报告同步失败。"""
        from uv_mgr.db import add_venv, get_venv_by_path

        path = "/tmp/missing-for-prune"
        add_venv(conn, path)
        assert sync_venv(conn, path, prune=True) is True
        assert get_venv_by_path(conn, path) is None
        assert "已清理失效记录" in capsys.readouterr().out

    def test_no_venvs_with_autodiscover(self, conn, monkeypatch, capsys):
        """#38 无注册 venv + auto_discover=True → 扫描当前目录 .venv。"""
        # 模拟 /tmp/some-project/.venv 存在，其他路径没有 .venv
        existing_dirs = {"/tmp/some-project/.venv"}
        monkeypatch.setattr(Path, "is_dir", lambda self: str(self) in existing_dirs)
        monkeypatch.setattr(Path, "cwd", lambda: Path("/tmp/some-project"))
        monkeypatch.setattr(os.path, "isdir", lambda p: p in existing_dirs)

        # 自动发现测试不依赖外部 uv；只验证目录发现和注册逻辑。
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            sync_all(conn, auto_discover=True)
        captured = capsys.readouterr()
        assert "发现新 venv" in captured.out or "已同步" in captured.out

    def test_no_venvs_no_autodiscover(self, conn, capsys):
        """#39 无注册 + auto_discover=False → 打印提示。"""
        sync_all(conn, auto_discover=False)
        captured = capsys.readouterr()
        assert "没有已注册的 venv" in captured.out

    def test_autodiscover_parent_dir_only(self, conn, monkeypatch, capsys):
        """#40 自动发现只注册最近的祖先 .venv（只发现 /parent/.venv）。"""
        from uv_mgr.db import list_venvs

        # 只在 /parent/.venv 存在, 子目录没有
        existing_dirs = {"/parent/.venv"}
        monkeypatch.setattr(Path, "is_dir", lambda self: str(self) in existing_dirs)
        monkeypatch.setattr(Path, "cwd", lambda: Path("/parent/child/deep"))
        monkeypatch.setattr(os.path, "isdir", lambda p: p in existing_dirs)

        # 自动发现测试不依赖外部 uv；只验证目录发现和注册逻辑。
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            sync_all(conn, auto_discover=True)
        venvs = list_venvs(conn)
        # 应只发现 /parent/.venv 这一个
        assert len(venvs) == 1
        assert "parent" in venvs[0]["path"]

    def test_skip_already_registered(self, conn, monkeypatch, capsys):
        """#41 已注册的 .venv 不重复 add。"""
        from uv_mgr.db import add_venv, list_venvs

        # 先手动注册
        add_venv(conn, "/tmp/project/.venv")
        count_before = len(list_venvs(conn))

        # sync_all 不会发现 /tmp/project/.venv，因为 .venv 在子目录的子目录里
        existing_dirs = {"/tmp/project/.venv"}
        monkeypatch.setattr(Path, "is_dir", lambda self: str(self) in existing_dirs)
        monkeypatch.setattr(Path, "cwd", lambda: Path("/tmp/project"))
        monkeypatch.setattr(os.path, "isdir", lambda p: p in existing_dirs)

        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            sync_all(conn, auto_discover=True)
        count_after = len(list_venvs(conn))
        assert count_after == count_before  # 不会增加

    def test_sync_all_venvs(self, conn, monkeypatch, mock_uv_run_success):
        """#42 sync_all 逐个同步所有已注册。"""
        from uv_mgr.db import add_venv

        add_venv(conn, "/tmp/venv-a")
        add_venv(conn, "/tmp/venv-b")
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        # 不报错即可
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            sync_all(conn, auto_discover=False)


# ── #43~45 run_uv_passthrough ──────────────────────────────────────

class TestRunUvPassthrough:
    @patch("subprocess.run")
    def test_normal_passthrough(self, mock_run):
        """#43 透传 uv 返回 uv 返回码。"""
        mock_run.return_value = MagicMock(returncode=0)
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            assert run_uv_passthrough(["pip", "list"]) == 0
        mock_run.assert_called_once_with(["uv", "pip", "list"])

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_uv_not_installed(self, mock_run, capsys):
        """#44 uv 命令不存在 → 错误信息 + 返回 1。"""
        code = run_uv_passthrough(["pip", "list"])
        assert code == 1
        captured = capsys.readouterr()
        assert "找不到 uv" in captured.err

    @patch("subprocess.run", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt(self, mock_run):
        """#45 Ctrl+C 中断返回 130。"""
        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            assert run_uv_passthrough(["pip", "list"]) == 130
