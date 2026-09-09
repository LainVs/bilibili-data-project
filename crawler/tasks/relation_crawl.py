"""按用户社交关系采集：对该用户（mid）的关注/粉丝列表中的每人，采集其视频的评论/弹幕。

复用 up_crawl.crawl_up_videos（本身含视频并发、增量去重、流式入库），
故这里只负责：拉关系列表 -> 逐人调用 crawl_up_videos 聚合统计。
"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient
from crawler.api.relation import RelationFetcher

logger = logging.getLogger(__name__)


def crawl_relation_videos(client: ApiClient, mid: int, *,
                          direction: str = "both",
                          max_users: int = 0,
                          with_comments: bool = True, with_danmaku: bool = False,
                          incremental: bool = True, max_pages: int = 100,
                          item_cb=None, progress_cb=None, stop_flag=None,
                          workers: int = 3) -> dict:
    """采集关注/粉丝列表内各用户的视频评论/弹幕。

    direction: "following" | "follower" | "both"
    返回 {"mid", "related", "ups", "ups_ok", "videos", "fetched",
          "comments", "danmaku", "errors"}
    """
    from crawler.tasks.up_crawl import crawl_up_videos

    stats = {"mid": mid, "related": 0, "ups": 0, "ups_ok": 0, "videos": 0,
             "fetched": 0, "comments": 0, "danmaku": 0, "errors": []}
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
        return stats

    fetcher = RelationFetcher(client)
    rel_users: list[dict] = []
    if direction in ("following", "both"):
        try:
            rel_users.extend(fetcher.list_followings(
                mid, max_users=max_users, progress_cb=progress_cb,
                stop_flag=stop_flag))
        except Exception as exc:
            stats["errors"].append(f"关注列表: {exc}")
            if progress_cb:
                progress_cb(f"错误：{exc}")
    if direction in ("follower", "both"):
        try:
            rel_users.extend(fetcher.list_followers(
                mid, max_users=max_users, progress_cb=progress_cb,
                stop_flag=stop_flag))
        except Exception as exc:
            stats["errors"].append(f"粉丝列表: {exc}")
            if progress_cb:
                progress_cb(f"错误：{exc}")

    # 去重并按入库顺序稳定（沿用优先关注后粉丝）
    seen: set[int] = set()
    unique: list[dict] = []
    for u in rel_users:
        uid = u.get("mid") or 0
        if uid and uid not in seen:
            seen.add(uid)
            unique.append(u)
    unique = unique[:max_users] if max_users > 0 else unique
    stats["related"] = len(unique)
    logger.info("用户 %s 关系列表 去重后共 %s 人", mid, len(unique))
    if progress_cb:
        progress_cb(f"去重后共 {len(unique)} 位关系用户，逐一采集…")

    # 注意：不写 up_crawl_stat（该表语义是"UP 的视频总数"，
    # 关系采集的人是消费者而非视频所有者）

    for idx, u in enumerate(unique, start=1):
        if stop_flag is not None and stop_flag.is_set():
            stats["errors"].append("用户停止")
            break
        uid = u["mid"]
        name = u.get("uname", "")
        stats["ups"] += 1
        if progress_cb:
            progress_cb(f"[关系 {idx}/{len(unique)}] 开始采集 {name or uid}({uid})")
        try:
            r = crawl_up_videos(
                client, uid,
                with_comments=with_comments, with_danmaku=with_danmaku,
                incremental=incremental, max_pages=max_pages,
                item_cb=item_cb, progress_cb=progress_cb,
                stop_flag=stop_flag, workers=workers)
            stats["ups_ok"] += 1
            stats["videos"] += r.get("videos", 0)
            stats["fetched"] += r.get("fetched", 0)
            stats["comments"] += r.get("comments", 0)
            stats["danmaku"] += r.get("danmaku", 0)
            if r.get("errors"):
                stats["errors"].extend(r["errors"])
        except Exception as exc:
            stats["errors"].append(f"UP{uid}: {exc}")
            logger.exception("关系UP采集失败 UID=%s", uid)
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
    return stats