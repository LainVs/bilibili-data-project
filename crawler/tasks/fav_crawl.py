"""按用户收藏夹采集：列出用户全部收藏夹内的视频，逐视频采集评论/弹幕。

复用 up_crawl._crawl_one 的单视频采集（元信息+评论+弹幕+流式写库），
兼容增量去重、stop_flag、item_cb/progress_cb 回调。
"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient
from crawler.api.fav import FavoritesFetcher

logger = logging.getLogger(__name__)


def crawl_user_favorites(client: ApiClient, mid: int, *,
                         with_comments: bool = True, with_danmaku: bool = False,
                         incremental: bool = True, max_pages: int = 50,
                         item_cb=None, progress_cb=None, stop_flag=None,
                         workers: int = 3) -> dict:
    """采集某用户全部收藏夹中视频的评论/弹幕。

    返回 {"mid", "folders", "videos", "fetched", "comments", "danmaku", "errors"}。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from threading import Lock

    from crawler.tasks.up_crawl import _crawl_one
    from storage import dao

    stats = {"mid": mid, "folders": 0, "videos": 0, "fetched": 0,
             "comments": 0, "danmaku": 0, "errors": []}
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
        return stats

    # 增量续跑：批量进度预载 + 收藏夹页"整页已采集即停"
    progress_map: dict[int, dict] = {}

    def _row_ok(row: dict | None) -> bool:
        return bool(row and (not with_comments or row.get("comment_done"))
                    and (not with_danmaku or row.get("danmaku_done")))

    def _load_progress(aids: list[int]) -> dict[int, dict]:
        try:
            batch = dao.get_progress_batch(aids)
            progress_map.update(batch)
            return batch
        except Exception:
            return {}

    def _all_done_page(items: list[dict]) -> bool:
        aids = [v.get("aid") for v in items if v.get("aid")]
        if not aids:
            return False
        batch = _load_progress(aids)
        return all(_row_ok(batch.get(a)) for a in aids)

    def _skip_done(aid: int) -> bool:
        row = progress_map.get(aid)
        if row is not None or aid in progress_map:
            return _row_ok(row)
        try:
            row = dao.get_progress(aid)
        except Exception:
            row = None
        progress_map[aid] = row or {}
        return _row_ok(row)

    fetcher = FavoritesFetcher(client)
    try:
        folders = fetcher.list_folders(mid, progress_cb=progress_cb)
    except Exception as exc:
        stats["errors"].append(f"收藏夹列表获取失败: {exc}")
        if progress_cb:
            progress_cb(f"错误：{exc}")
        return stats
    stats["folders"] = len(folders)
    logger.info("用户 mid=%s 共 %s 个收藏夹", mid, len(folders))
    if progress_cb:
        progress_cb(f"用户共 {len(folders)} 个收藏夹，开始逐一拉取视频…")

    # 收集全部收藏夹内的视频，跨夹按 bvid 去重
    seen: set[str] = set()
    videos: list[dict] = []
    stopped_early = False
    for folder in folders:
        if stop_flag is not None and stop_flag.is_set():
            stats["errors"].append("用户停止")
            return stats
        fid = folder["media_id"]
        if progress_cb:
            progress_cb(f"—— 收藏夹：{folder.get('title', fid)}（{folder.get('media_count', '?')} 个）")
        try:
            items = fetcher.list_folder_videos(fid, max_pages=max_pages,
                                               progress_cb=progress_cb,
                                               all_done_check=_all_done_page if incremental else None,
                                               stop_flag=stop_flag)
        except Exception as exc:
            stats["errors"].append(f"收藏夹{fid}失败: {exc}")
            logger.error("收藏夹 %s 视频列表失败: %s", fid, exc)
            continue
        stopped_early = stopped_early or getattr(fetcher, "early_stopped", False)
        for v in items:
            bvid = v.get("bvid")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            videos.append(v)
    stats["videos"] = len(videos)
    if progress_cb:
        progress_cb(f"收藏夹内共去重后 {len(videos)} 个视频，开始采集…"
                    + ("（旧视频均已采集，仅拉取最新部分）" if stopped_early else ""))

    # 收藏夹视频属于大量不同 UP，不写 up_crawl_stat.video_total（语义不符）

    lock = Lock()

    def _video(task: tuple[int, dict]) -> None:
        idx, v = task
        if stop_flag is not None and stop_flag.is_set():
            return
        bvid = v.get("bvid")
        aid = v.get("aid") or 0
        if not bvid:
            return
        if progress_cb:
            progress_cb(f"[{idx}/{len(videos)}] {v.get('title', '')[:40]}")
        if incremental and aid and _skip_done(aid):
            logger.info("跳过已完成 aid=%s %s", aid, bvid)
            return
        vclient = ApiClient(stop_event=stop_flag)
        try:
            r = _crawl_one(vclient, bvid, aid,
                           with_comments=with_comments, with_danmaku=with_danmaku,
                           incremental=incremental, item_cb=item_cb,
                           progress_cb=progress_cb, stop_flag=stop_flag,
                           meta_hint=v)
        finally:
            vclient.close()
        with lock:
            stats["fetched"] += 1
            stats["comments"] += r.get("comments", 0)
            stats["danmaku"] += r.get("danmaku", 0)
            if r.get("errors"):
                stats["errors"].extend(r["errors"])
        if aid and (stop_flag is None or not stop_flag.is_set()):
            errs = r.get("errors")
            try:
                if errs:
                    dao.update_progress(aid, last_error="; ".join(map(str, errs))[:500])
                else:
                    dao.update_progress(aid, danmaku_done=with_danmaku,
                                        comment_done=with_comments, last_error="")
            except Exception:
                pass

    tasks = [(idx, v) for idx, v in enumerate(videos, start=1) if v.get("bvid")]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [pool.submit(_video, t) for t in tasks]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                stats["errors"].append(f"worker异常: {exc}")
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
    return stats
