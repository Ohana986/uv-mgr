"""CLI 路由与参数解析测试 — 情景 #46~67。"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── #46 main() 顶层 ────────────────────────────────────────────────

class TestMainEntry:
    """测试 main() 入口逻辑。"""

    def test_no_args_shows_help(self, capsys, monkeypatch):
        """#46 无参数 → 打印帮助，返回 0。"""
        from uv_mgr.cli import main
        with patch("uv_mgr.cli.get_connection"), patch("uv_mgr.cli.init_db"):
            code = main([])
        assert code == 0
        captured = capsys.readouterr()
        assert "usage:" in captured.out

    def test_version(self, capsys, monkeypatch):
        """#47 --version 打印版本号。"""
        from uv_mgr.cli import main
        with patch("uv_mgr.cli.get_connection"), patch("uv_mgr.cli.init_db"):
            with patch("uv_mgr.cli.run_uv_passthrough") as mock_uv:
                mock_uv.return_value = 0
                code = main(["--version"])
        # --version 当前被透传给 uv, 由 uv 返回版本号
        assert code == 0


# ── #48~52 add / remove ───────────────────────────────────────────

class TestAddRemoveCommands:
    def test_add_valid_path(self, conn, capsys, monkeypatch):
        """#48 add 有效路径 → 注册成功。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        from uv_mgr.cli import _cmd_add
        args = MagicMock(venv_path="/tmp/valid-venv")
        code = _cmd_add(args)
        assert code == 0
        out = capsys.readouterr().out
        assert "已注册" in out

    def test_add_invalid_path(self, conn, capsys, monkeypatch):
        """#49 add 无效路径 → 报错返回 1。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        monkeypatch.setattr(os.path, "isdir", lambda p: False)
        from uv_mgr.cli import _cmd_add
        args = MagicMock(venv_path="/tmp/no-such-dir")
        code = _cmd_add(args)
        assert code == 1
        err = capsys.readouterr().err
        assert "目录不存在" in err

    def test_add_already_registered(self, db_path, monkeypatch):
        """#50 add 已注册路径 → 幂等不报错。"""
        # _cmd_add 内部会 close 连接，所以 get_connection 必须每次都返回新连接
        import sqlite3
        from uv_mgr.db import init_db

        def fresh_conn():
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn

        monkeypatch.setattr("uv_mgr.cli.get_connection", fresh_conn)
        monkeypatch.setattr("uv_mgr.db.DB_PATH", db_path)
        monkeypatch.setattr("uv_mgr.db.DB_DIR", db_path.parent)
        monkeypatch.setattr(os.path, "isdir", lambda p: True)
        # 先初始化数据库
        init_db(fresh_conn())
        from uv_mgr.cli import _cmd_add
        args = MagicMock(venv_path="/tmp/existing-venv")
        assert _cmd_add(args) == 0
        assert _cmd_add(args) == 0  # 第二次不报错

    def test_remove_existing(self, conn, capsys, monkeypatch):
        """#51 remove 已注册 → 移除成功，提示 GC。"""
        from uv_mgr.db import add_venv
        add_venv(conn, "/tmp/to-remove")

        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_remove
        args = MagicMock(venv_path="/tmp/to-remove")
        code = _cmd_remove(args)
        assert code == 0
        out = capsys.readouterr().out
        assert "已移除" in out
        assert "gc" in out.lower()

    def test_remove_nonexistent(self, conn, capsys, monkeypatch):
        """#52 remove 未注册 → 报错返回 1。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_remove
        args = MagicMock(venv_path="/tmp/not-registered")
        code = _cmd_remove(args)
        assert code == 1
        out = capsys.readouterr().out
        assert "未找到" in out


# ── #53~57 list ────────────────────────────────────────────────────

class TestListCommand:
    def test_list_all(self, conn, capsys, monkeypatch):
        """#53 list 无子选项 → 显示全部。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_list
        args = MagicMock(venvs=False, packages=False, orphans=False, type=None)
        code = _cmd_list(args)
        assert code == 0
        out = capsys.readouterr().out
        assert "已注册 venv" in out
        assert "已索引包" in out
        assert "孤立包" in out

    def test_list_venvs_only(self, conn, capsys, monkeypatch):
        """#54 list --venvs 只显示 venvs。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_list
        args = MagicMock(venvs=True, packages=False, orphans=False, type=None)
        _cmd_list(args)
        out = capsys.readouterr().out
        assert "已注册 venv" in out
        assert "已索引包" not in out

    def test_list_packages_only(self, conn, capsys, monkeypatch):
        """#55 list --packages 只显示 packages。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_list
        args = MagicMock(venvs=False, packages=True, orphans=False, type=None)
        _cmd_list(args)
        out = capsys.readouterr().out
        assert "已索引包" in out
        assert "已注册 venv" not in out

    def test_list_orphans_only(self, conn, capsys, monkeypatch):
        """#56 list --orphans 只显示 orphan。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_list
        args = MagicMock(venvs=False, packages=False, orphans=True, type=None)
        _cmd_list(args)
        out = capsys.readouterr().out
        assert "孤立包" in out

    def test_list_empty(self, conn, capsys, monkeypatch):
        """#57 无数据 → 显示 0。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_list
        args = MagicMock(venvs=False, packages=False, orphans=False, type=None)
        _cmd_list(args)
        out = capsys.readouterr().out
        assert "0 个" in out or "（0" in out


# ── #58~59 sync ────────────────────────────────────────────────────

class TestSyncCommand:
    def test_sync_all(self, conn, capsys, monkeypatch):
        """#58 sync 无参数 → sync_all。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        with patch("uv_mgr.cli.sync_all") as mock:
            from uv_mgr.cli import _cmd_sync
            args = MagicMock(venv_path=None)
            _cmd_sync(args)
        mock.assert_called_once()

    def test_sync_specific(self, conn, capsys, monkeypatch):
        """#59 sync <path> → sync_venv 指定路径。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        monkeypatch.setattr(os.path, "abspath", lambda p: p)
        with patch("uv_mgr.cli.sync_venv") as mock:
            from uv_mgr.cli import _cmd_sync
            args = MagicMock(venv_path="/tmp/my-venv", prune=False)
            _cmd_sync(args)
        mock.assert_called_once_with(conn, "/tmp/my-venv", auto_register=True, prune=False)


# ── #60~61 gc ──────────────────────────────────────────────────────

class TestGcCommand:
    def test_gc(self, monkeypatch):
        """#60 gc → 执行清理（dry_run=False）。"""
        with patch("uv_mgr.cli.gc") as mock:
            from uv_mgr.cli import _cmd_gc
            args = MagicMock(dry_run=False)
            _cmd_gc(args)
        mock.assert_called_once_with(dry_run=False)

    def test_gc_dry_run(self, monkeypatch):
        """#61 gc --dry-run → 预览模式。"""
        with patch("uv_mgr.cli.gc") as mock:
            from uv_mgr.cli import _cmd_gc
            args = MagicMock(dry_run=True)
            _cmd_gc(args)
        mock.assert_called_once_with(dry_run=True)


# ── #62 db ─────────────────────────────────────────────────────────

class TestDbCommand:
    def test_db_info(self, conn, capsys, monkeypatch):
        """#62 db info → 显示数据库统计。"""
        monkeypatch.setattr("uv_mgr.cli.get_connection", lambda: conn)
        from uv_mgr.cli import _cmd_db
        args = MagicMock(db_action="info")
        _cmd_db(args)
        out = capsys.readouterr().out
        assert "Schema 版本" in out
        assert "已注册 venv" in out
        assert "已索引包" in out


# ── #63~67 透传路由 ───────────────────────────────────────────────

class TestPassthroughRouting:
    def test_passthrough_called(self, monkeypatch):
        """#63 非自有命令 → 透传给 uv。"""
        from uv_mgr.cli import main
        with patch("uv_mgr.cli.get_connection"), patch("uv_mgr.cli.init_db"):
            with patch("uv_mgr.cli.run_uv_passthrough") as mock:
                mock.return_value = 0
                code = main(["pip", "install", "requests"])
        assert code == 0
        mock.assert_called_once_with(["pip", "install", "requests"])

    def test_auto_sync_after_success(self, monkeypatch):
        """#64 透传 uv 成功 (0) → 自动 sync。"""
        from uv_mgr.cli import main
        with patch("uv_mgr.cli.get_connection") as mock_conn:
            with patch("uv_mgr.cli.init_db"):
                with patch("uv_mgr.cli.run_uv_passthrough", return_value=0):
                    with patch("uv_mgr.cli.sync_all") as mock_sync:
                        code = main(["pip", "install", "x"])
        assert code == 0
        mock_sync.assert_called_once()

    def test_no_sync_after_failure(self, monkeypatch):
        """#65 透传 uv 失败 (非 0) → 跳过 sync。"""
        from uv_mgr.cli import main
        with patch("uv_mgr.cli.get_connection"):
            with patch("uv_mgr.cli.init_db"):
                with patch("uv_mgr.cli.run_uv_passthrough", return_value=1):
                    with patch("uv_mgr.cli.sync_all") as mock_sync:
                        code = main(["pip", "install", "x"])
        assert code == 1
        mock_sync.assert_not_called()

    def test_passthrough_skipped_for_own_commands(self, monkeypatch):
        """自有命令不触发透传。"""
        from uv_mgr.cli import main
        with patch("uv_mgr.cli.get_connection"), patch("uv_mgr.cli.init_db"):
            with patch("uv_mgr.cli.run_uv_passthrough") as mock:
                main(["index", "list"])
        mock.assert_not_called()
