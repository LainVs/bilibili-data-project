"""榜单与趋势：跨 E 全 shard 聚合排行榜、每日/每小时评论量。"""
from __future__ import annotations

import threading

from storage.dao._core import (
    StatsCancelled,
    _begin_stats_run,
    _drop_cached,
    _end_stats_run,
    _rcached,
    _shard_dbs,
    _stats_should_abort,
    _uid_same_dsn,
    logger,
)


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
                except Exception: pass

    def _query_shard(db, sql, params):
        """单仓查询，返回 rows（线程复用连接，跨榜只建连一次）。"""
        try:
            conn = _rcached("archive", db, 180)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        except Exception:
            _drop_cached("archive", db, 180)
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
            if _stats_should_abort(stop_event):
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
    _begin_stats_run()
    try:
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
    finally:
        _end_stats_run()
    if cancelled and _stats_should_abort(stop_event):
        raise StatsCancelled()
    return out


def get_daily_comment_counts(days: int = 30) -> list[dict]:
    """近 N 天每日评论量（跨 D+E 各仓求和，失败的仓跳过）。

    返回 [{"day": "YYYY-MM-DD", "cnt": n}]（缺失日期补 0，保证定长）。
    统计 V2 趋势图用；任何异常只记日志并返回已有数据，不抛。
    """
    import datetime as _dt

    days = max(1, min(365, int(days or 30)))
    today = _dt.date.today()
    base = {str(today - _dt.timedelta(days=i)): 0 for i in range(days - 1, -1, -1)}
    start = str(today - _dt.timedelta(days=days - 1))
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
    for db, role in targets:
        try:
            import pymysql

            from config import get_config
            cfg = get_config()
            base_cfg = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write",
                               cfg.mysql)
            conn = pymysql.connect(host=base_cfg["host"], port=base_cfg["port"],
                                   user=base_cfg["user"], password=base_cfg["password"],
                                   database=db, charset=base_cfg["charset"],
                                   cursorclass=pymysql.cursors.DictCursor,
                                   connect_timeout=5, read_timeout=60)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT DATE(ctime) AS d, COUNT(*) AS n FROM `{db}`.raw_comments "
                        "WHERE ctime >= %s GROUP BY DATE(ctime)", (start,))
                    for r in cur.fetchall():
                        k = str(r.get("d") or "")
                        if k in base:
                            base[k] += int(r.get("n") or 0)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("每日评论量 %s(%s) 跳过: %s", db, role, exc)
    return [{"day": k, "cnt": base[k]} for k in sorted(base)]


def get_hourly_comment_counts() -> list[dict]:
    """24 小时评论分布（跨 D+E 各仓求和，失败的仓跳过）。

    返回 [{"hour": 0..23, "cnt": n}]，统计 V2 时段分布图用。
    """
    cnt = [0] * 24
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
    for db, role in targets:
        try:
            import pymysql

            from config import get_config
            cfg = get_config()
            base_cfg = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write",
                               cfg.mysql)
            conn = pymysql.connect(host=base_cfg["host"], port=base_cfg["port"],
                                   user=base_cfg["user"], password=base_cfg["password"],
                                   database=db, charset=base_cfg["charset"],
                                   cursorclass=pymysql.cursors.DictCursor,
                                   connect_timeout=5, read_timeout=60)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT HOUR(ctime) AS h, COUNT(*) AS n FROM `{db}`.raw_comments "
                        "GROUP BY HOUR(ctime)")
                    for r in cur.fetchall():
                        try:
                            h = int(r.get("h"))
                        except (TypeError, ValueError):
                            continue
                        if 0 <= h < 24:
                            cnt[h] += int(r.get("n") or 0)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("小时分布 %s(%s) 跳过: %s", db, role, exc)
    return [{"hour": h, "cnt": cnt[h]} for h in range(24)]

