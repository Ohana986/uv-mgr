# uv-mgr

uv 包装器：全局索引管理 + 孤立包垃圾回收。

## 解决的问题

官方定义是：uv cache prune 只删除“未使用的缓存条目”，以及由 uv 集中管理的项目环境；它不会扫描普通虚拟环境的包引用关系，也不会因为你删除了某个 .venv 就判定相应包缓存失效。因此：
1. 创建 uv-cache-prune-test/.venv，用 uv pip install 安装 httpx。
2. 删除整个 uv-cache-prune-test。
3. 执行 uv cache prune。
第 3 步通常不会移除为 httpx 下载的 wheel、解压归档等缓存。

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
uv-mgr add requests         # 透传 uv add：添加依赖
uv-mgr remove requests      # 透传 uv remove：删除依赖
uv-mgr venv --python 3.11   # uv venv：创建虚拟环境（自动注册到索引）
uv-mgr run app.py           # uv run：运行脚本
uv-mgr pip list             # uv pip list：查看已安装包
```

透传 uv 执行成功后，会自动触发一次 `uv-mgr index sync` 更新索引。`add`、`remove`、`list` 等未出现在
`index` 后的命令都是 uv 命令透传；索引管理命令必须使用 `uv-mgr index <subcommand>`。

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

# 查看数据库中未被任何 venv 引用的包记录
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

# 显示每个 venv 的同步详情（默认静默）
uv-mgr index sync --verbose
```

> 同步成功的"已同步"详情默认隐藏。需要查看时加 `-v` / `--verbose`：
> `uv-mgr -v <uv 命令>`（透传 uv 后）或 `uv-mgr index sync -v`、`uv-mgr index gc -v`。

**垃圾回收**

```bash
# 预览：查看将要清理哪些包
uv-mgr index gc --dry-run

# 执行清理：调用 uv cache clean <pkg> 删除孤立缓存
uv-mgr index gc

# 同时处理有旧版本记录的在用包：清空包名缓存后，以临时项目重建当前版本
uv-mgr index gc --rebuild
```

### 数据库信息

```bash
uv-mgr db info
```

### 操作与包版本历史

```bash
# 最近操作审计（默认最近 50 条）
uv-mgr db history

# 某个 venv 的历史操作
uv-mgr db history --venv /path/to/venv

# 包的安装、移除、升级与降级事件
uv-mgr db history --events --package requests

# 成功同步的完整包快照
uv-mgr db history --snapshots --venv /path/to/venv
```

每次成功同步都会保存该 venv 的完整包版本快照，并与前一快照比较，记录安装、移除、升级和降级事件。注册、移除、同步失败、GC 预览和 GC 执行同样记录完成时刻。历史永久保留，独立于当前索引：移除 venv、清理当前索引的历史版本或执行 GC 都不会删除已有历史。

### 快速参考

| 想做的事 | 命令 |
|----------|------|
| 安装依赖 | `uv-mgr add requests`（透传 uv） |
| 创建 venv | `uv-mgr venv --python 3.11` |
| 注册 venv 到索引 | `uv-mgr index add .venv` |
| 查看索引状态 | `uv-mgr index list` |
| 同步索引 | `uv-mgr index sync --prune` |
| 清理孤立缓存 | `uv-mgr index gc` |
| 查看 uv 命令 | `uv-mgr --help` |
| 查看 uv-mgr 命令 | `uv-mgr`（无参数）或 `uv-mgr index --help` |

## GC 策略

- **安全优先**：仅当某个包的**所有版本**都不被任何 venv 引用时，才 `uv cache clean <pkg>`
- **历史记录清理**：每次 venv 成功同步后，自动删除“未被引用但同名其他版本仍在使用”的数据库历史版本；完全没有版本被引用的包记录会保留给 GC 判断
- **分离原则**：`index sync` 只更新索引，`index gc` 才执行物理删除——给用户反悔空间
- **预览模式**：`--dry-run` 可查看影响范围后再执行
- **旧版本重建**：`uv-mgr index gc --rebuild` 只处理同步历史中出现过旧版本、且当前仍有版本被 venv 使用的包。它先执行 `uv cache clean <包名>`，再在 `/tmp` 的临时项目中逐个 `uv add 包名==当前版本`，以重新填充缓存。成功重建后，在没有再次清理同名缓存前不会重复处理；同一包名再次重建时会使旧成功记录失效。重建失败会记录下来，下次运行同一命令会重试。

数据库索引记录的是各 venv 的最终安装清单，不等同于 uv 的全量缓存。缓存中未被索引的构建依赖或下载包不会被自动判定为垃圾；数据库中没有对应缓存的历史记录也不会直接触发缓存删除。

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

## 自动发现与 uv 兼容性

自动发现仅检查当前目录、最近祖先目录中的 `.venv`，以及 `uv tool dir` 返回的工具环境，
不会进行全盘扫描。项目支持 uv 0.4 或更高版本，运行时会检查 uv 是否存在及版本是否满足要求。
若 uv 不存在、执行失败或输出格式无效，同步会报告失败并保留该 venv 的原有索引；GC 在同步状态不完整时会中止，避免误清理缓存。

## 发布验证

发布前应在 Python 3.10 至 3.13 上运行完整测试，并构建源码包和 wheel。发行物安装后应验证
`uv-mgr --version`、`python -m uv_mgr --version` 以及非零退出码是否正确传递。本项目不在 CI 中自动上传 PyPI。
