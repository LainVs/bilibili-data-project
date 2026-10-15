"""storage/dao 包关键写入路径单元测试（不连 MySQL）。

聚焦数据正确性风险最高的部分：
- upsert_* 的字段顺序 / 默认值 / 空输入短路
- 批大小分片（_WRITE_BATCH）
- 死锁(1213)自动重试
- 全局去重表直播喂数的去重与容错
"""
from __future__ import annotations

import threading

import pymysql
import pytest

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
        # 统计取消 / 中止控制（让“取消统计”能真正打断在途 SQL）
        "cancel_stats_queries", "_stats_should_abort",
        "_begin_stats_run", "_end_stats_run",
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


# ---------- 搜索取消 ----------

def test_search_comments_stop_event_breaks_between_shards(monkeypatch):
    """stop_event 置位后应在仓与仓之间停止，不再遍历后续 shard。"""
    import threading

    from storage.dao import search as _search

    hit: list[str] = []
    stop = threading.Event()

    def _fake_one(db, role, kw, start, end, exclude, limit, **kw2):
        hit.append(db)
        if db == "bilibili_data":
            stop.set()  # 首个仓（D 缓冲）查完即触发取消
        return []

    monkeypatch.setattr(_search, "_shard_dbs", lambda: ["bilibili_s1", "bilibili_s2"])
    monkeypatch.setattr(_search, "_uid_same_dsn", lambda: False)
    monkeypatch.setattr(_search, "_search_one_db", _fake_one)

    out = _search.search_comments("kw", limit=10, stop_event=stop)
    assert hit == ["bilibili_data"], f"应仅在 D 缓冲查一次，实际 {hit}"
    assert out.get("cancelled") is True


def test_search_comments_without_stop_event_scans_all(monkeypatch):
    """不传 stop_event 时行为不变：D + 各 shard 全部查询。"""
    from storage.dao import search as _search

    hit: list[str] = []

    def _fake_one(db, role, kw, start, end, exclude, limit, **kw2):
        hit.append(db)
        return []

    monkeypatch.setattr(_search, "_shard_dbs", lambda: ["bilibili_s1", "bilibili_s2"])
    monkeypatch.setattr(_search, "_uid_same_dsn", lambda: False)
    monkeypatch.setattr(_search, "_search_one_db", _fake_one)

    out = _search.search_comments("kw", limit=10)
    assert hit == ["bilibili_data", "bilibili_s1", "bilibili_s2"]
    assert "cancelled" not in out


def test_stats_cancel_closes_inflight_conns_and_raises():
    """取消统计：关闭在途连接 + 置位中止，使被阻塞的 SQL 立即抛错并中止。

    模拟 worker 持有两个连接（E 归档 + D 缓冲），主线程调用
    cancel_stats_queries() 后应关掉它们并让 _stats_should_abort 返回 True，
    从而在途查询恢复后下一次守卫即抛 StatsCancelled（不再傻等 read_timeout）。
    """
    import storage.dao as dao
    import storage.dao._core as _core

    closed: list[int] = []

    class _FakeConn:
        def __init__(self, n: int) -> None:
            self.n = n

        def close(self):
            closed.append(self.n)

    # 登记两个“在途”连接
    _core._begin_stats_run()
    try:
        _core._stats_conns.add(_FakeConn(1))
        _core._stats_conns.add(_FakeConn(2))
        assert _core._stats_should_abort(None) is False

        # 主线程触发取消
        dao.cancel_stats_queries()

        assert _core._stats_aborted is True
        assert sorted(closed) == [1, 2]
        # 守卫现在应当指示中止（无论 stop_event 是否置位）
        assert _core._stats_should_abort(None) is True
        assert _core._stats_should_abort(threading.Event()) is True
    finally:
        _core._end_stats_run()
        # 收尾后中止标记应被清除，避免污染下一次统计
        assert _core._stats_aborted is False


def test_stats_cancel_noop_when_idle():
    """空闲时调用取消不抛错；标记会在下次 begin/end 周期自愈，不污染后续统计。"""
    import storage.dao as dao
    import storage.dao._core as _core

    dao.cancel_stats_queries()
    assert _core._stats_should_abort(None) is True  # 置位立即生效
    # 下次统计开始即清零，保证不污染新一轮
    _core._begin_stats_run()
    _core._end_stats_run()
    assert _core._stats_aborted is False
    assert _core._stats_should_abort(None) is False



# ---------- 检索计划：密度自适应（idx_ctime 反扫 vs 全表扫）----------

@pytest.fixture(autouse=True)
def _clear_search_caches():
    """检索计划的三个进程内缓存（索引/行数/最新时间）跨用例会互相污染，逐个清干净。"""
    from storage.dao import search as _search

    caches = (_search._IDX_CACHE, _search._ROWS_CACHE, _search._MAXCTIME_CACHE)
    for c in caches:
        c.clear()
    yield
    for c in caches:
        c.clear()


class _PlanCursor:
    """假游标：按 SQL 特征返回「有没有索引」「探测命中几行」。"""

    def __init__(self, has_index: bool = True, probe_hits: int = 0) -> None:
        self.has_index = has_index
        self.probe_hits = probe_hits
        self.sqls: list[str] = []

    def execute(self, sql, args=None):
        self.sqls.append(sql)
        return 1

    def fetchone(self):
        sql = self.sqls[-1]
        if "information_schema.STATISTICS" in sql:
            return (1,) if self.has_index else None
        if "COUNT(*)" in sql:
            return {"n": self.probe_hits}
        return None


def _plan(cur, limit, total=11_441_092):
    """跑一次 _plan_hint，total 行数直接桩掉（避免真连库）。"""
    from storage.dao import search as _search

    orig = _search._table_rows
    _search._table_rows = lambda db, role: total
    try:
        return _search._plan_hint(cur, "bilibili_x", "read", [["kw"]], "", [], "", [], limit, 0)
    finally:
        _search._table_rows = orig


def test_plan_hint_no_index_returns_empty():
    """仓里没有 idx_ctime 时不给提示词（MySQL 也不能用不存在的索引）。"""
    assert _plan(_PlanCursor(has_index=False, probe_hits=500), 100) == ""


def test_plan_hint_zero_hits_prefers_full_scan():
    """采样零命中 → 索引反扫会走到表尾（实测慢 3.9 倍），必须压成全表扫。"""
    assert _plan(_PlanCursor(probe_hits=0), 100) == " IGNORE INDEX(idx_ctime)"


def test_plan_hint_dense_prefers_index():
    """稠密词（2000/20000=10%）：凑 100 条只需走 1000 行，走索引早停。"""
    assert _plan(_PlanCursor(probe_hits=2000), 100) == " FORCE INDEX(idx_ctime)"


def test_plan_hint_sparse_with_huge_limit_prefers_full_scan():
    """稀疏词 + 巨大 limit：预期要走全表 25% 以上，全表扫更划算。"""
    assert _plan(_PlanCursor(probe_hits=100), 100_000) == " IGNORE INDEX(idx_ctime)"


def test_plan_hint_unlimited_returns_empty():
    """无限条数（导出全量）必须扫全部行，不给提示词。"""
    assert _plan(_PlanCursor(probe_hits=2000), 0) == ""


def test_probe_hits_builds_forced_index_probe():
    """探测 SQL 必须 FORCE INDEX 反扫 idx_ctime，且把关键词参数放在正确位置。"""
    from storage.dao import search as _search

    cur = _PlanCursor(probe_hits=7)
    n = _search._probe_hits(cur, "bilibili_x", [["残影"]], " AND c.ctime <= %s",
                            ["2026-10-01"], "", [], 0)
    assert n == 7
    sql = cur.sqls[-1]
    assert f"FORCE INDEX({_search._CTIME_IDX})" in sql
    assert "ORDER BY c.ctime DESC LIMIT %s" in sql
    assert "x.content LIKE %s" in sql


# ---------- 分仓排序 + 提前终止 ----------

def test_order_shards_fresh_first_sorts_newest_first(monkeypatch):
    """分仓按最新评论时间倒序；时间未知的排最后且保持原相对顺序。"""
    from storage.dao import search as _search

    table = {
        "bilibili_old": (True, "2026-09-01 00:00:00"),
        "bilibili_new": (True, "2026-10-09 00:00:00"),
        "bilibili_mid": (True, "2026-09-20 00:00:00"),
        "bilibili_unknown": (False, None),
    }
    monkeypatch.setattr(_search, "_shard_max_ctime", lambda db: table[db])
    got = _search._order_shards_fresh_first(list(table))
    assert got == ["bilibili_new", "bilibili_mid", "bilibili_old", "bilibili_unknown"]


def _rows(n, ctime="2026-10-01 00:00:00"):
    return [{"rpid": i, "ctime": ctime} for i in range(n)]


def test_can_stop_early_true_when_remaining_all_older(monkeypatch):
    from storage.dao import search as _search

    monkeypatch.setattr(_search, "_shard_max_ctime", lambda db: (True, "2026-09-01 00:00:00"))
    assert _search._can_stop_early(_rows(10), 10, ["bilibili_old"], False) is True


def test_can_stop_early_false_when_remaining_newer(monkeypatch):
    from storage.dao import search as _search

    monkeypatch.setattr(_search, "_shard_max_ctime", lambda db: (True, "2026-12-01 00:00:00"))
    assert _search._can_stop_early(_rows(10), 10, ["bilibili_x"], False) is False


def test_can_stop_early_false_when_unknown(monkeypatch):
    """时间未知时保守不提前停（宁可多扫，不可漏结果）。"""
    from storage.dao import search as _search

    monkeypatch.setattr(_search, "_shard_max_ctime", lambda db: (False, None))
    assert _search._can_stop_early(_rows(10), 10, ["bilibili_x"], False) is False


def test_can_stop_early_false_when_not_enough_rows_or_unlimited():
    from storage.dao import search as _search

    assert _search._can_stop_early(_rows(3), 10, ["bilibili_x"], False) is False
    assert _search._can_stop_early(_rows(10), 10, ["bilibili_x"], True) is False
    assert _search._can_stop_early(_rows(10), 10, [], False) is False


def test_search_comments_stops_early_on_dense_keyword(monkeypatch):
    """稠密词：D 缓冲 + 最新一仓就凑满 limit，且剩余仓更旧 → 不再扫后面的仓。"""
    from storage.dao import search as _search

    hit: list[str] = []

    def _fake_one(db, role, kw, start, end, exclude, limit, **kw2):
        hit.append(db)
        return [{"rpid": f"{db}-{i}", "ctime": "2026-10-09 00:00:00"} for i in range(limit)]

    maxes = {"bilibili_new": (True, "2026-10-09 00:00:00"),
             "bilibili_old": (True, "2026-09-01 00:00:00")}

    monkeypatch.setattr(_search, "_shard_dbs", lambda: ["bilibili_new", "bilibili_old"])
    monkeypatch.setattr(_search, "_uid_same_dsn", lambda: False)
    monkeypatch.setattr(_search, "_search_one_db", _fake_one)
    monkeypatch.setattr(_search, "_shard_max_ctime", lambda db: maxes[db])

    out = _search.search_comments("的", limit=10)
    assert hit == ["bilibili_data", "bilibili_new"], f"应提前结束，实际 {hit}"
    assert len(out["rows"]) == 10
    assert out["total"] is None  # 已截断：语义与「还有更多」一致


def test_search_comments_scans_all_when_keyword_is_sparse(monkeypatch):
    """稀疏词：凑不满 limit 时一个仓都不能少。"""
    from storage.dao import search as _search

    hit: list[str] = []

    def _fake_one(db, role, kw, start, end, exclude, limit, **kw2):
        hit.append(db)
        return []

    monkeypatch.setattr(_search, "_shard_dbs", lambda: ["bilibili_new", "bilibili_old"])
    monkeypatch.setattr(_search, "_uid_same_dsn", lambda: False)
    monkeypatch.setattr(_search, "_search_one_db", _fake_one)
    monkeypatch.setattr(_search, "_shard_max_ctime", lambda db: (True, "2026-10-09 00:00:00"))

    _search.search_comments("量子引力泡沫", limit=10)
    assert "bilibili_old" in hit


def test_has_ctime_index_cache_is_role_keyed():
    """`bilibili_data` 在 D(写缓冲) 与 E(主库) 是两个物理库，缓存必须按 role 分开。"""

    class _C:
        def __init__(self, ok):
            self.ok = ok
            self.sqls = []

        def execute(self, sql, args=None):
            self.sqls.append(sql)

        def fetchone(self):
            return (1,) if self.ok else None

    from storage.dao import search as _search

    assert _search._has_ctime_index(_C(True), "bilibili_data", "write") is True
    assert _search._has_ctime_index(_C(False), "bilibili_data", "read") is False
    # 写库角色的缓存不能被读库角色覆盖
    assert _search._has_ctime_index(_C(False), "bilibili_data", "write") is True
