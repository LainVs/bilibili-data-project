"""采集任务编排：单视频全量采集（弹幕+评论），含断点/完整性校验。"""
from __future__ import annotations

import logging
import time

from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher
from crawler.api.danmaku import DanmakuFetcher
from crawler.api.video import parse_info

logger = logging.getLogger(__name__)

VIDEO_INFO_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"


def fetch_video_meta(client: ApiClient, bvid: str) -> dict:
    """获取单视频元信息（aid/cid/标题/UP主/时间）。"""
    payload = client.get_json(VIDEO_INFO_API.format(bvid=bvid))
    info = parse_info(payload.get("data") or {})
    if not info.get("aid"):
        raise ValueError(f"视频信息解析失败: {bvid}")
    return info


def crawl_one_video(client: ApiClient, bvid: str, *, with_comments: bool = True,
                    with_danmaku: bool = True, incremental: bool = False) -> dict:
    """采集单个视频的评论与弹幕并存库。

    返回统计 {video_aid, comments, danmaku, users, errors: [..]}
    """
    stats: dict = {
        "bvid": bvid,
        "comments": 0,
        "danmaku": 0,
        "users": 0,
        "errors": [],
    }
    try:
        meta = fetch_video_meta(client, bvid)
        from storage import dao

        dao.upsert_video([meta])
        aid = meta["aid"]
        stats["video_aid"] = aid
        logger.info("开始采集 %s (aid=%s) 标题=%s", bvid, aid, meta.get("title", ""))

        if with_comments:
            try:
                as_dict = dao.get_max_comment_ctime(aid, ref_type=1)
                fetcher = CommentFetcher(client)
                comments = fetcher.collect(aid, with_sub=True,
                                           mode=2 if incremental else 3,
                                           min_ctime=as_dict or None)
                clean = []
                from crawler.pipelines.clean import normalize_comment

                for c in comments:
                    clean.append(normalize_comment(c, aid))
                dao.upsert_comments(clean)
                dao.upsert_users(_users_from_comments(clean))
                stats["comments"] = len(clean)
            except Exception as exc:
                stats["errors"].append(f"评论: {exc}")
                dao.update_progress(aid, comment_done=False, last_error=str(exc))

        if with_danmaku:
            try:
                dm = DanmakuFetcher(client)
                items = dm.collect(aid, meta["cid"], meta["pubdate"])
                from crawler.pipelines.clean import normalize_danmaku

                clean = [normalize_danmaku(d, aid) for d in items]
                dao.upsert_danmaku(clean)
                stats["danmaku"] = len(clean)
            except Exception as exc:
                stats["errors"].append(f"弹幕: {exc}")
                dao.update_progress(aid, danmaku_done=False, last_error=str(exc))

        dao.update_progress(aid, danmaku_done=with_danmaku, comment_done=with_comments, last_error="")
        if stats["errors"]:
            dao.update_progress(aid, last_error=";".join(stats["errors"]))
    except Exception as exc:
        stats["errors"].append(f"整体: {exc}")
        logger.error("采集失败 bvid=%s: %s", bvid, exc)
    finally:
        time.sleep(0.5)
    return stats


def _users_from_comments(comments: list[dict]) -> list[dict]:
    from crawler.pipelines.clean import build_user_records

    return build_user_records(comments, [])
