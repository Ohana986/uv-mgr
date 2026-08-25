"""Venv 状态同步：扫描 venv 已安装包，更新数据库索引。"""

import json
import os
import subprocess
import sys
import re
from pathlib import Path

from uv_mgr.db import (
    get_connection,
    init_db,
    add_venv,
    remove_venv,
    get_venv_by_path,
    ensure_package,
    replace_venv_packages,
    prune_historical_orphan_packages,
    record_operation,
    record_sync_history,
    list_venvs,
    get_venvs_by_source,
)


# 跳过 sync 的 uv 子命令
SKIP_SYNC_COMMANDS = frozenset({
    "self", "help", "version", "cache", "completions",
    "generate-shell-completion", "generate-default-namespace",
})

MIN_UV_VERSION = (0, 4)


def _parse_version(value: str) -> tuple[int, ...] | None:
    """解析 uv 的版本输出，忽略开发版后缀。"""
    if not isinstance(value, str):
        return None
    try:
        match = re.search(r"(?:^|\s)v?(\d+(?:\.\d+)+)", value)
        if not match:
            return None
        return tuple(int(part) for part in match.group(1).split("."))
    except (IndexError, ValueError):
        return None


def check_uv_version() -> tuple[bool, str]:
    """检查 uv 是否存在且满足最低版本要求。"""
    try:
        result = subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return False, "找不到 uv 命令，请先安装 uv"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"无法执行 uv --version：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"uv 版本检查失败{(': ' + detail) if detail else ''}"
    version = _parse_version(result.stdout or result.stderr)
    if version is None:
        return False, "无法解析 uv 版本，请升级 uv 后重试"
    if version < MIN_UV_VERSION:
        return False, f"uv 版本过低（当前 {'.'.join(map(str, version))}），最低支持 0.4"
    return True, ".".join(map(str, version))


def venv_python_path(venv_path: str) -> Path | None:
    """返回不同平台常见的 venv Python 解释器路径。"""
    root = Path(venv_path)
    for candidate in (root / "bin" / "python", root / "Scripts" / "python.exe",
                      root / "Scripts" / "python"):
        if candidate.exists():
            return candidate
    return None


def should_sync_after_uv(args: list[str]) -> bool:
    """判断 uv 子命令执行后是否需要 sync。"""
    if os.environ.get("UV_SYNC_AFTER") == "0":
        return False
    if not args:
        return False
    return args[0] not in SKIP_SYNC_COMMANDS


def discover_tool_venvs() -> list[str]:
    """运行 uv tool dir 获取所有工具 venv 路径。"""
    try:
        result = subprocess.run(
            ["uv", "tool", "dir"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        tool_dir = Path(result.stdout.strip())
        if not tool_dir.is_dir():
            return []
        return sorted(
            str(p.resolve())
            for p in tool_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def scan_venv_packages(venv_python: str, *, check_uv: bool = True) -> tuple[list[tuple[str, str]], bool]:
    """扫描指定 venv，返回 ``(包列表, 是否成功)``。"""
    if check_uv:
        ok, message = check_uv_version()
        if not ok:
            print(f"警告: {message}", file=sys.stderr)
            return [], False
    try:
        result = subprocess.run(
            ["uv", "pip", "list", "--python", venv_python,
             "--format=json", "--quiet"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"警告: 扫描 {venv_python} 失败:\n{result.stderr}",
                  file=sys.stderr)
            return [], False
        data = json.loads(result.stdout)
        if not isinstance(data, list):
            raise ValueError("uv 返回的包列表不是 JSON 数组")
        # uv 返回格式: [{"name": "pip", "version": "26.1.2"}, ...]
        packages = [(entry["name"], entry["version"]) for entry in data]
        return packages, True
    except (json.JSONDecodeError, KeyError, TypeError, ValueError,
            subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"警告: 扫描 venv 异常: {e}", file=sys.stderr)
        return [], False


def sync_venv(conn, venv_path: str, *,
              auto_register: bool = False, prune: bool = False,
              source: str = 'auto', verbose: bool = False,
              check_uv: bool = True) -> bool:
    """同步单个 venv 的包状态到数据库，返回是否成功。"""
    venv = get_venv_by_path(conn, venv_path)

    # 自动注册
    if venv is None:
        if not auto_register:
            print(f"未注册的 venv: {venv_path}，请先运行 uv-mgr index add {venv_path}")
            record_operation(conn, "sync", success=False, venv_path=venv_path,
                             error="venv 未注册")
            return False
        if not os.path.isdir(venv_path):
            print(f"错误: venv 目录不存在: {venv_path}", file=sys.stderr)
            record_operation(conn, "sync", success=False, venv_path=venv_path,
                             error="venv 目录不存在")
            return False
        add_venv(conn, venv_path, source=source)
        venv = get_venv_by_path(conn, venv_path)

    # 检查 venv 目录是否存在
    if not os.path.isdir(venv_path):
        print(f"警告: venv 目录不存在: {venv_path}", file=sys.stderr)
        if prune:
            remove_venv(conn, venv_path)
            print(f"已清理失效记录: {venv_path}")
        else:
            print("提示: 使用 --prune 自动清理失效记录", file=sys.stderr)
        record_operation(conn, "sync", success=False, venv_path=venv_path,
                         error="venv 目录不存在")
        return False

    # 找 python 解释器
    python_path = venv_python_path(venv_path)
    if python_path is None:
        print(f"警告: 未找到 Python 解释器: {venv_path}/bin/python（Windows 可使用 Scripts/python.exe）", file=sys.stderr)
        record_operation(conn, "sync", success=False, venv_path=venv_path,
                         error="未找到 Python 解释器")
        return False

    # 获取并记录 Python 版本
    python_version = None
    try:
        ver_res = subprocess.run(
            [str(python_path), "--version"],
            capture_output=True, text=True, timeout=10
        )
        if ver_res.returncode == 0:
            # Output is like "Python 3.11.15" or on stderr
            out = ver_res.stdout.strip() or ver_res.stderr.strip()
            if out.startswith("Python "):
                python_version = out.split()[1]
    except Exception:
        pass

    # 扫描已安装包
    scan_result = scan_venv_packages(str(python_path), check_uv=check_uv)
    packages, success = scan_result
    if not success:
        print(f"警告: 扫描失败，保留 {venv_path} 的现有索引记录。", file=sys.stderr)
        record_operation(conn, "sync", success=False, venv_path=venv_path,
                         error="扫描 venv 包列表失败")
        return False
    if not packages:
        if verbose:
            print(f"信息: {venv_path} 中没有已安装的包")
        # 仍然清空记录并更新同步时间和 python_version
        replace_venv_packages(conn, venv["id"], [])
        if python_version:
            conn.execute("UPDATE venvs SET python_version = ? WHERE id = ?", (python_version, venv["id"]))
            conn.commit()
        prune_historical_orphan_packages(conn)
        record_sync_history(conn, venv_path, python_version, packages)
        if verbose:
            print(f"已同步: {venv_path}（0 个包）")
        return True

    # 确保所有包在 packages 表中存在，获取 ID
    pkg_ids = [ensure_package(conn, name, ver) for name, ver in packages]

    # 全量替换关联
    replace_venv_packages(conn, venv["id"], pkg_ids)
    if python_version:
        conn.execute("UPDATE venvs SET python_version = ? WHERE id = ?", (python_version, venv["id"]))
        conn.commit()
    prune_historical_orphan_packages(conn)
    record_sync_history(conn, venv_path, python_version, packages)
    if verbose:
        print(f"已同步: {venv_path}（{len(packages)} 个包）")
    return True


def sync_all(conn, *, auto_discover: bool = True, prune: bool = False,
             verbose: bool = False) -> bool:
    """同步所有已注册 venv，返回是否所有环境均同步成功。"""
    venvs = list_venvs(conn)
    if not venvs and not auto_discover:
        print("没有已注册的 venv。")
        return True

    ok, message = check_uv_version()
    if not ok:
        print(f"错误: {message}", file=sys.stderr)
        return False

    # 自动发现：扫描常见位置
    discovered = set()
    if auto_discover:
        cwd = Path.cwd()
        # 当前目录的 .venv
        local_venv = cwd / ".venv"
        if local_venv.is_dir():
            discovered.add(str(local_venv.resolve()))
        # 父目录链中的 .venv
        for parent in cwd.parents:
            p = parent / ".venv"
            if p.is_dir():
                discovered.add(str(p.resolve()))
                break  # 只找最近的祖先

    known_paths = {v["path"] for v in venvs}

    # 自动注册未注册的 .venv
    for d in sorted(discovered):
        if d not in known_paths:
            print(f"发现新 venv: {d}")
            add_venv(conn, d, source='auto')
            venvs = list_venvs(conn)
            known_paths.add(d)

    # 自动发现 uv tool（无论 venvs 是否为空都执行）
    tool_paths = discover_tool_venvs()
    for tp in tool_paths:
        if tp not in known_paths:
            print(f"发现 uv tool: {Path(tp).name}")
            add_venv(conn, tp, source='tool')
            known_paths.add(tp)

    # 逐个同步（包括 tool venv）
    all_success = True
    for v in list_venvs(conn):
        if not sync_venv(conn, v["path"], auto_register=False, prune=prune,
                         verbose=verbose, check_uv=False):
            all_success = False
    return all_success


def run_uv_passthrough(args: list[str]) -> int:
    """透传执行 uv 命令。"""
    ok, message = check_uv_version()
    if not ok:
        print(f"错误: {message}", file=sys.stderr)
        return 1
    cmd = ["uv"] + args
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except FileNotFoundError:
        print("错误: 找不到 uv 命令，请确保已安装 (dnf install uv)", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
