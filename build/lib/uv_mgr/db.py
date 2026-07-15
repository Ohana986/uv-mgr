"""SQLite 数据库层：建表、迁移、CRUD。"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

DB_DIR = Path.home() / ".local" / "share" / "uv-mgr"
DB_PATH = DB_DIR / "index.db"


def _ensure_dir() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    cur = conn.execute("SELECT value FROM _meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()


# ── Venv CRUD ───────────────────────────────────────────────────────

def add_venv(conn: sqlite3.Connection, path: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    name = os.path.basename(os.path.normpath(path))
    cur = conn.execute(
        """INSERT OR IGNORE INTO venvs (path, name, created_at)
           VALUES (?, ?, ?)""",
        (path, name, now),
    )
    conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute("SELECT id FROM venvs WHERE path = ?", (path,)).fetchone()
    return row["id"]


def remove_venv(conn: sqlite3.Connection, path: str) -> bool:
    cur = conn.execute("DELETE FROM venvs WHERE path = ?", (path,))
    conn.commit()
    return cur.rowcount > 0


def list_venvs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM venvs ORDER BY created_at"
    ).fetchall()


def get_venv_by_path(conn: sqlite3.Connection, path: str):
    return conn.execute(
        "SELECT * FROM venvs WHERE path = ?", (path,)
    ).fetchone()


# ── Package CRUD ────────────────────────────────────────────────────

def ensure_package(conn: sqlite3.Connection, name: str, version: str) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO packages (name, version) VALUES (?, ?)""",
        (name, version),
    )
    if cur.rowcount > 0:
        return cur.lastrowid
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
