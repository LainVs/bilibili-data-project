"""采集族命令：crawl / increment / serve / up / post。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_COOKIE_WARN = "未配置 Cookie（评论接口需要登录）。请在 config/settings.ini 或环境变量 BILI_COOKIE 中配置"


def _read_bvid_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]


def _make_client():
    """按当前配置构造 ApiClient，并在缺 Cookie 时给出一次性告警。"""
    from config import get_config
    from crawler.api.client import ApiClient

    cfg = get_config()
    if not cfg.cookie:
        logger.warning(_COOKIE_WARN)
    return ApiClient(), cfg


def run_crawl(args) -> int:
    """全量/增量采集一个或多个视频（弹幕+评论）。"""
    from crawler.tasks.full import crawl_one_video
    from storage import dao

    client, _cfg = _make_client()

    bvids = list(args.bvids or [])
    if args.bvid_file:
        bvids.extend(_read_bvid_file(args.bvid_file))
    if not bvids:
        logger.error("请提供至少一个 BV 或 --bvid-file")
        return 2

    with_comments = not args.danmaku_only
    with_danmaku = not args.comments_only
    for bvid in bvids:
        if args.incremental:
            meta = dao.find_video_by_bvid(bvid)
            if meta:
                row = dao.get_progress(meta["aid"]) or {}
                done = (not with_comments or bool(row.get("comment_done"))) and \
                       (not with_danmaku or bool(row.get("danmaku_done")))
                if done:
                    logger.info("跳过已完成 %s (增量模式)", bvid)
                    continue
        stats = crawl_one_video(client, bvid, with_comments=with_comments, with_danmaku=with_danmaku,
                                incremental=args.incremental)
        logger.info("采集结果 %s: 评论=%s 弹幕=%s 错误=%s",
                    bvid, stats["comments"], stats["danmaku"], stats["errors"])
    return 0


def run_increment(args) -> int:
    """对所有已采集视频做一次增量更新。"""
    from crawler.tasks.increment import incremental_update
    from storage import dao

    client, _cfg = _make_client()

    aids = dao.list_all_video_aids()
    if not aids:
        logger.info("无已采集视频，跳过增量")
        return 0
    incremental_update(client, aids)
    return 0


def run_serve(args) -> int:
    """启动定时增量调度，前台常驻直到 Ctrl-C。"""
    from crawler.tasks.scheduler import start_scheduler

    client, _cfg = _make_client()

    scheduler = start_scheduler(client)
    logger.info("后台增量调度运行中，Ctrl-C 退出")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        scheduler.shutdown(wait=False)
        return 0


def run_up(args) -> int:
    """按 UP 主批量采集其全部视频。"""
    from config import get_config
    from crawler.api.client import ApiClient
    from crawler.tasks.up_crawl import crawl_up_videos

    cfg = get_config()
    if not cfg.cookie:
        logger.warning(_COOKIE_WARN)
    client = ApiClient()
    result = crawl_up_videos(
        client, args.uid,
        with_comments=not args.danmaku_only,
        with_danmaku=not args.comments_only,
        incremental=not args.full,
        max_pages=args.max_pages,
        workers=cfg.up_workers,
    )
    logger.info("UP采集结果 %s: 视频=%s 新采=%s 评论=%s 弹幕=%s 错误=%s",
                args.uid, result["videos"], result["fetched"],
                result["comments"], result["danmaku"], result["errors"])
    return 0


def run_post(args) -> int:
    """采集帖子/动态/专栏评论（可多个目标）。"""
    from crawler.api.client import ApiClient
    from crawler.tasks.post_crawl import crawl_post_link

    client = ApiClient()
    for target in args.targets:
        try:
            result = crawl_post_link(client, target, incremental=args.incremental)
            logger.info("帖子采集 %s 评论 %s 条", target, result.get("comments", 0))
        except Exception as exc:
            logger.error("帖子采集失败 %s: %s", target, exc)
    return 0
