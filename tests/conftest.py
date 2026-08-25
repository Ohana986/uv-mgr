"""pytest 共享 fixture：数据库隔离、mock uv 子进程、mock 文件系统。"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from uv_mgr.db import get_connection, init_db


# ── 数据库隔离 ──────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    """返回一个临时数据库路径，供 monkeypatch 使用。"""
    return tmp_path / "test_index.db"


@pytest.fixture
def conn(db_path, monkeypatch):
    """返回一个已初始化、指向临时文件的数据库连接。

    自动 monkeypatch uv_mgr.db.DB_PATH，每个测试独立。
    """
    monkeypatch.setattr("uv_mgr.db.DB_PATH", db_path)
    monkeypatch.setattr("uv_mgr.db.DB_DIR", db_path.parent)
    c = get_connection()
    init_db(c)
    yield c
    try:
        c.close()
    except Exception:
        pass
    if db_path.exists():
        db_path.unlink(missing_ok=True)


# ── Mock uv 子进程 ─────────────────────────────────────────────────

@pytest.fixture
def mock_uv_run():
    """Mock subprocess.run，返回可控的 CompletedProcess。

    用法:
        mock_uv_run.return_value = MagicMock(returncode=0, stdout="...")
    """
    with patch("subprocess.run") as mock:
        yield mock


@pytest.fixture
def mock_uv_run_success():
    """Mock subprocess.run 返回成功 (returncode=0, stdout='{}')。"""
    with patch("subprocess.run") as mock:
        mock.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr="",
        )
        yield mock


@pytest.fixture
def mock_scan_venv():
    """Mock sync.scan_venv_packages 返回可控的包列表。

        用法:
        mock_scan_venv.return_value = ([("requests", "2.31.0")], True)
    """
    with patch("uv_mgr.sync.scan_venv_packages") as mock:
        yield mock


# ── Mock 文件系统 ──────────────────────────────────────────────────

@pytest.fixture
def mock_isdir():
    """Mock os.path.isdir 默认返回 True。"""
    with patch("os.path.isdir", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_path_exists():
    """Mock Path.exists 默认返回 True。"""
    with patch.object(Path, "exists", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_path_isdir():
    """Mock Path.is_dir 默认返回 True。"""
    with patch.object(Path, "is_dir", return_value=True) as mock:
        yield mock


# ── Mock 环境变量 ──────────────────────────────────────────────────

@pytest.fixture
def unset_uv_sync_after(monkeypatch):
    """确保 UV_SYNC_AFTER 环境变量不存在。"""
    monkeypatch.delenv("UV_SYNC_AFTER", raising=False)


# ── 预制数据 ───────────────────────────────────────────────────────

@pytest.fixture
def sample_venvs(conn):
    """向数据库插入两个示例 venv，返回其 ID 列表。"""
    from uv_mgr.db import add_venv

    id_a = add_venv(conn, "/tmp/venv-a")
    id_b = add_venv(conn, "/tmp/venv-b")
    return [id_a, id_b]


@pytest.fixture
def sample_packages(conn):
    """向数据库插入三个示例包，返回其 ID 列表。"""
    from uv_mgr.db import ensure_package

    ids = [
        ensure_package(conn, "requests", "2.31.0"),
        ensure_package(conn, "pytest", "8.0.0"),
        ensure_package(conn, "flask", "3.0.0"),
    ]
    return ids


@pytest.fixture
def linked_packages(conn, sample_venvs, sample_packages):
    """将 sample_packages 关联到 sample_venvs[0]。"""
    from uv_mgr.db import replace_venv_packages

    replace_venv_packages(conn, sample_venvs[0], sample_packages)
    yield
