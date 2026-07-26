"""本地 SQLite 存储的公共底座。

所有「装上就开始攒」的本地数据都落在同一个库里（`~/.vibe-flow/history.db`），
由各模块注册自己的表。

⚠️ **不放仓库内**：更新代码 / 重新 clone 不该弄丢用户攒的历史
（VibeResearch 的 issue #12 就是这个坑）。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DIR = os.environ.get("VF_DATA_DIR") or os.path.expanduser("~/.vibe-flow")
DB_PATH = os.path.join(DEFAULT_DIR, "history.db")

_LOCK = threading.Lock()
_applied: set[str] = set()


def ensure_schema(name: str, schema_sql: str) -> None:
    """建表（幂等）。`name` 只用于避免同一进程内重复执行。"""
    if name in _applied:
        return
    with _LOCK:
        if name in _applied:
            return
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            conn.executescript(schema_sql)
        _applied.add(name)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """取一条连接（行按名字取值）。调用方负责先 `ensure_schema`。"""
    os.makedirs(DEFAULT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
