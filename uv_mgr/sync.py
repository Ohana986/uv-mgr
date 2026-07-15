"""Venv 状态同步：扫描 venv 已安装包，更新数据库索引。"""

import json
import os
import subprocess
import sys
from pathlib import Path

from uv_mgr.db import (
    get_connection,
    init_db,
    add_venv,
    remove_venv,
    get_venv_by_path,
    ensure_package,
    replace_venv_packages,
    list_venvs,
    get_venvs_by_source,
)

# 跳过 sync 的 uv 子命令
SKIP_SYNC_COMMANDS = frozenset({
    "self", "help", "version", "cache", "completions",
    "generate-shell-completion", "generate-default-namespace",
})


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


def scan_venv_packages(venv_python: str) -> list[tuple[str, str]]:
    """扫描指定 venv 中的已安装包，返回 [(name, version), ...]。"""
    try:
        result = subprocess.run(
            ["uv", "pip", "list", "--python", venv_python,
             "--format=json", "--quiet"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"警告: 扫描 {venv_python} 失败:\n{result.stderr}",
                  file=sys.stderr)
            return []
        data = json.loads(result.stdout)
        # uv 返回格式: [{"name": "pip", "version": "26.1.2"}, ...]
        return [(entry["name"], entry["version"]) for entry in data]
    except (json.JSONDecodeError, subprocess.TimeoutExpired,
            FileNotFoundError) as e:
        print(f"警告: 扫描 venv 异常: {e}", file=sys.stderr)
        return []


def sync_venv(conn, venv_path: str, *,
              auto_register: bool = False, prune: bool = False,
              source: str = 'auto') -> None:
    """同步单个 venv 的包状态到数据库。"""
    venv = get_venv_by_path(conn, venv_path)

    # 自动注册
    if venv is None:
        if not auto_register:
            print(f"未注册的 venv: {venv_path}，请先运行 uv-mgr add {venv_path}")
            return
        if not os.path.isdir(venv_path):
            print(f"错误: venv 目录不存在: {venv_path}", file=sys.stderr)
            return
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
        return

    # 找 python 解释器
    python_path = Path(venv_path) / "bin" / "python"
    if not python_path.exists():
        print(f"警告: 未找到 Python 解释器: {python_path}", file=sys.stderr)
        return

    # 扫描已安装包
    packages = scan_venv_packages(str(python_path))
    if not packages:
        print(f"信息: {venv_path} 中没有已安装的包")
        # 仍然清空记录并更新同步时间
        replace_venv_packages(conn, venv["id"], [])
        print(f"已同步: {venv_path}（0 个包）")
        return

    # 确保所有包在 packages 表中存在，获取 ID
    pkg_ids = [ensure_package(conn, name, ver) for name, ver in packages]

    # 全量替换关联
    replace_venv_packages(conn, venv["id"], pkg_ids)
    print(f"已同步: {venv_path}（{len(packages)} 个包）")


def sync_all(conn, *, auto_discover: bool = True, prune: bool = False) -> None:
    """同步所有已注册 venv，并可选自动发现新 venv 及 uv tool。"""
    venvs = list_venvs(conn)
    if not venvs and not auto_discover:
        print("没有已注册的 venv。")
        return

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
    for v in list_venvs(conn):
        sync_venv(conn, v["path"], auto_register=False, prune=prune)


def run_uv_passthrough(args: list[str]) -> int:
    """透传执行 uv 命令。"""
    cmd = ["uv"] + args
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except FileNotFoundError:
        print("错误: 找不到 uv 命令，请确保已安装 (dnf install uv)", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
