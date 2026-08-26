"""平台相关的路径与外部命令配置。

本模块只依赖标准库，集中处理 Windows 与 POSIX 的差异，避免业务模块各自
判断平台。环境变量在每次调用时读取，便于嵌入调用方和测试覆盖默认值。
"""

import ntpath
import os
import shutil
import sys
from pathlib import Path


def is_windows() -> bool:
    """当前运行环境是否为 Windows。"""
    return sys.platform == "win32"


def _path_from_environment(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def get_data_dir() -> Path:
    """返回 uv-mgr 数据目录，支持 ``UV_MGR_DATA_DIR`` 覆盖。"""
    configured = os.environ.get("UV_MGR_DATA_DIR")
    if configured:
        return _path_from_environment(configured)
    if is_windows():
        base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                or str(Path.home() / "AppData" / "Local"))
        return _path_from_environment(base) / "uv-mgr"
    base = os.environ.get("XDG_DATA_HOME")
    return (_path_from_environment(base) if base else Path.home() / ".local" / "share") / "uv-mgr"


def get_db_path() -> Path:
    """返回索引数据库路径，``UV_MGR_DB_PATH`` 的优先级最高。"""
    configured = os.environ.get("UV_MGR_DB_PATH")
    return _path_from_environment(configured) if configured else get_data_dir() / "index.db"


def get_uv_command() -> str:
    """返回实际应执行的 uv 可执行文件，支持 ``UV_MGR_UV_BIN`` 覆盖。"""
    configured = os.environ.get("UV_MGR_UV_BIN")
    if configured:
        return os.path.expanduser(os.path.expandvars(configured))
    found = shutil.which("uv")
    if found is None and is_windows():
        found = shutil.which("uv.exe")
    return found or "uv"


def resolve_uv_command(command: list[str]) -> list[str]:
    """将逻辑命令开头的 ``uv`` 替换为当前机器的实际可执行文件。"""
    if command and command[0] == "uv":
        # POSIX 下保留 PATH 中的逻辑命令，便于 shell 与既有调用方处理；
        # Windows 或显式覆盖时使用解析出的完整路径。
        if is_windows() or os.environ.get("UV_MGR_UV_BIN"):
            return [get_uv_command(), *command[1:]]
    return list(command)


def normalize_path(value: str | os.PathLike[str]) -> str:
    """规范化用户提供的路径，Windows 上同时消除大小写差异。"""
    raw = os.path.expanduser(os.path.expandvars(os.fspath(value)))
    if is_windows():
        return ntpath.normcase(ntpath.abspath(raw))
    return str(Path(raw).resolve())


def venv_python_candidates(venv_path: str | os.PathLike[str]) -> tuple[Path, ...]:
    """返回当前平台优先的 venv Python 解释器候选路径。"""
    root = Path(venv_path)
    posix = root / "bin" / "python"
    windows_exe = root / "Scripts" / "python.exe"
    windows_plain = root / "Scripts" / "python"
    if is_windows():
        return windows_exe, windows_plain, posix
    return posix, windows_exe, windows_plain


def subprocess_text_kwargs() -> dict[str, str]:
    """Windows 上为文本子进程指定稳定编码；POSIX 保持现有调用形状。"""
    return {"encoding": "utf-8", "errors": "replace"} if is_windows() else {}
