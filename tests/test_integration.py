"""集成测试 — 情景 #79~87。"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestFullLifecycle:
    """#79 完整的注册→同步→查询→GC 生命周期。"""

    def test_add_sync_list_gc(self, conn, monkeypatch, capsys, db_path):
        """add → sync → list → gc --dry-run → gc。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages

        # ── add ──
        add_venv(conn, "/tmp/integration-venv")

        # ── sync（mock 扫描结果）──
        with patch("uv_mgr.sync.scan_venv_packages") as mock_scan:
            mock_scan.return_value = ([("requests", "2.31.0"), ("flask", "3.0.0")], True)
            from uv_mgr.sync import sync_venv
            monkeypatch.setattr(Path, "exists", lambda self: True)
            monkeypatch.setattr(os.path, "isdir", lambda p: True)
            sync_venv(conn, "/tmp/integration-venv")

        # ── list（验证）──
        from uv_mgr.db import list_venvs, get_venv_packages, get_orphan_packages
        venvs = list_venvs(conn)
        assert len(venvs) == 1
        pkgs = get_venv_packages(conn, venvs[0]["id"])
        assert len(pkgs) == 2
        assert get_orphan_packages(conn) == []

        # ── gc --dry-run ──
        from uv_mgr.gc import gc
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")):
            code = gc(dry_run=True, auto_sync=False)
        assert code == 0

        # gc() close 了 conn，重新连接
        from uv_mgr.db import get_connection as get_c
        conn = get_c()

        # ── remove venv → gc → 孤立包清理 ──
        from uv_mgr.db import remove_venv
        remove_venv(conn, "/tmp/integration-venv")

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all", return_value=True), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code = gc(dry_run=False, auto_sync=False)
        assert code == 0
        # 重新连接验证包已清理
        from uv_mgr.db import get_connection
        c2 = get_connection()
        orphans = get_orphan_packages(c2)
        c2.close()
        assert len(orphans) == 0
        db_path.unlink(missing_ok=True)


class TestSharedPackages:
    """#80 多个 venv 共享包不应出现在孤儿列表中。"""

    def test_shared_package_not_orphan(self, conn):
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages

        v1 = add_venv(conn, "/tmp/venv-1")
        v2 = add_venv(conn, "/tmp/venv-2")
        p = ensure_package(conn, "pytest", "8.0.0")
        replace_venv_packages(conn, v1, [p])
        replace_venv_packages(conn, v2, [p])

        from uv_mgr.db import get_orphan_packages
        assert get_orphan_packages(conn) == []

    def test_orphan_after_one_venv_removed(self, conn):
        """移除一个 venv 后，共享包不应该变成孤立。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv, get_orphan_packages

        v1 = add_venv(conn, "/tmp/venv-1")
        v2 = add_venv(conn, "/tmp/venv-2")
        p = ensure_package(conn, "pytest", "8.0.0")
        replace_venv_packages(conn, v1, [p])
        replace_venv_packages(conn, v2, [p])

        remove_venv(conn, "/tmp/venv-1")
        orphans = get_orphan_packages(conn)
        assert len(orphans) == 0  # 还在 venv-2 引用

        remove_venv(conn, "/tmp/venv-2")
        orphans = get_orphan_packages(conn)
        assert len(orphans) == 1  # 现在孤立了


class TestRemoveThenGc:
    """#81 移除 venv 后 GC 清理其独占包。"""

    def test_remove_then_gc(self, conn, monkeypatch):
        from uv_mgr.db import (
            add_venv, ensure_package, replace_venv_packages,
            remove_venv, get_orphan_packages,
        )

        v = add_venv(conn, "/tmp/exclusive-venv")
        p = ensure_package(conn, "exclusive-pkg", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/exclusive-venv")

        assert len(get_orphan_packages(conn)) == 1

        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.sync_all"), patch("uv_mgr.gc.check_uv_version", return_value=(True, "0.4.0")), patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            from uv_mgr.gc import gc
            gc(dry_run=False, auto_sync=False)

        # gc() close 了连接，重新连接验证
        from uv_mgr.db import get_connection as get_c2
        c2 = get_c2()
        assert len(get_orphan_packages(c2)) == 0
        c2.close()


class TestAutoSyncFlow:
    """#82~83 透传 uv 后自动 sync。"""

    @patch("uv_mgr.cli.run_uv_passthrough", return_value=0)
    def test_auto_sync_after_install(self, mock_uv, conn, monkeypatch, capsys):
        """#82 uv pip install → sync → 索引更新。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        monkeypatch.setattr("uv_mgr.cli.init_db", lambda c: None)

        with patch("uv_mgr.cli.sync_all") as mock_sync:
            from uv_mgr.cli import main
            main(["pip", "install", "requests"])
        mock_sync.assert_called_once()

    @patch("uv_mgr.cli.run_uv_passthrough", return_value=0)
    def test_auto_sync_after_uninstall(self, mock_uv, conn, monkeypatch):
        """#83 uv pip uninstall → sync → 引用消失。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        monkeypatch.setattr("uv_mgr.cli.init_db", lambda c: None)

        with patch("uv_mgr.cli.sync_all") as mock_sync:
            from uv_mgr.cli import main
            main(["pip", "uninstall", "requests"])
        mock_sync.assert_called_once()


class TestAutoDiscover:
    """#84 自动发现 .venv。"""

    def test_auto_discover_dot_venv(self, conn, monkeypatch):
        """sync 时自动发现当前目录的 .venv。"""
        from uv_mgr.sync import sync_all

        # 模拟 Path.cwd() 返回一个包含 .venv 的目录
        mock_cwd = Path("/tmp/project-with-venv")
        monkeypatch.setattr(Path, "cwd", lambda: mock_cwd)
        monkeypatch.setattr(os.path, "isdir", lambda p: p == "/tmp/project-with-venv/.venv")
        monkeypatch.setattr(Path, "is_dir", lambda self: self.name == ".venv")

        with patch("uv_mgr.sync.check_uv_version", return_value=(True, "0.4.0")):
            sync_all(conn, auto_discover=True)

        from uv_mgr.db import list_venvs
        venvs = list_venvs(conn)
        # 至少自动注册了 .venv（可能有祖先路径的 .venv）
        assert any("/.venv" in v["path"] or v["path"].endswith(".venv") for v in venvs)


class TestEnvVarSkipSync:
    """#85 UV_MGR_SYNC_AFTER=0 跳过自动 sync。"""

    def test_env_var_skips_sync(self, monkeypatch):
        from uv_mgr.sync import should_sync_after_uv
        monkeypatch.setenv("UV_MGR_SYNC_AFTER", "0")
        assert should_sync_after_uv(["pip", "install", "x"]) is False


class TestReAddAfterRemove:
    """#86 移除后重新注册同一路径。"""

    def test_remove_then_readd(self, conn, monkeypatch, capsys):
        from uv_mgr.db import add_venv, remove_venv, get_venv_by_path

        # 注册
        add_venv(conn, "/tmp/re-add-venv")
        assert get_venv_by_path(conn, "/tmp/re-add-venv") is not None

        # 移除
        remove_venv(conn, "/tmp/re-add-venv")
        assert get_venv_by_path(conn, "/tmp/re-add-venv") is None

        # 重新注册
        new_id = add_venv(conn, "/tmp/re-add-venv")
        row = get_venv_by_path(conn, "/tmp/re-add-venv")
        assert row["id"] == new_id


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "test-proj-a" / ".venv").is_dir(),
    reason="test-proj-a/.venv 不存在，跳过",
)
class TestExistingTestVenves:
    """#87 使用 test-proj-a 和 test-proj-b 验证。"""

    def test_sync_test_proj_a(self, monkeypatch, capsys):
        """同步 test-proj-a/.venv 应正确扫描已安装包。"""
        from uv_mgr.sync import sync_venv
        from uv_mgr.db import get_connection, init_db, get_venv_packages

        proj_dir = Path(__file__).resolve().parent.parent
        venv_path = str(proj_dir / "test-proj-a" / ".venv")

        monkeypatch.setattr("uv_mgr.db.DB_DIR", proj_dir / ".pytest-tmp" / "db")
        monkeypatch.setattr("uv_mgr.db.DB_PATH", proj_dir / ".pytest-tmp" / "db" / "index.db")
        os.makedirs(proj_dir / ".pytest-tmp" / "db", exist_ok=True)

        conn = get_connection()
        init_db(conn)

        # 执行 real sync（调用真实 uv）
        sync_venv(conn, venv_path, auto_register=True)

        packages = get_venv_packages(conn, 1)
        assert len(packages) > 0
        print(f"test-proj-a 包: {[p['name'] for p in packages]}")
        conn.close()
