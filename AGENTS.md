# uv-mgr 项目

项目目录: `/home/xr_w/Documents/My_Doc/Projects/uv_mgr/`

## 项目概述

**uv-mgr** 是一个 uv 包装器（CLI 工具），提供全局索引管理和孤立包垃圾回收功能。uv 使用硬链接/副本在虚拟环境之间共享包缓存，但没有全局状态追踪。长时间使用后缓存中会累积大量已不被任何 venv 引用的旧版本包，占用磁盘空间。uv-mgr 维护一个 SQLite 索引数据库来跟踪每个 venv 的包状态，并提供 GC 命令安全地清理无用缓存。

### 技术栈

- 纯 Python 3.10+，零外部依赖（只用标准库: `sqlite3`, `json`, `subprocess`, `argparse`, `os`, `sys`, `pathlib`, `datetime`, `re`, `ntpath`, `shutil`）
- 构建系统: setuptools
- 数据库: SQLite (WAL 模式)，路径 `~/.local/share/uv-mgr/index.db`
- 安装方式: `pip install -e .` (editable) 或 `uv tool install .`

## 架构

```
├── pyproject.toml           # 项目元数据，entry point: uv_mgr.cli:main
├── uv.lock                  # uv 锁文件
├── README.md                # 用户文档
├── CHANGELOG.md             # 更新日志
├── RELEASE_NOTES.md         # 发布说明
├── CONTRIBUTING.md          # 贡献指南
├── LICENSE                  # MIT 许可
├── AGENTS.md                # 本文件：项目配置 + 已知陷阱
├── .github/
│   ├── workflows/ci.yml     # CI 流水线
│   └── ISSUE_TEMPLATE/      # Issue 模板
├── uv_mgr/
│   ├── __init__.py           # __version__ = "0.1.0"
│   ├── __main__.py           # python -m uv_mgr 入口
│   ├── cli.py                # argparse 调度 + uv 透传
│   ├── config.py             # 平台路径与外部命令配置
│   ├── db.py                 # SQLite 层（建表/迁移/CRUD）
│   ├── sync.py               # Venv 状态同步（扫描+索引更新）
│   └── gc.py                 # 孤立包 GC（委托 uv cache clean）
├── tests/
│   ├── __init__.py
│   ├── conftest.py           # pytest fixtures（内存数据库、mock 环境）
│   ├── test_config.py        # 配置与路径测试
│   ├── test_db.py            # 数据库 CRUD 与迁移测试
│   ├── test_sync.py          # 同步逻辑与 uv 版本检查测试
│   ├── test_gc.py            # GC 策略与重建逻辑测试
│   ├── test_cli.py           # CLI 入口与 --version 测试
│   └── test_integration.py   # 端到端集成测试
├── test-proj-a/
│   └── .venv/                # 测试用 venv（pytest, pygments, packaging 等）
├── build/                    # 构建产物（可删除）
└── dist/                     # 发行物（可删除）
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `cli.py` | argparse 解析、命令路由、自有命令（index/db/gc）与 uv 透传的调度 |
| `config.py` | 平台判断（Windows/POSIX）、数据目录与数据库路径解析、uv 可执行文件发现、路径规范化 |
| `db.py` | SQLite 建表、schema 版本管理（当前 v10）、venv/package/venv_packages 及历史表的 CRUD |
| `sync.py` | 扫描 venv 已安装包 -> 更新数据库索引，自动发现新 venv，uv 版本检查 |
| `gc.py` | 找出孤立包 -> `uv cache clean <pkg>` 安全清理，旧版本重建与恢复 |

### 数据库 Schema

Schema 版本: 10（通过 `_meta` 表的 `schema_version` 键管理，含 v1->v2 ... v9->v10 迁移函数）

| 表 | 说明 |
|------|------|
| `_meta` | key-value 存储，`schema_version` |
| `venvs` | id, path(UNIQUE), name, python_version, source('user'/'auto'/'tool'), created_at, last_synced_at |
| `packages` | id, name, version (UNIQUE name+version) |
| `venv_packages` | venv_id FK, package_id FK, installed_at (复合主键) |
| `operations` | id, occurred_at, operation_type, success, venv_path, summary, error |
| `sync_snapshots` | id, venv_path, python_version, occurred_at, operation_id FK |
| `snapshot_packages` | snapshot_id FK, name, version (复合主键) |
| `package_events` | id, venv_path, occurred_at, event_type, name, old_version, new_version, snapshot_id FK |
| `cache_rebuild_failures` | environment_path(PK), environment_type, command_json, attempts, last_error, last_failed_at |
| `cache_rebuild_baseline` | name, version, observed_at (复合主键) |
| `tool_rebuild_metadata` | environment_path(PK), arguments_json, python_version, recorded_at |

- 外键: CASCADE 删除（venv_packages、snapshot_packages）；SET NULL（sync_snapshots.operation_id、package_events.snapshot_id）

## CLI 命令

| 命令 | 说明 |
|------|------|
| `uv-mgr index add <path>` | 注册一个 venv |
| `uv-mgr index remove <path>` | 取消注册 |
| `uv-mgr index list [--venvs\|--packages\|--orphans] [--type user\|auto\|tool]` | 查询索引状态 |
| `uv-mgr index sync [path] [-v]` | 同步 venv 包状态（默认全部） |
| `uv-mgr index gc [path] [-v]` | 同步并清理已不存在的 venv 记录 |
| `uv-mgr gc [--dry-run] [--rebuild] [--retry]` | GC：清理孤立缓存包 |
| `uv-mgr db info` | 数据库统计信息 |
| `uv-mgr db history [--venv\|--package\|--events\|--snapshots] [--limit N]` | 操作/事件/快照历史 |
| `uv-mgr <uv args...>` | 透传 uv，执行后自动 sync |

### 环境变量

- `UV_MGR_SYNC_AFTER=0` - 跳过透传 uv 后的自动 sync（旧名 `UV_SYNC_AFTER` 仍可用，但会打印弃用警告）
- `UV_MGR_DATA_DIR` - 数据目录覆盖
- `UV_MGR_DB_PATH` - 数据库完整路径覆盖（优先级最高）
- `UV_MGR_UV_BIN` - uv 可执行文件路径覆盖

## 构建与运行

```bash
# 开发安装
pip install -e .

# 运行（两种方式）
uv-mgr <command>
python -m uv_mgr <command>

# 运行测试
python -m pytest -q

# 构建发行物（需先删除旧 build/ 目录）
uv run python -m build --sdist --wheel
uv run python -m twine check dist/*
```

## 开发约定

### 代码风格

- Python 3.10+（使用 `match/case`、`X | Y` 类型联合语法）
- 类型注解：函数参数和返回值标注类型，但允许部分省略（`list | None = None` 风格）
- 所有自有命令字符串用 `frozenset` 定义 (`OWN_COMMANDS`)
- 数据库连接使用 `sqlite3.Row` 作为 row_factory，通过列名访问
- 工具类函数使用 `*` 强制关键字参数（如 `sync_venv(conn, path, *, auto_register=False)`）

### 设计原则（已确认，不可违背）

1. **纯 stdlib** - 零外部依赖，只用 Python 标准库
2. **委托 uv** - 包操作（install/uninstall/list/clean）全部委托给 uv 命令，uv-mgr 只维护索引
3. **安全优先** - GC 只清理所有版本都不被引用的包，提供 `--dry-run` 预览
4. **分离原则** - `index sync` 只更新索引，`index gc` 在同步时清理失效 venv 记录，`gc` 才执行物理缓存删除，三者不自动联动
5. **全量替换** - Sync 时全量替换该 venv 的 packages 记录（先删后插），不做增量 diff
6. **命令优先级** - uv-mgr 自有命令优先处理，非自有命令全部透传给 uv
7. **自动发现** - venv 发现采用手动注册（`uv-mgr index add`）+ sync 时自动扫描当前目录及祖先目录 `.venv` 和 `uv tool dir` 工具环境的模式

### 测试

- 测试目录: `tests/`，使用 pytest
- 测试文件:
  - `test_config.py` - 配置与路径解析
  - `test_db.py` - 数据库 CRUD 与 schema 迁移
  - `test_sync.py` - 同步逻辑与 uv 版本检查
  - `test_gc.py` - GC 策略与重建逻辑
  - `test_cli.py` - CLI 入口与 --version 行为
  - `test_integration.py` - 端到端集成测试（使用 `test-proj-a/.venv`）
- `conftest.py` 提供内存数据库 fixture、mock 环境变量等
- CI: GitHub Actions，在 Ubuntu/Windows 上测试 Python 3.10-3.13

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

### 4. 数据库

- 数据库使用了 WAL 模式（`PRAGMA journal_mode=WAL`），并发读取性能较好
- `db.py` 中 `DB_PATH` 和 `DB_DIR` 在模块加载时从 `config.py` 计算，但 `_current_db_path()` 会在运行时重新读取环境变量，确保 `UV_MGR_DB_PATH` 和测试覆盖生效

### 5. 环境变量命名

- 所有 uv-mgr 自有环境变量使用 `UV_MGR_` 前缀（`UV_MGR_SYNC_AFTER`、`UV_MGR_DATA_DIR`、`UV_MGR_DB_PATH`、`UV_MGR_UV_BIN`）
- 旧名 `UV_SYNC_AFTER` 保留兼容层，触发时打印弃用警告到 stderr
