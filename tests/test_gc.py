"""GC 层测试。"""

from unittest.mock import MagicMock, patch
from uv_mgr.config import normalize_path, resolve_uv_command, subprocess_text_kwargs

from uv_mgr.gc import (
    _get_packages_all_versions_orphaned, _parse_tool_receipt,
    _tool_rebuild_command, gc,
)


class TestGetPackagesAllVersionsOrphaned:
    def test_partial_version_not_counted(self, conn):
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages

        venv = add_venv(conn, "/tmp/venv")
        current = ensure_package(conn, "foo", "1.0")
        ensure_package(conn, "foo", "2.0")
        replace_venv_packages(conn, venv, [current])
        assert _get_packages_all_versions_orphaned(conn) == []


class TestGc:
    def test_default_gc_cleans_only_completely_orphaned_package(self, conn, monkeypatch):
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        venv = add_venv(conn, "/tmp/venv")
        orphan = ensure_package(conn, "orphan-pkg", "1.0")
        used = ensure_package(conn, "used-pkg", "1.0")
        replace_venv_packages(conn, venv, [orphan])
        remove_venv(conn, "/tmp/venv")
        active = add_venv(conn, "/tmp/active")
        replace_venv_packages(conn, active, [used])
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("uv_mgr.gc.check_uv_version", return_value=(True, "1.0")), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as run:
            assert gc(auto_sync=False) == 0
        assert [call.args[0] for call in run.call_args_list] == [resolve_uv_command(["uv", "cache", "clean", "orphan-pkg"])]

    def test_rebuild_cleans_candidate_names_once_and_syncs_project(self, conn, tmp_path, monkeypatch):
        from uv_mgr.db import add_venv, ensure_package, record_sync_history, replace_venv_packages

        project = tmp_path / "project"
        venv_path = project / ".venv"
        venv_path.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
        venv = add_venv(conn, str(venv_path), source="auto")
        first = ensure_package(conn, "first", "2.0")
        second = ensure_package(conn, "second", "3.0")
        replace_venv_packages(conn, venv, [first, second])
        record_sync_history(conn, str(venv_path), "3.11", [("first", "1.0"), ("second", "2.0")])
        record_sync_history(conn, str(venv_path), "3.11", [("first", "2.0"), ("second", "3.0")])
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        monkeypatch.setattr("uv_mgr.gc.sync_venv", lambda *args, **kwargs: True)
        with patch("uv_mgr.gc.check_uv_version", return_value=(True, "1.0")), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            assert gc(auto_sync=False, rebuild=True) == 0
        commands = [call.args[0] for call in run.call_args_list]
        assert resolve_uv_command(["uv", "cache", "clean", "first", "second"]) in commands
        sync_call = next(call for call in run.call_args_list if call.args[0] == resolve_uv_command(["uv", "sync"]))
        assert sync_call.kwargs == {"cwd": normalize_path(project), "timeout": 900}

    def test_rebuild_failure_is_retried_without_cleaning(self, conn, db_path, tmp_path, monkeypatch):
        from uv_mgr.db import add_venv, ensure_package, get_rebuild_failures, record_sync_history, replace_venv_packages

        project = tmp_path / "project"
        venv_path = project / ".venv"
        venv_path.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
        venv = add_venv(conn, str(venv_path), source="auto")
        package = ensure_package(conn, "retry-pkg", "2.0")
        replace_venv_packages(conn, venv, [package])
        record_sync_history(conn, str(venv_path), "3.11", [("retry-pkg", "1.0")])
        record_sync_history(conn, str(venv_path), "3.11", [("retry-pkg", "2.0")])
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        monkeypatch.setattr("uv_mgr.gc.sync_venv", lambda *args, **kwargs: True)
        with patch("uv_mgr.gc.check_uv_version", return_value=(True, "1.0")), \
             patch("subprocess.run", side_effect=[
                 MagicMock(returncode=0, stderr=""), MagicMock(returncode=1, stdout="", stderr="failed"),
             ]):
            assert gc(auto_sync=False, rebuild=True) == 1
        import sqlite3
        reopened = sqlite3.connect(str(db_path))
        reopened.row_factory = sqlite3.Row
        assert len(get_rebuild_failures(reopened)) == 1
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: reopened)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            assert gc(auto_sync=False, rebuild=True, retry=True) == 0
        run.assert_called_once_with(resolve_uv_command(["uv", "sync"]), cwd=normalize_path(project), capture_output=True, text=True, timeout=900, **subprocess_text_kwargs())
        final = sqlite3.connect(str(db_path))
        final.row_factory = sqlite3.Row
        assert get_rebuild_failures(final) == []
        final.close()

    def test_rebuild_failure_records_generic_error_when_output_is_streamed(
            self, conn, db_path, tmp_path, monkeypatch):
        from uv_mgr.db import add_venv, get_rebuild_failures, ensure_package, record_sync_history, replace_venv_packages

        project = tmp_path / "project"
        venv_path = project / ".venv"
        venv_path.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
        venv = add_venv(conn, str(venv_path), source="auto")
        package = ensure_package(conn, "failed-pkg", "2.0")
        replace_venv_packages(conn, venv, [package])
        record_sync_history(conn, str(venv_path), "3.11", [("failed-pkg", "1.0")])
        record_sync_history(conn, str(venv_path), "3.11", [("failed-pkg", "2.0")])
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        monkeypatch.setattr("uv_mgr.gc.sync_venv", lambda *args, **kwargs: True)
        with patch("uv_mgr.gc.check_uv_version", return_value=(True, "1.0")), \
             patch("subprocess.run", side_effect=[
                 MagicMock(returncode=0, stderr=""), MagicMock(returncode=1),
             ]) as run:
            assert gc(auto_sync=False, rebuild=True) == 1
        restore_call = run.call_args_list[1]
        assert restore_call.kwargs == {"cwd": normalize_path(project), "timeout": 900}
        import sqlite3
        reopened = sqlite3.connect(str(db_path))
        reopened.row_factory = sqlite3.Row
        assert "详见上方 uv 输出" in get_rebuild_failures(reopened)[0]["last_error"]
        reopened.close()

    def test_rebuild_skips_missing_venv_and_restores_others(self, conn, tmp_path, monkeypatch, capsys):
        """重建跳过失效环境，不阻断存在项目的恢复。"""
        from uv_mgr.db import add_venv, ensure_package, record_sync_history, replace_venv_packages

        project = tmp_path / "project"
        venv_path = project / ".venv"
        venv_path.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0'\n")
        active = add_venv(conn, str(venv_path), source="auto")
        missing = add_venv(conn, str(tmp_path / "missing" / ".venv"), source="auto")
        package = ensure_package(conn, "rebuild-pkg", "2.0")
        missing_package = ensure_package(conn, "missing-pkg", "2.0")
        replace_venv_packages(conn, active, [package])
        replace_venv_packages(conn, missing, [missing_package])
        record_sync_history(conn, str(venv_path), "3.11", [("rebuild-pkg", "1.0")])
        record_sync_history(conn, str(venv_path), "3.11", [("rebuild-pkg", "2.0")])
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        monkeypatch.setattr("uv_mgr.gc.sync_all", lambda *args, **kwargs: True)
        monkeypatch.setattr("uv_mgr.gc.sync_venv", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            "uv_mgr.gc.get_new_rebuild_packages",
            lambda _conn: [
                {"name": "rebuild-pkg", "version": "1.0"},
                {"name": "missing-pkg", "version": "1.0"},
            ],
        )
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            assert gc(rebuild=True) == 0
        assert resolve_uv_command(["uv", "sync"]) in [call.args[0] for call in run.call_args_list]
        assert "跳过" in capsys.readouterr().out

    def test_dry_run_does_not_execute_commands(self, conn, monkeypatch):
        monkeypatch.setattr("uv_mgr.gc.get_connection", lambda: conn)
        with patch("subprocess.run") as run:
            assert gc(dry_run=True, rebuild=True) == 0
        run.assert_not_called()


class TestToolReceipt:
    def test_recognized_receipt_replays_requirements_and_options(self, tmp_path):
        receipt = tmp_path / "uv-receipt.toml"
        receipt.write_text(
            "[tool]\nrequirements = [{ name = \"demo\" }, { name = \"extra\", specifier = \"<2\" }]\n"
            "python = \"3.11\"\n[tool.options]\nlink-mode = \"hardlink\"\n"
        )
        assert _parse_tool_receipt(receipt) == [
            "uv", "tool", "install", "--force", "demo", "--with", "extra<2",
            "--python", "3.11", "--link-mode", "hardlink",
        ]

    def test_unrecognized_receipt_is_skipped(self, tmp_path):
        receipt = tmp_path / "uv-receipt.toml"
        receipt.write_text("[tool]\nrequirements = [{ name = \"demo\", url = \"https://example.test/x\" }]\n")
        assert _parse_tool_receipt(receipt) is None

    def test_rebuild_uses_saved_metadata_with_fixed_python(self, conn, tmp_path):
        from uv_mgr.db import add_venv, record_tool_rebuild_metadata

        tool = tmp_path / "demo"
        tool.mkdir()
        venv_id = add_venv(conn, str(tool), source="tool")
        conn.execute("UPDATE venvs SET python_version = '3.12.1' WHERE id = ?", (venv_id,))
        record_tool_rebuild_metadata(
            conn, str(tool), '["uv", "tool", "install", "demo", "--with", "extra"]', "3.11",
        )
        venv = conn.execute("SELECT * FROM venvs WHERE id = ?", (venv_id,)).fetchone()
        assert _tool_rebuild_command(conn, venv) == (
            ["uv", "tool", "install", "--force", "demo", "--with", "extra", "--python", "3.11"],
            None,
        )

    def test_rebuild_receipt_falls_back_to_index_python(self, conn, tmp_path):
        from uv_mgr.db import add_venv

        tool = tmp_path / "demo"
        tool.mkdir()
        (tool / "uv-receipt.toml").write_text(
            'requirements = [{ name = "demo" }]\nlink-mode = "copy"\n'
        )
        venv_id = add_venv(conn, str(tool), source="tool")
        conn.execute("UPDATE venvs SET python_version = '3.12.4' WHERE id = ?", (venv_id,))
        venv = conn.execute("SELECT * FROM venvs WHERE id = ?", (venv_id,)).fetchone()
        assert _tool_rebuild_command(conn, venv) == (
            ["uv", "tool", "install", "--force", "demo", "--link-mode", "copy", "--python", "3.12"],
            None,
        )

    def test_rebuild_skips_tool_without_known_python(self, conn, tmp_path):
        from uv_mgr.db import add_venv

        tool = tmp_path / "demo"
        tool.mkdir()
        (tool / "uv-receipt.toml").write_text('requirements = [{ name = "demo" }]\n')
        venv_id = add_venv(conn, str(tool), source="tool")
        venv = conn.execute("SELECT * FROM venvs WHERE id = ?", (venv_id,)).fetchone()
        assert _tool_rebuild_command(conn, venv) == (None, "无法确定原 tool 的 Python 主次版本")
