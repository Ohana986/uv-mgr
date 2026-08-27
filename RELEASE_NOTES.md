# uv-mgr 0.1.1

0.1.1 为缺陷修复版本，主要解决 Windows 平台下缓存重建失败记录清理失效的问题。

变更内容：
- `prune_rebuild_failures` 不再使用 SQL 字符串拼接路径，改为遍历已索引 venv 按平台（`is_windows()` 判定）正确分割项目目录，并经 `normalize_path` 归一后比对，从而在 Windows 下也能正确剔除失效记录。
- 补充 POSIX / Linux 操作系统分类符。
- README 澄清自动发现的范围与命名环境的手动登记方式。

升级与发布流程同 0.1.0：发布前请在 Python 3.10 至 3.13 上运行完整测试，并执行 `uv run python -m twine check dist/*` 校验发行物。

---

# uv-mgr 0.1.0

uv-mgr 0.1.0 是首个正式版本，提供 uv 透明透传、虚拟环境索引同步及孤立缓存垃圾回收。

重要安全行为：当任一虚拟环境同步失败时，程序会保留原有索引并中止 GC，不会基于不完整状态执行缓存清理。
运行需要 uv 0.4 或更高版本。发布前请在 Python 3.10 至 3.13 上运行完整测试并验证源码包与 wheel。

构建工具和发行物校验工具属于开发依赖。首次构建前请运行 `uv-mgr sync`；若根目录存在旧的 `build/` 目录，请先手动删除后再执行 `uv run python -m build --sdist --wheel`，随后运行 `uv run python -m twine check dist/*`。
