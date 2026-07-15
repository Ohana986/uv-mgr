"""CLI 入口：参数解析、命令路由、uv 透传。"""

import argparse
import os
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
    get_orphan_packages,
    get_stats,
)
from uv_mgr.sync import (
    should_sync_after_uv,
    run_uv_passthrough,
    sync_all,
    sync_venv,
)
from uv_mgr.gc import gc

# uv-mgr 自有命令（不透传 uv）
OWN_COMMANDS = frozenset({
    "index", "db",
})


_HELP_EPILOG = """\
Learn uv commands:  use 'uv-mgr <command>' to run any uv command (e.g. uv-mgr sync)
Index management:   use 'uv-mgr index <command>' (e.g. uv-mgr index gc --dry-run)
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
    # 子命令
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── index 子命令（索引管理） ─────────────────────────────────
    p_idx = sub.add_parser("index", help="索引管理：注册/清理/同步/GC")
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
    p_il.add_argument("--packages", action="store_true", help="列出所有已索引包")
    p_il.add_argument("--orphans", action="store_true", help="列出孤立包")

    # index sync
    p_is = idx_sub.add_parser("sync", help="同步 venv 包状态到索引")
    p_is.add_argument("venv_path", nargs="?", default=None, help="指定 venv 路径（默认全部）")
    p_is.add_argument("--prune", action="store_true",
                      help="同步时自动清理已不存在的 venv 记录")

    # index gc
    p_ig = idx_sub.add_parser("gc", help="清理孤立缓存包")
    p_ig.add_argument("--dry-run", action="store_true", help="预览模式，不实际清理")

    # db
    p_db = sub.add_parser("db", help="数据库管理")
    p_db.add_argument("db_action", nargs="?", default="info",
                      choices=["info"], help="操作（默认 info）")

    return parser


def _cmd_add(args) -> int:
    path = os.path.abspath(args.venv_path)
    if not os.path.isdir(path):
        print(f"错误: 目录不存在: {path}", file=sys.stderr)
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
        conn.close()
        return 1
    conn.close()
    return 0


def _cmd_list(args) -> int:
    conn = get_connection()
    show_all = not (args.venvs or args.packages or args.orphans)

    if show_all or args.venvs:
        venvs = list_venvs(conn)
        print(f"已注册 venv（{len(venvs)} 个）：")
        for v in venvs:
            last = v["last_synced_at"] or "（未同步）"
            print(f"  {v['path']}  [{last}]")

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
        print(f"\n孤立包（{len(orphans)} 个记录）：")
        for o in orphans:
            print(f"  {o['name']}=={o['version']}")

    conn.close()
    return 0


def _cmd_sync(args) -> int:
    conn = get_connection()
    if args.venv_path:
        sync_venv(conn, os.path.abspath(args.venv_path),
                  auto_register=True, prune=args.prune)
    else:
        sync_all(conn, auto_discover=True, prune=args.prune)
    conn.close()
    return 0


def _cmd_gc(args) -> int:
    return gc(dry_run=args.dry_run)


def _cmd_db(args) -> int:
    conn = get_connection()
    stats = get_stats(conn)
    print(f"数据库路径: {conn.execute('PRAGMA database_list').fetchone()[2]}")
    row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
    print(f"Schema 版本: {row[0]}")
    print(f"已注册 venv: {stats['venvs']}")
    print(f"已索引包: {stats['packages']}")
    print(f"venv-包关联: {stats['venv_package_links']}")
    print(f"孤立包: {stats['orphans']}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    # 初始化数据库
    conn = get_connection()
    init_db(conn)
    conn.close()

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        _build_parser().print_help()
        return 0

    # 检查是否为 uv-mgr 自有命令
    if argv[0] in OWN_COMMANDS:
        parser = _build_parser()
        parsed = parser.parse_args(argv)
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
                        return _cmd_gc(parsed)
                    case _:
                        print("用法: uv-mgr index <subcommand> ...\n")
                        print("可用的子命令:")
                        print("  add       注册一个 venv")
                        print("  remove    取消注册一个 venv")
                        print("  list      查询索引状态")
                        print("  sync      同步 venv 包状态到索引")
                        print("  gc        清理孤立缓存包")
                        return 0
            case "db":
                return _cmd_db(parsed)
            case _:
                parser.print_help()
                return 0
    else:
        # 透传给 uv
        code = run_uv_passthrough(argv)
        if code == 0 and should_sync_after_uv(argv):
            print("\n正在同步 venv 状态...")
            conn = get_connection()
            sync_all(conn, auto_discover=True)
            conn.close()
        return code
