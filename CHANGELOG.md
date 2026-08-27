# 更新日志

## 0.1.1

- 修复 Windows 下缓存重建失败记录的清理逻辑：`prune_rebuild_failures` 原以 SQL 字符串拼接路径（`environment_path || '/.venv'`），在 Windows 路径分隔符下无法正确匹配；改为按平台分割路径并经 `normalize_path` 归一后比对。
- 补充 `Operating System :: POSIX` 与 `POSIX :: Linux` 分类符，明确平台支持范围。
- 文档澄清：自动发现仅注册当前目录或最近祖先目录中的 `.venv`，通过 `uv-mgr venv <其他路径>` 创建的命名环境需使用 `uv-mgr index add` 手动登记。

## 0.1.0（正式版）

- 提供 uv 透明透传、venv 索引同步和孤立缓存垃圾回收。
- 扫描失败时保留原有索引，避免误删包引用。
- 修复 `python -m uv_mgr` 的退出码传递。
- GC 部分清理失败时返回非零状态码。

## 发布前完善

- 同步失败时阻止 GC，避免使用不完整索引清理缓存。
- 增加 uv 版本检查（最低 0.4）。
- 统一构建元数据的版本来源，并完善发行物验证流程。
