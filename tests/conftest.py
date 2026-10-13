"""测试公共夹具：内存假连接，用于在不连 MySQL 的前提下校验 DAO 层
的 SQL 文本与参数行映射（字段顺序/默认值/分批），这是数据正确性的核心。
"""
from __future__ import annotations

from contextlib import contextmanager


class _FakeCursor:
    """记录 execute/executemany 的假游标（支持 with 上下文）。"""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def executemany(self, sql, rows):
        self._conn.calls.append(("executemany", sql, list(rows)))
        self._conn.committed = False
        return len(rows)

    def execute(self, sql, args=None):
        self._conn.calls.append(("execute", sql, args))
        self._conn.committed = False
        return 1

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConn:
    """假数据库连接：捕获全部 SQL 调用，供断言。

    - calls: [(method, sql, args)] 按调用顺序记录
    - commit_calls / rollback_calls / close_calls: 计数
    - fail_times: 前 N 次 executemany 抛 MySQLError（测试重试路径）
    """

    def __init__(self, fail_times: int = 0, errno: int = 1213) -> None:
        self.calls: list[tuple] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.committed = True
        self._fail_times = fail_times
        self._errno = errno

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def executemany(self, sql, rows):
        # 供不走 cursor 的直连调用（一般不会触发）
        self.calls.append(("executemany", sql, list(rows)))
        return len(rows)

    def commit(self) -> None:
        self.commit_calls += 1
        self.committed = True

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def ping(self, reconnect: bool = True) -> None:
        return None

    # ---- 断言辅助 ----
    def inserted_sql(self) -> list[str]:
        return [sql for m, sql, _ in self.calls if m == "executemany"]

    def all_rows(self) -> list[tuple]:
        out: list[tuple] = []
        for m, _sql, rows in self.calls:
            if m == "executemany":
                out.extend(rows)
        return out


class FailingCursor(_FakeCursor):
    """前 N 次 executemany 抛指定 errno 的游标（测试死锁重试）。"""

    def executemany(self, sql, rows):
        conn = self._conn
        if conn._fail_times > 0:
            conn._fail_times -= 1
            import pymysql

            raise pymysql.err.OperationalError(conn._errno, "fake deadlock")
        return super().executemany(sql, rows)


class FailingConn(FakeConn):
    def cursor(self):
        return FailingCursor(self)


@contextmanager
def patch_write_conn(conn: FakeConn):
    """把 dao._w 临时替换为返回 conn 的函数（不改全局）。"""
    from storage import dao

    original = dao._w
    dao._w = lambda: conn
    try:
        yield conn
    finally:
        dao._w = original
