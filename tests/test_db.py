"""数据库层测试 — 情景 #1~21。"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from uv_mgr.db import (
    get_connection,
    init_db,
    SCHEMA_VERSION,
    DB_DIR,
    DB_PATH,
    add_venv,
    remove_venv,
    list_venvs,
    get_venv_by_path,
    ensure_package,
    replace_venv_packages,
    get_venv_packages,
    get_orphan_packages,
    remove_orphan_packages,
    normalize_package_name,
    prune_historical_orphan_packages,
    get_stats,
    record_sync_history,
    get_package_events,
    get_snapshots,
    get_snapshot_packages,
)
from uv_mgr.config import normalize_path


# ── #1 初始化空库 ──────────────────────────────────────────────────

class TestInitDb:
    def test_tables_exist(self, conn):
        """初始化后应包含所有表和 _meta。"""
        meta = conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        assert meta is not None
        assert meta["value"] == str(SCHEMA_VERSION)

        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "venvs" in tables
        assert "packages" in tables
        assert "venv_packages" in tables
        assert "_meta" in tables
        assert "tool_rebuild_metadata" in tables

    # ── #2 重复初始化 ──────────────────────────────────────────

    def test_reinit_is_idempotent(self, conn):
        """重复 init_db 不报错，数据不丢失。"""
        add_venv(conn, "/tmp/my-venv")
        init_db(conn)
        venvs = list_venvs(conn)
        assert len(venvs) == 1

    def test_migrate_v2_to_v3_normalizes_and_merges_packages(self, conn):
        v = add_venv(conn, "/tmp/venv")
        conn.execute("UPDATE _meta SET value = '2' WHERE key = 'schema_version'")
        conn.execute("INSERT INTO packages (name, version) VALUES ('Spire.Doc_Free', '1.0')")
        first = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO packages (name, version) VALUES ('spire-doc-free', '1.0')")
        second = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO venv_packages (venv_id, package_id) VALUES (?, ?)",
            (v, second),
        )
        conn.commit()

        init_db(conn)

        packages = conn.execute("SELECT id, name FROM packages").fetchall()
        assert [(row["id"], row["name"]) for row in packages] == [
            (first, "spire-doc-free"),
        ]
        links = conn.execute("SELECT package_id FROM venv_packages").fetchall()
        assert [row["package_id"] for row in links] == [first]
        assert conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)

    def test_migrate_v3_to_v4_creates_history_tables(self, conn):
        conn.execute("UPDATE _meta SET value = '3' WHERE key = 'schema_version'")
        conn.commit()
        init_db(conn)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert {"operations", "sync_snapshots", "snapshot_packages", "package_events"} <= tables
        assert conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)

    def test_migrate_v8_to_v9_creates_tool_rebuild_metadata(self, conn):
        conn.execute("UPDATE _meta SET value = '8' WHERE key = 'schema_version'")
        conn.execute("DROP TABLE tool_rebuild_metadata")
        conn.commit()
        init_db(conn)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tool_rebuild_metadata'"
        ).fetchone() is not None

    def test_migrate_v9_to_v10_normalizes_windows_paths(self, conn, monkeypatch):
        monkeypatch.setattr("uv_mgr.config.sys.platform", "win32")
        conn.execute("UPDATE _meta SET value = '9' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO venvs (id, path, name, source, created_at) VALUES "
            "(10, ?, 'project', 'auto', '2024-01-01'), "
            "(11, ?, 'project', 'auto', '2024-01-02')",
            (r"C:\Users\Alice\Proj\.venv", r"c:\users\alice\proj\.VENV"),
        )
        package = ensure_package(conn, "demo", "1.0")
        conn.execute(
            "INSERT INTO venv_packages (venv_id, package_id) VALUES (11, ?)",
            (package,),
        )
        conn.execute(
            "INSERT INTO tool_rebuild_metadata "
            "(environment_path, arguments_json, python_version, recorded_at) "
            "VALUES (?, '[]', '3.11', '2024-01-01')",
            (r"C:\USERS\ALICE\PROJ\.VENV",),
        )
        conn.execute(
            "INSERT INTO cache_rebuild_failures "
            "(environment_path, environment_type, command_json, last_error, last_failed_at) "
            "VALUES (?, 'project', '[]', 'failed', '2024-01-01')",
            (r"C:\USERS\ALICE\PROJ",),
        )
        conn.commit()

        init_db(conn)

        rows = conn.execute("SELECT id, path FROM venvs").fetchall()
        assert [(row["id"], row["path"]) for row in rows] == [
            (10, r"c:\users\alice\proj\.venv"),
        ]
        assert conn.execute(
            "SELECT venv_id FROM venv_packages"
        ).fetchone()[0] == 10
        assert get_venv_by_path(conn, r"C:\USERS\ALICE\PROJ\.VENV")["id"] == 10
        assert conn.execute(
            "SELECT environment_path FROM tool_rebuild_metadata"
        ).fetchone()[0] == r"c:\users\alice\proj\.venv"
        assert conn.execute(
            "SELECT environment_path FROM cache_rebuild_failures"
        ).fetchone()[0] == r"c:\users\alice\proj"
        assert conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)

        from uv_mgr.sync import sync_venv
        monkeypatch.setattr("uv_mgr.sync.os.path.isdir", lambda _path: True)
        monkeypatch.setattr("uv_mgr.sync.venv_python_path", lambda _path: Path("python.exe"))
        with patch("uv_mgr.sync.scan_venv_packages", return_value=([], True)):
            assert sync_venv(conn, r"C:\USERS\ALICE\PROJ\.VENV", check_uv=False)
        assert remove_venv(conn, r"C:\USERS\ALICE\PROJ\.VENV")
        assert get_venv_by_path(conn, r"c:\users\alice\proj\.venv") is None
        assert conn.execute("SELECT COUNT(*) FROM tool_rebuild_metadata").fetchone()[0] == 0

    def test_migrate_v9_to_v10_keeps_posix_paths(self, conn, monkeypatch):
        monkeypatch.setattr("uv_mgr.config.sys.platform", "linux")
        conn.execute("UPDATE _meta SET value = '9' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO venvs (path, name, source, created_at) VALUES "
            "('/Tmp/Mixed/.venv', 'project', 'auto', '2024-01-01')"
        )
        conn.commit()

        init_db(conn)

        assert conn.execute("SELECT path FROM venvs").fetchone()[0] == "/Tmp/Mixed/.venv"

    def test_migrate_v10_to_v11_normalizes_history_paths(self, conn, monkeypatch):
        monkeypatch.setattr("uv_mgr.config.sys.platform", "win32")
        conn.execute("UPDATE _meta SET value = '10' WHERE key = 'schema_version'")
        conn.execute(
            "INSERT INTO operations (occurred_at, operation_type, success, venv_path) "
            "VALUES ('2024-01-01', 'sync', 1, ?)",
            (r"C:\Users\Alice\Proj\.venv",),
        )
        conn.execute(
            "INSERT INTO sync_snapshots (venv_path, occurred_at) VALUES (?, '2024-01-01')",
            (r"C:\Users\Alice\Proj\.venv",),
        )
        conn.execute(
            "INSERT INTO package_events (venv_path, occurred_at, event_type, name) "
            "VALUES (?, '2024-01-01', 'installed', 'demo')",
            (r"C:\Users\Alice\Proj\.venv",),
        )
        conn.commit()

        init_db(conn)

        expected = r"c:\users\alice\proj\.venv"
        for table in ("operations", "sync_snapshots", "package_events"):
            assert conn.execute(f"SELECT venv_path FROM {table}").fetchone()[0] == expected
        assert conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)


# ── #3~9 Venv CRUD ────────────────────────────────────────────────

class TestVenvCrud:
    def test_add_venv_returns_id(self, conn):
        """#3 注册 venv 返回自增 ID。"""
        vid = add_venv(conn, "/tmp/test-venv")
        assert isinstance(vid, int)
        assert vid > 0

    def test_add_duplicate_path(self, conn):
        """#4 重复注册同一路径，返回已有 ID，不报错。"""
        vid1 = add_venv(conn, "/tmp/test-venv")
        vid2 = add_venv(conn, "/tmp/test-venv")
        assert vid1 == vid2
        assert len(list_venvs(conn)) == 1

    def test_get_venv_by_path(self, conn):
        """#5 add 后 get_venv_by_path 返回正确行。"""
        vid = add_venv(conn, "/tmp/test-venv")
        row = get_venv_by_path(conn, "/tmp/test-venv")
        assert row is not None
        assert row["id"] == vid
        assert row["path"] == normalize_path("/tmp/test-venv")
        assert row["name"] == "test-venv"
        assert row["created_at"] is not None

    def test_remove_venv_removes_tool_rebuild_metadata(self, conn):
        from uv_mgr.db import get_tool_rebuild_metadata, record_tool_rebuild_metadata

        add_venv(conn, "/tmp/tool", source="tool")
        record_tool_rebuild_metadata(conn, "/tmp/tool", '["uv"]', "3.11")
        assert remove_venv(conn, "/tmp/tool")
        assert get_tool_rebuild_metadata(conn, "/tmp/tool") is None

    def test_remove_existing(self, conn):
        """#6 移除存在的 venv 返回 True，级联删除关联。"""
        vid = add_venv(conn, "/tmp/test-venv")
        ensure_package(conn, "foo", "1.0")
        replace_venv_packages(conn, vid, [1])
        assert remove_venv(conn, "/tmp/test-venv") is True
        # venv_packages 应级联清理
        links = conn.execute(
            "SELECT COUNT(*) FROM venv_packages WHERE venv_id=?", (vid,)
        ).fetchone()[0]
        assert links == 0

    def test_remove_nonexistent(self, conn):
        """#7 移除不存在的 venv 返回 False。"""
        assert remove_venv(conn, "/tmp/no-such-venv") is False

    def test_list_venvs_empty(self, conn):
        """#8 空列表。"""
        assert list_venvs(conn) == []

    def test_list_venvs_ordered(self, conn):
        """#9 多个 venv 按 created_at 排序。"""
        v1 = add_venv(conn, "/tmp/venv-a")
        v2 = add_venv(conn, "/tmp/venv-b")
        v3 = add_venv(conn, "/tmp/venv-c")
        ids = [r["id"] for r in list_venvs(conn)]
        assert ids == [v1, v2, v3]  # created_at 递增


# ── #10~13 Package CRUD ────────────────────────────────────────────

class TestPackageCrud:
    @pytest.mark.parametrize(("raw_name", "expected"), [
        ("Spire.Doc_Free", "spire-doc-free"),
        ("foo---bar__baz..qux", "foo-bar-baz-qux"),
    ])
    def test_normalize_package_name(self, raw_name, expected):
        assert normalize_package_name(raw_name) == expected

    def test_ensure_new_package(self, conn):
        """#10 新包 INSERT，返回新 ID。"""
        pid = ensure_package(conn, "requests", "2.31.0")
        assert isinstance(pid, int)
        assert pid > 0

    def test_ensure_existing_package(self, conn):
        """#11 已存在包返回已有 ID。"""
        pid1 = ensure_package(conn, "requests", "2.31.0")
        pid2 = ensure_package(conn, "requests", "2.31.0")
        assert pid1 == pid2

    def test_package_unique_constraint(self, conn):
        """同名同版本只存一条。"""
        ensure_package(conn, "foo", "1.0")
        ensure_package(conn, "foo", "1.0")
        rows = conn.execute(
            "SELECT COUNT(*) FROM packages"
        ).fetchone()[0]
        assert rows == 1

    def test_same_name_diff_version(self, conn):
        """同名不同版本各自独立。"""
        p1 = ensure_package(conn, "foo", "1.0")
        p2 = ensure_package(conn, "foo", "2.0")
        assert p1 != p2

    def test_name_variants_share_one_package_record(self, conn):
        p1 = ensure_package(conn, "Spire.Doc_Free", "1.0")
        p2 = ensure_package(conn, "spire-doc-free", "1.0")
        assert p1 == p2
        assert conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 1


class TestReplaceVenvPackages:
    def test_replace_linked(self, conn):
        """#12 全量替换—先删后插，更新 last_synced_at。"""
        v = add_venv(conn, "/tmp/venv")
        p1 = ensure_package(conn, "a", "1.0")
        p2 = ensure_package(conn, "b", "2.0")
        replace_venv_packages(conn, v, [p1, p2])

        rows = get_venv_packages(conn, v)
        assert len(rows) == 2

        row = get_venv_by_path(conn, "/tmp/venv")
        assert row["last_synced_at"] is not None

    def test_replace_empty(self, conn):
        """#13 传入空列表清空关联。"""
        v = add_venv(conn, "/tmp/venv")
        p1 = ensure_package(conn, "a", "1.0")
        replace_venv_packages(conn, v, [p1])
        replace_venv_packages(conn, v, [])
        assert get_venv_packages(conn, v) == []


class TestHistory:
    def test_snapshots_and_events_cover_lifecycle(self, conn):
        path = "/tmp/history-venv"
        record_sync_history(conn, path, "3.11", [("foo", "1.0"), ("bar", "1.0")])
        record_sync_history(conn, path, "3.11", [("foo", "2.0"), ("baz", "1.0")])
        record_sync_history(conn, path, "3.11", [("foo", "1.5"), ("baz", "1.0")])
        record_sync_history(conn, path, "3.11", [("foo", "1.5"), ("baz", "1.0")])

        snapshots = get_snapshots(conn, venv_path=path)
        assert len(snapshots) == 4
        assert len(get_snapshot_packages(conn, snapshots[0]["id"])) == 2
        events = get_package_events(conn, venv_path=path, limit=20)
        assert [event["event_type"] for event in events].count("installed") == 3
        assert [event["event_type"] for event in events].count("removed") == 1
        assert [event["event_type"] for event in events].count("upgraded") == 1
        assert [event["event_type"] for event in events].count("downgraded") == 1

    def test_history_package_filter_normalizes_name(self, conn):
        record_sync_history(conn, "/tmp/history-venv", None,
                            [("Spire.Doc_Free", "1.0")])
        events = get_package_events(conn, package_name="spire_doc_free")
        snapshots = get_snapshots(conn, package_name="spire-doc-free")
        assert len(events) == 1
        assert len(snapshots) == 1

    def test_prerelease_to_final_is_neutral_version_change(self, conn):
        path = "/tmp/history-venv"
        record_sync_history(conn, path, None, [("foo", "1.0rc1")])
        record_sync_history(conn, path, None, [("foo", "1.0")])

        events = get_package_events(conn, venv_path=path, limit=10)
        assert events[0]["event_type"] == "changed"

    def test_removing_venv_preserves_history(self, conn):
        path = "/tmp/history-venv"
        v = add_venv(conn, path)
        record_sync_history(conn, path, None, [("foo", "1.0")])
        remove_venv(conn, path)
        assert get_snapshots(conn, venv_path=path)[0]["venv_path"] == path
        assert v > 0


# ── #14~21 查询 ─────────────────────────────────────────────────────

class TestQueries:
    def test_get_venv_packages_ordered(self, conn, sample_venvs, sample_packages):
        """#14 get_venv_packages 按包名排序。"""
        from uv_mgr.db import replace_venv_packages

        # 乱序传入以检验 ORDER BY
        replace_venv_packages(conn, sample_venvs[0], [sample_packages[2], sample_packages[0], sample_packages[1]])
        pkgs = get_venv_packages(conn, sample_venvs[0])
        names = [p["name"] for p in pkgs]
        assert names == sorted(names)  # flask, pytest, requests

    def test_no_orphans_when_linked(self, conn, linked_packages):
        """#15 所有包都被引用时无孤立包。"""
        orphans = get_orphan_packages(conn)
        assert len(orphans) == 0

    def test_orphans_after_venv_removed(self, conn, linked_packages):
        """#16 删除 venv 后包变为孤立。"""
        from uv_mgr.db import remove_venv
        remove_venv(conn, "/tmp/venv-a")
        orphans = get_orphan_packages(conn)
        assert len(orphans) == 3

    def test_partial_version_orphan_not_counted(self, conn):
        """#17 pkgA v1 被引用、v2 未引用 → v2 仍计入孤立（存在未引用的记录就计入）。"""
        v = add_venv(conn, "/tmp/venv")
        p1 = ensure_package(conn, "foo", "1.0")
        _p2 = ensure_package(conn, "foo", "2.0")
        replace_venv_packages(conn, v, [p1])

        orphans = get_orphan_packages(conn)
        assert len(orphans) == 1       # 只有 v2 是孤立
        assert orphans[0]["version"] == "2.0"

    def test_stats_correct(self, conn, linked_packages):
        """#18 get_stats 计数准确。"""
        stats = get_stats(conn)
        assert stats["venvs"] == 2
        assert stats["packages"] == 3
        assert stats["venv_package_links"] == 3  # 只关联到 venv-a
        assert stats["orphans"] == 0

    def test_stats_with_orphans(self, conn, linked_packages):
        """删除 venv 后孤立计数正确。"""
        from uv_mgr.db import remove_venv
        remove_venv(conn, "/tmp/venv-a")
        stats = get_stats(conn)
        assert stats["orphans"] == 3

    def test_remove_orphan_packages(self, conn):
        """#19 批量删除孤立包记录。"""
        p1 = ensure_package(conn, "del-me", "1.0")
        p2 = ensure_package(conn, "keep-me", "2.0")
        # 不关联任何 venv 使两者都是孤立
        remove_orphan_packages(conn, [p1])
        rows = conn.execute("SELECT id FROM packages").fetchall()
        ids = [r["id"] for r in rows]
        assert p1 not in ids
        assert p2 in ids

    def test_prune_historical_orphan_keeps_fully_orphaned_package(self, conn):
        v = add_venv(conn, "/tmp/venv")
        current = ensure_package(conn, "foo", "2.0")
        old = ensure_package(conn, "foo", "1.0")
        fully_orphaned = ensure_package(conn, "bar", "1.0")
        replace_venv_packages(conn, v, [current])

        assert prune_historical_orphan_packages(conn) == 1
        ids = {row["id"] for row in conn.execute("SELECT id FROM packages")}
        assert old not in ids
        assert current in ids
        assert fully_orphaned in ids

    def test_prune_historical_orphan_keeps_shared_current_version(self, conn):
        v1 = add_venv(conn, "/tmp/venv-a")
        v2 = add_venv(conn, "/tmp/venv-b")
        current = ensure_package(conn, "foo", "2.0")
        old = ensure_package(conn, "foo", "1.0")
        replace_venv_packages(conn, v1, [current])
        replace_venv_packages(conn, v2, [current])

        prune_historical_orphan_packages(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM packages WHERE id = ?", (current,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM packages WHERE id = ?", (old,)
        ).fetchone()[0] == 0

    def test_db_path(self):
        """#20 数据库路径使用当前平台默认配置。"""
        import uv_mgr.db
        from uv_mgr.config import get_data_dir, get_db_path
        assert uv_mgr.db.DB_PATH == get_db_path()
        assert uv_mgr.db.DB_DIR == get_data_dir()

    def test_foreign_key_cascade_on_venv_delete(self, conn):
        """#21 删除 venv → venv_packages 级联清理。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_venv

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "x", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_venv(conn, "/tmp/venv")

        remaining = conn.execute(
            "SELECT COUNT(*) FROM venv_packages"
        ).fetchone()[0]
        assert remaining == 0
        # packages 表不变（只有关联被删除）
        assert conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 1

    def test_foreign_key_cascade_on_package_delete(self, conn):
        """#21 删除 package → venv_packages 级联清理。"""
        from uv_mgr.db import add_venv, ensure_package, replace_venv_packages, remove_orphan_packages

        v = add_venv(conn, "/tmp/venv")
        p = ensure_package(conn, "x", "1.0")
        replace_venv_packages(conn, v, [p])
        remove_orphan_packages(conn, [p])

        remaining = conn.execute(
            "SELECT COUNT(*) FROM venv_packages"
        ).fetchone()[0]
        assert remaining == 0
