"""CLI 入口：参数解析、命令路由、uv 透传。"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from uv_mgr import __version__
from uv_mgr.db import (
    get_connection,
    init_db,
    add_venv,
    remove_venv,
    list_venvs,
    get_venv_packages,
    get_venvs_by_source,
    get_orphan_packages,
    get_stats,
    get_operations,
    get_package_events,
    get_snapshots,
    get_snapshot_packages,
    get_venv_by_path,
    record_operation,
    record_tool_rebuild_metadata,
)
from uv_mgr.sync import (
    should_sync_after_uv,
    run_uv_passthrough,
    sync_all,
    sync_venv,
    venv_python_path,
    discover_tool_venvs,
)
from uv_mgr.gc import _parse_tool_receipt, gc as clean_cache

# uv-mgr 自有命令（不透传 uv）
OWN_COMMANDS = frozenset({
    "index", "db", "gc",
})

_TOOL_REPLAY_OPTIONS = {
    "--with": ("--with", True), "-w": ("--with", True),
    "--editable": ("--editable", False), "-e": ("--editable", False),
    "--with-editable": ("--with-editable", True),
    "--with-executables-from": ("--with-executables-from", True),
    "--python-platform": ("--python-platform", True),
    "--link-mode": ("--link-mode", True),
    "--compile-bytecode": ("--compile-bytecode", False),
    "--resolution": ("--resolution", True),
    "--prerelease": ("--prerelease", True),
    "--exclude-newer": ("--exclude-newer", True),
    "--torch-backend": ("--torch-backend", True),
}


def _python_major_minor(value: str | None) -> str | None:
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", value or "")
    return f"{match.group(1)}.{match.group(2)}" if match else None


def _extract_tool_replay_command(args: list[str]) -> list[str] | None:
    """从常规 tool install 中提取可安全持久化的白名单参数。"""
    if len(args) < 3 or args[:2] != ["tool", "install"] or args[2].startswith("-"):
        return None
    replay = ["uv", "tool", "install", args[2]]
    index = 3
    while index < len(args):
        option = args[index]
        if option == "--python" or option.startswith("--python=") or option == "-p":
            if option == "--python" or option == "-p":
                index += 1
                if index >= len(args) or args[index].startswith("-"):
                    return None
            index += 1
            continue
        if option == "--force":
            index += 1
            continue
        name, separator, inline_value = option.partition("=")
        details = _TOOL_REPLAY_OPTIONS.get(name)
        if details is None:
            return None
        canonical, needs_value = details
        replay.append(canonical)
        if needs_value:
            if separator:
                if not inline_value:
                    return None
                replay.append(inline_value)
            else:
                index += 1
                if index >= len(args) or args[index].startswith("-"):
                    return None
                replay.append(args[index])
        elif separator:
            return None
        index += 1
    return replay


def _tool_primary_requirement(venv_path: str) -> str | None:
    command = _parse_tool_receipt(Path(venv_path) / "uv-receipt.toml")
    if command is None or len(command) < 5:
        return None
    return command[4]


def _record_tool_install(conn, replay_command: list[str], before_paths: set[str]) -> None:
    """在同步后将常规 tool install 的重放数据绑定到唯一 tool 环境。"""
    primary = replay_command[3]
    after_paths = set(discover_tool_venvs())
    candidates = [path for path in after_paths if _tool_primary_requirement(path) == primary]
    new_candidates = [path for path in candidates if path not in before_paths]
    if len(new_candidates) == 1:
        candidates = new_candidates
    if len(candidates) != 1:
        return
    venv = get_venv_by_path(conn, candidates[0])
    if venv is None:
        return
    python = _python_major_minor(venv["python_version"])
    if python is None:
        return
    record_tool_rebuild_metadata(conn, candidates[0], json.dumps(replay_command), python)


_HELP_EPILOG = """\
Learn uv commands:  use 'uv-mgr <command>' to run any uv command (e.g. uv-mgr sync)
Index management:   use 'uv-mgr index <command>' (e.g. uv-mgr index gc)
Cache cleanup:      use 'uv-mgr gc' (e.g. uv-mgr gc --dry-run)
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv-mgr",
        description="uv 包装器：全局索引管理 + 孤立包垃圾回收",
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"uv-mgr {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="显示每个 venv 的同步详情（默认隐藏）",
    )
    # 子命令
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── index 子命令（索引管理） ─────────────────────────────────
    p_idx = sub.add_parser("index", help="索引管理：注册/查询/同步/清理失效记录")
    idx_sub = p_idx.add_subparsers(dest="index_command", metavar="<subcommand>")

    # index add
    p_ia = idx_sub.add_parser("add", help="注册一个 venv")
    p_ia.add_argument("venv_path", help="venv 路径")

    # index remove
    p_ir = idx_sub.add_parser("remove", help="取消注册一个 venv")
    p_ir.add_argument("venv_path", help="venv 路径")

    # index list
    p_il = idx_sub.add_parser("list", help="查询索引状态")
    p_il.add_argument("--venvs", action="store_true", help="列出已注册 venv")
    p_il.add_argument("--type", choices=['user', 'auto', 'tool'], default=None,
                      help="按来源过滤 venv（user/auto/tool）")
    p_il.add_argument("--packages", action="store_true", help="列出所有已索引包")
    p_il.add_argument("--orphans", action="store_true",
                      help="列出未被 venv 引用的数据库包记录")

    # index sync
    p_is = idx_sub.add_parser("sync", help="同步 venv 包状态到索引")
    p_is.add_argument("venv_path", nargs="?", default=None, help="指定 venv 路径（默认全部）")
    p_is.add_argument("-v", "--verbose", action="store_true",
                      help="显示每个 venv 的同步详情（默认隐藏）")

    # index gc
    p_ig = idx_sub.add_parser("gc", help="同步索引并清理已不存在的 venv 记录")
    p_ig.add_argument("venv_path", nargs="?", default=None,
                      help="指定 venv 路径（默认全部）")
    p_ig.add_argument("-v", "--verbose", action="store_true",
                      help="显示每个 venv 的同步详情（默认隐藏）")

    # gc（缓存垃圾回收）
    p_gc = sub.add_parser("gc", help="清理完全孤立包名对应的缓存")
    p_gc.add_argument("--dry-run", action="store_true", help="预览模式，不实际清理")
    p_gc.add_argument("--rebuild", action="store_true",
                      help="清理上次重建后新增的未使用旧版本缓存，并在原环境中恢复")
    p_gc.add_argument("--retry", action="store_true",
                      help="重试上次重建失败的环境（必须与 --rebuild 一起使用）")

    # db
    p_db = sub.add_parser("db", help="数据库管理")
    p_db.add_argument("db_action", nargs="?", default="info",
                      choices=["info", "history"], help="操作（默认 info）")
    p_db.add_argument("--venv", dest="history_venv", help="按 venv 路径筛选历史")
    p_db.add_argument("--package", dest="history_package", help="按包名筛选历史")
    p_db.add_argument("--limit", type=int, default=50, help="最多显示记录数（默认 50）")
    history_view = p_db.add_mutually_exclusive_group()
    history_view.add_argument("--events", action="store_true", help="显示包变更事件")
    history_view.add_argument("--snapshots", action="store_true", help="显示同步快照及包清单")

    return parser


def _cmd_add(args) -> int:
    path = os.path.abspath(args.venv_path)
    if not os.path.isdir(path):
        print(f"错误: 目录不存在: {path}", file=sys.stderr)
        conn = get_connection()
        try:
            record_operation(conn, "venv_added", success=False, venv_path=path,
                             error="venv 目录不存在")
        finally:
            conn.close()
        return 1
    if venv_python_path(path) is None:
        print(f"错误: 不是有效的 venv（未找到 Python 解释器: {path}/bin/python 或 {path}/Scripts/python.exe）", file=sys.stderr)
        conn = get_connection()
        try:
            record_operation(conn, "venv_added", success=False, venv_path=path,
                             error="未找到 Python 解释器")
        finally:
            conn.close()
        return 1
    conn = get_connection()
    add_venv(conn, path)
    print(f"已注册 venv: {path}")
    conn.close()
    return 0


def _cmd_remove(args) -> int:
    path = os.path.abspath(args.venv_path)
    conn = get_connection()
    if remove_venv(conn, path):
        print(f"已移除 venv: {path}")
        print("提示: 可运行 uv-mgr gc 清理对应的孤立缓存包")
    else:
        print(f"未找到已注册的 venv: {path}")
        record_operation(conn, "venv_removed", success=False, venv_path=path,
                         error="venv 未注册")
        conn.close()
        return 1
    conn.close()
    return 0


def _cmd_list(args) -> int:
    conn = get_connection()
    show_venvs = args.venvs or (hasattr(args, 'type') and bool(args.type))
    show_all = not (show_venvs or args.packages or args.orphans)

    if show_all or show_venvs:
        if hasattr(args, 'type') and args.type:
            venvs = get_venvs_by_source(conn, args.type)
        else:
            venvs = list_venvs(conn)
        print(f"已注册 venv（{len(venvs)} 个）：")
        for v in venvs:
            last = v["last_synced_at"] or "（未同步）"
            source = v["source"] if v["source"] else "user"
            tag = f"[{source}] " if source != "user" else ""
            py_ver = f" (Python {v['python_version']})" if v["python_version"] else ""
            print(f"  {tag}{v['path']}{py_ver}  [{last}]")

    if show_all or args.packages:
        packages = conn.execute(
            "SELECT p.name, p.version, COUNT(vp.venv_id) as refs "
            "FROM packages p "
            "LEFT JOIN venv_packages vp ON vp.package_id = p.id "
            "GROUP BY p.id ORDER BY p.name"
        ).fetchall()
        print(f"\n已索引包（{len(packages)} 个记录）：")
        for p in packages:
            print(f"  {p['name']}=={p['version']}  (引用: {p['refs']})")

    if show_all or args.orphans:
        orphans = get_orphan_packages(conn)
        print(f"\n未引用包记录（{len(orphans)} 个）：")
        for o in orphans:
            print(f"  {o['name']}=={o['version']}")

    conn.close()
    return 0


def _cmd_sync(args) -> int:
    conn = get_connection()
    try:
        if args.venv_path:
            success = sync_venv(
                conn, os.path.abspath(args.venv_path),
                auto_register=True, prune=getattr(args, "prune", False),
                verbose=args.verbose,
            )
        else:
            success = sync_all(conn, auto_discover=True,
                               prune=getattr(args, "prune", False),
                               verbose=args.verbose)
        return 0 if success else 1
    finally:
        conn.close()


def _cmd_index_gc(args) -> int:
    """同步全部索引，并移除已不存在的 venv 记录。"""
    conn = get_connection()
    try:
        if args.venv_path:
            success = sync_venv(
                conn, os.path.abspath(args.venv_path), auto_register=True,
                prune=True, verbose=args.verbose,
            )
        else:
            success = sync_all(conn, auto_discover=True, prune=True,
                               verbose=args.verbose)
        return 0 if success else 1
    finally:
        conn.close()


def _cmd_gc(args) -> int:
    if args.retry and not args.rebuild:
        print("错误: --retry 必须与 --rebuild 一起使用", file=sys.stderr)
        return 2
    if args.retry and args.dry_run:
        print("错误: --retry 不能与 --dry-run 一起使用", file=sys.stderr)
        return 2
    return clean_cache(dry_run=args.dry_run, rebuild=args.rebuild, retry=args.retry)


def _cmd_db(args) -> int:
    conn = get_connection()
    try:
        if args.db_action == "history":
            return _cmd_db_history(conn, args)
        stats = get_stats(conn)
        print(f"数据库路径: {conn.execute('PRAGMA database_list').fetchone()[2]}")
        row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        print(f"Schema 版本: {row[0]}")
        print(f"已注册 venv: {stats['venvs']}")
        print(f"已索引包: {stats['packages']}")
        print(f"venv-包关联: {stats['venv_package_links']}")
        print(f"未引用包记录: {stats['orphans']}")
        print(f"历史操作: {conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]}")
        print(f"同步快照: {conn.execute('SELECT COUNT(*) FROM sync_snapshots').fetchone()[0]}")
        print(f"包变更事件: {conn.execute('SELECT COUNT(*) FROM package_events').fetchone()[0]}")
        return 0
    finally:
        conn.close()


def _cmd_db_history(conn, args) -> int:
    limit = args.limit
    if limit < 1:
        print("错误: --limit 必须大于 0", file=sys.stderr)
        return 1
    venv_path = os.path.abspath(args.history_venv) if args.history_venv else None
    package_name = args.history_package
    if args.snapshots:
        snapshots = get_snapshots(
            conn, venv_path=venv_path, package_name=package_name, limit=limit,
        )
        print(f"同步快照（{len(snapshots)} 条）：")
        for snapshot in snapshots:
            py_ver = f"，Python {snapshot['python_version']}" if snapshot["python_version"] else ""
            print(f"  {snapshot['occurred_at']}  {snapshot['venv_path']}{py_ver}")
            for package in get_snapshot_packages(conn, snapshot["id"]):
                print(f"    {package['name']}=={package['version']}")
        return 0
    if args.events or package_name:
        events = get_package_events(
            conn, venv_path=venv_path, package_name=package_name, limit=limit,
        )
        print(f"包变更事件（{len(events)} 条）：")
        labels = {"installed": "安装", "removed": "移除", "upgraded": "升级",
                  "downgraded": "降级", "changed": "版本变更"}
        for event in events:
            change = f"{event['old_version'] or '—'} → {event['new_version'] or '—'}"
            print(f"  {event['occurred_at']}  {labels[event['event_type']]}  "
                  f"{event['name']}  {change}  [{event['venv_path']}]")
        return 0

    operations = get_operations(conn, venv_path=venv_path, limit=limit)
    print(f"历史操作（{len(operations)} 条）：")
    for operation in operations:
        result = "成功" if operation["success"] else "失败"
        path = f"  [{operation['venv_path']}]" if operation["venv_path"] else ""
        detail = operation["summary"] or operation["error"] or ""
        print(f"  {operation['occurred_at']}  {operation['operation_type']}  "
              f"{result}{path}  {detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        _build_parser().print_help()
        return 0

    # 全局 --verbose（用于 uv 透传场景，如 uv-mgr -v pip install）
    verbose = False
    if argv[0] in ("--verbose", "-v"):
        verbose = True
        argv = argv[1:]

    if not argv:
        _build_parser().print_help()
        return 0

    # 自身版本请求：显示 uv-mgr 版本，不透传 uv、不触发 sync
    if argv[0] in ("--version", "-V"):
        print(f"uv-mgr {__version__}")
        return 0

    # 初始化数据库（版本查询不依赖数据库可用性）
    conn = get_connection()
    init_db(conn)
    conn.close()

    # 检查是否为 uv-mgr 自有命令
    if argv[0] in OWN_COMMANDS:
        parser = _build_parser()
        parsed = parser.parse_args(argv)
        # 全局 -v 优先于子命令默认值
        if verbose:
            parsed.verbose = True
        match parsed.command:
            case "index":
                match parsed.index_command:
                    case "add":
                        return _cmd_add(parsed)
                    case "remove":
                        return _cmd_remove(parsed)
                    case "list":
                        return _cmd_list(parsed)
                    case "sync":
                        return _cmd_sync(parsed)
                    case "gc":
                        return _cmd_index_gc(parsed)
                    case _:
                        print("用法: uv-mgr index <subcommand> ...\n")
                        print("可用的子命令:")
                        print("  add       注册一个 venv")
                        print("  remove    取消注册一个 venv")
                        print("  list      查询索引状态")
                        print("  sync      同步 venv 包状态到索引")
                        print("  gc        同步索引并清理已不存在的 venv 记录")
                        return 0
            case "db":
                return _cmd_db(parsed)
            case "gc":
                return _cmd_gc(parsed)
            case _:
                parser.print_help()
                return 0
    else:
        # 透传给 uv
        tool_replay = _extract_tool_replay_command(argv)
        tool_paths_before = set(discover_tool_venvs()) if tool_replay is not None else set()
        code = run_uv_passthrough(argv)
        if code == 0 and should_sync_after_uv(argv):
            if verbose:
                print("\n正在同步 venv 状态...")
            conn = get_connection()
            sync_ok = sync_all(conn, auto_discover=True, verbose=verbose)
            if sync_ok and tool_replay is not None:
                _record_tool_install(conn, tool_replay, tool_paths_before)
            conn.close()
            if not sync_ok:
                return 1
        return code
