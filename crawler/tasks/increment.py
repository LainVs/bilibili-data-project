"""增量采集任务：对已完成视频做最新评论/弹幕增量补充。"""
from __future__ import annotations

import datetime
import logging

from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher
from crawler.api.danmaku import DanmakuFetcher

logger = logging.getLogger(__name__)


def incremental_update(client: ApiClient, video_aids: list[int],
                       *, progress_cb=None, item_cb=None, stop_flag=None) -> None:
    """对给定视频列表执行增量采集。

    增量策略：
      - 评论：只拉"上次已采集时刻之后"的新评论（min_ctime 断点），
        超时遇到旧评论即停，不重拉全树。
      - 弹幕：重拉今日一天的历史弹幕。
    """
    from storage import dao

    if not video_aids:
        return
    metas = dao.list_video_meta(video_aids)
    for idx, meta in enumerate(metas, start=1):
        if stop_flag is not None and stop_flag.is_set():
            logger.info("用户停止增量采集")
            return
        aid = meta["aid"]
        if progress_cb is not None:
            try:
                progress_cb(f"[{idx}/{len(metas)}] 视频 aid={aid} 增量更新中…")
            except Exception:
                pass
        # 评论增量
        try:
            min_ctime = dao.get_max_comment_ctime(aid, ref_type=1)
            comments = CommentFetcher(client).collect(
                aid, with_sub=True, mode=2, min_ctime=min_ctime or None,
                item_cb=item_cb, stop_flag=stop_flag)
            from crawler.pipelines.clean import build_user_records, normalize_comment

            clean = [normalize_comment(c, aid) for c in comments]
            affected = dao.upsert_comments(clean)
            if clean:
                dao.upsert_users(build_user_records(clean, []))
            logger.info("增量评论 aid=%s 处理 %s 条/更新 %s 行(断点=%s)",
                        aid, len(clean), affected, min_ctime)
            if progress_cb is not None:
                try:
                    progress_cb(f"[{idx}/{len(metas)}] aid={aid} 评论 +{len(clean)}")
                except Exception:
                    pass
        except Exception as exc:
            logger.error("增量评论失败 aid=%s: %s", aid, exc)

        # 弹幕增量（今日）
        try:
            dm = DanmakuFetcher(client)
            recent = dm.fetch_day(meta["cid"], datetime.date.today())
            from crawler.pipelines.clean import normalize_danmaku

            clean = [normalize_danmaku(d, aid) for d in recent]
            dao.upsert_danmaku(clean)
            logger.info("增量弹幕 aid=%s 处理 %s 条", aid, len(clean))
        except Exception as exc:
            logger.error("增量弹幕失败 aid=%s: %s", aid, exc)


def update_ups_batch(client: ApiClient, uids: list[int], *,
                     max_pages: int = 100, incremental: bool = True,
                     item_cb=None, progress_cb=None, stop_flag=None,
                     workers: int = 3) -> dict:
    """对一批历史博主执行 UP 级增量更新：重拉视频列表，抓新发的视频+新评论。

    增量模式下 crawl_up_videos 会跳过已完成视频、只补新评论（断点续传）。

    返回 {"ups", "ups_ok", "videos", "comments", "errors"}
    """
    from storage import dao

    stats = {"ups": len(uids), "ups_ok": 0, "videos": 0, "comments": 0, "errors": []}
    if not uids:
        return stats
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
        return stats

    from crawler.tasks.up_crawl import crawl_up_videos

    for idx, uid in enumerate(uids, start=1):
        if stop_flag is not None and stop_flag.is_set():
            stats["errors"].append("用户停止")
            break
        if progress_cb:
            try:
                progress_cb(f"[UP {idx}/{len(uids)}] UID={uid} 增量更新中…")
            except Exception:
                pass
        logger.info("批量UP增量更新: %s/%s UID=%s", idx, len(uids), uid)
        try:
            r = crawl_up_videos(client, uid,
                                with_comments=True, with_danmaku=False,
                                incremental=incremental, max_pages=max_pages,
                                item_cb=item_cb, progress_cb=progress_cb,
                                stop_flag=stop_flag, workers=workers)
            stats["videos"] += r.get("videos", 0)
            stats["comments"] += r.get("comments", 0)
            stats["ups_ok"] += 1
            if r.get("errors"):
                stats["errors"].extend(r["errors"])
        except Exception as exc:
            stats["errors"].append(f"UP{uid}: {exc}")
            logger.exception("批量UP增量失败 UID=%s", uid)
    return stats