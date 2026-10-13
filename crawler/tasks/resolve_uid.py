"""对已入库且未解析 UID 的弹幕执行 mid_hash -> UID 反查。"""
from __future__ import annotations

import logging

from crc32_crack.core import UidResolver, build_forward_table

logger = logging.getLogger(__name__)


def resolve_batch(bvid: str, resolver: UidResolver | None = None, limit: int = 10000) -> dict:
    """对某视频未解析 UID 的弹幕做反查，并回写数据库。

    返回 {"video_aid": int, "unresolved": int, "resolved": int, "total": int}
    """
    from storage import dao
    from storage.db import get_conn

    resolver = resolver or UidResolver(use_db_cache=True)
    meta = dao.find_video_by_bvid(bvid)
    if not meta:
        logger.warning("视频不存在: %s", bvid)
        return {"video_aid": 0, "unresolved": 0, "resolved": 0, "total": 0}
    aid = meta["aid"]

    conn = get_conn()
    try:
        records = _load_unparsed(conn, aid, limit)
        updates: list[tuple[int, int]] = []
        unresolved = 0
        for rec in records:
            mid_hash = rec["mid_hash"]
            uid, ok = resolver.resolve(mid_hash)
            if ok:
                updates.append((uid, rec["id"]))
            else:
                unresolved += 1
                resolver.cache_result(mid_hash, 0, resolved=False)
        if updates:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE raw_danmaku SET uid = %s, is_parsed = 1 WHERE id = %s",
                    updates,
                )
            conn.commit()
    finally:
        conn.close()

    total = len(records)
    logger.info("反查完成: 共 %s 条, 解析 %s 条, 未解析 %s 条", total, len(updates), unresolved)
    return {"video_aid": aid, "unresolved": unresolved, "resolved": len(updates), "total": total}


def _load_unparsed(conn, aid: int, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, mid_hash FROM raw_danmaku "
            "WHERE video_aid = %s AND is_parsed = 0 AND mid_hash != '' "
            "LIMIT %s",
            (aid, limit),
        )
        return list(cur.fetchall())


def resolve_all_from_library() -> dict:
    """从库内已知 uid 构建 mid_hash→uid 正向表，回填全部未解析弹幕。

    核心逻辑：弹幕的 mid_hash = crc32(str(uid))。它不可逆，所以
    直接用"库里已知的真实 uid"正向生成哈希倒排索引，凡是已知用户
    发的弹幕都能立即命中。评论采得越多，uid 池越大，命中越多。

    返回 {"uids": int, "hashes": int, "matched": int}
    """
    from storage import dao
    from storage.db import get_conn

    uids = dao.list_user_uids()
    if not uids:
        return {"uids": 0, "hashes": 0, "matched": 0}

    table = build_forward_table(uids)
    hashes = dao.list_unparsed_danmaku_hashes()
    matched = {h for h in hashes if h in table}

    # 1) 缓存所有命中结果（含未命中标记为 resolved=0）
    bulk = [(h, table[h], True) for h in matched]
    dao.bulk_set_crc_cache(bulk)

    # 2) 回写弹幕 uid 并标记解析完成
    conn = get_conn()
    try:
        matched_hashes = list(matched)
        if matched_hashes:
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE raw_danmaku SET uid = %s, is_parsed = 1 "
                    "WHERE mid_hash = %s AND is_parsed = 0",
                    [(table[h], h) for h in matched_hashes],
                )
                conn.commit()
                hit = cur.rowcount
        else:
            hit = 0
    finally:
        conn.close()

    logger.info(
        "库内反查: 已知uid=%s 未解析hash=%s 命中=%s 回写弹幕=%s",
        len(uids), len(hashes), len(matched), hit,
    )
    return {"uids": len(uids), "hashes": len(hashes), "matched": len(matched)}
