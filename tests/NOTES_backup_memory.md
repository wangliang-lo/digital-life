**覆盖**：`snapshot()` 用 tmp_path 里现造的两表小库导出 → 断言 `.gz` 存在、解压后可打开、表和行数/内容都在（真实 `data/xiaoyu.db` 全程不碰）；文件名严格匹配 `xiaoyu-YYYYMMDD.db.gz` 且同日只留一份；`upload()` 在 scp 返回非零、以及 `subprocess.run` 直接抛 `FileNotFoundError` 两种情况下都不删本地备份（前者返回 False）；`main()` 上传失败返回 1 且本地文件仍在、成功返回 0 且写一条 `ok=true` 日志、导出失败返回 1 且不调 upload、`--dry` 返回 0 且不上传；远端清理命令断言 `find ... -name 'xiaoyu-*.db.gz' -mtime +14 -delete`、无 `-mtime -14`、无 `rm -rf`，且 scp 只出现在上传那一次调用里。
**怎么跑**：`cd PROJECT_ROOT && C:\Users\Lenovo\anaconda3\python.exe -m pytest tests/test_backup_memory.py -q`（15 passed，约 0.3s）。
**没覆盖**：真实 ssh/scp 与网络、公网服务器的实际清理结果（只断言命令行文本）、gzip 写入中途失败/磁盘满、`backup_one()`（已有 `test_backup_restore.py` 覆盖）、WAL 模式或并发写入时的在线备份正确性、日志文件不可写时的降级行为。

## 读代码发现的问题（未改生产文件）

1. **`snapshot()` 第 93–94 行，被备份的当日文件在重写前就被 unlink**：`if gz.exists(): gz.unlink()` 之后才做 gzip 压缩。若压缩这一步失败（磁盘满、权限变更、`LOCAL_BACKUPS` 被占），当天早上那份**好备份已被删掉、新的又没写成**，当天无备份可恢复。应先写 `.gz.tmp` 再 `replace()`（`backup_one()` 就是这么做的，`snapshot()` 没有）。
2. **`main()` 第 138 行，`upload()` 抛异常时整条链路裸奔**：`upload()` 里的 `subprocess.run`（112、121 行）没有 try，`scp.exe` 不存在或 SSH 组件被卸载时抛 `FileNotFoundError`，直接从 `main()` 冒出去。后果：Python 带 traceback 退出、返回码不是 1 而是未定义值、`_log()` 里**不会写 `ok=false` 的记录**——而 cron 和日志恰恰是靠这两点判断"今晚备份失败了"。同样，ssh 清理失败（121 行）被完全静默，`upload()` 仍返回 True（已写成测试固化当前行为）。
3. **`snapshot()` 第 79 行创建的临时目录在导出失败时泄漏**：第 87–89 行 `except` 直接 `return None`，没有 `shutil.rmtree(temp.parent)`（只在成功路径第 97 行清理）。每次备份失败都会在 `%TEMP%` 下留一个 `xiaoyu.db` 大小的目录，恰好赶上磁盘满时会越积越多，让失败雪上加霜。
4. **`snapshot()` 的 `dry` 参数（第 73 行）完全没用**：`--dry` 只跳过上传，**照样真的导出并覆盖当天的 `.gz`**，与模块 docstring"只看会备份什么"不符；这个"只看"还会触发上面第 1 条，把当天的正常备份替换成一次演练结果。
5. **小问题**：第 120 行 `-mtime +14` 是"严格超过 14 天"才删，实际保留 15 天，与注释"保留 14 天"差一天；本地 `LOCAL_BACKUPS` 没有任何轮转，每天一个文件会无限增长（远端清了、本机不清）；第 99–105 行的 `counts = {}` 和那段连上又立刻关掉的 `conn` 是死代码。
