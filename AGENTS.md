# uv-mgr 项目

项目目录: `/run/media/xr_w/U_Pan/Documents/Projects/auv/`

## 项目概述

**uv-mgr** 是一个 uv 包装器（CLI 工具），提供全局索引管理和孤立包垃圾回收功能。uv 使用硬链接/副本在虚拟环境之间共享包缓存，但没有全局状态追踪。长时间使用后缓存中会累积大量已不被任何 venv 引用的旧版本包，占用磁盘空间。uv-mgr 维护一个 SQLite 索引数据库来跟踪每个 venv 的包状态，并提供 GC 命令安全地清理无用缓存。

### 技术栈

- 纯 Python 3.10+，零外部依赖（只用标准库: `sqlite3`, `json`, `subprocess`, `argparse`, `os`, `sys`, `pathlib`, `datetime`）
- 构建系统: setuptools
- 数据库: SQLite (WAL 模式)，路径 `~/.local/share/uv-mgr/index.db`
- 安装方式: `pip install -e .` (editable) 或 `uv tool install .`

## 架构

```
├── pyproject.toml          # 项目元数据，entry point: uv_mgr.cli:main
├── README.md               # 用户文档
├── QWEN.md                 # 本文件：项目 QWEN 配置 + 已知陷阱
├── uv_mgr/
│   ├── __init__.py          # __version__ = "0.1.0"
│   ├── __main__.py          # python -m uv_mgr 入口
│   ├── cli.py               # argparse 调度 + uv 透传
│   ├── db.py                # SQLite 层（建表/迁移/CRUD）
│   ├── sync.py              # Venv 状态同步（扫描+索引更新）
│   └── gc.py                # 孤立包 GC（委托 uv cache clean）
├── test-proj-a/
│   └── .venv/               # 测试用 venv
├── test-proj-b/
│   └── .venv/               # 测试用 venv
└── uv_mgr.egg-info/         # pip install -e . 生成
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `cli.py` | argparse 解析、命令路由、自有命令（add/remove/list/sync/gc/db）与 uv 透传的调度 |
| `db.py` | SQLite 建表、schema 版本管理、venv/package/venv_packages 的 CRUD |
| `sync.py` | 扫描 venv 已安装包 → 更新数据库索引，自动发现新 venv |
| `gc.py` | 找出孤立包 → `uv cache clean <pkg>` 安全清理 |

### 数据库 Schema

- `_meta`: key-value 存储，`schema_version=1`
- `venvs`: id, path(UNIQUE), name, python_version, created_at, last_synced_at
- `packages`: id, name, version (UNIQUE name+version)
- `venv_packages`: venv_id FK, package_id FK, installed_at (复合主键 PK)
- 外键: CASCADE 删除

## CLI 命令

| 命令 | 说明 |
|------|------|
| `uv-mgr add <path>` | 注册一个 venv |
| `uv-mgr remove <path>` | 取消注册 |
| `uv-mgr list [--venvs\|--packages\|--orphans]` | 查询索引状态 |
| `uv-mgr sync [path]` | 同步 venv 包状态（默认全部） |
| `uv-mgr gc [--dry-run]` | GC：清理孤立缓存包 |
| `uv-mgr db info` | 数据库统计信息 |
| `uv-mgr <uv args...>` | 透传 uv，执行后自动 sync |

### 环境变量

- `UV_SYNC_AFTER=0` — 跳过透传 uv 后的自动 sync

## 构建与运行

```bash
# 开发安装
pip install -e .

# 运行（两种方式）
uv-mgr <command>
python -m uv_mgr <command>

# 测试用 venv 已预置
# test-proj-a/.venv/  - 含 pytest, attrs, iniconfig, packaging, pluggy
# test-proj-b/.venv/  - 含 pygments
```

## 开发约定

### 代码风格

- Python 3.10+（使用 `match/case`、`X | Y` 类型联合语法）
- 类型注解：函数参数和返回值标注类型，但允许部分省略（`list | None = None` 风格）
- 所有自有命令字符串用 `frozenset` 定义 (`OWN_COMMANDS`)
- 数据库连接使用 `sqlite3.Row` 作为 row_factory，通过列名访问
- 工具类函数使用 `*` 强制关键字参数（如 `sync_venv(conn, path, *, auto_register=False)`）

### 设计原则（已确认，不可违背）

1. **纯 stdlib** — 零外部依赖，只用 Python 标准库
2. **委托 uv** — 包操作（install/uninstall/list/clean）全部委托给 uv 命令，uv-mgr 只维护索引
3. **安全优先** — GC 只清理所有版本都不被引用的包，提供 `--dry-run` 预览
4. **分离原则** — `sync` 只更新索引，`gc` 才执行物理删除，不自动联动
5. **全量替换** — Sync 时全量替换该 venv 的 packages 记录（先删后插），不做增量 diff
6. **命令优先级** — uv-mgr 自有命令优先处理，非自有命令全部透传给 uv
7. **手动注册** — venv 发现采用手动注册（`uv-mgr add`）+ sync 时自动扫描 `.venv` 的模式

### 测试

- 项目没有单元测试文件（无 `tests/` 目录）
- 验证方式：手动端到端测试，使用 `test-proj-a/.venv` 和 `test-proj-b/.venv` 两个测试环境
- TODO: 需要补充测试

## 已知陷阱 / 注意事项

### 1. f-string 嵌套双引号导致 SyntaxError

**文件:** `uv_mgr/cli.py`
**已修复。** `\"` 在 f-string 的 `{}` 表达式中被解析为字符串结束符。将 SQL 查询提取到变量中再引用。

### 2. `cursor.lastrowid` 在 INSERT OR IGNORE 被忽略时不重置

**文件:** `uv_mgr/db.py`
**已修复。** `INSERT OR IGNORE` 因 UNIQUE 约束被跳过时，`cursor.lastrowid` 不归零。改用 `cursor.rowcount > 0` 判断插入是否实际发生。

### 3. uv 命令行为

- `uv cache clean <pkg>` 只接受裸包名，不支持 `pkg==ver`
- `uv cache list` **不存在**，`uv cache` 只有 `clean/prune/dir/size`
- `uv pip list --format=json` 返回 `[{"name":"pip","version":"26.1.2"}]` 格式
- `uv tool run` 的别名是 `uvx`，所以本工具不能叫 `uvx`（已改为 `uv-mgr`）

### 4. 开发环境

- 工具安装在 `Agent` conda 环境中（`~/.local/share/uv-mgr/index.db` 是数据库路径）
- 项目本身使用系统 Python，不依赖 conda 环境
- 数据库使用了 WAL 模式（`PRAGMA journal_mode=WAL`），并发读取性能较好

## 相关项目技能

该项目注册了 `build-cli-wrapper` 技能，用于指导 CLI 包装器的构建模式。
