# uv-mgr

uv 包装器：全局索引管理 + 孤立包垃圾回收。

## 解决的问题

`uv` 使用硬链接/副本在虚拟环境之间共享包缓存，但没有全局状态追踪。长时间使用后，缓存中会累积大量已不被任何 venv 引用的旧版本包，占用磁盘空间。`uv-mgr` 维护一个索引数据库来跟踪每个 venv 的包状态，并提供 GC 命令安全地清理无用缓存。

## 安装

```bash
uv tool install .
# 或
pip install .
```

路径下直接运行也支持：

```bash
python -m uv_mgr <command>
```

## 使用

`uv-mgr` 是 `uv` 的透明包装器——所有非索引命令直接透传给 `uv`，你只需记住一个命令即可完成日常操作和索引管理。

### 日常操作（透传 uv）

```bash
# 直接透传 uv 命令，执行后自动同步索引
uv-mgr sync                 # uv sync：同步项目环境
uv-mgr add requests         # uv add：添加依赖
uv-mgr remove requests      # uv remove：删除依赖
uv-mgr venv --python 3.11   # uv venv：创建虚拟环境（自动注册到索引）
uv-mgr run app.py           # uv run：运行脚本
uv-mgr pip list             # uv pip list：查看已安装包
```

透传 uv 执行成功后，会自动触发一次 `uv-mgr index sync` 更新索引。

### 索引管理（`uv-mgr index <subcommand>`）

**注册 / 取消注册**

```bash
# 注册一个 venv 纳入追踪
uv-mgr index add .venv
uv-mgr index add /path/to/venv

# 取消注册（不会删除目录）
uv-mgr index remove .venv
```

**索引查询**

```bash
# 查看所有注册 venv
uv-mgr index list --venvs

# 查看所有已索引包
uv-mgr index list --packages

# 查看孤立包（未被任何 venv 引用）
uv-mgr index list --orphans

# 查看全部
uv-mgr index list
```

**状态同步**

```bash
# 同步所有已注册 venv 的包状态到索引
uv-mgr index sync

# 同步后自动清理已删除的 venv 记录
uv-mgr index sync --prune
```

**垃圾回收**

```bash
# 预览：查看将要清理哪些包
uv-mgr index gc --dry-run

# 执行清理：调用 uv cache clean <pkg> 删除孤立缓存
uv-mgr index gc
```

### 数据库信息

```bash
uv-mgr db info
```

### 快速参考

| 想做的事 | 命令 |
|----------|------|
| 安装依赖 | `uv-mgr add requests` |
| 创建 venv | `uv-mgr venv --python 3.11` |
| 注册 venv 到索引 | `uv-mgr index add .venv` |
| 查看索引状态 | `uv-mgr index list` |
| 同步索引 | `uv-mgr index sync --prune` |
| 清理孤立缓存 | `uv-mgr index gc` |
| 查看 uv 命令 | `uv-mgr --help` |
| 查看 uv-mgr 命令 | `uv-mgr`（无参数）或 `uv-mgr index --help` |

## GC 策略

- **安全优先**：仅当某个包的**所有版本**都不被任何 venv 引用时，才 `uv cache clean <pkg>`
- **分离原则**：`index sync` 只更新索引，`index gc` 才执行物理删除——给用户反悔空间
- **预览模式**：`--dry-run` 可查看影响范围后再执行

## 环境变量

- `UV_SYNC_AFTER=0` — 跳过透传 uv 后的自动 sync

## 项目结构

```
uv-mgr/
├── __init__.py   # 版本
├── __main__.py   # python -m 入口
├── cli.py        # CLI 调度
├── db.py         # SQLite 数据库
├── sync.py       # 状态同步
└── gc.py         # 垃圾回收
```

数据库位置：`~/.local/share/uv-mgr/index.db`
