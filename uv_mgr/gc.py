"""清理孤立缓存，并在原环境中恢复被清理的在用缓存。"""

import json
import re
import subprocess
import sys
from pathlib import Path

from uv_mgr.config import resolve_uv_command, subprocess_text_kwargs

from uv_mgr.db import (
    clear_rebuild_failure,
    get_connection,
    get_orphan_packages,
    get_rebuild_failures,
    get_tool_rebuild_metadata,
    get_new_rebuild_packages,
    list_venvs,
    prune_rebuild_failures,
    record_operation,
    record_rebuild_failure,
    replace_rebuild_baseline,
    remove_orphan_packages,
)
from uv_mgr.sync import check_uv_version, sync_all, sync_venv


def _get_packages_all_versions_orphaned(conn) -> list[dict]:
    """返回所有版本都孤立了的包名列表，附版本列表。"""
    by_name: dict[str, list[str]] = {}
    for row in get_orphan_packages(conn):
        by_name.setdefault(row["name"], []).append(row["version"])
    result = []
    for name, versions in by_name.items():
        used = conn.execute(
            """SELECT COUNT(*) FROM packages p
               JOIN venv_packages vp ON vp.package_id = p.id WHERE p.name = ?""",
            (name,),
        ).fetchone()[0]
        if used == 0:
            result.append({"name": name, "versions": versions})
    return result


def _parse_tool_receipt(receipt: Path) -> list[str] | None:
    """读取可安全重放的 uv receipt；无法可靠识别时返回 None。

    uv-mgr 支持 Python 3.10，故不依赖 Python 3.11 才提供的 tomllib。仅接受
    uv receipt 中稳定的 requirements/name/specifier/directory/python/link-mode 字段。
    """
    try:
        text = receipt.read_text(encoding="utf-8")
    except OSError:
        return None
    requirements_block = re.search(
        r"^requirements\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL,
    )
    if requirements_block is None:
        return None
    requirements = re.findall(r"\{\s*([^{}]+)\s*\}", requirements_block.group(1))
    requirement_values: list[str] = []
    for item in requirements:
        fields = dict(re.findall(r'([\w-]+)\s*=\s*"([^"]*)"', item))
        name = fields.get("name")
        if not name or set(fields) - {"name", "specifier", "directory"}:
            return None
        if "directory" in fields:
            if len(requirements) != 1:
                return None
            requirement_values.append(fields["directory"])
        else:
            requirement_values.append(name + fields.get("specifier", ""))
    if not requirement_values:
        return None
    command = ["uv", "tool", "install", "--force", requirement_values[0]]
    for requirement in requirement_values[1:]:
        command.extend(["--with", requirement])
    python = re.search(r'^python\s*=\s*"([^"]+)"', text, re.MULTILINE)
    link_mode = re.search(r'^link-mode\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if python:
        command.extend(["--python", python.group(1)])
    if link_mode:
        command.extend(["--link-mode", link_mode.group(1)])
    return command


def _python_major_minor(value: str | None) -> str | None:
    """将解释器版本规范为可传给 uv 的 ``major.minor``。"""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", value)
    if match is None:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def _command_with_python(command: list[str], python_version: str | None) -> list[str] | None:
    """为安全重放命令加入固定 Python；原命令中的 Python 由此覆盖。"""
    python = _python_major_minor(python_version)
    if python is None or command[:3] != ["uv", "tool", "install"]:
        return None
    result = command[:3] + ["--force"]
    index = 3
    while index < len(command):
        argument = command[index]
        if argument == "--force":
            index += 1
        elif argument == "--python":
            if index + 1 >= len(command) or command[index + 1].startswith("-"):
                return None
            index += 2
        elif argument.startswith("--python="):
            if not argument.removeprefix("--python="):
                return None
            index += 1
        else:
            result.append(argument)
            index += 1
    result.extend(["--python", python])
    return result


def _tool_rebuild_command(conn, venv) -> tuple[list[str] | None, str | None]:
    """优先使用 uv-mgr 元数据，否则从 receipt 和索引安全回退。"""
    metadata = get_tool_rebuild_metadata(conn, venv["path"])
    if metadata is not None:
        try:
            saved = json.loads(metadata["arguments_json"])
            if not isinstance(saved, list) or not all(isinstance(item, str) for item in saved):
                raise ValueError("参数格式无效")
        except (json.JSONDecodeError, ValueError):
            return None, "保存的 tool 重建参数损坏"
        command = _command_with_python(saved, metadata["python_version"])
        if command is not None:
            return command, None
        return None, "保存的 tool Python 版本无效"

    command = _parse_tool_receipt(Path(venv["path"]) / "uv-receipt.toml")
    if command is None:
        return None, "uv-receipt.toml 缺失或无法安全重放"
    receipt_python = None
    if "--python" in command:
        receipt_python = command[command.index("--python") + 1]
    command = _command_with_python(command, receipt_python or venv["python_version"])
    if command is None:
        return None, "无法确定原 tool 的 Python 主次版本"
    return command, None


def _environment_package_names(conn, venv_id: int) -> set[str]:
    return {
        row["name"] for row in conn.execute(
            """SELECT p.name FROM packages p JOIN venv_packages vp
               ON vp.package_id = p.id WHERE vp.venv_id = ?""", (venv_id,)
        )
    }


def _get_rebuild_environments(conn, package_names: set[str]) -> tuple[list[dict], list[dict], set[str]]:
    """返回可恢复环境、跳过环境和必须保留缓存的候选包名。"""
    targets: list[dict] = []
    skipped: list[dict] = []
    protected_names: set[str] = set()
    for venv in list_venvs(conn):
        affected = _environment_package_names(conn, venv["id"]) & package_names
        if not affected:
            continue
        venv_path = Path(venv["path"])
        if not venv_path.is_dir():
            reason = "venv 目录不存在"
        elif venv["source"] == "tool":
            command, reason = _tool_rebuild_command(conn, venv)
            tool_cwd = venv_path.parent
            if command is not None and tool_cwd.is_dir():
                targets.append({
                    "path": str(venv_path), "type": "tool", "command": command,
                    "cwd": str(tool_cwd), "packages": affected,
                })
                continue
            if command is not None:
                reason = "tool 父目录不存在"
        else:
            project = venv_path.parent
            if venv_path.name == ".venv" and (project / "pyproject.toml").is_file():
                targets.append({
                    "path": str(project), "type": "project", "command": ["uv", "sync"],
                    "cwd": str(project), "packages": affected,
                })
                continue
            reason = "未找到对应项目的 pyproject.toml"
        skipped.append({"path": str(venv_path), "reason": reason, "packages": affected})
        protected_names.update(affected)
    return targets, skipped, protected_names


def _run_environment(conn, environment: dict, *, stream_output: bool = False) -> bool:
    """在原项目或原 tool 来源中恢复缓存，并维护失败记录。

    ``stream_output`` 仅供 ``gc --rebuild`` 使用，使 uv 的下载进度直接显示在终端。
    """
    command = environment["command"]
    try:
        if stream_output:
            result = subprocess.run(resolve_uv_command(command), cwd=environment["cwd"], timeout=900)
        else:
            result = subprocess.run(
                resolve_uv_command(command), cwd=environment["cwd"], capture_output=True, text=True,
                timeout=900, **subprocess_text_kwargs(),
            )
        if result.returncode == 0 and environment["type"] == "project":
            venv_path = str(Path(environment["path"]) / ".venv")
            if not sync_venv(conn, venv_path, auto_register=False, check_uv=False):
                error = "项目环境已恢复，但索引同步失败"
                record_rebuild_failure(
                    conn, environment["path"], environment["type"], json.dumps(command), error,
                )
                print(f"  恢复失败: {environment['path']}: {error}")
                return False
        if result.returncode == 0:
            clear_rebuild_failure(conn, environment["path"])
            print(f"  已恢复: {environment['path']}")
            return True
        if stream_output:
            error = "命令执行失败（详见上方 uv 输出）"
        else:
            error = result.stderr.strip() or result.stdout.strip() or "命令执行失败"
    except subprocess.TimeoutExpired:
        error = "恢复超时"
    except (FileNotFoundError, OSError) as exc:
        error = f"无法执行恢复命令: {exc}"
    record_rebuild_failure(
        conn, environment["path"], environment["type"], json.dumps(command), error,
    )
    print(f"  恢复失败: {environment['path']}: {error}")
    return False


def _retry_rebuilds(conn) -> int:
    failures = get_rebuild_failures(conn)
    if not failures:
        print("没有待重试的环境恢复记录。")
        return 0
    print(f"重试 {len(failures)} 个失败环境：")
    failed = 0
    for row in failures:
        try:
            command = json.loads(row["command_json"])
            if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
                raise ValueError("命令格式无效")
        except (json.JSONDecodeError, ValueError) as exc:
            record_rebuild_failure(conn, row["environment_path"], row["environment_type"],
                                   row["command_json"], f"失败记录损坏: {exc}")
            print(f"  恢复失败: {row['environment_path']}: 失败记录损坏")
            failed += 1
            continue
        environment_path = Path(row["environment_path"])
        if row["environment_type"] == "project":
            available = environment_path.is_dir() and (environment_path / ".venv").is_dir()
        else:
            available = environment_path.is_dir() and environment_path.parent.is_dir()
        if not available:
            clear_rebuild_failure(conn, row["environment_path"])
            print(f"  跳过失效环境: {row['environment_path']}")
            continue
        environment = {
            "path": row["environment_path"], "type": row["environment_type"],
            "command": command,
            "cwd": (row["environment_path"] if row["environment_type"] == "project"
                    else str(environment_path.parent)),
        }
        if not _run_environment(conn, environment):
            failed += 1
    record_operation(conn, "gc_rebuild_retry", success=failed == 0,
                     summary=f"重试 {len(failures) - failed}/{len(failures)} 个环境",
                     error=None if failed == 0 else f"{failed} 个环境恢复失败")
    return 0 if failed == 0 else 1


def _clean_orphans(conn, orphans: list[dict]) -> tuple[list[str], int]:
    cleaned_names: list[str] = []
    failed = 0
    for orphan in orphans:
        name = orphan["name"]
        print(f"正在清理: {name}...", end=" ", flush=True)
        try:
            result = subprocess.run(resolve_uv_command(["uv", "cache", "clean", name]),
                                    capture_output=True, text=True, timeout=120,
                                    **subprocess_text_kwargs())
            if result.returncode == 0:
                print("完成")
                cleaned_names.append(name)
                continue
            print(f"失败: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print("超时")
        except FileNotFoundError:
            print("错误: 找不到 uv 命令")
        failed += 1
    if cleaned_names:
        placeholders = ",".join("?" for _ in cleaned_names)
        ids = [row[0] for row in conn.execute(
            f"""SELECT p.id FROM packages p WHERE p.name IN ({placeholders})
                AND p.id NOT IN (SELECT DISTINCT package_id FROM venv_packages)""", cleaned_names,
        ).fetchall()]
        if ids:
            remove_orphan_packages(conn, ids)
    return cleaned_names, failed


def gc(dry_run: bool = False, *, auto_sync: bool = True, rebuild: bool = False,
       retry: bool = False) -> int:
    """执行孤立缓存清理，或按原环境恢复有历史版本的缓存。"""
    conn = get_connection()
    try:
        if retry:
            return _retry_rebuilds(conn)
        if not dry_run:
            if auto_sync:
                if not sync_all(conn, auto_discover=True, ignore_missing=rebuild):
                    print("错误: venv 同步失败，已中止 GC；未执行缓存清理。", file=sys.stderr)
                    record_operation(conn, "gc", success=False, error="venv 同步失败，未执行缓存清理")
                    return 1
            else:
                ok, message = check_uv_version()
                if not ok:
                    print(f"错误: {message}", file=sys.stderr)
                    return 1
            prune_rebuild_failures(conn)

        orphans = _get_packages_all_versions_orphaned(conn)
        rebuild_packages = get_new_rebuild_packages(conn) if rebuild else []
        candidate_names = {row["name"] for row in rebuild_packages}
        targets, skipped, protected = _get_rebuild_environments(conn, candidate_names)
        clean_names = sorted(candidate_names - protected)
        targets = [target for target in targets if target["packages"] & set(clean_names)]

        if dry_run:
            print(f"[dry-run] 将清理 {len(orphans)} 个完全孤立包。")
            if rebuild:
                print(f"[dry-run] 检测到 {len(rebuild_packages)} 个上次重建后新增的未使用包版本。")
                print(f"[dry-run] 将以一次 uv cache clean 清理 {len(clean_names)} 个候选包名。")
                if clean_names:
                    print("  uv cache clean " + " ".join(clean_names))
                for target in targets:
                    print(f"  {' '.join(target['command'])}  [{target['path']}]")
                for item in skipped:
                    print(f"  跳过 {item['path']}: {item['reason']}")
            return 0

        cleaned_names, clean_failed = _clean_orphans(conn, orphans)
        rebuild_failed = 0
        rebuild_clean_ok = True
        if rebuild and clean_names:
            print(f"正在一次清理 {len(clean_names)} 个待重建包名...")
            try:
                result = subprocess.run(
                    resolve_uv_command(["uv", "cache", "clean", *clean_names]),
                    capture_output=True, text=True, timeout=300, **subprocess_text_kwargs(),
                )
                rebuild_clean_ok = result.returncode == 0
                clean_error = result.stderr.strip() or "uv cache clean 执行失败"
            except subprocess.TimeoutExpired:
                rebuild_clean_ok, clean_error = False, "清理缓存超时"
            except FileNotFoundError:
                rebuild_clean_ok, clean_error = False, "找不到 uv 命令"
            if not rebuild_clean_ok:
                print(f"待重建包缓存清理失败: {clean_error}")
                rebuild_failed = len(targets)
            else:
                print(f"正在恢复 {len(targets)} 个受影响环境：")
                rebuild_failed = sum(
                    not _run_environment(conn, target, stream_output=True) for target in targets
                )
        if rebuild and rebuild_clean_ok:
            # 缓存清理失败时保留旧基线，以便下一次仍能处理同一批版本。
            replace_rebuild_baseline(conn)
        for item in skipped:
            print(f"跳过 {item['path']}: {item['reason']}；保留相关缓存。")
        print(f"\n已清理 {len(cleaned_names)}/{len(orphans)} 个孤立包。")
        success = clean_failed == 0 and rebuild_failed == 0
        record_operation(conn, "gc", success=success,
                         summary=f"已清理 {len(cleaned_names)}/{len(orphans)} 个完全孤立包；"
                                 f"恢复 {len(targets) - rebuild_failed}/{len(targets)} 个环境",
                         error=None if success else f"{clean_failed} 个孤立包清理失败，{rebuild_failed} 个环境恢复失败")
        return 0 if success else 1
    finally:
        conn.close()
