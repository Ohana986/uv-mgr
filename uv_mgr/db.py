"""SQLite 数据库层：建表、迁移、CRUD。"""

import os
import re
import sqlite3
import ntpath
from datetime import datetime, timezone
from pathlib import Path

from uv_mgr.config import get_data_dir, get_db_path, is_windows, normalize_path

SCHEMA_VERSION = 10

DB_DIR = get_data_dir()
DB_PATH = get_db_path()


def _ensure_dir() -> None:
    # 以实际数据库文件的父目录为准，确保 UV_MGR_DB_PATH 与测试覆盖生效。
    _current_db_path().parent.mkdir(parents=True, exist_ok=True)


def _current_db_path() -> Path:
    """读取运行时环境变量，同时保留 DB_PATH 的测试/嵌入覆盖能力。"""
    if os.environ.get("UV_MGR_DB_PATH") or os.environ.get("UV_MGR_DATA_DIR"):
        return get_db_path()
    return DB_PATH


def get_connection() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(str(_current_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ── Schema ──────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venvs (
    id              INTEGER PRIMARY KEY,
    path            TEXT UNIQUE NOT NULL,
    name            TEXT,
    python_version  TEXT,
    source          TEXT NOT NULL DEFAULT 'user',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    version TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS venv_packages (
    venv_id     INTEGER NOT NULL REFERENCES venvs(id) ON DELETE CASCADE,
    package_id  INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    installed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (venv_id, package_id)
);

CREATE INDEX IF NOT EXISTS idx_venv_packages_package
    ON venv_packages(package_id);

CREATE TABLE IF NOT EXISTS operations (
    id             INTEGER PRIMARY KEY,
    occurred_at    TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    success        INTEGER NOT NULL,
    venv_path      TEXT,
    summary        TEXT NOT NULL DEFAULT '',
    error          TEXT
);

CREATE TABLE IF NOT EXISTS sync_snapshots (
    id             INTEGER PRIMARY KEY,
    venv_path      TEXT NOT NULL,
    python_version TEXT,
    occurred_at    TEXT NOT NULL,
    operation_id   INTEGER REFERENCES operations(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS snapshot_packages (
    snapshot_id INTEGER NOT NULL REFERENCES sync_snapshots(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, name)
);

CREATE TABLE IF NOT EXISTS package_events (
    id          INTEGER PRIMARY KEY,
    venv_path   TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    name        TEXT NOT NULL,
    old_version TEXT,
    new_version TEXT,
    snapshot_id INTEGER REFERENCES sync_snapshots(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_venv_time
    ON operations(venv_path, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_venv_time
    ON sync_snapshots(venv_path, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_package_events_name_time
    ON package_events(name, occurred_at DESC);

CREATE TABLE IF NOT EXISTS cache_rebuild_failures (
    environment_path TEXT PRIMARY KEY,
    environment_type TEXT NOT NULL,
    command_json TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 1,
    last_error   TEXT NOT NULL,
    last_failed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_rebuild_baseline (
    name       TEXT NOT NULL,
    version    TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS tool_rebuild_metadata (
    environment_path TEXT PRIMARY KEY,
    arguments_json   TEXT NOT NULL,
    python_version   TEXT NOT NULL,
    recorded_at      TEXT NOT NULL
);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        ALTER TABLE venvs ADD COLUMN source TEXT NOT NULL DEFAULT 'user';

    """)
    for row in conn.execute("SELECT id, path FROM venvs").fetchall():
        path = row["path"]
        portable = path.replace("\\", "/").lower()
        source = "user"
        if "/uv/tools/" in portable:
            source = "tool"
        elif portable.endswith("/.venv") or "/.venv/" in portable:
            source = "auto"
        conn.execute("UPDATE venvs SET source = ? WHERE id = ?", (source, row["id"]))
    conn.execute(
        "UPDATE _meta SET value = '2' WHERE key = 'schema_version'"
    )
    conn.commit()
    print("数据库迁移: v1 → v2（venvs 表新增 source 列，已分类现有记录）")


def normalize_package_name(name: str) -> str:
    """按 PEP 503 规则规范化 Python 发行包名称。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """统一包名，并合并规范化后重复的包版本记录。"""
    rows = conn.execute(
        "SELECT id, name, version FROM packages ORDER BY id"
    ).fetchall()
    groups: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        groups.setdefault(
            (normalize_package_name(row["name"]), row["version"]), []
        ).append(row["id"])

    # 先转移关联、删除重复行，避免更新名称时触发 UNIQUE(name, version)。
    for _key, ids in groups.items():
        retained_id, *duplicate_ids = ids
        for duplicate_id in duplicate_ids:
            conn.execute(
                """INSERT OR IGNORE INTO venv_packages (venv_id, package_id, installed_at)
                   SELECT venv_id, ?, installed_at
                   FROM venv_packages WHERE package_id = ?""",
                (retained_id, duplicate_id),
            )
            conn.execute("DELETE FROM packages WHERE id = ?", (duplicate_id,))

    # 重复项已移除，逐行更新不会再违反唯一约束。
    for (normalized_name, version), ids in groups.items():
        conn.execute(
            "UPDATE packages SET name = ? WHERE id = ? AND version = ?",
            (normalized_name, ids[0], version),
        )
    conn.execute(
        "UPDATE _meta SET value = '3' WHERE key = 'schema_version'"
    )
    conn.commit()
    print("数据库迁移: v2 → v3（已按 PEP 503 规范化包名并合并重复记录）")


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """启用操作审计、同步快照与包变更历史。"""
    conn.execute(
        "UPDATE _meta SET value = '4' WHERE key = 'schema_version'"
    )
    conn.commit()
    print("数据库迁移: v3 → v4（已启用操作审计与包版本历史）")


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """保存按包名重建缓存时的可重试失败项。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS cache_rebuild_failures (
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        last_error TEXT NOT NULL,
        last_failed_at TEXT NOT NULL,
        PRIMARY KEY (name, version)
    )""")
    conn.execute("UPDATE _meta SET value = '5' WHERE key = 'schema_version'")
    conn.commit()
    print("数据库迁移: v4 → v5（已启用缓存重建失败重试）")


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """保存已成功重建的版本，避免重复清理同一缓存。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS cache_rebuild_successes (
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        rebuilt_at TEXT NOT NULL,
        PRIMARY KEY (name, version)
    )""")
    conn.execute("UPDATE _meta SET value = '6' WHERE key = 'schema_version'")
    conn.commit()
    print("数据库迁移: v5 → v6（已启用缓存重建完成记录）")


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """改为按环境保存可重试的缓存恢复失败记录。"""
    conn.execute("DROP TABLE IF EXISTS cache_rebuild_successes")
    conn.execute("DROP TABLE IF EXISTS cache_rebuild_failures")
    conn.execute("""CREATE TABLE cache_rebuild_failures (
        environment_path TEXT PRIMARY KEY,
        environment_type TEXT NOT NULL,
        command_json TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        last_error TEXT NOT NULL,
        last_failed_at TEXT NOT NULL
    )""")
    conn.execute("UPDATE _meta SET value = '7' WHERE key = 'schema_version'")
    conn.commit()
    print("数据库迁移: v6 → v7（缓存重建失败记录已改为按环境保存）")


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """保存上次缓存重建时已观察到的未使用版本。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS cache_rebuild_baseline (
        name TEXT NOT NULL,
        version TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (name, version)
    )""")
    conn.execute("UPDATE _meta SET value = '8' WHERE key = 'schema_version'")
    conn.commit()
    print("数据库迁移: v7 → v8（已启用缓存重建增量基线）")


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """保存由 uv-mgr 安装的 tool 的安全重放元数据。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS tool_rebuild_metadata (
        environment_path TEXT PRIMARY KEY,
        arguments_json TEXT NOT NULL,
        python_version TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""")
    conn.execute("UPDATE _meta SET value = '9' WHERE key = 'schema_version'")
    conn.commit()
    print("数据库迁移: v8 → v9（已启用 uv tool 重建元数据）")


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """规范化 Windows venv 路径，并合并大小写造成的重复记录。"""
    if is_windows():
        rows = conn.execute(
            "SELECT id, path FROM venvs ORDER BY id"
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(normalize_path(row["path"]), []).append(row)

        for normalized, group in groups.items():
            keeper = group[0]
            keeper_id = keeper["id"]
            duplicate_ids = [row["id"] for row in group[1:]]

            # 先转移关联，避免删除重复 venv 时丢失其包清单。
            for duplicate_id in duplicate_ids:
                conn.execute(
                    """INSERT OR IGNORE INTO venv_packages
                       (venv_id, package_id, installed_at)
                       SELECT ?, package_id, installed_at
                       FROM venv_packages WHERE venv_id = ?""",
                    (keeper_id, duplicate_id),
                )

            # 两张重建状态表均以路径为主键。冲突时保留主 venv 的状态；
            # 主路径没有状态时，使用最早重复记录的状态补入。
            old_paths = [row["path"] for row in group]
            _migrate_environment_paths(
                conn, "tool_rebuild_metadata", old_paths, normalized,
            )
            _migrate_environment_paths(
                conn, "cache_rebuild_failures",
                [ntpath.dirname(path) for path in old_paths],
                ntpath.dirname(normalized), environment_type="project",
            )
            _migrate_environment_paths(
                conn, "cache_rebuild_failures", old_paths, normalized,
                environment_type="tool",
            )

            if duplicate_ids:
                placeholders = ",".join("?" for _ in duplicate_ids)
                conn.execute(
                    f"DELETE FROM venvs WHERE id IN ({placeholders})", duplicate_ids,
                )
            conn.execute(
                "UPDATE venvs SET path = ? WHERE id = ?", (normalized, keeper_id),
            )

    conn.execute("UPDATE _meta SET value = '10' WHERE key = 'schema_version'")
    conn.commit()
    print("数据库迁移: v9 → v10（已规范化 Windows venv 路径）")


def _migrate_environment_paths(conn: sqlite3.Connection, table: str,
                               old_paths: list[str], normalized: str,
                               *, environment_type: str | None = None) -> None:
    """将状态表中的一组旧路径合并为规范路径，保留最早记录。"""
    type_sql = " AND environment_type = ?" if environment_type else ""
    type_args = (environment_type,) if environment_type else ()
    # 状态记录可能使用了与 venvs 不同大小写的路径；按 Windows 规范化值
    # 补充候选，确保这些记录也能迁移。
    candidates = list(old_paths)
    for row in conn.execute(
        f"SELECT environment_path FROM {table} WHERE 1=1{type_sql}", type_args
    ).fetchall():
        path = row[0]
        if normalize_path(path) == normalized and path not in candidates:
            candidates.append(path)
    if conn.execute(
        f"SELECT 1 FROM {table} WHERE environment_path = ?{type_sql}",
        (normalized, *type_args),
    ).fetchone() is None:
        for old_path in candidates:
            if old_path == normalized:
                continue
            changed = conn.execute(
                f"UPDATE OR IGNORE {table} SET environment_path = ? "
                f"WHERE environment_path = ?{type_sql}",
                (normalized, old_path, *type_args),
            ).rowcount
            if changed:
                break
    for old_path in candidates:
        if old_path != normalized:
            conn.execute(
                f"DELETE FROM {table} WHERE environment_path = ?{type_sql}",
                (old_path, *type_args),
            )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    cur = conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    else:
        version = int(row[0])
        if version < 2:
            _migrate_v1_to_v2(conn)
            version = 2
        if version < 3:
            _migrate_v2_to_v3(conn)
            version = 3
        if version < 4:
            _migrate_v3_to_v4(conn)
            version = 4
        if version < 5:
            _migrate_v4_to_v5(conn)
            version = 5
        if version < 6:
            _migrate_v5_to_v6(conn)
            version = 6
        if version < 7:
            _migrate_v6_to_v7(conn)
            version = 7
        if version < 8:
            _migrate_v7_to_v8(conn)
            version = 8
        if version < 9:
            _migrate_v8_to_v9(conn)
            version = 9
        if version < 10:
            _migrate_v9_to_v10(conn)
        elif version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本 {version} 高于当前支持的版本 {SCHEMA_VERSION}，"
                "请升级 uv-mgr。"
            )
    conn.commit()


# ── Venv CRUD ───────────────────────────────────────────────────────

def add_venv(conn: sqlite3.Connection, path: str, source: str = 'user') -> int:
    path = normalize_path(path)
    now = datetime.now(timezone.utc).isoformat()
    name = os.path.basename(os.path.normpath(path))
    existing = get_venv_by_path(conn, path)
    conn.execute(
        """INSERT OR IGNORE INTO venvs (path, name, source, created_at)
           VALUES (?, ?, ?, ?)""",
        (path, name, source, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM venvs WHERE path = ?", (path,)).fetchone()
    if existing is None:
        record_operation(
            conn, "venv_added", venv_path=path,
            summary=f"已注册 venv（来源: {source}）",
        )
    return row["id"]


def remove_venv(conn: sqlite3.Connection, path: str) -> bool:
    path = normalize_path(path)
    conn.execute("DELETE FROM tool_rebuild_metadata WHERE environment_path = ?", (path,))
    cur = conn.execute("DELETE FROM venvs WHERE path = ?", (path,))
    conn.commit()
    if cur.rowcount > 0:
        record_operation(conn, "venv_removed", venv_path=path,
                         summary="已取消注册 venv")
    return cur.rowcount > 0


def list_venvs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM venvs ORDER BY created_at"
    ).fetchall()


def get_venvs_by_source(conn: sqlite3.Connection, source: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM venvs WHERE source = ? ORDER BY created_at", (source,)
    ).fetchall()


def get_venv_by_path(conn: sqlite3.Connection, path: str):
    path = normalize_path(path)
    return conn.execute(
        "SELECT * FROM venvs WHERE path = ?", (path,)
    ).fetchone()


# ── Package CRUD ────────────────────────────────────────────────────

def ensure_package(conn: sqlite3.Connection, name: str, version: str) -> int:
    name = normalize_package_name(name)
    conn.execute(
        """INSERT OR IGNORE INTO packages (name, version) VALUES (?, ?)""",
        (name, version),
    )
    row = conn.execute(
        "SELECT id FROM packages WHERE name = ? AND version = ?",
        (name, version),
    ).fetchone()
    return row["id"]


# ── Venv-Package 关联 ───────────────────────────────────────────────

def replace_venv_packages(
    conn: sqlite3.Connection, venv_id: int, package_ids: list[int]
) -> None:
    """全量替换该 venv 的包列表（先删后插）。"""
    conn.execute("DELETE FROM venv_packages WHERE venv_id = ?", (venv_id,))
    now = datetime.now(timezone.utc).isoformat()
    for pid in package_ids:
        conn.execute(
            """INSERT OR IGNORE INTO venv_packages (venv_id, package_id, installed_at)
               VALUES (?, ?, ?)""",
            (venv_id, pid, now),
        )
    conn.execute(
        "UPDATE venvs SET last_synced_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), venv_id),
    )
    conn.commit()


def prune_historical_orphan_packages(conn: sqlite3.Connection) -> int:
    """删除同名包仍有在用版本时遗留的未引用历史版本记录。

    完全没有版本被引用的包会保留，供 GC 按包名安全处理。
    """
    cur = conn.execute(
        """DELETE FROM packages AS old
           WHERE NOT EXISTS (
               SELECT 1 FROM venv_packages AS old_link
               WHERE old_link.package_id = old.id
           )
           AND EXISTS (
               SELECT 1
               FROM packages AS current
               JOIN venv_packages AS current_link
                 ON current_link.package_id = current.id
               WHERE current.name = old.name
           )"""
    )
    conn.commit()
    return cur.rowcount


# ── 操作与包版本历史 ───────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_operation(
    conn: sqlite3.Connection, operation_type: str, *, success: bool = True,
    venv_path: str | None = None, summary: str = "", error: str | None = None,
    occurred_at: str | None = None,
) -> int:
    """写入一条独立于当前索引的内部操作审计记录。"""
    cur = conn.execute(
        """INSERT INTO operations
           (occurred_at, operation_type, success, venv_path, summary, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (occurred_at or _now(), operation_type, int(success), venv_path,
         summary, error),
    )
    conn.commit()
    return cur.lastrowid


def _compare_simple_versions(old: str, new: str) -> int | None:
    """比较仅含数字和点的版本；其他 PEP 440 形式返回 None。

    项目不引入外部依赖，标准库也没有 PEP 440 解析器。预发布、开发版和
    本地版本等形式若强行按字符串拆分会产生错误的升级/降级结论，因此由
    调用方将其记录为中性的版本变更。
    """
    if not re.fullmatch(r"\d+(?:\.\d+)*", old) or not re.fullmatch(
        r"\d+(?:\.\d+)*", new
    ):
        return None

    def parts(version: str) -> tuple[int, ...]:
        values = [int(part) for part in version.split(".")]
        while len(values) > 1 and values[-1] == 0:
            values.pop()
        return tuple(values)

    old_parts, new_parts = parts(old), parts(new)
    if new_parts > old_parts:
        return 1
    if new_parts < old_parts:
        return -1
    return 0


def record_sync_history(
    conn: sqlite3.Connection, venv_path: str, python_version: str | None,
    packages: list[tuple[str, str]],
) -> int:
    """保存成功同步的完整快照，并记录相对前一快照的包变更。"""
    now = _now()
    current = {normalize_package_name(name): version for name, version in packages}
    previous_snapshot = conn.execute(
        """SELECT id FROM sync_snapshots WHERE venv_path = ?
           ORDER BY occurred_at DESC, id DESC LIMIT 1""",
        (venv_path,),
    ).fetchone()
    previous: dict[str, str] = {}
    if previous_snapshot is not None:
        previous = {
            row["name"]: row["version"]
            for row in conn.execute(
                "SELECT name, version FROM snapshot_packages WHERE snapshot_id = ?",
                (previous_snapshot["id"],),
            ).fetchall()
        }

    operation_id = record_operation(
        conn, "sync", venv_path=venv_path,
        summary=f"同步成功（{len(current)} 个包）", occurred_at=now,
    )
    cur = conn.execute(
        """INSERT INTO sync_snapshots
           (venv_path, python_version, occurred_at, operation_id)
           VALUES (?, ?, ?, ?)""",
        (venv_path, python_version, now, operation_id),
    )
    snapshot_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO snapshot_packages (snapshot_id, name, version) VALUES (?, ?, ?)",
        [(snapshot_id, name, version) for name, version in current.items()],
    )

    events: list[tuple[str, str, str | None, str | None]] = []
    for name in sorted(current.keys() - previous.keys()):
        events.append(("installed", name, None, current[name]))
    for name in sorted(previous.keys() - current.keys()):
        events.append(("removed", name, previous[name], None))
    for name in sorted(current.keys() & previous.keys()):
        old_version, new_version = previous[name], current[name]
        if old_version == new_version:
            continue
        comparison = _compare_simple_versions(old_version, new_version)
        if comparison is None or comparison == 0:
            event_type = "changed"
        elif comparison > 0:
            event_type = "upgraded"
        else:
            event_type = "downgraded"
        events.append((event_type, name, old_version, new_version))
    conn.executemany(
        """INSERT INTO package_events
           (venv_path, occurred_at, event_type, name, old_version, new_version, snapshot_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(venv_path, now, event_type, name, old, new, snapshot_id)
         for event_type, name, old, new in events],
    )
    conn.commit()
    return snapshot_id


def get_operations(conn: sqlite3.Connection, *, venv_path: str | None = None,
                   limit: int = 50) -> list[sqlite3.Row]:
    sql = "SELECT * FROM operations"
    params: list[object] = []
    if venv_path:
        sql += " WHERE venv_path = ?"
        params.append(venv_path)
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_package_events(conn: sqlite3.Connection, *, venv_path: str | None = None,
                       package_name: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if venv_path:
        clauses.append("venv_path = ?")
        params.append(venv_path)
    if package_name:
        clauses.append("name = ?")
        params.append(normalize_package_name(package_name))
    sql = "SELECT * FROM package_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_snapshots(conn: sqlite3.Connection, *, venv_path: str | None = None,
                  package_name: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    params: list[object] = []
    sql = "SELECT s.* FROM sync_snapshots s"
    if package_name:
        sql += " JOIN snapshot_packages p ON p.snapshot_id = s.id"
    clauses: list[str] = []
    if venv_path:
        clauses.append("s.venv_path = ?")
        params.append(venv_path)
    if package_name:
        clauses.append("p.name = ?")
        params.append(normalize_package_name(package_name))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY s.occurred_at DESC, s.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_snapshot_packages(conn: sqlite3.Connection, snapshot_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, version FROM snapshot_packages WHERE snapshot_id = ? ORDER BY name",
        (snapshot_id,),
    ).fetchall()


# ── 查询 ────────────────────────────────────────────────────────────

def get_venv_packages(conn: sqlite3.Connection, venv_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.id, p.name, p.version
           FROM packages p
           JOIN venv_packages vp ON vp.package_id = p.id
           WHERE vp.venv_id = ?
           ORDER BY p.name""",
        (venv_id,),
    ).fetchall()


def get_orphan_packages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.id, p.name, p.version
           FROM packages p
           WHERE p.id NOT IN (
               SELECT DISTINCT vp.package_id FROM venv_packages vp
           )
           ORDER BY p.name, p.version""",
    ).fetchall()


def remove_orphan_packages(conn: sqlite3.Connection, ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM packages WHERE id IN ({placeholders})", ids
    )
    conn.commit()


def get_new_rebuild_packages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """返回上次重建后新出现、且目前不再被任何环境使用的包版本。

    首次执行时基线为空，所有满足条件的历史版本都会被返回。每次成功清理
    待重建缓存后，调用方会把当前完整状态写回基线；因此后续只会处理新增项。
    """
    rows = conn.execute(
        """WITH current_names AS (
               SELECT DISTINCT p.name
               FROM packages p JOIN venv_packages vp ON vp.package_id = p.id
           ), historical_versions AS (
               SELECT DISTINCT old.name, old.version
               FROM snapshot_packages old
               JOIN current_names current ON current.name = old.name
               WHERE NOT EXISTS (
                   SELECT 1 FROM packages active
                   JOIN venv_packages link ON link.package_id = active.id
                   WHERE active.name = old.name AND active.version = old.version
               )
           )
           SELECT historical.name, historical.version
           FROM historical_versions historical
           LEFT JOIN cache_rebuild_baseline baseline
             ON baseline.name = historical.name AND baseline.version = historical.version
           WHERE baseline.name IS NULL
           ORDER BY historical.name, historical.version"""
    ).fetchall()
    return rows


def replace_rebuild_baseline(conn: sqlite3.Connection) -> None:
    """以当前未使用历史版本替换重建增量基线。"""
    now = _now()
    conn.execute("DELETE FROM cache_rebuild_baseline")
    conn.execute(
        """INSERT INTO cache_rebuild_baseline (name, version, observed_at)
           WITH current_names AS (
               SELECT DISTINCT p.name
               FROM packages p JOIN venv_packages vp ON vp.package_id = p.id
           )
           SELECT DISTINCT old.name, old.version, ?
           FROM snapshot_packages old
           JOIN current_names current ON current.name = old.name
           WHERE NOT EXISTS (
               SELECT 1 FROM packages active
               JOIN venv_packages link ON link.package_id = active.id
               WHERE active.name = old.name AND active.version = old.version
           )""",
        (now,),
    )
    conn.commit()


def record_tool_rebuild_metadata(conn: sqlite3.Connection, environment_path: str,
                                 arguments_json: str, python_version: str) -> None:
    """保存由 uv-mgr 安装的 tool 的安全重放参数。"""
    conn.execute(
        """INSERT INTO tool_rebuild_metadata
           (environment_path, arguments_json, python_version, recorded_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(environment_path) DO UPDATE SET
             arguments_json = excluded.arguments_json,
             python_version = excluded.python_version,
             recorded_at = excluded.recorded_at""",
        (normalize_path(environment_path), arguments_json, python_version, _now()),
    )
    conn.commit()


def get_tool_rebuild_metadata(conn: sqlite3.Connection, environment_path: str):
    """取得指定 tool 环境的安全重放元数据。"""
    return conn.execute(
        "SELECT * FROM tool_rebuild_metadata WHERE environment_path = ?",
        (normalize_path(environment_path),),
    ).fetchone()


def record_rebuild_failure(conn: sqlite3.Connection, environment_path: str,
                           environment_type: str, command_json: str,
                           error: str) -> None:
    """记录环境恢复失败；同一路径再次失败时增加重试次数。"""
    conn.execute(
        """INSERT INTO cache_rebuild_failures
           (environment_path, environment_type, command_json, attempts, last_error, last_failed_at)
           VALUES (?, ?, ?, 1, ?, ?)
           ON CONFLICT(environment_path) DO UPDATE SET
             environment_type = excluded.environment_type,
             command_json = excluded.command_json,
             attempts = attempts + 1,
             last_error = excluded.last_error,
             last_failed_at = excluded.last_failed_at""",
        (normalize_path(environment_path), environment_type, command_json, error, _now()),
    )
    conn.commit()


def clear_rebuild_failure(conn: sqlite3.Connection, environment_path: str) -> None:
    conn.execute(
        "DELETE FROM cache_rebuild_failures WHERE environment_path = ?",
        (normalize_path(environment_path),),
    )
    conn.commit()


def get_rebuild_failures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """返回待重试的环境恢复记录。"""
    return conn.execute(
        "SELECT * FROM cache_rebuild_failures ORDER BY last_failed_at, environment_path"
    ).fetchall()


def prune_rebuild_failures(conn: sqlite3.Connection) -> None:
    """删除不再对应已索引环境的缓存恢复失败记录。"""
    conn.execute(
        """DELETE FROM cache_rebuild_failures AS failure
           WHERE NOT EXISTS (
               SELECT 1 FROM venvs v
               WHERE (failure.environment_type = 'project'
                      AND v.path = failure.environment_path || '/.venv')
                  OR (failure.environment_type = 'tool'
                      AND v.path = failure.environment_path)
           )"""
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection) -> dict:
    venv_count = conn.execute("SELECT COUNT(*) FROM venvs").fetchone()[0]
    pkg_count = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
    vp_count = conn.execute("SELECT COUNT(*) FROM venv_packages").fetchone()[0]
    orphan_count = conn.execute(
        "SELECT COUNT(*) FROM packages WHERE id NOT IN (SELECT DISTINCT package_id FROM venv_packages)"
    ).fetchone()[0]
    return {
        "venvs": venv_count,
        "packages": pkg_count,
        "venv_package_links": vp_count,
        "orphans": orphan_count,
    }
