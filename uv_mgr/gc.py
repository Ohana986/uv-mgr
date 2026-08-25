"""垃圾回收：找出孤立包并通过 uv cache clean 清理。"""

import subprocess
import sys
import tempfile
from pathlib import Path

from uv_mgr.db import (
    get_connection,
    get_orphan_packages,
    remove_orphan_packages,
    record_operation,
    get_rebuild_candidates,
    record_rebuild_failure,
    clear_rebuild_failure,
    record_rebuild_success,
    prune_rebuild_failures,
)
from uv_mgr.sync import check_uv_version, sync_all


def _get_packages_all_versions_orphaned(conn) -> list[dict]:
    """返回所有版本都孤立了的包名列表，附版本列表。"""
    orphans = get_orphan_packages(conn)
    by_name: dict[str, list[str]] = {}
    for row in orphans:
        by_name.setdefault(row["name"], []).append(row["version"])

    result = []
    for name, versions in by_name.items():
        cur = conn.execute(
            """SELECT COUNT(*) FROM packages p
               JOIN venv_packages vp ON vp.package_id = p.id
               WHERE p.name = ?""",
            (name,),
        )
        in_use_count = cur.fetchone()[0]
        if in_use_count == 0:
            result.append({"name": name, "versions": versions})
    return result


def _run_rebuild(conn, name: str, versions: list[str]) -> bool:
    """清空一个包名的缓存，并用临时项目恢复仍在使用的版本。"""
    try:
        cleaned = subprocess.run(
            ["uv", "cache", "clean", name], capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        error = "清理缓存超时" if isinstance(exc, subprocess.TimeoutExpired) else "找不到 uv 命令"
        for version in versions:
            record_rebuild_failure(conn, name, version, error)
        print(f"重建失败: {error}")
        return False
    if cleaned.returncode != 0:
        error = cleaned.stderr.strip() or "uv cache clean 执行失败"
        for version in versions:
            record_rebuild_failure(conn, name, version, error)
        print(f"重建失败: {error}")
        return False

    all_success = True
    with tempfile.TemporaryDirectory(prefix="uv-mgr-rebuild-", dir="/tmp") as temp_dir:
        for version in versions:
            project_dir = Path(temp_dir) / f"project-{version.replace('/', '_')}"
            requirement = f"{name}=={version}"
            try:
                result = subprocess.run(
                    ["uv", "init", "--bare", "--vcs", "none", "--no-readme", str(project_dir)],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    result = subprocess.run(
                        ["uv", "add", requirement], cwd=project_dir,
                        capture_output=True, text=True, timeout=300,
                    )
                if result.returncode == 0:
                    clear_rebuild_failure(conn, name, version)
                    record_rebuild_success(conn, name, version)
                    print(f"  已重建: {requirement}")
                    continue
                error = result.stderr.strip() or "uv add 执行失败"
            except subprocess.TimeoutExpired:
                error = "重建超时"
            except FileNotFoundError:
                error = "找不到 uv 命令"
            record_rebuild_failure(conn, name, version, error)
            print(f"  重建失败: {requirement}: {error}")
            all_success = False
    return all_success


def gc(dry_run: bool = False, *, auto_sync: bool = True,
       verbose: bool = False, rebuild: bool = False) -> int:
    """GC 入口：找出并清理孤立缓存包。

    返回清理的包数量。
    """
    conn = get_connection()
    try:
        if auto_sync:
            if verbose:
                print("正在同步 venv 状态...")
            if not sync_all(conn, auto_discover=True, verbose=verbose):
                print("错误: venv 同步失败，已中止 GC；未执行缓存清理。", file=sys.stderr)
                record_operation(conn, "gc", success=False,
                                 error="venv 同步失败，未执行缓存清理")
                return 1
        else:
            ok, message = check_uv_version()
            if not ok:
                print(f"错误: {message}", file=sys.stderr)
                record_operation(conn, "gc", success=False, error=message)
                return 1

        prune_rebuild_failures(conn)
        # 找出完全孤立的包
        orphans = _get_packages_all_versions_orphaned(conn)
        rebuild_rows = get_rebuild_candidates(conn) if rebuild else []
        rebuild_packages: dict[str, list[str]] = {}
        for row in rebuild_rows:
            rebuild_packages.setdefault(row["name"], []).append(row["version"])

        if not orphans and not rebuild_packages:
            print("没有需要清理的孤立包。")
            record_operation(conn, "gc_dry_run" if dry_run else "gc",
                             summary="没有需要清理的完全孤立包")
            return 0

        total_versions = sum(len(o["versions"]) for o in orphans)

        print(f"发现 {len(orphans)} 个孤立包（共 {total_versions} 个版本）：")
        for o in orphans:
            versions = ", ".join(o["versions"])
            print(f"  {o['name']} [{versions}]")
        if rebuild_packages:
            print(f"发现 {len(rebuild_packages)} 个有旧版本记录的在用包，将清理并重建：")
            for name, versions in rebuild_packages.items():
                print(f"  {name} [{', '.join(versions)}]")

        if dry_run:
            print(f"\n[dry-run] 将清理 {len(orphans)} 个完全孤立包，共 {total_versions} 个版本")
            if rebuild_packages:
                print(f"[dry-run] 将重建 {len(rebuild_packages)} 个包的 {len(rebuild_rows)} 个在用版本")
            print("[dry-run] 未执行实际清理。运行 uv-mgr gc（不带 --dry-run）以执行。")
            record_operation(
                conn, "gc_dry_run",
                summary=f"预览 {len(orphans)} 个包、{total_versions} 个版本",
            )
            return 0

        # 实际清理
        cleaned_names: list[str] = []
        failed_count = 0
        for o in orphans:
            pkg_name = o["name"]
            print(f"正在清理: {pkg_name}...", end=" ", flush=True)
            try:
                result = subprocess.run(
                    ["uv", "cache", "clean", pkg_name],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    print("完成")
                    cleaned_names.append(pkg_name)
                else:
                    failed_count += 1
                    print(f"失败: {result.stderr.strip()}")
            except subprocess.TimeoutExpired:
                failed_count += 1
                print("超时")
            except FileNotFoundError:
                failed_count += 1
                print("错误: 找不到 uv 命令")

        rebuild_failed = 0
        for name, versions in rebuild_packages.items():
            print(f"正在清理并重建: {name}...", flush=True)
            if not _run_rebuild(conn, name, versions):
                rebuild_failed += 1

        # 从 DB 删除成功清理的包记录
        if cleaned_names:
            placeholders = ",".join("?" for _ in cleaned_names)
            orphan_ids = [
                row[0] for row in conn.execute(
                    f"""SELECT p.id FROM packages p
                        WHERE p.name IN ({placeholders})
                          AND p.id NOT IN (
                              SELECT DISTINCT vp.package_id FROM venv_packages vp
                          )""",
                    cleaned_names,
                ).fetchall()
            ]
            if orphan_ids:
                remove_orphan_packages(conn, orphan_ids)

        print(f"\n已清理 {len(cleaned_names)}/{len(orphans)} 个孤立包。")
        success = failed_count == 0 and rebuild_failed == 0
        record_operation(
            conn, "gc", success=success,
            summary=(f"已清理 {len(cleaned_names)}/{len(orphans)} 个完全孤立包；"
                     f"重建 {len(rebuild_packages) - rebuild_failed}/{len(rebuild_packages)} 个在用包"),
            error=None if success else f"{failed_count} 个孤立包清理失败，{rebuild_failed} 个包重建失败",
        )
        return 0 if success else 1
    finally:
        conn.close()
