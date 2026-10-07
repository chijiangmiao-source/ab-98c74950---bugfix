"""整库原子持久化。

一个 JSON 文件承载两部分概念上独立的数据：

- ``publications``：本端（数据官/发送方）额度账本，按稳定发布标识索引；
- ``deliveries`` ：接收端持久化的交付记录（首份摘要与回执），按同一
  发布标识索引；不同标识即使载荷完全相同也各自独立成一条记录。

每次写入整库序列化到同目录临时文件，``fsync`` 后以 ``os.replace`` 原子
替换。进程在两次落盘之间被硬杀（模拟断电）时，磁盘上只可能是完整的旧版
或完整的新版，不会出现半份文件。所有变更在同一把可重入锁内进行，保证
同一发布标识的并发相同提交被串行化。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any


class JsonStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": 1,
            "publications": {},
            "deliveries": {},
        }
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                try:
                    loaded = json.load(fh)
                except json.JSONDecodeError:
                    loaded = None
            if isinstance(loaded, dict):
                self.data.update(loaded)
        self.data.setdefault("publications", {})
        self.data.setdefault("deliveries", {})

    def flush(self) -> None:
        """将当前内存状态原子落盘（调用方须持有 ``self.lock``）。"""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def snapshot(self) -> dict[str, Any]:
        """返回整库的深拷贝，供锁外只读渲染使用。"""
        with self.lock:
            return json.loads(json.dumps(self.data))
