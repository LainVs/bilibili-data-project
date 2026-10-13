"""storage/dao 包关键写入路径单元测试（不连 MySQL）。

聚焦数据正确性风险最高的部分：
- upsert_* 的字段顺序 / 默认值 / 空输入短路
- 批大小分片（_WRITE_BATCH）
- 死锁(1213)自动重试
- 全局去重表直播喂数的去重与容错
"""
from __future__ import annotations

import pymysql

from storage import dao
from tests.conftest import FailingConn, FakeConn

# ---------- upsert_danmaku ----------

def test_upsert_danmaku_maps_fields_in_order():
    conn = FakeConn()
    items = [{"id": 111, "video_aid": 900, "uid": 42, "mid_hash": "deadbeef",
              "content": "弹幕A", "progress": 12.5, "mode": 1, "color": 16711680,
              "ctime": "2026-01-01 00:00:00"}]
    n = dao.upsert_danmaku(items, conn=conn)

    assert n == 1
    assert len(conn.calls) == 1
    method, sql, rows = conn.calls[0]
    assert method == "executemany"
    assert "INSERT INTO raw_danmaku" in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    # 字段顺序: id, video_aid, uid, mid_hash, content, progress, mode, color, ctime
    assert rows[0] == (111, 900, 42, "deadbeef", "弹幕A", 12.5, 1, 16711680,
                       "2026-01-01 00:00:00")


def test_upsert_danmaku_defaults():
    conn = FakeConn()
    dao.upsert_danmaku([{"id": 1, "video_aid": 2}], conn=conn)
    rows = conn.all_rows()
    # uid 缺省 0, mid_hash '', content '', progress 0.0, mode 1, color 白, ctime None
    assert rows[0] == (1, 2, 0, "", "", 0.0, 1, 16777215, None)


def test_upsert_danmaku_empty_is_noop():
    conn = FakeConn()
    assert dao.upsert_danmaku([], conn=conn) == 0
    assert conn.calls == []


# ---------- upsert_comments ----------

def test_upsert_comments_maps_ref_type_and_ip():
    conn = FakeConn()
    items = [{"rpid": 5, "video_aid": 88, "ref_type": 17, "parent": 4, "uid": 7,
              "content": "评论", "like_num": 3, "ctime": "2026-02-02 10:00:00",
              "ip_location": "北京"}]
    n = dao.upsert_comments(items, conn=conn)
    assert n == 1
    method, sql, rows = conn.calls[0]
    assert "INSERT INTO raw_comments" in sql
    # 字段顺序: rpid, video_aid, ref_type, parent, uid, content, like_num, ctime, ip_location
    assert rows[0] == (5, 88, 17, 4, 7, "评论", 3, "2026-02-02 10:00:00", "北京")


def test_upsert_comments_defaults_ref_type_is_video():
    conn = FakeConn()
    dao.upsert_comments([{"rpid": 1, "video_aid": 2, "uid": 3}], conn=conn)
    # 缺省 ref_type=1(视频), parent=0, like_num=0, ip_location=''
    assert conn.all_rows()[0] == (1, 2, 1, 0, 3, "", 0, None, "")


def test_upsert_comments_empty_is_noop():
    conn = FakeConn()
    assert dao.upsert_comments([], conn=conn) == 0
    assert conn.calls == []


# ---------- upsert_users ----------

def test_upsert_users_maps_and_truncates_fans_default():
    conn = FakeConn()
    items = [{"uid": 9, "uname": "u", "level": 3, "sex": "男", "is_vip": 1,
              "signature": "sig", "fans": None}]
    assert dao.upsert_users(items, conn=conn) == 1
    method, sql, rows = conn.calls[0]
    assert "INSERT INTO dim_user" in sql
    # fans=None 兜底为 0；顺序: uid, uname, level, sex, is_vip, signature, fans
    assert rows[0] == (9, "u", 3, "男", 1, "sig", 0)


def test_upsert_users_fans_string_coerced():
    conn = FakeConn()
    dao.upsert_users([{"uid": 1, "fans": "123"}], conn=conn)
    assert conn.all_rows()[0][-1] == 123


# ---------- 批大小分片 ----------

def test_executemany_splits_into_batches():
    conn = FakeConn()
    rows = [(i,) for i in range(dao._WRITE_BATCH * 2 + 5)]
    total = dao._executemany("INSERT INTO t (x) VALUES (%s)", rows, conn=conn)
    assert total == len(rows)
    # 应拆成 3 批: 2000 + 2000 + 5
    assert len(conn.calls) == 3
    assert len(conn.calls[0][2]) == dao._WRITE_BATCH
    assert len(conn.calls[2][2]) == 5


def test_executemany_empty_returns_zero():
    conn = FakeConn()
    assert dao._executemany("INSERT INTO t (x) VALUES (%s)", [], conn=conn) == 0
    assert conn.calls == []


# ---------- 死锁重试 ----------

def test_executemany_retries_on_deadlock_then_succeeds():
    conn = FailingConn(fail_times=1, errno=1213)
    n = dao._executemany("INSERT INTO t (x) VALUES (%s)", [(1,), (2,)], conn=conn)
    assert n == 2
    # 第一次失败 rollback，第二次成功
    assert conn.rollback_calls == 1


def test_executemany_raises_non_retryable():
    conn = FailingConn(fail_times=5, errno=1062)  # 非可重试错误
    try:
        dao._executemany("INSERT INTO t (x) VALUES (%s)", [(1,)], conn=conn)
    except pymysql.MySQLError:
        pass
    else:
        raise AssertionError("非可重试错误应直接抛出")


# ---------- 全局去重直播喂数 ----------

def test_add_global_comment_uids_dedups_and_writes_two_tables():
    conn = FakeConn()
    n = dao.add_global_comment_uids([1, 2, 2, 0, 3, None], conn=conn)
    assert n == 3  # 去重 + 剔除 0/None
    sqls = conn.inserted_sql()
    assert any("global_uids" in s for s in sqls)
    assert any("global_comment_uids" in s for s in sqls)
    # 去重后每表 3 行
    assert all(len(r) == 3 for m, _s, r in conn.calls if m == "executemany")


def test_add_global_users_skips_bad_rows_and_truncates_uname():
    conn = FakeConn()
    long_name = "x" * 300
    n = dao.add_global_users([(1, "ok"), (2, long_name), (0, "zero"), ("bad", "x")], conn=conn)
    assert n == 2  # uid=0 跳过；("bad","x") 不可转 int -> 跳过
    rows = conn.all_rows()
    assert rows[0] == (1, "ok")
    assert len(rows[1][1]) == 120  # uname 截断到 120


def test_add_global_comment_uids_empty_returns_zero():
    conn = FakeConn()
    assert dao.add_global_comment_uids([], conn=conn) == 0
    assert dao.add_global_comment_uids([0, None], conn=conn) == 0


def test_add_global_comment_uids_e_offline_returns_zero(monkeypatch):
    """E 归档库离线时应静默返回 0（不抛异常）。"""
    def _boom():
        raise dao.EArchiveOfflineError("E offline")

    monkeypatch.setattr(dao, "_r_strict", _boom)
    assert dao.add_global_comment_uids([1, 2, 3]) == 0


# ---------- 拆包契约 ----------

def test_dao_package_exposes_public_api():
    """dao 已由单文件拆为包；此测试守卫对外契约，防止重导出遗漏。

    所有外部调用（含 GUI/采集/分析）都走 `from storage import dao; dao.x()`，
    一旦某名字漏重导出，这里会立刻失败。
    """
    required = [
        # 写入路径
        "upsert_video", "upsert_danmaku", "upsert_comments", "upsert_users",
        "upsert_posts", "upsert_up_stat", "update_progress",
        # 全局去重直播喂数
        "add_global_comment_uids", "add_global_users",
        # 查询
        "query_comments_by_uid", "query_danmaku_by_uid", "query_user",
        "query_recent_videos", "query_comments_by_video", "query_danmaku_by_video",
        "find_video_by_bvid", "get_progress", "get_progress_batch",
        # 统计 / 榜单 / 趋势（自 rankings 子模块重导出）
        "get_collection_stats", "get_rankings",
        "get_daily_comment_counts", "get_hourly_comment_counts",
        # 搜索（自 search 子模块重导出）
        "parse_smart_query", "flatten_smart_query",
        "search_comments", "search_danmaku", "search_videos",
        "iter_search_comments", "iter_search_danmaku",
        # 基础设施 / 状态（自 _core 重导出，含 monkeypatch 目标）
        "_w", "_r", "_r_strict", "_executemany", "_rcached", "_drop_cached",
        "_shard_dbs", "_uid_same_dsn", "_SHARD_CACHE", "_tls", "logger",
        "EArchiveOfflineError", "StatsCancelled",
    ]
    missing = [n for n in required if not hasattr(dao, n)]
    assert not missing, f"dao 包缺少重导出: {missing}"


def test_search_and_rankings_live_in_submodules():
    """确认拆包落地：search/rankings 符号来自子模块（而非 __init__ 定义）。"""
    import storage.dao.rankings as rankings
    import storage.dao.search as search

    assert dao.get_rankings is rankings.get_rankings
    assert dao.search_comments is search.search_comments
    assert dao.get_hourly_comment_counts is rankings.get_hourly_comment_counts
