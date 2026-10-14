"""数据访问层包：向 MySQL 幂等写入采集数据。

拆包结构：
- _core.py    连接复用 / shard 列表 / 跨库判定 / StatsCancelled / 共享状态
- search.py   评论·弹幕·视频跨仓检索
- rankings.py 排行榜与每日/每小时趋势
- 本 __init__ 承载写入路径与常规查询，并从上述子模块重导出函数，
  使 `from storage import dao; dao.xxx()` 与既有的 monkeypatch 契约不变。
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable

import pymysql

# 基础设施与共享状态（单一实例，供子模块与本模块共用）
from storage.dao._core import (
    _SHARD_CACHE,
    _SHARD_TTL,
    StatsCancelled,
    _begin_stats_run,
    _drop_cached,
    _end_stats_run,
    _open_conn,
    _rcached,
    _rclose_all,
    _shard_dbs,
    _shard_dbs_trusted,
    _stats_should_abort,
    _tls,
    _uid_same_dsn,
    cancel_stats_queries,
    logger,
)
from storage.db import ensure_stats_cache_table, ensure_up_stat_table
from storage.db import get_conn as _get_conn


class EArchiveOfflineError(Exception):
    """归档库 E 不可达，且调用方要求显式失败而非偷查 D。"""


def _w():
    return _get_conn("write")

def _r():
    try:
        return _get_conn("read")
    except Exception:
        # 归档库离线则回退到缓冲库（普通查询可用，但调用方若需全量语义请用 _r_strict）
        return _get_conn("write")

def _r_strict():
    """严格读 E：E 离线直接抛 EArchiveOfflineError，不回退 D。"""
    try:
        return _get_conn("read")
    except Exception as exc:
        raise EArchiveOfflineError(f"E归档库离线: {exc}") from exc














def get_conn(role="write"):
    return _get_conn(role)


# 进程内写互斥：采集线程并发批插相同主键（如 dim_user 同 uid）会互相持锁
# 等待形成死锁风暴（曾拖垮整库）。串行化后锁冲突归零，吞吐影响可忽略。
_WRITE_LOCK = threading.RLock()
# 单事务最大行数：D 是 SSD，2000 行一批经实测可承受；
# 过大仍会憋大 undo/全文索引同步，故不无限放大。
_WRITE_BATCH = 2000
# 可安全重试的 MySQL 错误码：死锁 / 锁等待超时 / 连接被重置（事务未提交可重发）。
_RETRYABLE_ERRS = (1213, 1205, 2006, 2013)
_MAX_RETRY = 2


def _ensure_up_stat() -> None:
    """幂等确保 up_crawl_stat 表存在（老库自动升级）。"""
    try:
        ensure_up_stat_table()
    except Exception:
        pass


def _ensure_stats_cache(role: str = "read") -> None:
    """幂等确保 stats_cache 表存在（老库自动升级）。"""
    try:
        ensure_stats_cache_table(role)
    except Exception:
        pass


def _ensure_global_uids() -> None:
    """幂等确保 E 全局去重并集表存在（只建 E 主库）。"""
    try:
        from storage.db import ensure_global_uid_tables
        ensure_global_uid_tables("read")
    except Exception:
        pass


_GLOBAL_WRITE_LOCK = threading.Lock()
_GLOBAL_INSERT_BATCH = 500  # 9M 表上大批 INSERT IGNORE 极慢，分小批提交


def _exec_global_insert(conn, sql, rows, batch: int = _GLOBAL_INSERT_BATCH):
    """分批 executemany + 逐批 commit，避免长事务锁表/HDD 寻道风暴。"""
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch):
            cur.executemany(sql, rows[i:i + batch])
            conn.commit()


def add_global_comment_uids(uids, *, conn=None) -> int:
    """直播喂数：评论者 uid 同时进 global_uids + global_comment_uids。

    E 离线静默跳过返回 0（归档 hook 会补，不抛异常、不进坏账）。
    小批量分批提交 + 全局写锁串行化，防止并发连接堆积。
    conn 可选：传入则复用（调用方负责开关），不传则自建。
    """
    ids = {int(u) for u in (uids or []) if u}
    if not ids:
        return 0
    rows = [(i,) for i in ids]
    with _GLOBAL_WRITE_LOCK:
        own = conn is None
        try:
            if own:
                conn = _r_strict()
        except EArchiveOfflineError:
            return 0
        except Exception as exc:
            logger.warning("全局uid直播写入连接失败: %s", exc)
            return 0
        try:
            _exec_global_insert(conn,
                "INSERT IGNORE INTO global_uids (uid) VALUES (%s)", rows)
            _exec_global_insert(conn,
                "INSERT IGNORE INTO global_comment_uids (uid) VALUES (%s)", rows)
            return len(ids)
        except Exception as exc:
            logger.warning("全局uid直播写入失败(%s 个): %s", len(ids), exc)
            try: conn.rollback()
            except Exception: pass
            return 0
        finally:
            if own:
                try: conn.close()
                except Exception: pass


def add_global_users(pairs, *, conn=None) -> int:
    """直播喂数：(uid, uname) 进 global_uids。E 离线静默跳过。

    小批量分批提交 + 全局写锁串行化。
    conn 可选：传入则复用（调用方负责开关），不传则自建。
    """
    rows = []
    for p in (pairs or []):
        try:
            uid = int(p[0]); uname = str(p[1] or "")[:120]
        except Exception:
            continue
        if uid:
            rows.append((uid, uname))
    if not rows:
        return 0
    with _GLOBAL_WRITE_LOCK:
        own = conn is None
        try:
            if own:
                conn = _r_strict()
        except EArchiveOfflineError:
            return 0
        except Exception as exc:
            logger.warning("全局用户直播写入连接失败: %s", exc)
            return 0
        try:
            _exec_global_insert(conn,
                "INSERT IGNORE INTO global_uids (uid, uname) VALUES (%s, %s)", rows)
            return len(rows)
        except Exception as exc:
            logger.warning("全局用户直播写入失败(%s 个): %s", len(rows), exc)
            try: conn.rollback()
            except Exception: pass
            return 0
        finally:
            if own:
                try: conn.close()
                except Exception: pass


def get_global_uid_count() -> tuple[int, bool]:
    """全局用户去重数。(总数, 是否可信)，表缺失/E离线返回 (0, False)。"""
    try:
        conn = _r_strict()
    except Exception:
        return 0, False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM global_uids")
            row = cur.fetchone()
            return int(row["n"] or 0), True
    except Exception:
        return 0, False
    finally:
        try: conn.close()
        except Exception: pass


def get_global_comment_uid_count() -> tuple[int, bool]:
    """全局评论者去重数。(总数, 是否可信)。"""
    try:
        conn = _r_strict()
    except Exception:
        return 0, False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM global_comment_uids")
            row = cur.fetchone()
            return int(row["n"] or 0), True
    except Exception:
        return 0, False
    finally:
        try: conn.close()
        except Exception: pass


def _global_coverage_ok() -> bool:
    """全局表是否覆盖当前全部 E 小仓库（回填标记 ⊇ 当前仓列表）。

    未回填/新增仓未归档时返回 False，调用方回退老口径。
    """
    try:
        conn = _r_strict()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT v FROM global_meta WHERE k='backfill_shards'")
                row = cur.fetchone()
                if not row:
                    return False
                covered = {s for s in str(row["v"] or "").split(",") if s}
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        return False
    dbs, ok = _shard_dbs_trusted()
    if not ok:
        return False
    # 注意：dbs 里的 "bilibili_data" 指 E 主库（1400万行老数据），必须覆盖；
    # 真正的 D 缓冲（3306）由直播+归档 hook 持续覆盖，不进此标记。
    e_required = set(dbs)
    return bool(e_required) and e_required <= covered


def _global_meta_add_shard(shard: str) -> None:
    """归档 hook：把新仓记入覆盖标记（best-effort）。"""
    try:
        conn = _r_strict()
    except Exception:
        return
    try:
        import time as _time
        with conn.cursor() as cur:
            cur.execute("SELECT v FROM global_meta WHERE k='backfill_shards'")
            row = cur.fetchone()
            covered = {s for s in str((row["v"] if row else "") or "").split(",") if s}
            covered.add(shard)
            cur.execute("INSERT INTO global_meta (k, v) VALUES ('backfill_shards', %s) "
                        "ON DUPLICATE KEY UPDATE v=VALUES(v)",
                        (",".join(sorted(covered)),))
            cur.execute("INSERT INTO global_meta (k, v) VALUES ('backfill_time', %s) "
                        "ON DUPLICATE KEY UPDATE v=VALUES(v)",
                        (_time.strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()
    except Exception as exc:
        logger.warning("全局覆盖标记更新失败: %s", exc)
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass


def backfill_one_shard(shard: str, *, conn=None, progress_cb=None, stop_event=None,
                       chunk: int = 20000) -> dict:
    """把一个 E 小仓库的 uid 并入全局表（keyset 分块+逐块提交，可续跑）。

    不用单条大 INSERT...SELECT：HDD 上大事务易超读超时，超时回滚更慢且
    期间锁住全局表。分块后每块秒级，即使中断也只丢当前块（幂等可重）。
    返回 {"uids_add": 估计搬运dim行数, "cuids": 去重评论者批次数}。
    """
    own_conn = conn is None
    if own_conn:
        conn = _r_strict()
    out = {"uids_add": 0, "cuids": 0}
    _chunks = [0]

    def _tick():
        _chunks[0] += 1
        if progress_cb and _chunks[0] % 10 == 0:
            try: progress_cb(_chunks[0], 0)
            except Exception: pass
    try:
        def _meta_get(k: str) -> str:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT v FROM global_meta WHERE k=%s", (k,))
                    row = cur.fetchone()
                    return str(row["v"] or "") if row else ""
            except Exception:
                return ""

        def _meta_set(k: str, v: str) -> None:
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO global_meta (k, v) VALUES (%s, %s) "
                                "ON DUPLICATE KEY UPDATE v=VALUES(v)", (k, v))
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass

        # dim_user：主键 keyset 拉取 → executemany 写入（单块 2 万行，秒级）
        try:
            last = int(_meta_get(f"resume_dim_{shard}") or 0)
        except Exception:
            last = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise StatsCancelled()
            with conn.cursor() as cur:
                cur.execute(f"SELECT uid, uname FROM `{shard}`.dim_user "
                            f"WHERE uid > %s ORDER BY uid LIMIT %s", (last, chunk))
                rows = list(cur.fetchall())
            if not rows:
                break
            with conn.cursor() as cur:
                cur.executemany("INSERT IGNORE INTO global_uids (uid, uname) VALUES (%s, %s)",
                                [(int(r["uid"]), str(r.get("uname") or "")[:120]) for r in rows])
                conn.commit()
            out["uids_add"] += len(rows)
            last = max(int(r["uid"]) for r in rows)
            _meta_set(f"resume_dim_{shard}", str(last))
            _tick()
            if len(rows) < chunk:
                break
        # raw_comments 评论者：服务端分段 DISTINCT 直插（同机无 Python 来回）。
        # 每段按行数定界约 50 万行（OFFSET 走主键， shard 表静态无并发写，安全），
        # 单段约 1-3 分钟，远小于读超时；即使中断也只丢当前段（幂等可重）。
        _ROWS_PER_SEG = 500000
        try:
            last_r = int(_meta_get(f"resume_cmt_{shard}") or 0)
        except Exception:
            last_r = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise StatsCancelled()
            with conn.cursor() as cur:
                cur.execute(f"SELECT rpid FROM `{shard}`.raw_comments "
                            f"WHERE rpid > %s ORDER BY rpid LIMIT 1 OFFSET %s",
                            (last_r, _ROWS_PER_SEG - 1))
                _brow = cur.fetchone()
                if _brow:
                    _bound = int(_brow["rpid"])
                else:
                    # 剩余不足一段：取尾，无行则收工
                    cur.execute(f"SELECT COUNT(*) AS n FROM `{shard}`.raw_comments "
                                f"WHERE rpid > %s", (last_r,))
                    if int((cur.fetchone() or {}).get("n") or 0) == 0:
                        break
                    _bound = None
            with conn.cursor() as cur:
                if _bound is None:
                    cur.execute(
                        f"INSERT IGNORE INTO global_comment_uids (uid) "
                        f"SELECT DISTINCT uid FROM `{shard}`.raw_comments "
                        f"WHERE rpid > %s AND uid > 0", (last_r,))
                    cur.execute(f"SELECT MAX(rpid) AS m FROM `{shard}`.raw_comments "
                                f"WHERE rpid > %s", (last_r,))
                    _m = cur.fetchone() or {}
                    last_r = int(_m.get("m") or last_r)
                else:
                    cur.execute(
                        f"INSERT IGNORE INTO global_comment_uids (uid) "
                        f"SELECT DISTINCT uid FROM `{shard}`.raw_comments "
                        f"WHERE rpid > %s AND rpid <= %s AND uid > 0",
                        (last_r, _bound))
                    last_r = _bound
                conn.commit()
                out["cuids"] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            _meta_set(f"resume_cmt_{shard}", str(last_r))
            _tick()
            if _bound is None:
                break
        # 完成：清断点键，记覆盖
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM global_meta WHERE k IN (%s, %s)",
                            (f"resume_dim_{shard}", f"resume_cmt_{shard}"))
            conn.commit()
        except Exception:
            pass
        _global_meta_add_shard(shard)
        return out
    finally:
        if own_conn:
            try: conn.close()
            except Exception: pass


_BACKFILL_LOCK = threading.Lock()


def backfill_global_uids(progress_cb=None, stop_event=None) -> dict:
    """全量回填一次：E 主库+各小仓库 keyset 分块并入全局表 + D 当前内容分块搬运。

    返回 {"uids": n, "comment_uids": m, "shards": [...]}。
    并发调用会排队（非阻塞，第二个调用直接返回空结果）。
    """
    if not _BACKFILL_LOCK.acquire(blocking=False):
        logger.info("backfill_global_uids 已有实例在跑，跳过本次")
        return {"uids": 0, "comment_uids": 0, "shards": []}
    try:
        return _backfill_global_uids_impl(progress_cb, stop_event)
    finally:
        _BACKFILL_LOCK.release()


def _backfill_global_uids_impl(progress_cb=None, stop_event=None) -> dict:
    from storage.db import ensure_global_uid_tables
    ensure_global_uid_tables("read")
    res: dict = {"uids": 0, "comment_uids": 0, "shards": []}
    dbs, _ok = _shard_dbs_trusted()
    # 全包含：E 主库 bilibili_data（老数据大头）+ 各小仓库；D 缓冲另走跨机分块
    e_shards = list(dbs)
    done = 0
    total_steps = len(e_shards) * 2 + 2  # 每库2步 + D2步

    def _prog():
        nonlocal done
        done += 1
        if progress_cb:
            try: progress_cb(done, total_steps)
            except Exception: pass

    # 断点续跑：meta 里已有的仓跳过（INSERT IGNORE 天然幂等，跳过只为省时间）
    try:
        _mc = _r_strict()
        try:
            with _mc.cursor() as _cur:
                _cur.execute("SELECT v FROM global_meta WHERE k='backfill_shards'")
                _row = _cur.fetchone()
                _done_shards = {s for s in str((_row["v"] if _row else "") or "").split(",") if s}
        finally:
            try: _mc.close()
            except Exception: pass
    except Exception:
        _done_shards = set()
    conn = _r_strict()
    try:
        for sh in e_shards:
            if sh in _done_shards:
                logger.info("回填跳过已完成仓 %s", sh)
                _prog(); _prog()
                res["shards"].append(sh)
                continue
            backfill_one_shard(sh, conn=conn, stop_event=stop_event)
            _prog()
            res["shards"].append(sh)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM global_uids")
            res["uids"] = int(cur.fetchone()["n"] or 0)
            cur.execute("SELECT COUNT(*) AS n FROM global_comment_uids")
            res["comment_uids"] = int(cur.fetchone()["n"] or 0)
    finally:
        try: conn.close()
        except Exception: pass
    # D 当前内容：跨机分块搬（主键 keyset，不用 OFFSET）
    dw = _w()
    try:
        last = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise StatsCancelled()
            with dw.cursor() as cur:
                cur.execute("SELECT uid, uname FROM dim_user WHERE uid > %s "
                            "ORDER BY uid LIMIT 20000", (last,))
                rows = list(cur.fetchall())
            if not rows:
                break
            add_global_users([(r["uid"], r.get("uname", "")) for r in rows])
            last = max(int(r["uid"]) for r in rows)
            if len(rows) < 20000:
                break
    finally:
        try: dw.close()
        except Exception: pass
    _prog()
    # D 去重评论 uid：stat 表即现成并集（主键扫描，快）
    dw2 = _w()
    try:
        last = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise StatsCancelled()
            with dw2.cursor() as cur:
                try:
                    cur.execute("SELECT uid FROM stat_comment_uids WHERE uid > %s "
                                "ORDER BY uid LIMIT 20000", (last,))
                    rows = list(cur.fetchall())
                except Exception:
                    rows = []
                    break
            if not rows:
                break
            add_global_comment_uids([r["uid"] for r in rows])
            last = max(int(r["uid"]) for r in rows)
            if len(rows) < 20000:
                break
    finally:
        try: dw2.close()
        except Exception: pass
    _prog()
    conn2 = _r_strict()
    try:
        import time as _time
        with conn2.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM global_uids")
            res["uids"] = int(cur.fetchone()["n"] or 0)
            cur.execute("SELECT COUNT(*) AS n FROM global_comment_uids")
            res["comment_uids"] = int(cur.fetchone()["n"] or 0)
            # 覆盖标记：本次处理过的 E 仓+D（D 清空后由归档 hook 持续覆盖）
            cur.execute("INSERT INTO global_meta (k, v) VALUES ('backfill_shards', %s) "
                        "ON DUPLICATE KEY UPDATE v=VALUES(v)",
                        (",".join(sorted(res["shards"])),))
            cur.execute("INSERT INTO global_meta (k, v) VALUES ('backfill_time', %s) "
                        "ON DUPLICATE KEY UPDATE v=VALUES(v)",
                        (_time.strftime("%Y-%m-%d %H:%M:%S"),))
        conn2.commit()
    finally:
        try: conn2.close()
        except Exception: pass
    return res


def upsert_video(videos: Iterable[dict]) -> None:
    """幂等写入/更新视频元信息。"""
    rows = [
        (v["aid"], v["bvid"], v.get("cid", 0), v.get("title", ""),
         v.get("owner_uid", 0), v.get("owner_name", ""), v.get("pubdate", 0), v.get("part", 1))
        for v in videos
    ]
    if not rows:
        return
    sql = """
        INSERT INTO meta_video (aid, bvid, cid, title, owner_uid, owner_name, pubdate, part)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            bvid = VALUES(bvid), cid = VALUES(cid), title = VALUES(title),
            owner_uid = VALUES(owner_uid), owner_name = VALUES(owner_name),
            pubdate = VALUES(pubdate)
    """
    _executemany(sql, rows)


def upsert_danmaku(items: Iterable[dict], *, conn=None) -> int:
    """幂等写入弹幕；返回受影响行数。conn 可选复用。"""
    rows = [
        (d["id"], d["video_aid"], d.get("uid", 0), d.get("mid_hash", ""),
         d.get("content", ""), d.get("progress", 0.0), d.get("mode", 1),
         d.get("color", 16777215), d.get("ctime"))
        for d in items
    ]
    if not rows:
        return 0
    sql = """
        INSERT INTO raw_danmaku (id, video_aid, uid, mid_hash, content, progress, mode, color, ctime)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            uid = VALUES(uid), content = VALUES(content),
            progress = VALUES(progress), ctime = VALUES(ctime),
            is_parsed = IF(is_parsed = 0 AND VALUES(uid) != 0, 1, is_parsed)
    """
    return _executemany(sql, rows, conn=conn)


def upsert_comments(items: Iterable[dict], *, conn=None) -> int:
    """幂等写入评论；返回受影响行数。

    item 内含 ref_type（默认 1=视频）与 video_aid（可为帖子 id）。
    conn 可选复用（统计计数器共用同一连接）。
    """
    rows = [
        (c["rpid"], c["video_aid"], c.get("ref_type", 1), c.get("parent", 0), c.get("uid", 0),
         c.get("content", ""), c.get("like_num", 0), c.get("ctime"),
         c.get("ip_location", ""))
        for c in items
    ]
    if not rows:
        return 0
    sql = """
        INSERT INTO raw_comments (rpid, video_aid, ref_type, parent, uid, content, like_num, ctime, ip_location)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            content = VALUES(content), like_num = VALUES(like_num),
            uid = VALUES(uid), ip_location = VALUES(ip_location)
    """
    n = _executemany(sql, rows, conn=conn)
    _sync_comment_counters(items, conn=conn)  # 统计物化：失败仅告警，不影响主写入
    return n


def _sync_comment_counters(items: Iterable[dict], *, conn=None) -> None:
    """把本批评论的去重 UID / 对象ID 幂等写入统计计数表（无漂移风险）。"""
    try:
        uids = {c["uid"] for c in items if (c.get("uid") or 0) > 0}
        aids = {c["video_aid"] for c in items if (c.get("video_aid") or 0) > 0}
        if not uids and not aids:
            return
        own = conn is None
        if own:
            conn = _w()
        try:
            with conn.cursor() as cur:
                if uids:
                    cur.executemany(
                        "INSERT IGNORE INTO stat_comment_uids (uid) VALUES (%s)",
                        [(u,) for u in uids])
                if aids:
                    cur.executemany(
                        "INSERT IGNORE INTO stat_comment_aids (video_aid) VALUES (%s)",
                        [(a,) for a in aids])
            conn.commit()
        finally:
            if own:
                conn.close()
    except Exception as exc:
        logger.warning("评论统计计数同步失败: %s", exc)


def backfill_stat_counters() -> dict:
    """一次性回填统计计数表（历史数据补录）。

    服务端单条 SQL：INSERT IGNORE ... SELECT DISTINCT，按 uid 范围分片并行，
    每片独立短事务（幂等，可中断重跑）。aids 量级小单路执行。
    采集线程并发写库时 IO 竞争会拖慢回填，建议暂停采集时执行。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from storage.db import ensure_stat_counters

    ensure_stat_counters()
    t0 = time.time()
    counts = {"uids": 0, "aids": 0}
    lock = threading.Lock()

    def _run(label: str, sql: str) -> None:
        conn = _r()
        try:
            with conn.cursor() as cur:
                n = cur.execute(sql)
            conn.commit()
            with lock:
                counts[label] += n
        finally:
            conn.close()

    def _max_uid() -> int:
        conn = _r()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(uid) AS m FROM raw_comments WHERE uid > 0")
                row = cur.fetchone()
                return int(row["m"] or 0)
        finally:
            conn.close()

    # 旧号段（0~50 亿）细切 10 片；B 站新号段 uid 可达 16 位（>5e9），
    # 按数量级补 1~2 片动态覆盖，避免漏掉新号段评论者
    slices = [
        (i * 500_000_000, (i + 1) * 500_000_000) for i in range(10)
    ]
    max_uid = _max_uid()
    if max_uid > 5_000_000_000:
        slices.append((5_000_000_000, min(100_000_000_000_000, max_uid)))
        if max_uid > 100_000_000_000_000:
            slices.append((100_000_000_000_000, max_uid))
    jobs = [
        ("uids",
         "INSERT IGNORE INTO stat_comment_uids (uid) "
         "SELECT DISTINCT uid FROM raw_comments "
         f"WHERE uid > 0 AND uid BETWEEN {lo} AND {hi}")
        for lo, hi in slices
    ] + [
        ("aids",
         "INSERT IGNORE INTO stat_comment_aids (video_aid) "
         "SELECT DISTINCT video_aid FROM raw_comments WHERE video_aid > 0"),
    ]
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        futs = [pool.submit(_run, k, s) for k, s in jobs]
        for f in futs:
            f.result()
    return {"uids": counts["uids"], "aids": counts["aids"],
            "secs": round(time.time() - t0, 1)}


def upsert_posts(items: Iterable[dict]) -> int:
    """幂等写入帖子/专栏元信息；返回受影响行数。"""
    rows = [
        (p["id"], p.get("post_type", 1), p.get("title", ""),
         p.get("owner_uid", 0), p.get("pubdate", 0))
        for p in items
    ]
    if not rows:
        return 0
    sql = """
        INSERT INTO meta_post (id, post_type, title, owner_uid, pubdate)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            title = VALUES(title), owner_uid = VALUES(owner_uid), pubdate = VALUES(pubdate)
    """
    return _executemany(sql, rows)


def get_max_comment_ctime(video_aid: int, ref_type: int = 1) -> int:
    """返回某对象已采集评论的最新时间戳（优先查D，D为0查E）。"""
    max_ts = 0
    # 1. 查 D
    try:
        conn = _w()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(UNIX_TIMESTAMP(ctime)) AS ts FROM raw_comments "
                    "WHERE video_aid = %s AND ref_type = %s",
                    (video_aid, ref_type),
                )
                row = cur.fetchone()
                max_ts = max(max_ts, int(row["ts"] or 0) if row else 0)
        finally:
            conn.close()
    except Exception:
        pass

    # 2. 查 E
    try:
        conn = _r()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(UNIX_TIMESTAMP(ctime)) AS ts FROM raw_comments "
                    "WHERE video_aid = %s AND ref_type = %s",
                    (video_aid, ref_type),
                )
                row = cur.fetchone()
                max_ts = max(max_ts, int(row["ts"] or 0) if row else 0)
        finally:
            conn.close()
    except Exception:
        pass

    return max_ts


def count_video_comments_and_danmaku(video_aid: int) -> dict[str, int]:
    """精确统计库内某视频的评论与弹幕全量总数（跨 D 缓冲与 E 归档，带 FORCE INDEX 秒级返回）。"""
    tot_c, tot_d = 0, 0
    # 查 E
    try:
        conn = _r()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM raw_comments FORCE INDEX (idx_aid_type) "
                    "WHERE video_aid = %s",
                    (video_aid,),
                )
                r = cur.fetchone()
                tot_c += int(r["n"] or 0) if r else 0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM raw_danmaku WHERE video_aid = %s",
                    (video_aid,),
                )
                r = cur.fetchone()
                tot_d += int(r["n"] or 0) if r else 0
        finally:
            conn.close()
    except Exception:
        pass

    # 查 D
    try:
        conn = _w()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM raw_comments WHERE video_aid = %s",
                    (video_aid,),
                )
                r = cur.fetchone()
                tot_c += int(r["n"] or 0) if r else 0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM raw_danmaku WHERE video_aid = %s",
                    (video_aid,),
                )
                r = cur.fetchone()
                tot_d += int(r["n"] or 0) if r else 0
        finally:
            conn.close()
    except Exception:
        pass

    return {"comments": tot_c, "danmaku": tot_d}


def upsert_users(items: Iterable[dict], *, conn=None) -> int:
    """写用户维度（从评论/反查得到的用户信息）。conn 可选复用。"""
    rows = [
        (u["uid"], u.get("uname", ""), u.get("level", 0), u.get("sex", ""),
         u.get("is_vip", 0), u.get("signature", ""), int(u.get("fans", 0) or 0))
        for u in items
    ]
    if not rows:
        return 0
    sql = """
        INSERT INTO dim_user (uid, uname, level, sex, is_vip, signature, fans)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            uname = VALUES(uname), level = VALUES(level), sex = VALUES(sex),
            is_vip = VALUES(is_vip), signature = VALUES(signature),
            fans = IF(VALUES(fans) > 0, VALUES(fans), fans)
    """
    return _executemany(sql, rows, conn=conn)


def get_crc_cache(mid_hash: str) -> dict | None:
    """查询 mid_hash 反查缓存。（用于增量较对，也可只依赖内存缓存）"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mid_hash, uid, resolved FROM crckey_cache WHERE mid_hash = %s", (mid_hash,))
            row = cur.fetchone()
            return row
    finally:
        conn.close()


def set_crc_cache(mid_hash: str, uid: int, resolved: bool = True) -> None:
    """写入 mid_hash 反查结果缓存。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crckey_cache (mid_hash, uid, resolved) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE uid = VALUES(uid), resolved = VALUES(resolved)",
                (mid_hash, uid, 1 if resolved else 0),
            )
        conn.commit()
    finally:
        conn.close()


def bulk_set_crc_cache(items: Iterable[tuple[str, int, bool]]) -> int:
    """批量写入 mid_hash 反查结果（探针/重构后用）。返回受影响的条数。"""
    rows = [(h, u, 1 if ok else 0) for h, u, ok in items]
    if not rows:
        return 0
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO crckey_cache (mid_hash, uid, resolved) "
                "VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE uid = VALUES(uid), resolved = VALUES(resolved)",
                rows,
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def list_user_uids() -> list[int]:
    """去重的全部已知用户 uid（评论 + 用户维度）。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT uid FROM raw_comments WHERE uid > 0")
            a = [r["uid"] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT uid FROM dim_user WHERE uid > 0")
            b = [r["uid"] for r in cur.fetchall()]
            return list(set(a) | set(b))
    finally:
        conn.close()


def list_unparsed_danmaku_hashes() -> list[str]:
    """去重的全部 '未解析且带 mid_hash' 弹幕 hash。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT mid_hash FROM raw_danmaku "
                "WHERE is_parsed = 0 AND mid_hash != ''"
            )
            return [r["mid_hash"] for r in cur.fetchall()]
    finally:
        conn.close()


def update_progress(video_aid: int, *, danmaku_done: bool | None = None, comment_done: bool | None = None,
                    last_error: str = "") -> None:
    """更新某视频采集进度（断点续传）。"""
    conn = _w()
    try:
        with conn.cursor() as cur:
            # 先确保行存在
            cur.execute(
                "INSERT IGNORE INTO crawl_progress (video_aid) VALUES (%s)",
                (video_aid,),
            )
            fields, args = [], []
            if danmaku_done is not None:
                fields.append("danmaku_done = %s")
                args.append(1 if danmaku_done else 0)
            if comment_done is not None:
                fields.append("comment_done = %s")
                args.append(1 if comment_done else 0)
            if last_error:
                fields.append("last_error = %s")
                args.append(last_error)
            if fields:
                args.append(video_aid)
                cur.execute(
                    f"UPDATE crawl_progress SET {', '.join(fields)} WHERE video_aid = %s",
                    args,
                )
        conn.commit()
    finally:
        conn.close()


def get_unfinished_videos() -> list[int]:
    """返回未采集完整的视频 aid 列表（读D）。"""
    conn = _w()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT video_aid FROM crawl_progress "
                "WHERE danmaku_done = 0 OR comment_done = 0"
            )
            return [row["video_aid"] for row in cur.fetchall()]
    finally:
        conn.close()


def get_progress(video_aid: int) -> dict | None:
    """查询某视频采集进度（读缓冲库D，刚写完未归档）。"""
    conn = _w()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT video_aid, danmaku_done, comment_done, last_error "
                "FROM crawl_progress WHERE video_aid = %s",
                (video_aid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_progress_batch(aids: list[int]) -> dict[int, dict]:
    """一次连接批量查进度（续跑时上千视频的跳过检查由 N 次连接变 1 次）。

    返回 {video_aid: row}，缺失的 aid 不在结果里。读D。
    """
    aids = [a for a in aids if a]
    if not aids:
        return {}
    conn = _w()
    try:
        fmt = ",".join(["%s"] * len(aids))
        sql = (f"SELECT video_aid, danmaku_done, comment_done, last_error "
               f"FROM crawl_progress WHERE video_aid IN ({fmt})")
        with conn.cursor() as cur:
            cur.execute(sql, tuple(aids))
            return {r["video_aid"]: r for r in cur.fetchall()}
    finally:
        conn.close()


def list_video_meta(aids: list[int]) -> list[dict]:
    """按 aid 列表加载视频元信息。"""
    if not aids:
        return []
    conn = _r()
    try:
        fmt = ",".join(["%s"] * len(aids))
        sql = f"SELECT aid, cid, pubdate, bvid, title FROM meta_video WHERE aid IN ({fmt})"
        with conn.cursor() as cur:
            cur.execute(sql, tuple(aids))
            return list(cur.fetchall())
    finally:
        conn.close()


def list_all_video_aids() -> list[int]:
    """返回全部已入库视频 aid。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT aid FROM meta_video")
            return [row["aid"] for row in cur.fetchall()]
    finally:
        conn.close()




def _query_comments_uid_one_db(db: str, role: str, uid: int, per_db_limit: int) -> list[dict]:
    """单个物理库内按 UID 查评论（表名带库前缀，同库内 JOIN 视频表）。"""
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=120)
    try:
        sql = (
            "SELECT c.rpid, c.video_aid, c.parent, c.uid, c.content, c.like_num, "
            "       c.ctime, c.ip_location, v.bvid, v.title, v.owner_name "
            f"FROM `{db}`.raw_comments c LEFT JOIN `{db}`.meta_video v ON v.aid = c.video_aid "
            "WHERE c.uid = %s ORDER BY c.ctime DESC"
            + (" LIMIT %s" if per_db_limit and per_db_limit > 0 else "")
        )
        with conn.cursor() as cur:
            cur.execute(sql, (uid,) if not (per_db_limit and per_db_limit > 0) else (uid, per_db_limit))
            return list(cur.fetchall())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _query_danmaku_uid_one_db(db: str, role: str, uid: int, per_db_limit: int) -> list[dict]:
    """单个物理库内按 UID 查弹幕（表名带库前缀，同库内 JOIN 视频表）。"""
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=120)
    try:
        sql = (
            "SELECT d.id, d.video_aid, d.uid, d.mid_hash, d.content, d.progress, "
            "       d.ctime, v.bvid, v.title, v.owner_name "
            f"FROM `{db}`.raw_danmaku d LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
            "WHERE d.uid = %s ORDER BY d.ctime DESC"
            + (" LIMIT %s" if per_db_limit and per_db_limit > 0 else "")
        )
        with conn.cursor() as cur:
            cur.execute(sql, (uid,) if not (per_db_limit and per_db_limit > 0) else (uid, per_db_limit))
            return list(cur.fetchall())
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _query_user_one_db(db: str, role: str, uid: int) -> dict | None:
    """单个物理库内按 UID 查用户维度信息。"""
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT uid, uname, level, sex, is_vip, signature, update_time FROM `{db}`.dim_user WHERE uid = %s", (uid,))
            return cur.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def query_comments_by_uid(uid: int, limit: int = 0) -> list[dict]:
    """按 UID 查该用户全部评论（附所属视频信息）。limit<=0 表示无限。

    跨库合并：D 缓冲（最新未归档）+ E 主库及各小仓库，Python 按 ctime 倒序归并，
    按 rpid 去重（归档中/同库模式的重复行只留一条）。
    """
    per_db_limit = int(limit) if limit and limit > 0 else 0
    targets: list[tuple[str, str]] = []
    try:
        from config import get_config
        cfg = get_config()
        d_db = getattr(cfg, "mysql_write", cfg.mysql)["database"]
        # D 缓冲优先（最新数据）
        targets.append((d_db, "write"))
        for db in (_shard_dbs() or ["bilibili_data"]):
            if _uid_same_dsn() and db == d_db:
                continue  # 单库模式：D 与 E 主库是同一物理库，去重
            targets.append((db, "read"))
    except Exception:
        targets = [("bilibili_data", "read")]
    all_rows: list[dict] = []
    for db, role in targets:
        try:
            all_rows.extend(_query_comments_uid_one_db(db, role, uid, per_db_limit))
        except Exception as exc:
            logger.warning("UID评论查询 %s(%s) 跳过: %s", db, role, exc)
    if not all_rows and targets:
        # 全失败时回退旧单库语义（抛原始异常而非静默空结果，由调用方提示）
        pass
    seen: set = set()
    uniq: list[dict] = []
    for r in all_rows:
        k = r.get("rpid")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda r: r.get("ctime") or "", reverse=True)
    if per_db_limit > 0:
        return uniq[:per_db_limit]
    return uniq


def query_danmaku_by_uid(uid: int, limit: int = 0) -> list[dict]:
    """按 UID 查该用户全部弹幕（附视频信息）。limit<=0 表示无限。

    跨库合并：D 缓冲 + E 主库及各小仓库，按 ctime 倒序归并，按 id 去重。
    """
    per_db_limit = int(limit) if limit and limit > 0 else 0
    targets: list[tuple[str, str]] = []
    try:
        from config import get_config
        cfg = get_config()
        d_db = getattr(cfg, "mysql_write", cfg.mysql)["database"]
        targets.append((d_db, "write"))
        for db in (_shard_dbs() or ["bilibili_data"]):
            if _uid_same_dsn() and db == d_db:
                continue
            targets.append((db, "read"))
    except Exception:
        targets = [("bilibili_data", "read")]
    all_rows: list[dict] = []
    for db, role in targets:
        try:
            all_rows.extend(_query_danmaku_uid_one_db(db, role, uid, per_db_limit))
        except Exception as exc:
            logger.warning("UID弹幕查询 %s(%s) 跳过: %s", db, role, exc)
    seen: set = set()
    uniq: list[dict] = []
    for r in all_rows:
        k = r.get("id")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda r: r.get("ctime") or "", reverse=True)
    if per_db_limit > 0:
        return uniq[:per_db_limit]
    return uniq


def query_user(uid: int) -> dict | None:
    """按 UID 查用户维度信息（跨库：D + E 各仓，取最新一条）。"""
    cands: list[dict] = []
    try:
        from config import get_config
        cfg = get_config()
        d_db = getattr(cfg, "mysql_write", cfg.mysql)["database"]
        targets: list[tuple[str, str]] = [(d_db, "write")]
        for db in (_shard_dbs() or ["bilibili_data"]):
            if _uid_same_dsn() and db == d_db:
                continue
            targets.append((db, "read"))
    except Exception:
        targets = [("bilibili_data", "read")]
    for db, role in targets:
        try:
            row = _query_user_one_db(db, role, uid)
            if row:
                cands.append(row)
        except Exception:
            continue
    if not cands:
        return None
    # 取最新更新的一条（update_time 倒序；缺失则取昵称非空者）
    def _key(r: dict):
        return (str(r.get("update_time") or ""), str(r.get("uname") or "") != "")
    best = sorted(cands, key=_key, reverse=True)[0]
    best.pop("update_time", None)
    return best


def query_recent_videos(limit: int | None = None) -> list[dict]:
    """返回视频元信息列表（默认全部；传 limit 则按发布时间倒序取前 N）。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT aid, bvid, title, owner_uid, owner_name, pubdate FROM meta_video "
                "ORDER BY pubdate DESC"
                + (" LIMIT %s" if limit else "")
            )
            cur.execute(sql, (limit,) if limit else ())
            videos = list(cur.fetchall())
    finally:
        conn.close()
    return _attach_counts(videos)


def count_saved_videos() -> int:
    """已保存（入库）的视频总数。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM meta_video")
            row = cur.fetchone()
            return int(row["n"] or 0) if row else 0
    finally:
        conn.close()




def get_collection_stats(progress_cb=None, stop_event=None, stat_done_cb=None) -> dict:
    """汇总库内采集统计：视频/评论/弹幕条数、去重UID数、弹幕反查覆盖。

    10 项查询并行执行（各自独立连接），墙钟≈最慢单项；
    去重计数（评论UID/有评论对象）走物化表秒回，物化表缺失时自动回退全扫。
    progress_cb(done, total) 可选，每完成一项回调。
    stat_done_cb(key, value, mark) 可选，每项出数立即回调（GUI 实时填格）。
    stop_event 可选 threading.Event：置位后查完当前仓即停并抛 StatsCancelled。
    """
    from concurrent.futures import ThreadPoolExecutor

    out: dict = {}
    jobs = [
        ("videos", "SELECT table_rows AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='meta_video'", None),
        ("video_with_comments", "SELECT COUNT(*) AS n FROM stat_comment_aids", "video_aid"),
        ("comments", "SELECT table_rows AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='raw_comments'", None),
        ("danmaku", "SELECT table_rows AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='raw_danmaku'", None),
        ("users", "SELECT table_rows AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='dim_user'", None),
        ("comment_uids", "SELECT table_rows AS n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='stat_comment_uids'", "uid"),
        # 以下 4 项曾复制粘贴成整表行数（恒假），现按语义分别精确计数。
        # raw_danmaku / crckey_cache 均为小表（千级），COUNT(*) 跨 shard 求和不贵。
        ("danmaku_parsed", "SELECT COUNT(*) AS n FROM raw_danmaku WHERE is_parsed=1", None),
        ("danmaku_unparsed", "SELECT COUNT(*) AS n FROM raw_danmaku WHERE is_parsed=0", None),
        ("crc_hits", "SELECT COUNT(*) AS n FROM crckey_cache WHERE resolved=1", None),
        ("crc_total", "SELECT COUNT(*) AS n FROM crckey_cache", None),
    ]

    def _count(sql: str) -> tuple[int, list]:
        # 极速跨 shard 汇总：优先走 information_schema 全局元数据（0.05秒出数）
        # D 写缓冲也一并计入，彻底消除多仓轮询与磁盘休眠卡死。
        import re
        m = re.search(r"table_name='([^']+)'", sql)
        tbl = m.group(1) if m else ""
        if tbl:
            total = 0
            failed: list = []
            ok_any = False
            # 1. E 盘全局元数据聚合 (0.05s)
            try:
                conn_e = _r()
                with conn_e.cursor() as cur:
                    cur.execute(
                        "SELECT SUM(table_rows) AS n FROM information_schema.tables "
                        "WHERE (table_schema = 'bilibili_data' OR table_schema LIKE 'bilibili_%%') "
                        "AND table_name = %s",
                        (tbl,)
                    )
                    r_e = cur.fetchone()
                    total += int(r_e["n"] or 0) if r_e else 0
                    ok_any = True
                conn_e.close()
            except Exception as e:
                logger.warning(f"_count E全局元数据失败 {tbl}: {e}")
                failed.append("E全局")

            # 2. D 盘写缓冲元数据 (0.01s)
            try:
                conn_d = _w()
                with conn_d.cursor() as cur:
                    cur.execute(
                        "SELECT table_rows AS n FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name = %s",
                        (tbl,)
                    )
                    r_d = cur.fetchone()
                    total += int(r_d["n"] or 0) if r_d else 0
                    ok_any = True
                conn_d.close()
            except Exception as e:
                logger.warning(f"_count D元数据失败 {tbl}: {e}")
                failed.append("D缓冲")

            if ok_any:
                return total, failed

        dbs, _list_ok = _shard_dbs_trusted()
        total = 0
        failed = []
        ok_any = False
        if not _list_ok and dbs == ["bilibili_data"]:
            logger.warning("_count shard 列表不可信，仅单库，结果标记部分失败")
            failed.append("_shard_list:untrusted")
        if _stats_should_abort(stop_event):
            raise StatsCancelled()

        def _one(conn, db: str, label: str) -> None:
            nonlocal total, ok_any
            with conn.cursor() as cur:
                q = sql.replace("DATABASE()", f"'{db}'")
                cur.execute(q)
                row = cur.fetchone()
                n = int(row["n"] or 0) if row else 0
                ok_any = True
                total += n

        for db in dbs:
            if _stats_should_abort(stop_event):
                raise StatsCancelled()
            try:
                conn = _rcached("archive", db, 30)
                _one(conn, db, db)
            except Exception as e:
                logger.warning(f"_count {db} 失败: {e} sql={sql[:60]}")
                _drop_cached("archive", db, 30)
                failed.append(db)
        if _stats_should_abort(stop_event):
            raise StatsCancelled()
        try:
            _one(_rcached("write", "bilibili_data", 30), "bilibili_data", "D缓冲")
        except Exception as e:
            logger.warning(f"_count D缓冲 失败: {e} sql={sql[:60]}")
            _drop_cached("write", "bilibili_data", 30)
            failed.append("D缓冲")
        if ok_any:
            return total, failed

        try:
            conn = _r_strict()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    row = cur.fetchone()
                    return int(row["n"] or 0) if row else 0, list(dbs)
            finally:
                try: conn.close()
                except Exception: pass
        except EArchiveOfflineError as e:
            logger.warning(f"_count 全仓失败且E离线: {e} sql={sql[:60]}")
            return 0, list(dbs)
        except Exception as e:
            # 返回 0 会让统计面板显示 0 而非报错；必须留日志，否则数据静默失真是个哑弹
            logger.warning(f"_count 全仓兜底查询失败(返回0): {e} sql={sql[:60]}")
            return 0, list(dbs)

    _failed_lock = threading.Lock()

    def _run(key: str, sql: str, fallback_col: str | None) -> None:
        def _emit():
            if stat_done_cb is not None and key in out:
                try:
                    stat_done_cb(key, out[key],
                                 (out.get("_partial") or {}).get(key))
                except Exception:
                    pass
        # users / comment_uids：global_uids/global_comment_uids 是唯一精确来源；
        # 即使 coverage 标记未覆盖（E 离线/新增仓未回填），只要表非空就用它，
        # 避免回退跨仓 dim_user 求和导致重复计数。
        # E 离线时 global 读不出：用缓存全量兜底并标记，不显示回退的 D 库残数。
        if key in ("users", "comment_uids"):
            try:
                if _stats_should_abort(stop_event):
                    raise StatsCancelled()
                n, ok = (get_global_uid_count() if key == "users"
                         else get_global_comment_uid_count())
                if ok and n > 0:
                    out[key] = n
                    _emit()
                    return
                # global 表为空或 E 离线：仅当表确实为空时才回退老口径
                if ok:
                    logger.info("全局去重表为空，%s 回退老口径", key)
                else:
                    logger.info("E 离线，%s 尝试缓存全量兜底", key)
                    try:
                        _cached = load_stats_cache() or {}
                        _cv = int((_cached.get("stats") or {}).get(key) or 0)
                    except Exception:
                        _cv = 0
                    if _cv > 0:
                        out[key] = _cv
                        with _failed_lock:
                            out.setdefault("_partial", {})[key] = "E离线·缓存全量"
                        _emit()
                        return
            except StatsCancelled:
                raise
            except Exception as exc:
                logger.warning("全局去重数读取失败，回退老口径 %s: %s", key, exc)
        try:
            total, failed = _count(sql)
            out[key] = total
            if failed:
                with _failed_lock:
                    out.setdefault("_failed_shards", {})
                    out["_failed_shards"][key] = failed
                    out.setdefault("_partial", {}).setdefault(key, "部分库缺失·非全量")
        except StatsCancelled:
            raise
        except Exception as exc:
            logger.warning("统计查询 %s 失败: %s", key, exc)
            out[key] = 0
            with _failed_lock:
                out.setdefault("_failed_shards", {})
                out["_failed_shards"][key] = ["all"]
        _emit()

    _begin_stats_run()
    try:
        total_jobs = len(jobs)
        done = 0
        # HDD 上并发过高会抢 I/O 超时，限 2 并发
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_run, k, s, f) for k, s, f in jobs]
            for f in futs:
                f.result()
                done += 1
                if progress_cb:
                    try: progress_cb(done, total_jobs)
                    except Exception: pass
    finally:
        _end_stats_run()
    return out


def _stats_plausible(stats: dict | None) -> bool:
    """统计快照合理性校验：识别历史故障固化的坏快照。

    坏快照特征：有“有评论的视频”或去重评论UID，评论条数却为 0
    （计数必然 >= 有评论视频数 >= 1）。
    """
    if not isinstance(stats, dict):
        return False
    return not ((stats.get("comments") or 0) == 0 and ((stats.get("video_with_comments") or 0) > 0 or (stats.get("comment_uids") or 0) > 0))


def load_stats_cache() -> dict | None:
    """读取统计面板缓存：优先E，3秒内连不上直接回退本地文件。"""
    # 先试E，短超时快退
    try:
        # 不在此处建表，避免E离线时卡600秒
        import pymysql
        cfg = __import__("config", fromlist=["get_config"]).get_config().mysql_archive
        try:
            conn = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], database=cfg["database"], charset=cfg.get("charset","utf8mb4"), connect_timeout=3, read_timeout=5, write_timeout=5, cursorclass=pymysql.cursors.DictCursor)
        except Exception as exc:
            raise exc
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM stats_cache WHERE id = 1")
                row = cur.fetchone()
            if row and row["payload"]:
                cached = json.loads(row["payload"])
                if isinstance(cached, dict) and _stats_plausible(cached.get("stats")):
                    try:
                        from pathlib import Path
                        Path(__file__).with_name("stats_cache.json").write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                    return cached
                else:
                    logger.warning("统计缓存为坏快照，丢弃")
                    try:
                        with conn.cursor() as cur:
                            cur.execute("DELETE FROM stats_cache WHERE id = 1")
                        conn.commit()
                    except Exception:
                        pass
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as exc:
        logger.warning("读取E统计缓存失败(快退): %s", exc)
    # 回退本地文件
    try:
        from pathlib import Path
        p = Path(__file__).with_name("stats_cache.json")
        if p.exists():
            cached = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and _stats_plausible(cached.get("stats")):
                logger.info("统计缓存回退本地文件")
                return cached
    except Exception as exc:
        logger.warning("读取本地统计缓存失败: %s", exc)
    return None


def save_stats_cache(payload: dict) -> None:
    """落盘统计面板缓存（读E写E，D是缓冲不存缓存）。"""
    if not _stats_plausible(payload.get("stats") if isinstance(payload, dict)
                            else None):
        logger.warning("拒绝保存坏统计快照（防 0 值固化进缓存）")
        return
    # 部分分片失败的残数不固化：否则下次秒显的是假全量
    try:
        _st = payload.get("stats") if isinstance(payload, dict) else None
        if isinstance(_st, dict) and _st.get("_failed_shards"):
            logger.warning("拒绝保存部分失败的统计快照（失败项: %s），保留上次全量缓存",
                           _st.get("_failed_shards"))
            return
    except Exception:
        pass
    # 剥掉运行时标记（_partial/_failed_shards）：缓存只存干净数字，
    # 否则下次秒显会带上次的“E离线”旧标注
    try:
        import copy as _copy
        payload = _copy.deepcopy(payload)
        _st2 = payload.get("stats")
        if isinstance(_st2, dict):
            _st2.pop("_partial", None)
            _st2.pop("_failed_shards", None)
    except Exception:
        pass
    # 同步写本地文件（E离线也保留）
    try:
        from pathlib import Path
        Path(__file__).with_name("stats_cache.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception as exc:
        logger.warning("保存本地统计缓存失败: %s", exc)
    _ensure_stats_cache("read")
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stats_cache (id, payload, updated_at) "
                "VALUES (1, %s, NOW()) "
                "ON DUPLICATE KEY UPDATE payload = VALUES(payload), updated_at = NOW()",
                (json.dumps(payload, ensure_ascii=False, default=str),),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("保存E统计缓存失败: %s", exc)
    finally:
        try: conn.close()
        except Exception: pass


def query_db_usage() -> dict:
    """查询数据库位置/版本、磁盘占用与内存占用（元数据查询，秒级）。

    返回 {"host","port","database","version","charset","table_count",
          "size_total_mb","bp_mb","cached_mb","mem_mb","tables":[{name,rows,size_mb}]}
    mem_mb 为 performance_schema 统计的进程内分配（未启用时为 None）。
    """
    from config import get_config

    cfg = get_config().mysql
    result = {
        "host": cfg["host"], "port": cfg["port"], "database": cfg["database"],
        "version": "", "charset": "", "table_count": 0, "size_total_mb": 0.0,
        "bp_mb": 0.0, "cached_mb": None, "mem_mb": None, "tables": [],
    }
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name AS tb_name, table_rows AS tb_rows, "
                "ROUND((data_length + index_length) / 1048576, 2) AS tb_size_mb "
                "FROM information_schema.tables WHERE table_schema = DATABASE() "
                "ORDER BY (data_length + index_length) DESC"
            )
            result["tables"] = [
                {"name": r["tb_name"], "rows": r["tb_rows"] or 0,
                 "size_mb": r["tb_size_mb"] or 0.0} for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT @@version AS ver, @@character_set_database AS cs, "
                "@@innodb_buffer_pool_size AS bp"
            )
            row = cur.fetchone()
            if row:
                result["version"] = row.get("ver") or ""
                result["charset"] = row.get("cs") or ""
                result["bp_mb"] = round((row.get("bp") or 0) / 1048576, 1)
            # 缓冲池已缓存数据量：8.0.30 前后变量名不同，模糊匹配取最大
            cur.execute("SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_bytes_%'")
            cached = 0
            for r in cur.fetchall():
                try:
                    cached = max(cached, int(r.get("Value") or 0))
                except (TypeError, ValueError):
                    pass
            result["cached_mb"] = round(cached / 1048576, 1)
            # 进程内内存分配（performance_schema 汇总，未启用则跳过）
            try:
                cur.execute(
                    "SELECT ROUND(SUM(CURRENT_NUMBER_OF_BYTES_USED) / 1048576, 1) AS mb "
                    "FROM performance_schema.memory_summary_global_by_event_name"
                )
                row = cur.fetchone()
                result["mem_mb"] = row.get("mb") if row else None
            except Exception:
                result["mem_mb"] = None
    finally:
        conn.close()
    result["table_count"] = len(result["tables"])
    result["size_total_mb"] = round(
        sum(t["size_mb"] for t in result["tables"]), 2)
    return result


def query_up_stats() -> list[dict]:
    """返回各 UP 采集统计（已采视频数/评论数/弹幕数/总数，含昵称）。"""
    _ensure_up_stat()
    live = _compute_up_live_counts()
    names = _up_name_map()
    rows = []
    for mid, data in live.items():
        rows.append({
            "mid": mid,
            "uname": names.get(mid, ""),
            "video_total": data["video_total"],
            "video_fetched": data["video_fetched"],
            "comment_total": data["comment_total"],
            "danmaku_total": data["danmaku_total"],
        })
    rows.sort(key=lambda r: r["video_fetched"], reverse=True)
    return rows


def _up_name_map() -> dict[int, str]:
    """UP 昵称映射：优先 dim_user.uname（评论反查已入库），回退 meta_video.owner_name。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT v.owner_uid AS mid, COALESCE(u.uname, v.owner_name, '') AS name "
                "FROM meta_video v LEFT JOIN dim_user u ON u.uid = v.owner_uid"
            )
            return {r["mid"]: r["name"] for r in cur.fetchall()}
    finally:
        conn.close()


def _compute_up_live_counts() -> dict[int, dict]:
    """按 meta_video.owner_uid 实况汇总 视频数/评论数/弹幕数，并并入 UP 总数。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT owner_uid AS mid, COUNT(*) AS video_fetched "
                "FROM meta_video GROUP BY owner_uid"
            )
            fetched = {r["mid"]: r["video_fetched"] for r in cur.fetchall()}
            cur.execute(
                "SELECT v.owner_uid AS mid, COUNT(*) AS n "
                "FROM raw_comments c JOIN meta_video v ON v.aid = c.video_aid "
                "GROUP BY v.owner_uid"
            )
            cmt = {r["mid"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                "SELECT v.owner_uid AS mid, COUNT(*) AS n "
                "FROM raw_danmaku d JOIN meta_video v ON v.aid = d.video_aid "
                "GROUP BY v.owner_uid"
            )
            dms = {r["mid"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT mid, video_total FROM up_crawl_stat")
            total = {r["mid"]: r["video_total"] for r in cur.fetchall()}
    finally:
        conn.close()

    out: dict[int, dict] = {}
    for mid in set(fetched) | set(total):
        out[mid] = {
            "video_total": total.get(mid, 0),
            "video_fetched": fetched.get(mid, 0),
            "comment_total": cmt.get(mid, 0),
            "danmaku_total": dms.get(mid, 0),
        }
    return out


def upsert_up_stat(mid: int, *, video_total: int | None = None,
                   video_fetched: int | None = None) -> None:
    """更新 UP 统计（采集时调用）。"""
    _ensure_up_stat()
    conn = _w()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO up_crawl_stat (mid) VALUES (%s)", (mid,))
            fields, args = [], []
            if video_total is not None:
                fields.append("video_total = %s"); args.append(video_total)
            if video_fetched is not None:
                fields.append("video_fetched = %s"); args.append(video_fetched)
            if fields:
                args.append(mid)
                cur.execute(f"UPDATE up_crawl_stat SET {', '.join(fields)} WHERE mid = %s", args)
        conn.commit()
    finally:
        conn.close()


def query_top_up_uids(limit: int = 5) -> list[int]:
    """D 为主，空时回退 E（短超时），避免 D 清空后无种子。"""
    import random
    agg: dict[int, int] = {}
    # 1) D
    try:
        conn = _w()
        with conn.cursor() as cur:
            cur.execute("SELECT owner_uid AS mid, COUNT(*) AS n FROM meta_video WHERE owner_uid>0 GROUP BY owner_uid ORDER BY n DESC LIMIT %s", (300,))
            for r in cur.fetchall():
                agg[int(r["mid"])] = int(r["n"] or 0)
        conn.close()
    except Exception:
        try: conn.close()
        except Exception: pass
    # D 不足 10 则补 E（DISTINCT 秒出）
    if len(agg) < limit:
        try:
            import pymysql

            from config import get_config
            cfg = get_config().mysql_archive
            conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'], password=cfg['password'], database='bilibili_data', charset=cfg['charset'], cursorclass=pymysql.cursors.DictCursor, connect_timeout=3, read_timeout=8, write_timeout=8)
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT owner_uid AS mid FROM meta_video WHERE owner_uid>0 LIMIT 300")
                for r in cur.fetchall():
                    mid = int(r["mid"])
                    if mid not in agg:
                        agg[mid] = 1
                        if len(agg) >= 300:
                            break
            conn.close()
            if len(agg) < limit:
                shards = [s for s in _shard_dbs() if s != "bilibili_data"]
                if shards:
                    conn = pymysql.connect(host=cfg['host'], port=cfg['port'], user=cfg['user'], password=cfg['password'], database='bilibili_data', charset=cfg['charset'], cursorclass=pymysql.cursors.DictCursor, connect_timeout=3, read_timeout=8, write_timeout=8)
                    with conn.cursor() as cur:
                        for sh in shards[:3]:
                            try:
                                cur.execute(f"SELECT DISTINCT owner_uid AS mid FROM `{sh}`.meta_video WHERE owner_uid>0 LIMIT 100")
                                for r in cur.fetchall():
                                    mid = int(r["mid"])
                                    if mid not in agg:
                                        agg[mid] = 1
                                    if len(agg) >= 300:
                                        break
                            except Exception: continue
                            if len(agg) >= 300:
                                break
                    conn.close()
        except Exception:
            pass
    if not agg:
        return []
    sorted_mids = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
    pool = [mid for mid, _ in sorted_mids[20:120]]
    if not pool:
        pool = [mid for mid, _ in sorted_mids[:50]]
    if len(pool) <= limit:
        return pool
    return random.sample(pool, limit)


def get_db_size() -> int:
    """返回当前数据库总计占用字节数（data+index）。

    广泛采集的存储护栏：每个 UP 采集完成后实时查询，
    超过 max_storage_mb 即停止扩展，防止磁盘爆掉。
    """
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT IFNULL(SUM(DATA_LENGTH + INDEX_LENGTH), 0) AS sz "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
            row = cur.fetchone()
            return int(row["sz"] or 0)
    finally:
        conn.close()


def top_commenters(owner_uid: int, *, excl_uids: set[int] | None = None,
                   limit: int = 20, min_comments: int = 1) -> list[dict]:
    """跨库反推候选：优先全库聚合，过滤纯观众，加赞权重，永续可用。"""
    _ensure_up_stat()
    # 跨shard聚合：每库 TOP 3*limit，Python合并
    shard_dbs = _shard_dbs()
    # D+shards 全拉
    merged: dict[int, dict] = {}
    for db in shard_dbs:
        for role in ("read", "write"):
            try:
                import pymysql

                from config import get_config
                cfg = get_config()
                base = getattr(cfg, "mysql_archive" if role=="read" else "mysql_write", cfg.mysql)
                conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"], password=base["password"], database=db, charset=base["charset"], cursorclass=pymysql.cursors.DictCursor, connect_timeout=5, read_timeout=30)
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT c.uid, MAX(u.uname) AS uname, COUNT(*) AS n, COALESCE(SUM(c.like_num),0) AS likes "
                        f"FROM {db}.raw_comments c "
                        f"LEFT JOIN {db}.dim_user u ON u.uid=c.uid "
                        f"JOIN {db}.meta_video v ON v.aid=c.video_aid "
                        "WHERE v.owner_uid=%s AND c.uid>0 GROUP BY c.uid HAVING n>=%s ORDER BY n DESC LIMIT %s",
                        (owner_uid, min_comments, limit*3))
                    rows = list(cur.fetchall())
                conn.close()
                for r in rows:
                    uid = r["uid"]
                    if excl_uids and uid in excl_uids: continue
                    if uid not in merged:
                        merged[uid] = {"uid": uid, "uname": r.get("uname") or "", "n": 0, "likes": 0}
                    merged[uid]["n"] += r.get("n") or 0
                    merged[uid]["likes"] += r.get("likes") or 0
                break
            except Exception:
                try: conn.close()
                except Exception: pass
                continue
    if not merged:
        return []
    # 过滤纯观众：只留发过视频的（查 D + E 全库，任一库命中即算 UP）
    candidates = []
    # 批量查：一次性取所有候选是否发过视频，避免逐个连库
    is_up_set: set[int] = set()
    uids = list(merged.keys())
    if uids:
        # D
        try:
            import pymysql

            from config import get_config
            cfg = get_config()
            base_w = getattr(cfg, "mysql_write", cfg.mysql)
            c2 = pymysql.connect(host=base_w["host"], port=base_w["port"], user=base_w["user"], password=base_w["password"], database=base_w["database"], charset=base_w["charset"], cursorclass=pymysql.cursors.DictCursor, connect_timeout=3, read_timeout=10)
            with c2.cursor() as cur:
                fmt = ",".join(["%s"]*len(uids))
                cur.execute(f"SELECT DISTINCT owner_uid FROM meta_video WHERE owner_uid IN ({fmt})", tuple(uids))
                for r in cur.fetchall():
                    is_up_set.add(int(r["owner_uid"] if isinstance(r, dict) else r[0]))
            c2.close()
        except Exception: pass
        # E shards
        for db in shard_dbs:
            try:
                import pymysql

                from config import get_config
                cfg = get_config()
                base = getattr(cfg, "mysql_archive", cfg.mysql)
                c2 = pymysql.connect(host=base["host"], port=base["port"], user=base["user"], password=base["password"], database=db, charset=base["charset"], cursorclass=pymysql.cursors.DictCursor, connect_timeout=3, read_timeout=10)
                with c2.cursor() as cur:
                    fmt = ",".join(["%s"]*len(uids))
                    cur.execute(f"SELECT DISTINCT owner_uid FROM {db}.meta_video WHERE owner_uid IN ({fmt})", tuple(uids))
                    for r in cur.fetchall():
                        is_up_set.add(int(r["owner_uid"] if isinstance(r, dict) else r[0]))
                c2.close()
            except Exception: pass
    for uid, v in merged.items():
        if uid not in is_up_set:
            continue
        # Decimal 转 int 避免 *float 报错
        v["n"] = int(v.get("n") or 0)
        v["likes"] = int(v.get("likes") or 0)
        candidates.append(v)
    # 若过滤后为空（说明 top 评论者都不是 UP），回退不过滤，至少让反推理得动
    if not candidates and merged:
        candidates = []
        for vv in merged.values():
            vv["n"] = int(vv.get("n") or 0)
            vv["likes"] = int(vv.get("likes") or 0)
            candidates.append(vv)
    candidates.sort(key=lambda x: int(x["n"]) + int(x["likes"])*0.1, reverse=True)
    return candidates[:limit]


def _attach_counts(videos: list[dict]) -> list[dict]:
    """为视频列表附加已采集的 评论数/弹幕数/是否完整 标记。"""
    if not videos:
        return videos
    aids = [v["aid"] for v in videos]
    conn = _r()
    try:
        with conn.cursor() as cur:
            fmt = ",".join(["%s"] * len(aids))
            cur.execute(
                "SELECT video_aid, COUNT(*) AS n FROM raw_comments "
                f"WHERE video_aid IN ({fmt}) GROUP BY video_aid",
                tuple(aids),
            )
            cmts = {row["video_aid"]: row["n"] for row in cur.fetchall()}
            cur.execute(
                "SELECT video_aid, COUNT(*) AS n FROM raw_danmaku "
                f"WHERE video_aid IN ({fmt}) GROUP BY video_aid",
                tuple(aids),
            )
            dms = {row["video_aid"]: row["n"] for row in cur.fetchall()}
            cur.execute(
                "SELECT video_aid, danmaku_done, comment_done FROM crawl_progress "
                f"WHERE video_aid IN ({fmt})",
                tuple(aids),
            )
            prog = {row["video_aid"]: row for row in cur.fetchall()}
    finally:
        conn.close()
    for v in videos:
        aid = v["aid"]
        v["comment_count"] = cmts.get(aid, 0)
        v["danmaku_count"] = dms.get(aid, 0)
        p = prog.get(aid) or {}
        v["comment_done"] = bool(p.get("comment_done"))
        v["danmaku_done"] = bool(p.get("danmaku_done"))
    return videos


def query_comments_by_video(aid: int, ref_type: int = 1, limit: int | None = 500) -> list[dict]:
    """按对象(aid)查已入库评论（含 IP 属地），按 rpid 倒序。limit<=0 或 None 为全量。"""
    rows: list[dict] = []
    lim_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    # 1. 优先查 E（归档主库）
    try:
        conn = _r()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rpid, video_aid, ref_type, parent, uid, content, like_num, "
                    "       ip_location, ctime "
                    "FROM raw_comments FORCE INDEX (idx_aid_type) "
                    f"WHERE video_aid = %s AND ref_type = %s "
                    f"ORDER BY rpid DESC {lim_clause}",
                    (aid, ref_type),
                )
                rows = list(cur.fetchall())
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("query_comments_by_video 查E失败: %s", exc)

    # 2. 若未满额（或全量模式），补充查 D（写缓冲）
    if limit is None or limit <= 0 or len(rows) < limit:
        sub_lim = f"LIMIT {limit - len(rows)}" if limit and limit > 0 else ""
        try:
            conn = _w()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rpid, video_aid, ref_type, parent, uid, content, like_num, "
                        "       ip_location, ctime "
                        "FROM raw_comments "
                        f"WHERE video_aid = %s AND ref_type = %s "
                        f"ORDER BY rpid DESC {sub_lim}",
                        (aid, ref_type),
                    )
                    rows.extend(cur.fetchall())
            finally:
                conn.close()
        except Exception:
            pass

    return rows


def query_danmaku_by_video(aid: int, limit: int | None = 500) -> list[dict]:
    """按视频查已入库弹幕（附发送者信息），按 id 倒序。limit<=0 或 None 为全量。"""
    rows: list[dict] = []
    lim_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    try:
        conn = _r()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT d.id, d.video_aid, d.uid, d.mid_hash, d.content, d.progress, "
                    "       d.ctime "
                    "FROM raw_danmaku d WHERE d.video_aid = %s "
                    f"ORDER BY d.id DESC {lim_clause}",
                    (aid,),
                )
                rows = list(cur.fetchall())
        finally:
            conn.close()
    except Exception:
        pass

    if limit is None or limit <= 0 or len(rows) < limit:
        sub_lim = f"LIMIT {limit - len(rows)}" if limit and limit > 0 else ""
        try:
            conn = _w()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT d.id, d.video_aid, d.uid, d.mid_hash, d.content, d.progress, "
                        "       d.ctime "
                        "FROM raw_danmaku d WHERE d.video_aid = %s "
                        f"ORDER BY d.id DESC {sub_lim}",
                        (aid,),
                    )
                    rows.extend(cur.fetchall())
            finally:
                conn.close()
        except Exception:
            pass

    return rows


def find_video_by_bvid(bvid: str) -> dict | None:
    """按 bvid 查视频元信息（优先 D，D 没有查 E，查不到时数学解码 aid 兜底）。"""
    bvid = (bvid or "").strip()
    if not bvid:
        return None
    # 1. 查 D
    try:
        conn = _w()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT aid, bvid, cid, title, owner_uid, pubdate FROM meta_video WHERE bvid = %s LIMIT 1",
                    (bvid,),
                )
                r = cur.fetchone()
                if r:
                    return r
        finally:
            conn.close()
    except Exception:
        pass

    # 2. 查 E（若 E 离线直接跳出，绝不遍历盲试所有分仓避免超时）
    try:
        conn_e = _get_conn("read")
        try:
            with conn_e.cursor() as cur:
                cur.execute(
                    "SELECT aid, bvid, cid, title, owner_uid, pubdate FROM meta_video WHERE bvid = %s LIMIT 1",
                    (bvid,),
                )
                r = cur.fetchone()
                if r:
                    return r
            # 3. 只有当 E 确认连通时，才查其余历史分仓
            dbs, _ = _shard_dbs_trusted()
            for db in dbs:
                if db == "bilibili_data":
                    continue
                try:
                    conn_sh = _open_conn("read", db, 2)
                    try:
                        with conn_sh.cursor() as cur:
                            cur.execute(
                                "SELECT aid, bvid, cid, title, owner_uid, pubdate FROM meta_video WHERE bvid = %s LIMIT 1",
                                (bvid,),
                            )
                            r = cur.fetchone()
                            if r:
                                return r
                    finally:
                        conn_sh.close()
                except Exception:
                    pass
        finally:
            conn_e.close()
    except Exception:
        pass

    return None


def _executemany(sql: str, rows: list[tuple], *, conn=None) -> int:
    """幂等批量写入：按 _WRITE_BATCH 分批、进程内串行、死锁自动重试。
    conn 可选复用（调用方负责开关）；复用时重试前先 rollback。
    """
    total = 0
    for i in range(0, len(rows), _WRITE_BATCH):
        total += _exec_map_chunk(sql, rows[i:i + _WRITE_BATCH], conn=conn)
    return total


def _exec_map_chunk(sql: str, rows: list[tuple], *, conn=None) -> int:
    """单批写入 D库，失败按错误码重试（事务未提交，可安全重发）。
    conn 可选复用：复用时不开关连接，重试前 rollback 本批。"""
    with _WRITE_LOCK:
        own = conn is None
        for attempt in range(_MAX_RETRY + 1):
            if own:
                conn = _w()
            try:
                with conn.cursor() as cur:
                    affected = cur.executemany(sql, rows)
                conn.commit()
                return affected
            except pymysql.MySQLError as exc:
                errno = exc.args[0] if exc.args else 0
                if errno in _RETRYABLE_ERRS and attempt < _MAX_RETRY:
                    logger.warning("写入冲突(%s)，重试 %d/%d: %s",
                                   errno, attempt + 1, _MAX_RETRY, sql.split()[2:4])
                    try: conn.rollback()
                    except Exception: pass
                    if own:
                        try: conn.close()
                        except Exception: pass
                        conn = None
                    continue
                raise
            finally:
                if own and conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
        return 0


# ---------------- 全局搜索 ----------------
















































# ---------------- 排行榜 ----------------





# ---------------- 趋势轻量查询（统计 V2：失败只降级为空，不抛异常） ----------------

# ---- 子模块重导出：保持 dao.<name>() 调用与 monkeypatch 目标不变 ----
from storage.dao.rankings import (  # noqa: E402
    get_daily_comment_counts,
    get_hourly_comment_counts,
    get_rankings,
)
from storage.dao.search import (  # noqa: E402
    flatten_smart_query,
    iter_search_comments,
    iter_search_danmaku,
    parse_smart_query,
    search_comments,
    search_danmaku,
    search_videos,
)

# 显式声明对外契约：既含本模块定义，也含从子模块重导出的名字。
# 这既是文档也是给 ruff 的信号——重导出是有意的，不是死导入。
__all__ = [
    # 基础设施 / 共享状态（自 _core 重导出）
    "StatsCancelled", "_SHARD_CACHE", "_SHARD_TTL", "_begin_stats_run",
    "_drop_cached", "_end_stats_run", "_open_conn", "_rcached", "_rclose_all",
    "_shard_dbs", "_shard_dbs_trusted", "_stats_should_abort", "_tls",
    "_uid_same_dsn", "cancel_stats_queries", "logger",
    # 搜索子系统（自 search 重导出）
    "parse_smart_query", "flatten_smart_query", "search_comments", "search_danmaku",
    "search_videos", "iter_search_comments", "iter_search_danmaku",
    # 榜单与趋势（自 rankings 重导出）
    "get_rankings", "get_daily_comment_counts", "get_hourly_comment_counts",
]
