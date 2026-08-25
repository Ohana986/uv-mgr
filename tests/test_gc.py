"""GC 层测试 — 情景 #68~78。"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from uv_mgr.gc import _get_packages_all_versions_orphaned, gc


class TestGetPackagesAllVersionsOrphaned:
    """#68~70 _get_packages_all_versions_orphaned 内部函数测试。"""

    def test_no_orphans(self, conn, linked_packages):
        """#68 所有包都被引用 → 返回空。"""
        result = _get_packages_all_versions_orphaned(conn)
        assert result == []

    def test_all_versions_orphaned(self, conn, linked_packages):
        """#69 整个包没有任何引用。"""
        from uv_mgr.db import remove_venv

        remove_venv(conn, "/tmp/venv-a")
        result = _get_packages_all_versions_orphaned(conn)
        assert len(result) == 3
        names = {r["name"] for r in result}
        assert names == {"requests", "pytest", "flask"}

    def test_partial_version_not_counted(self, conn):
        """#70 pkg foo v1 被引用、v2 未引用 → 因 v1 还在使用，不计入。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages

        v = add_venv(conn, "/tmp/venv")
        p1 = ensure_package(conn, "foo", "1.0")
        _p2 = ensure_package(conn, "foo", "2.0")
        replace_venv_packages(conn, v, [p1])

        result = _get_packages_all_versions_orphaned(conn)
        # foo 的 v1 被引用，v2 虽未被引用但 package 整体有引用，不计入
        assert result == []


class TestGc:
    """#71~78 gc 主函数测试。"""

    def test_sync_failure_aborts_without_cleaning(self, conn, monkeypatch, capsys):
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=False), patch("subprocess.run") as mock_run:
            code = gc(auto_sync=True)
        assert code == 1
        mock_run.assert_not_called()
        assert "中止 GC" in capsys.readouterr().err

    def test_no_orphans(self, conn, linked_packages, capsys, monkeypatch):
        """#71 无孤立包 → 打印提示，返回 0。"""
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        # gc 内部会调用 sync_all，需要 mock
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")):
            code = gc(dry_run=False, auto_sync=False)
        assert code == 0
        captured = capsys.readouterr()
        assert "没有需要清理" in captured.out

    def test_dry_run(self, conn, capsys, monkeypatch, mock_uv_run):
        """#72 有孤立 + dry-run → 列出包，不执行清理。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "orphan-pkg", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")):
            code = gc(dry_run=True, auto_sync=False)

        assert code == 0
        captured = capsys.readouterr()
        assert "dry-run" in captured.out
        assert "orphan-pkg" in captured.out
        mock_uv_run.assert_not_called()

    def test_actual_gc(self, conn, capsys, monkeypatch):
        """#73 实际执行 gc → 调用 uv cache clean，删除 DB 记录。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "garbage-pkg", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code = gc(dry_run=False, auto_sync=False)

        assert code == 0
        captured = capsys.readouterr()
        assert "已清理" in captured.out
        # gc() 执行完后 conn 已被 close。重新获取连接验证
        from uv_mgr.db import get_connection
        c2 = get_connection()
        rows = c2.execute(
            "SELECT COUNT(*) FROM packages WHERE name='garbage-pkg'"
        ).fetchone()[0]
        c2.close()
        assert rows == 0

    def test_gc_auto_sync_false(self, conn, capsys, monkeypatch):
        """#74 auto_sync=False 不调用 sync_all。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "pkg", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True) as mock_sync:
            with patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                gc(dry_run=False, auto_sync=False)
        mock_sync.assert_not_called()

    def test_uv_cache_clean_fails(self, conn, capsys, monkeypatch):
        """#75 uv cache clean 失败 → 打印失败信息，跳过该包。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p1 = ensure_package(conn, "fail-pkg", "1.0")
        replace_venv_packages(conn, v, [p1])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
            gc(dry_run=False, auto_sync=False)

        captured = capsys.readouterr()
        assert "失败" in captured.out
        # 重新连接验证 DB 状态
        from uv_mgr.db import get_connection
        c2 = get_connection()
        rows = c2.execute(
            "SELECT COUNT(*) FROM packages WHERE name='fail-pkg'"
        ).fetchone()[0]
        c2.close()
        assert rows == 1

    def test_uv_cache_clean_timeout(self, conn, capsys, monkeypatch):
        """#76 uv cache clean 超时 → 打印超时信息。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "timeout-pkg", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="uv", timeout=120)):
            gc(dry_run=False, auto_sync=False)

        captured = capsys.readouterr()
        assert "超时" in captured.out

    def test_uv_not_found(self, conn, capsys, monkeypatch):
        """#77 uv 命令不存在 → 返回 1。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "pkg", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run", side_effect=FileNotFoundError):
            code = gc(dry_run=False, auto_sync=False)

        assert code == 1
        captured = capsys.readouterr()
        assert "找不到 uv" in captured.out

    def test_only_cleaned_packages_removed_from_db(self, conn, capsys, monkeypatch):
        """#78 只删除成功清理的包记录，失败的保留。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p1 = ensure_package(conn, "good-pkg", "1.0")
        p2 = ensure_package(conn, "bad-pkg", "2.0")
        replace_venv_packages(conn, v, [p1, p2])
        remove_venv(conn, "/tmp/venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run") as mock_run:
            # good-pkg 成功，bad-pkg 失败
            def side_effect(cmd, **kwargs):
                if "good-pkg" in cmd:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=1, stdout="", stderr="fail")
            mock_run.side_effect = side_effect
            gc(dry_run=False, auto_sync=False)

        # gc() close 了连接，重新打开验证
        from uv_mgr.db import get_connection
        c2 = get_connection()
        good_count = c2.execute(
            "SELECT COUNT(*) FROM packages WHERE name='good-pkg'"
        ).fetchone()[0]
        bad_count = c2.execute(
            "SELECT COUNT(*) FROM packages WHERE name='bad-pkg'"
        ).fetchone()[0]
        c2.close()
        assert good_count == 0  # 成功 → 删除
        assert bad_count == 1   # 失败 → 保留
