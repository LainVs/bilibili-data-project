"""数据访问层：向 MySQL 幂等写入采集数据。"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Iterable

import pymysql

from storage.db import ensure_stats_cache_table
from storage.db import ensure_up_stat_table
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


# 线程级读连接复用：同一线程内同 (role, db, 超时) 只建连一次，
# ping(reconnect=True) 保活；短生命周期的线程结束时由 GC 回收。
_tls = threading.local()


def _open_conn(role: str, db: str, read_timeout: int):
    import pymysql
    from config import get_config
    if role == "write":
        base = getattr(get_config(), "mysql_write", get_config().mysql)
    else:
        base = getattr(get_config(), "mysql_archive", get_config().mysql)
    return pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
                           read_timeout=read_timeout)


def _rcached(role: str, db: str, read_timeout: int = 120):
    """取线程复用读连接；断线自动重连。调用方不关闭。"""
    cache = getattr(_tls, "conns", None)
    if cache is None:
        cache = {}
        _tls.conns = cache
    key = (role, db, read_timeout)
    conn = cache.get(key)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try: conn.close()
            except Exception: pass
    conn = _open_conn(role, db, read_timeout)
    cache[key] = conn
    return conn


def _drop_cached(role: str, db: str, read_timeout: int = 120) -> None:
    """丢掉线程复用池里坏掉的连接，下次重建。"""
    cache = getattr(_tls, "conns", None)
    if not cache:
        return
    conn = cache.pop((role, db, read_timeout), None)
    if conn is not None:
        try: conn.close()
        except Exception: pass


def _rclose_all() -> None:
    """关闭本线程全部复用读连接（如取消/收尾时调用，可选）。"""
    cache = getattr(_tls, "conns", None)
    if not cache:
        return
    for conn in list(cache.values()):
        try: conn.close()
        except Exception: pass
    cache.clear()

_SHARD_CACHE: dict = {"time": 0.0, "shards": [], "ok": False}
_SHARD_TTL = 60.0

def _shard_dbs_trusted() -> tuple[list, bool]:
    """带可信标记的 shard 列表（60s 缓存）。

    返回 (dbs, ok)：ok=False 表示本次列表拉取失败（用了兜底/缓存），
    调用方不得把结果当全量存缓存。
    """
    import time as _t
    now = _t.monotonic()
    hit = _SHARD_CACHE.get("shards")
    if hit and (now - _SHARD_CACHE.get("time", 0)) < _SHARD_TTL:
        return list(hit), bool(_SHARD_CACHE.get("ok", True))
    try:
        from storage.archive_sync import list_shards
        shards = [s for s in (list_shards() or []) if s]
        base = "bilibili_data"
        uniq: list = []
        seen: set = set()
        for s in shards:
            if s not in seen:
                seen.add(s); uniq.append(s)
        if base not in seen:
            uniq = [base] + uniq
        _SHARD_CACHE.update({"time": now, "shards": list(uniq), "ok": True})
        return uniq, True
    except Exception as exc:
        logger.warning("shard 列表拉取失败: %s", exc)
        if hit:
            # 用上次可信列表，但标记不可信
            _SHARD_CACHE["ok"] = False
            return list(hit), False
        _SHARD_CACHE.update({"time": now, "shards": ["bilibili_data"], "ok": False})
        return ["bilibili_data"], False

def _shard_dbs():
    dbs, _ok = _shard_dbs_trusted()
    return dbs

def get_conn(role="write"):
    return _get_conn(role)

logger = logging.getLogger(__name__)

# 进程内写互斥：采集线程并发批插相同主键（如 dim_user 同 uid）会互相持锁
# 等待形成死锁风暴（曾拖垮整库）。串行化后锁冲突归零，吞吐影响可忽略。
_WRITE_LOCK = threading.RLock()
# 单事务最大行数：避免超大事务把 InnoDB 全文索引同步/undo 一次性憋大。
_WRITE_BATCH = 500
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
    import time as _time
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
    """返回某对象已采集评论的最新时间戳（读D，新写入未归档）。"""
    conn = _w()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(UNIX_TIMESTAMP(ctime)) AS ts FROM raw_comments "
                "WHERE video_aid = %s AND ref_type = %s",
                (video_aid, ref_type),
            )
            row = cur.fetchone()
            return int(row["ts"] or 0) if row else 0
    finally:
        conn.close()


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


def query_comments_by_uid(uid: int, limit: int = 0) -> list[dict]:
    """按 UID 查该用户全部评论（附所属视频信息）。limit<=0 表示无限。"""
    conn = _r()
    try:
        sql = (
            "SELECT c.rpid, c.video_aid, c.parent, c.uid, c.content, c.like_num, "
            "       c.ctime, c.ip_location, v.bvid, v.title, v.owner_name "
            "FROM raw_comments c LEFT JOIN meta_video v ON v.aid = c.video_aid "
            "WHERE c.uid = %s ORDER BY c.ctime DESC"
            + (" LIMIT %s" if limit and limit > 0 else "")
        )
        with conn.cursor() as cur:
            cur.execute(sql, (uid,) if not (limit and limit > 0) else (uid, limit))
            return list(cur.fetchall())
    finally:
        conn.close()


def query_danmaku_by_uid(uid: int, limit: int = 0) -> list[dict]:
    """按 UID 查该用户全部弹幕（附视频信息）。limit<=0 表示无限。"""
    conn = _r()
    try:
        sql = (
            "SELECT d.id, d.video_aid, d.uid, d.mid_hash, d.content, d.progress, "
            "       d.ctime, v.bvid, v.title, v.owner_name "
            "FROM raw_danmaku d LEFT JOIN meta_video v ON v.aid = d.video_aid "
            "WHERE d.uid = %s ORDER BY d.ctime DESC"
            + (" LIMIT %s" if limit and limit > 0 else "")
        )
        with conn.cursor() as cur:
            cur.execute(sql, (uid,) if not (limit and limit > 0) else (uid, limit))
            return list(cur.fetchall())
    finally:
        conn.close()


def query_user(uid: int) -> dict | None:
    """按 UID 查用户维度信息。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT uid, uname, level, sex, is_vip, signature FROM dim_user WHERE uid = %s", (uid,))
            return cur.fetchone()
    finally:
        conn.close()


def query_recent_videos(limit: int | None = None) -> list[dict]:
    """返回视频元信息列表（默认全部；传 limit 则按发布时间倒序取前 N）。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT aid, bvid, title, owner_uid, owner_name, pubdate FROM meta_video "
                "ORDER BY pubdate DESC"
                + (f" LIMIT %s" if limit else "")
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


class StatsCancelled(Exception):
    """统计/排行榜被用户取消（协作式，查完当前仓即停）。"""


def get_collection_stats(progress_cb=None, stop_event=None, stat_done_cb=None) -> dict:
    """汇总库内采集统计：视频/评论/弹幕条数、去重UID数、弹幕反查覆盖。

    10 项查询并行执行（各自独立连接），墙钟≈最慢单项；
    去重计数（评论UID/有评论对象）走物化表秒回，物化表缺失时自动回退全扫。
    progress_cb(done, total) 可选，每完成一项回调。
    stat_done_cb(key, value, mark) 可选，每项出数立即回调（GUI 实时填格）。
    stop_event 可选 threading.Event：置位后查完当前仓即停并抛 StatsCancelled。
    """
    from concurrent.futures import ThreadPoolExecutor
    from storage.db import ensure_stat_counters

    try:
        ensure_stat_counters()
    except Exception:
        pass
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
        # 跨shard求和：返回 (总数, 失败的仓列表)。失败的仓不再按 0 混入总数。
        # table_rows 估算为0时回退 COUNT(*)（LOAD DATA 后未 ANALYZE 的情况）。
        # D 写缓冲也计入（以前只算 E，D 的新数据在总览里隐身）。
        import re
        m = re.search(r"table_name='([^']+)'", sql)
        tbl = m.group(1) if m else ""
        dbs, _list_ok = _shard_dbs_trusted()
        total = 0
        failed: list = []
        ok_any = False
        if not _list_ok and dbs == ["bilibili_data"]:
            # 列表拉取失败且无缓存：只能看到单库，结果必不全，直接标记
            logger.warning("_count shard 列表不可信，仅单库，结果标记部分失败")
            failed.append("_shard_list:untrusted")
        if stop_event is not None and stop_event.is_set():
            raise StatsCancelled()

        def _one(conn, db: str, label: str) -> None:
            nonlocal total, ok_any
            with conn.cursor() as cur:
                q = sql.replace("DATABASE()", f"'{db}'")
                cur.execute(q)
                row = cur.fetchone()
                n = int(row["n"] or 0) if row else 0
                if n == 0 and tbl in ("raw_comments", "meta_video", "dim_user", "raw_danmaku"):
                    try:
                        cur.execute(f"SELECT COUNT(*) AS n FROM `{db}`.`{tbl}`")
                        r2 = cur.fetchone()
                        n = int(r2["n"] or 0) if r2 else 0
                    except Exception as e2:
                        logger.warning(f"_count {label}.{tbl} COUNT(*) 回退失败: {e2}")
                        raise
                ok_any = True
                total += n

        for db in dbs:
            if stop_event is not None and stop_event.is_set():
                raise StatsCancelled()
            try:
                conn = _rcached("archive", db, 120)
                _one(conn, db, db)
            except Exception as e:
                logger.warning(f"_count {db} 失败: {e} sql={sql[:60]}")
                # 复用连接坏了就丢掉，下次重建，避免坏连接反复失败
                _drop_cached("archive", db, 120)
                failed.append(db)
        # D 写缓冲（SSD，快）
        if stop_event is not None and stop_event.is_set():
            raise StatsCancelled()
        try:
            _one(_rcached("write", "bilibili_data", 120), "bilibili_data", "D缓冲")
        except Exception as e:
            logger.warning(f"_count D缓冲 失败: {e} sql={sql[:60]}")
            _drop_cached("write", "bilibili_data", 120)
            failed.append("D缓冲")
        if ok_any:
            return total, failed
        # 全仓都失败：严格读 E 一次（尽力），E 也不通则报 0+失败，
        # 绝不拿 D 缓冲数冒充全量总数。
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
        except Exception:
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
                if stop_event is not None and stop_event.is_set():
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
            # 物化表为空（0）且有回退列时，走 DISTINCT 全扫保底（慢但对）
            if total == 0 and not failed and fallback_col:
                if stop_event is not None and stop_event.is_set():
                    raise StatsCancelled()
                try:
                    total2, failed2 = _count(
                        f"SELECT COUNT(DISTINCT {fallback_col}) AS n "
                        f"FROM raw_comments WHERE {fallback_col} > 0")
                    total, failed = total2, failed2
                except StatsCancelled:
                    raise
                except Exception:
                    pass
            out[key] = total
            if failed:
                with _failed_lock:
                    out.setdefault("_failed_shards", {})
                    out["_failed_shards"][key] = failed
                    # 有仓缺失的残数必须标注，免得当成全量显示
                    out.setdefault("_partial", {}).setdefault(key, "部分库缺失·非全量")
        except StatsCancelled:
            raise
        except Exception as exc:
            logger.warning("统计查询 %s 失败: %s", key, exc)
            if fallback_col:
                try:  # 物化表缺失 → 回退原 DISTINCT 全扫（慢但保底）
                    total2, failed2 = _count(
                        f"SELECT COUNT(DISTINCT {fallback_col}) AS n "
                        f"FROM raw_comments WHERE {fallback_col} > 0")
                    out[key] = total2
                    if failed2:
                        with _failed_lock:
                            out.setdefault("_failed_shards", {})
                            out["_failed_shards"][key] = failed2
                except Exception:
                    out[key] = 0
                    with _failed_lock:
                        out.setdefault("_failed_shards", {})
                        out["_failed_shards"][key] = ["all"]
            else:
                out[key] = 0
                with _failed_lock:
                    out.setdefault("_failed_shards", {})
                    out["_failed_shards"][key] = ["all"]
        _emit()

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
                except: pass
    return out


def _stats_plausible(stats: dict | None) -> bool:
    """统计快照合理性校验：识别历史故障固化的坏快照。

    坏快照特征：有“有评论的视频”或去重评论UID，评论条数却为 0
    （计数必然 >= 有评论视频数 >= 1）。
    """
    if not isinstance(stats, dict):
        return False
    if (stats.get("comments") or 0) == 0:
        if (stats.get("video_with_comments") or 0) > 0 \
                or (stats.get("comment_uids") or 0) > 0:
            return False
    return True


def load_stats_cache() -> dict | None:
    """读取统计面板缓存：优先E，3秒内连不上直接回退本地文件。"""
    # 先试E，短超时快退
    try:
        # 不在此处建表，避免E离线时卡600秒
        from storage.db import get_conn as _get_conn_raw
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
        except: pass
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
                            except: continue
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
                except: pass
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
        except: pass
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
            except: pass
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


def query_comments_by_video(aid: int, ref_type: int = 1, limit: int = 500) -> list[dict]:
    """按对象(aid)查已入库评论（含 IP 属地），按时间倒序。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT rpid, video_aid, ref_type, parent, uid, content, like_num, "
                "       ip_location, ctime "
                "FROM raw_comments WHERE video_aid = %s AND ref_type = %s "
                "ORDER BY ctime DESC LIMIT %s",
                (aid, ref_type, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def query_danmaku_by_video(aid: int, limit: int = 500) -> list[dict]:
    """按视频查已入库弹幕（附发送者信息），按时间倒序。"""
    conn = _r()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id, d.video_aid, d.uid, d.mid_hash, d.content, d.progress, "
                "       d.ctime "
                "FROM raw_danmaku d WHERE d.video_aid = %s "
                "ORDER BY d.ctime DESC LIMIT %s",
                (aid, limit),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def find_video_by_bvid(bvid: str) -> dict | None:
    """按 bvid 查视频元信息（走 D 避免 E 卡）。"""
    conn = _w()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT aid, bvid, cid, title, owner_uid, pubdate FROM meta_video WHERE bvid = %s LIMIT 1",
                (bvid,),
            )
            return cur.fetchone()
    finally:
        conn.close()


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

def _like_esc(keyword: str) -> str:
    """转义 LIKE 元字符（% _ \\），防用户输入干扰通配。"""
    return (
        keyword
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _parse_keywords(raw: str) -> list[str]:
    """把逗号/空格分隔的关键词字符串解析为去重列表（保持顺序）。"""
    import re as _re
    parts = _re.split(r"[,，\s]+", (raw or "").strip())
    seen: set = set()
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _ft_term(keyword: str) -> str:
    """把关键词包装成 ngram 全文索引的布尔短语（含转义）。

    ngram_token_size=2：短语"残影里"会被分词为 残影/影里 两个二元组，
    与正文的二元组分词逐段比对，等效"包含子串"匹配；对于 2 字符词
    （如"残影"）即是精确包含匹配。返回 "kw" 形式（IN BOOLEAN MODE）。
    """
    kw = keyword.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{kw}"'


def _ft_usable(keyword: str) -> bool:
    """是否是 ngram 全文索引可处理的词（≥2 字符；单字走原 LIKE 兜底）。"""
    return len(keyword.strip()) >= 2


def _multi_ft_usable(keywords: list[str]) -> bool:
    """多关键词场景：全部 ≥2 字符才走 FULLTEXT，否则回退 LIKE。"""
    return bool(keywords) and all(_ft_usable(k) for k in keywords)


def _multi_like_cond(alias: str, keywords: list[str]) -> tuple[str, list]:
    """多关键词 AND LIKE 条件：每个关键词都要出现在 content 中。"""
    conds, params = [], []
    for kw in keywords:
        conds.append(f" AND {alias}.content LIKE %s")
        params.append("%" + _like_esc(kw) + "%")
    return "".join(conds), params


def _multi_ft_where(alias: str, keywords: list[str]) -> tuple[str, list]:
    """多关键词 FULLTEXT AND 条件：用 +\"kw1\" +\"kw2\" ... 布尔模式。

    ngram 全文索引下 +\"kw\" 表示该短语必须出现，多个 + 之间是 AND。
    """
    terms = " ".join(f'+{_ft_term(k)}' for k in keywords)
    return f" AND MATCH({alias}.content) AGAINST(%s IN BOOLEAN MODE)", [terms]


def _dt_cond(alias: str, start: str, end: str) -> tuple[str, tuple]:
    """时间范围条件（按每行日期字符串 ts >= start AND ts <= end 过滤）。"""
    cond, params = "", []
    if start:
        cond += f" AND {alias} >= %s"
        params.append(start)
    if end:
        cond += f" AND {alias} <= %s"
        params.append(end)
    return cond, params


def _exclude_likes(exclude: str) -> tuple[str, list]:
    """把“排除词(逗号分隔)”转成 NOT LIKE 条件。

    解决子串匹配误伤：如搜“国男”会命中“中国男人/外国男人”，
    填排除词“中国男,外国男”即可过滤。
    """
    words = [w.strip() for w in (exclude or "").split(",") if w.strip()]
    conds = []
    params: list = []
    for w in words:
        conds.append(" AND c.content NOT LIKE %s")
        params.append("%" + _like_esc(w) + "%")
    return "".join(conds), params


def _search_one_db(db: str, role: str, keyword: str, start: str, end: str, exclude: str, limit: int,
                   keywords: list[str] | None = None):
    kw_list = keywords or _parse_keywords(keyword)
    if not kw_list:
        return []
    if not limit or limit <= 0:
        limit = 500000
    import pymysql
    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role=="read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"], password=base["password"], database=db, charset=base["charset"], cursorclass=pymysql.cursors.DictCursor, connect_timeout=5, read_timeout=120)
    try:
        with conn.cursor() as cur:
            cond, params = _dt_cond("c.ctime", start, end)
            excl, excl_params = _exclude_likes(exclude)
            cols = ("t.rpid, t.uid, u.uname, t.content, t.like_num, t.ctime, t.ip_location, t.video_aid, v.bvid, v.title, v.owner_uid, v.owner_name")
            if len(kw_list) == 1 and _ft_usable(kw_list[0]):
                # 单关键词 FULLTEXT 路径（原有逻辑）
                term = _ft_term(kw_list[0])
                sub_lim = limit * 3 + 3
                inner_cols = "c.rpid, c.uid, c.content, c.like_num, c.ctime, c.ip_location, c.video_aid"
                inner = f"SELECT {inner_cols} FROM {db}.raw_comments c WHERE MATCH(c.content) AGAINST(%s IN BOOLEAN MODE)" + cond + excl + " LIMIT %s"
                sql = f"SELECT {cols} FROM ({inner}) t LEFT JOIN {db}.meta_video v ON v.aid=t.video_aid LEFT JOIN {db}.dim_user u ON u.uid=t.uid ORDER BY t.ctime DESC LIMIT %s"
                try:
                    cur.execute(sql, [term, *params, *excl_params, sub_lim, limit])
                except Exception:
                    pat = "%" + _like_esc(kw_list[0]) + "%"
                    where = "c.content LIKE %s" + cond + excl
                    cols_c = cols.replace("t.", "c.")
                    sql2 = f"SELECT {cols_c} FROM {db}.raw_comments c LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                    cur.execute(sql2, [pat, *params, *excl_params, limit])
            elif len(kw_list) >= 2 and _multi_ft_usable(kw_list):
                # 多关键词 FULLTEXT AND 路径
                ft_where, ft_params = _multi_ft_where("c", kw_list)
                inner_cols = "c.rpid, c.uid, c.content, c.like_num, c.ctime, c.ip_location, c.video_aid"
                inner = f"SELECT {inner_cols} FROM {db}.raw_comments c WHERE 1=1" + ft_where + cond + excl + f" LIMIT {limit * 3 + 3}"
                sql = f"SELECT {cols} FROM ({inner}) t LEFT JOIN {db}.meta_video v ON v.aid=t.video_aid LEFT JOIN {db}.dim_user u ON u.uid=t.uid ORDER BY t.ctime DESC LIMIT %s"
                try:
                    cur.execute(sql, [*ft_params, *params, *excl_params, limit])
                except Exception:
                    # FULLTEXT 失败回退 LIKE
                    like_where, like_params = _multi_like_cond("c", kw_list)
                    cols_c = cols.replace("t.", "c.")
                    where = "1=1" + like_where + cond + excl
                    sql2 = f"SELECT {cols_c} FROM {db}.raw_comments c LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                    cur.execute(sql2, [*like_params, *params, *excl_params, limit])
            else:
                # LIKE 路径（单关键词单字 / 多关键词回退）
                like_where, like_params = _multi_like_cond("c", kw_list)
                cols_c = cols.replace("t.", "c.")
                where = "1=1" + like_where + cond + excl
                sql = f"SELECT {cols_c} FROM {db}.raw_comments c LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                cur.execute(sql, [*like_params, *params, *excl_params, limit])
            return list(cur.fetchall())
    finally:
        conn.close()

def search_comments(keyword: str, *, start: str = "", end: str = "", exclude: str = "", limit: int = 100,
                    progress_cb=None) -> dict:
    """跨库搜评论。D 写缓冲最先查（全文索引秒出首屏），E 各仓随后逐仓回调。

    progress_cb(db_label, rows): 每完成一仓立即回调，GUI 流式展示。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"rows": [], "total": 0}
    kw_list = _parse_keywords(keyword)
    is_unlimited = not limit or limit <= 0
    qlimit = 500000 if is_unlimited else limit
    dbs = _shard_dbs()
    all_rows = []
    # D 写缓冲优先（全文索引，最快，先画首屏；以前只在 E 零命中才查 D）
    try:
        rows = _search_one_db("bilibili_data", "write", keyword, start, end, exclude, qlimit, keywords=kw_list)
        all_rows.extend(rows)
        if progress_cb:
            try: progress_cb("D缓冲", rows)
            except Exception: pass
    except Exception:
        pass
    for db in dbs:
        try:
            rows = _search_one_db(db, "read", keyword, start, end, exclude, qlimit, keywords=kw_list)
            all_rows.extend(rows)
            if progress_cb:
                try: progress_cb(db, rows)
                except Exception: pass
        except Exception:
            pass
    all_rows.sort(key=lambda r: r.get("ctime") or "", reverse=True)
    if is_unlimited:
        return {"rows": all_rows, "total": len(all_rows)}
    truncated = len(all_rows) > limit
    return {"rows": all_rows[:limit], "total": None if truncated else len(all_rows)}


def _exclude_likes_danmaku(exclude: str) -> tuple[str, list]:
    """同 _exclude_likes，但针对弹幕表别名 d。"""
    words = [w.strip() for w in (exclude or "").split(",") if w.strip()]
    conds, params = [], []
    for w in words:
        conds.append(" AND d.content NOT LIKE %s")
        params.append("%" + _like_esc(w) + "%")
    return "".join(conds), params


def search_danmaku(keyword: str, *, start: str = "", end: str = "",
                   exclude: str = "", limit: int = 100) -> dict:
    """全局搜索弹幕（ngram 全文索引）或发送者标识（mid_hash/uid LIKE）。

    exclude：逗号分隔的排除词（对弹幕内容生效）。
    多关键词用逗号分隔，全部 AND 必须出现在弹幕内容中。
    ≥2 字关键词走 MATCH…AGAINST；单字词回退 LIKE 全扫。
    total 语义同 search_comments（LIMIT+1 探测截断）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"rows": [], "total": 0}
    kw_list = _parse_keywords(keyword)
    conn = _r()
    try:
        with conn.cursor() as cur:
            cond, params = _dt_cond("d.ctime", start, end)
            excl, excl_params = _exclude_likes_danmaku(exclude)
            cols = ("t.id, t.uid, t.mid_hash, u.uname, t.content, t.progress, "
                    "t.ctime, t.video_aid, v.bvid, v.title, v.owner_name")
            if len(kw_list) == 1 and _ft_usable(kw_list[0]):
                # 单关键词 FULLTEXT（原有逻辑）
                term = _ft_term(kw_list[0])
                sub_lim = limit * 5 + 5
                inner_cols = ("d.id, d.uid, d.mid_hash, d.content, d.progress, "
                              "d.ctime, d.video_aid")
                inner = (
                    f"SELECT {inner_cols} FROM raw_danmaku d "
                    "WHERE MATCH(d.content) AGAINST(%s IN BOOLEAN MODE)"
                    + cond + excl + " "
                    "LIMIT %s"
                )
                sql = (
                    "(SELECT " + cols + " FROM (" + inner + ") t "
                    "LEFT JOIN meta_video v ON v.aid = t.video_aid "
                    "LEFT JOIN dim_user u ON u.uid = t.uid) "
                    "UNION "
                    "(SELECT d.id, d.uid, d.mid_hash, u.uname, d.content, "
                    "d.progress, d.ctime, d.video_aid, "
                    "v.bvid, v.title, v.owner_name "
                    "FROM raw_danmaku d "
                    "LEFT JOIN meta_video v ON v.aid = d.video_aid "
                    "LEFT JOIN dim_user u ON u.uid = d.uid "
                    "WHERE (d.mid_hash LIKE %s OR d.uid LIKE %s)"
                    + cond + excl + " "
                    "ORDER BY d.ctime DESC LIMIT %s) "
                    "ORDER BY ctime DESC LIMIT %s"
                )
                pat = "%" + _like_esc(kw_list[0]) + "%"
                cur.execute(sql, [term, *params, *excl_params, sub_lim,
                                  pat, pat, *params, *excl_params, sub_lim,
                                  limit + 1])
            elif len(kw_list) >= 2:
                # 多关键词：内容 ALL LIKE + mid_hash/uid 兜底
                like_where, like_params = _multi_like_cond("d", kw_list)
                cols_d = cols.replace("t.", "d.")
                # mid_hash/uid 搜第一个关键词（通常用户搜的是同一个人）
                uid_pat = "%" + _like_esc(kw_list[0]) + "%"
                where = ("1=1" + like_where + cond + excl)
                uid_where = ("(d.mid_hash LIKE %s OR d.uid LIKE %s)"
                             + cond + excl)
                sql = (
                    "SELECT " + cols_d + " FROM raw_danmaku d "
                    "LEFT JOIN meta_video v ON v.aid = d.video_aid "
                    "LEFT JOIN dim_user u ON u.uid = d.uid "
                    f"WHERE {where} "
                    "UNION "
                    "SELECT d.id, d.uid, d.mid_hash, u.uname, d.content, "
                    "d.progress, d.ctime, d.video_aid, "
                    "v.bvid, v.title, v.owner_name "
                    "FROM raw_danmaku d "
                    "LEFT JOIN meta_video v ON v.aid = d.video_aid "
                    "LEFT JOIN dim_user u ON u.uid = d.uid "
                    f"WHERE {uid_where} "
                    "ORDER BY ctime DESC LIMIT %s"
                )
                cur.execute(sql, [*like_params, *params, *excl_params,
                                  uid_pat, uid_pat, *params, *excl_params,
                                  limit + 1])
            else:
                # 单字关键词 LIKE 路径
                pat = "%" + _like_esc(kw_list[0]) + "%"
                where = ("(d.content LIKE %s OR d.mid_hash LIKE %s "
                         "OR d.uid LIKE %s)" + cond + excl)
                cols_d = cols.replace("t.", "d.")
                sql = (
                    "SELECT " + cols_d + " FROM raw_danmaku d "
                    "LEFT JOIN meta_video v ON v.aid = d.video_aid "
                    "LEFT JOIN dim_user u ON u.uid = d.uid "
                    f"WHERE {where} "
                    "ORDER BY d.ctime DESC LIMIT %s"
                )
                cur.execute(sql, [pat, pat, pat, *params, *excl_params,
                                  limit + 1])
            rows = list(cur.fetchall())
            truncated = len(rows) > limit
            return {"rows": rows[:limit],
                    "total": None if truncated else len(rows)}
    finally:
        conn.close()


def _iter_search(keyword: str, *, table: str, col: str, id_col: str,
                 alias: str, cols: str, start: str = "", end: str = "",
                 exclude: str = "", page: int = 5000) -> Iterable[dict]:
    """流式导出搜索命中全部行（keyset 分页，无深偏移，千万级可导出）。

    仅内容命中（不含昵称分支），时间/排除词条件一致；
    按 (ctime, 主键) 倒序翻页，ctime 撞车用主键兜底保证不重不漏。
    多关键词用逗号分隔，全部 AND 必须出现在内容中。
    """
    kw_list = _parse_keywords(keyword)
    cond, params = _dt_cond(f"{alias}.ctime", start, end)
    excl, excl_params = _exclude_likes(exclude)
    # 多关键词 AND LIKE 条件
    like_where, like_params = _multi_like_cond(alias, kw_list)
    conn = _r()
    try:
        with conn.cursor() as cur:
            last_ctime = None
            last_id = None
            while True:
                params_batch = list(like_params) + list(params) + list(excl_params)
                if last_ctime is not None:
                    sql = (
                        f"SELECT {cols} FROM {table} {alias} "
                        "LEFT JOIN meta_video v ON v.aid = " + alias + ".video_aid "
                        "LEFT JOIN dim_user u ON u.uid = " + alias + ".uid "
                        f"WHERE 1=1 {like_where}"
                        + cond + excl +
                        f" AND ({alias}.ctime, {alias}.{id_col}) < (%s, %s) "
                        f"ORDER BY {alias}.ctime DESC, {alias}.{id_col} DESC LIMIT %s"
                    )
                    args = [*params_batch, last_ctime, last_id, page]
                else:
                    sql = (
                        f"SELECT {cols} FROM {table} {alias} "
                        "LEFT JOIN meta_video v ON v.aid = " + alias + ".video_aid "
                        "LEFT JOIN dim_user u ON u.uid = " + alias + ".uid "
                        f"WHERE 1=1 {like_where}"
                        + cond + excl +
                        f" ORDER BY {alias}.ctime DESC, {alias}.{id_col} DESC LIMIT %s"
                    )
                    args = [*params_batch, page]
                cur.execute(sql, args)
                rows = list(cur.fetchall())
                if not rows:
                    break
                for r in rows:
                    yield r
                if len(rows) < page:
                    break
                last = rows[-1]
                last_ctime = last["ctime"]
                last_id = last[id_col]
    finally:
        conn.close()


def iter_search_comments(keyword: str, *, start: str = "", end: str = "",
                         exclude: str = "", page: int = 5000) -> Iterable[dict]:
    """流式导出全部命中评论（keyset 分页），字段同 search_comments 内容分支。"""
    cols = ("c.rpid, c.uid, u.uname, c.content, c.like_num, c.ctime, "
            "c.ip_location, c.video_aid, v.bvid, v.title, "
            "v.owner_uid, v.owner_name")
    return _iter_search(keyword, table="raw_comments", col="content",
                        id_col="rpid", alias="c", cols=cols,
                        start=start, end=end, exclude=exclude, page=page)


def iter_search_danmaku(keyword: str, *, start: str = "", end: str = "",
                        exclude: str = "", page: int = 5000) -> Iterable[dict]:
    """流式导出全部命中弹幕（keyset 分页）。"""
    cols = ("d.id, d.uid, d.mid_hash, u.uname, d.content, d.progress, "
            "d.ctime, d.video_aid, v.bvid, v.title, v.owner_name")
    return _iter_search(keyword, table="raw_danmaku", col="content",
                        id_col="id", alias="d", cols=cols,
                        start=start, end=end, exclude=exclude, page=page)


def search_videos(keyword: str, *, start: str = "", end: str = "",
                  limit: int = 50) -> dict:
    """全局搜索视频（标题/BV/UP昵称/UP UID 匹配）。

    多关键词用逗号分隔，全部 AND 必须命中。
    total 语义同 search_comments（LIMIT+1 探测截断）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"rows": [], "total": 0}
    kw_list = _parse_keywords(keyword)
    conn = _r()
    try:
        with conn.cursor() as cur:
            # 每个关键词都要命中（标题/BV/UP 任一字段）
            all_conds, all_params = [], []
            for kw in kw_list:
                pat = "%" + _like_esc(kw) + "%"
                all_conds.append("(v.title LIKE %s OR v.bvid LIKE %s "
                                 "OR v.owner_name LIKE %s OR v.owner_uid LIKE %s)")
                all_params.extend([pat] * 4)
            where_content = " AND ".join(all_conds)
            conditions = [where_content]
            params: list = list(all_params)
            if start:
                conditions.append("FROM_UNIXTIME(v.pubdate) >= %s")
                params.append(start)
            if end:
                conditions.append("FROM_UNIXTIME(v.pubdate) <= %s")
                params.append(end)
            where = " AND ".join(conditions)
            sql = (
                "SELECT v.aid, v.bvid, v.title, v.owner_uid, v.owner_name, v.pubdate, "
                "       v.part, "
                "       (SELECT COUNT(*) FROM raw_comments c WHERE c.video_aid = v.aid) AS comment_count, "
                "       (SELECT COUNT(*) FROM raw_danmaku d WHERE d.video_aid = v.aid) AS danmaku_count "
                "FROM meta_video v "
                f"WHERE {where} "
                "ORDER BY v.pubdate DESC LIMIT %s"
            )
            cur.execute(sql, [*params, limit + 1])
            rows = list(cur.fetchall())
            truncated = len(rows) > limit
            return {"rows": rows[:limit],
                    "total": None if truncated else len(rows)}
    finally:
        conn.close()


# ---------------- 排行榜 ----------------

def _time_cond(alias: str, start: str, end: str) -> tuple[str, tuple]:
    """时间范围条件（与 _dt_cond 相同语义，供排行榜复用）。"""
    cond, params = "", []
    if start:
        cond += f" AND {alias} >= %s"
        params.append(start)
    if end:
        cond += f" AND {alias} <= %s"
        params.append(end)
    return cond, params


def get_rankings(*, start: str = "", end: str = "", top: int = 100,
                 min_text_len: int = 0, progress_cb=None, stop_event=None,
                 which: list[str] | None = None,
                 rank_done_cb=None) -> dict:
    """跨 E 全 shard 聚合排行榜：逐榜完成逐榜回调。

    which: 要跑的榜名列表，None = 全部5榜。
    rank_done_cb(key, rows): 每个榜跑完立即回调（可逐个落缓存+逐个显示）。
    progress_cb(done, total) 每完成一榜回调。
    stop_event 可选：置位后查完当前仓即停（抛 StatsCancelled，调用方按取消处理）。
    """
    tc, tp = _time_cond("c.ctime", start, end)
    out: dict[str, list[dict]] = {}
    done_count = [0]
    total_jobs = [0]  # jobs 建好后赋值（_report 调用时已就绪）
    lock = threading.Lock()

    def _report():
        with lock:
            done_count[0] += 1
            if progress_cb:
                try: progress_cb(done_count[0], total_jobs[0])
                except: pass

    def _query_shard(db, sql, params):
        """单仓查询，返回 rows（线程复用连接，跨榜只建连一次）。"""
        try:
            conn = _rcached("archive", db, 600)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        except Exception:
            _drop_cached("archive", db, 600)
            raise

    def _merge_users(all_rows):
        merged = {}
        for r in all_rows:
            uid = int(r["uid"]); cnt = int(r["cnt"])
            if uid in merged:
                merged[uid]["cnt"] += cnt
            else:
                merged[uid] = {"uid": uid, "uname": r.get("uname") or str(uid), "cnt": cnt}
        lst = sorted(merged.values(), key=lambda x: x["cnt"], reverse=True)
        return lst[:100]

    def _merge_videos(all_rows):
        merged = {}
        for r in all_rows:
            aid = int(r["aid"]); cnt = int(r["cnt"])
            if aid in merged:
                merged[aid]["cnt"] += cnt
            else:
                merged[aid] = {"aid": aid, "bvid": r.get("bvid"), "title": r.get("title"),
                               "owner_name": r.get("owner_name"), "cnt": cnt}
        lst = sorted(merged.values(), key=lambda x: x["cnt"], reverse=True)
        return lst[:100]

    def _merge_ups(all_rows):
        merged = {}
        for r in all_rows:
            uid = int(r["uid"]); cnt = int(r["cnt"])
            if uid in merged:
                merged[uid]["cnt"] += cnt
            else:
                merged[uid] = {"uid": uid, "uname": r.get("uname") or "", "cnt": cnt}
        lst = sorted(merged.values(), key=lambda x: x["cnt"], reverse=True)
        return lst[:100]

    def _merge_texts(all_rows):
        merged = {}
        for r in all_rows:
            content = r["content"]; cnt = int(r["cnt"])
            if content in merged:
                merged[content]["cnt"] += cnt
                merged[content]["senders"] = max(merged[content].get("senders", 0),
                                                  int(r.get("senders", 0) or 0))
            else:
                merged[content] = {"content": content, "cnt": cnt,
                                   "senders": int(r.get("senders", 0) or 0),
                                   "tlen": int(r.get("tlen", 0) or 0)}
        lst = sorted(merged.values(), key=lambda x: x["cnt"], reverse=True)
        return lst[:100]

    # 5 榜定义（必须放在 _merge_* 之后，字典求值时它们已存在）
    all_jobs = {
        "top_users": (
            "SELECT t.uid, MAX(COALESCE(u.uname, CAST(t.uid AS CHAR))) AS uname, t.cnt "
            "FROM (SELECT c.uid, COUNT(*) AS cnt FROM raw_comments c "
            "WHERE c.uid > 0 GROUP BY c.uid ORDER BY cnt DESC LIMIT %s) t "
            "LEFT JOIN dim_user u ON u.uid = t.uid GROUP BY t.uid, t.cnt ORDER BY t.cnt DESC",
            [top], _merge_users),
        "top_texts": (
            "SELECT content, COUNT(*) AS cnt, COUNT(DISTINCT uid) AS senders, "
            "CHAR_LENGTH(content) AS tlen FROM raw_comments "
            "WHERE content IS NOT NULL AND content != '' "
            "AND ctime >= DATE_SUB(NOW(), INTERVAL 30 DAY) "
            + (" AND CHAR_LENGTH(content) >= %s" if min_text_len > 0 else "")
            + " GROUP BY content ORDER BY cnt DESC LIMIT %s",
            [*([min_text_len] if min_text_len > 0 else []), top], _merge_texts),
        "top_liked": (
            "SELECT c.rpid, c.uid, COALESCE(u.uname,'') AS uname, c.content, "
            "c.like_num, c.ctime, c.ip_location, v.bvid, v.title, v.owner_name "
            "FROM raw_comments c LEFT JOIN dim_user u ON u.uid=c.uid "
            "LEFT JOIN meta_video v ON v.aid=c.video_aid "
            "WHERE c.like_num > 0 ORDER BY c.like_num DESC LIMIT %s",
            [top], lambda rows: sorted(rows, key=lambda x: int(x.get("like_num",0) or 0), reverse=True)[:100]),
        "top_videos": (
            "SELECT t.aid, v.bvid, v.title, v.owner_name, t.cnt "
            "FROM (SELECT c.video_aid AS aid, COUNT(*) AS cnt FROM raw_comments c "
            "WHERE c.video_aid > 0 GROUP BY c.video_aid ORDER BY cnt DESC LIMIT %s) t "
            "LEFT JOIN meta_video v ON v.aid=t.aid ORDER BY t.cnt DESC",
            [top], _merge_videos),
        "top_ups": (
            "SELECT v.owner_uid AS uid, MAX(COALESCE(v.owner_name,'')) AS uname, "
            "SUM(g.cnt) AS cnt FROM "
            "(SELECT c.video_aid AS aid, COUNT(*) AS cnt FROM raw_comments c "
            "WHERE c.video_aid > 0 GROUP BY c.video_aid) g "
            "JOIN meta_video v ON v.aid=g.aid WHERE v.owner_uid > 0 "
            "GROUP BY v.owner_uid ORDER BY cnt DESC LIMIT %s",
            [top], _merge_ups),
    }
    jobs = [(k, *all_jobs[k]) for k in (which or list(all_jobs.keys()))]
    total_jobs[0] = len(jobs)

    def _run_ranking(key, sql, params, merge_fn):
        """单榜：串行跑全仓，合并结果；取消时查完当前仓即停"""
        import time as _time
        dbs = _shard_dbs()
        all_rows = []
        for db in dbs:
            if stop_event is not None and stop_event.is_set():
                raise StatsCancelled()
            try:
                _t0 = _time.monotonic()
                rows = _query_shard(db, sql, params)
                all_rows.extend(rows)
                _dt = _time.monotonic() - _t0
                if _dt > 10:
                    logger.warning("排行榜 %s 仓 %s 耗时 %.0fs（行数 %s）", key, db, _dt, len(rows))
            except StatsCancelled:
                raise
            except Exception:
                continue
        out[key] = merge_fn(all_rows)

    def _worker(key, sql, params, merge_fn):
        try:
            _run_ranking(key, sql, params, merge_fn)
        except StatsCancelled:
            raise
        except Exception as exc:
            out[key] = []
            logger.warning("排行榜 %s 失败: %s", key, exc)
        finally:
            _report()
            if rank_done_cb:
                try:
                    rank_done_cb(key, out.get(key, []))
                except Exception:
                    pass

    # 串行跑选中的榜（HDD 上多榜并行寻道打架）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        futs = [pool.submit(_worker, key, sql, params, merge_fn)
                for key, sql, params, merge_fn in jobs]
        cancelled = False
        for f in futs:
            try:
                f.result()
            except StatsCancelled:
                cancelled = True
            except Exception as exc:
                logger.warning("排行榜 worker 异常: %s", exc)
    if cancelled and (stop_event is not None and stop_event.is_set()):
        raise StatsCancelled()
    return out