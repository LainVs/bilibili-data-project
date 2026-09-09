"""帖子/动态/专栏评论采集任务：支持单条或按 UP 批量。"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient
from crawler.api.comments import (
    CommentFetcher,
    REPLY_TYPE_ARTICLE,
    REPLY_TYPE_DYNAMIC,
    REPLY_TYPE_VIDEO,
)
from crawler.api.posts import fetch_post_meta

logger = logging.getLogger(__name__)

# 类型速查
TYPE_LABEL = {REPLY_TYPE_VIDEO: "视频", REPLY_TYPE_ARTICLE: "专栏", REPLY_TYPE_DYNAMIC: "动态"}


def crawl_one_post(client: ApiClient, kind: str, pid: int, *,
                   with_sub: bool = True, incremental: bool = False,
                   item_cb=None, stop_flag=None, progress_cb=None) -> dict:
    """采集单个帖子/动态/专栏的评论并存库。

    kind: 'dynamic'(type=17) | 'article'(type=12)
    返回 {"post": pid, "kind": kind, "comments": n, "errors": [...]}
    """
    from storage import dao
    from storage.async_writer import AsyncDbWriter

    ref_type = REPLY_TYPE_DYNAMIC if kind == "dynamic" else REPLY_TYPE_ARTICLE
    stats = {"post": pid, "kind": kind, "comments": 0, "errors": []}
    meta = fetch_post_meta(client, kind, pid)
    if progress_cb:
        progress_cb(f"帖子元信息：{meta.get('title', '')[:60]}")
    dao.upsert_posts([{
        "id": pid,
        "post_type": ref_type,
        "title": meta.get("title", ""),
        "owner_uid": meta.get("owner_uid", 0),
        "pubdate": meta.get("pubdate", 0),
    }])

    from crawler.pipelines.clean import normalize_comment

    min_ctime = dao.get_max_comment_ctime(pid, ref_type=ref_type) if incremental else 0
    fetcher = CommentFetcher(client)
    writer = AsyncDbWriter()
    try:
        def on_cb(kind_: str, raw: dict) -> None:
            if kind_ == "comment":
                # 非阻塞：攒批异步落库（停止后线程秒退，数据后台写完）
                writer.add_comment(
                    normalize_comment(fetcher._normalize(raw, pid, ref_type=ref_type), pid))
                stats["comments"] += 1
            if item_cb:
                try:
                    item_cb(kind_, raw)
                except Exception:
                    pass

        fetcher.collect(pid, with_sub=with_sub, ref_type=ref_type,
                        item_cb=on_cb, stop_flag=stop_flag,
                        mode=2 if incremental else 3,
                        min_ctime=min_ctime or None)
    except Exception as exc:
        stats["errors"].append(f"评论: {exc}")
        logger.error("帖子评论采集失败 kind=%s id=%s: %s", kind, pid, exc)
    finally:
        # 用户停止时不等队列：线程立即退出，数据由写线程继续落库
        writer.close(wait=not (stop_flag is not None and stop_flag.is_set()))
    return stats


def crawl_post_link(client: ApiClient, text: str, *, incremental: bool = False,
                    item_cb=None, stop_flag=None, progress_cb=None) -> dict:
    """根据链接/ID 自动识别并采集该帖子评论。"""
    from crawler.api.posts import parse_post_link

    parsed = parse_post_link(text)
    if not parsed:
        raise ValueError(f"无法识别的帖子链接/ID: {text}")
    return crawl_one_post(client, parsed["kind"], parsed["id"], incremental=incremental,
                          item_cb=item_cb, stop_flag=stop_flag, progress_cb=progress_cb)