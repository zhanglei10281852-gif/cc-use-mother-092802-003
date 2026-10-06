"""应用装配：数据库路径、可控时钟、按线程的连接与服务实例。

并发模型：每个工作线程持有自己的 sqlite3 连接（WAL 模式），
写事务用 BEGIN IMMEDIATE 抢同一把数据库写锁，从而真实地
串行化并发审批 / 并发回执，而不是在应用层假锁。
内存库按连接隔离，因此多线程场景必须使用文件路径（可用临时文件）。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .clock import Clock
from .database import init_db
from .services import FundService


class Application:
    def __init__(self, db_path: str | Path = ":memory:", clock: Clock | None = None):
        self.db_path = str(db_path)
        self.clock = clock or Clock()
        self._local = threading.local()
        if self.db_path != ":memory:":
            init_db(self._new_connection())
        else:
            init_db(self.connection)  # 单线程内存库：先建好表

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    @property
    def service(self) -> FundService:
        svc = getattr(self._local, "service", None)
        if svc is None:
            svc = FundService(self.connection, self.clock)
            self._local.service = svc
        return svc

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
