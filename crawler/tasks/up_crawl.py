"""按 UP 主批量采集其视频（可选评论/弹幕），智能增量去重、逐视频流式入库。

设计要点（对应"采集一半停止再启动不重复"）：
  - 每个视频评论采集时周期性批量写库，中断不丢失已完成部分。
  - 增量模式：评论用 mode=2(时间倒序)+min_ctime 只拉新；弹幕按 id 幂等。
  - crawl_progress 完成后标记，重跑自动跳过已完成视频。
"""
from __future__ import annotations

import logging
import time

from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher
from crawler.api.space import SpaceFetcher

logger = logging.getLogger(__name__)


def crawl_up_videos(client: ApiClient, mid: int, *,
                    with_comments: bool = True, with_danmaku: bool = False,
                    incremental: bool = True, max_pages: int = 100,
                    item_cb=None, progress_cb=None, video_cb=None,
                    stop_flag=None, pause_flag=None, workers: int = 3) -> dict:
    """抓取某 UP 主全部投稿视频并逐视频采集。

    视频级并发：多个视频并行采集（每个视频独立 ApiClient + 独立节流），
    同一视频内的评论翻页保持串行 + 限速（评论区分页并发才是封号点）。
    每个 worker 传入独立 stop_event/节流器，配合全局 QPS 护栏防过载。

    video_cb(idx, total, title)：可选，每个视频开始前回调（供 GUI 显示当前视频）。

    返回 {"mid", "videos", "fetched", "comments", "danmaku", "errors"}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from threading import Lock

    from storage import dao

    stats = {"mid": mid, "videos": 0, "fetched": 0, "comments": 0,
             "danmaku": 0, "errors": []}

    # 增量续跑：批量进度预载 + 列表页"整页已采集即停"
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
        """一页 items 是否全部已在库完成（供列表 fetcher 提前停页）。"""
        aids = [v.get("aid") for v in items if v.get("aid")]
        if not aids:
            return False
        batch = _load_progress(aids)
        return all(_row_ok(batch.get(a)) for a in aids)

    try:
        fetcher = SpaceFetcher(client)
        videos = fetcher.list_videos(mid, max_pages=max_pages,
                                     progress_cb=progress_cb,
                                     all_done_check=_all_done_page if incremental else None,
                                     stop_flag=stop_flag)
    except Exception as exc:
        stats["errors"].append(f"视频列表获取失败: {exc}")
        if progress_cb:
            progress_cb(f"错误：{exc}")
        return stats
    stats["videos"] = len(videos)
    total_known = fetcher.last_total or len(videos)
    logger.info("UP主 mid=%s 共 %s 个视频%s", mid, total_known,
                "（进度已达边界，提前停页）" if fetcher.early_stopped else "")
    if progress_cb:
        progress_cb(f"UP主共 {total_known} 个视频"
                    + ("，旧视频均已采集，仅拉取最新部分" if fetcher.early_stopped else ""))

    # 记录该 UP 视频总数（供 GUI 汇总表显示 已采/总数）
    try:
        dao.upsert_up_stat(mid, video_total=total_known)
    except Exception:
        pass

    # 每个 worker 使用自己的消息队列 + 独立客户端，汇总到调用方
    lock = Lock()

    def _skip_done(aid: int) -> bool:
        """内存进度 map 命中即免查库；miss 时单查兜底补入 map。"""
        row = progress_map.get(aid)
        if row is not None or aid in progress_map:
            return _row_ok(row)
        try:
            row = dao.get_progress(aid)
        except Exception:
            row = None
        progress_map[aid] = row or {}
        return _row_ok(row)

    def _video(item: tuple[int, dict]) -> None:
        idx, v = item
        if stop_flag is not None and stop_flag.is_set():
            return
        bvid = v.get("bvid")
        aid = v.get("aid")
        if not bvid:
            return
        if video_cb:
            try:
                video_cb(idx, len(videos), str(v.get("title", "") or "")[:40])
            except Exception:
                pass
        if incremental and _skip_done(aid):
            logger.info("跳过已完成 %s/%s %s", idx, len(videos), bvid)
            if progress_cb:
                progress_cb(f"[{idx}/{len(videos)}] 跳过 {bvid}")
            return
        logger.info("[%s/%s] 开始 %s %s", idx, len(videos), bvid, v.get('title','')[:30])
        if progress_cb:
            progress_cb(f"[{idx}/{len(videos)}] {v.get('title', '')[:40]}")
        # 增量：已完成跳过（内存 map 命中优先，miss 时单查兜底）
        if incremental and aid and _skip_done(aid):
            logger.info("跳过已完成 aid=%s %s", aid, bvid)
            return
        # 每视频一个独立客户端（独立节流），stop 用共享 flag
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
        # 标记完成：仅当本视频无错误才标 done；有错误只记 last_error
        # （保持 done=0，增量轮询会再补采，避免静默漏数据）
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

    # 批量预载进度，避免102次单查被写锁阻塞
    try:
        all_aids = [v.get("aid") for v in videos if v.get("aid")]
        if all_aids:
            _load_progress(all_aids)
    except Exception:
        pass
    tasks = [(idx, v) for idx, v in enumerate(videos, start=1) if v.get("bvid")]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        fut_map = {pool.submit(_video, t): t for t in tasks}
        # 用 wait(timeout) 轮询，避免 as_completed 卡 120s 导致停止卡死
        from concurrent.futures import wait as _wait
        pending = set(fut_map.keys())
        while pending:
            if pause_flag is not None and pause_flag.is_set():
                time.sleep(0.5)
                continue
            if stop_flag is not None and stop_flag.is_set():
                for f in pending:
                    f.cancel()
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pass
                break
            done, pending = _wait(pending, timeout=0.5)
            for fut in done:
                try:
                    fut.result()
                except Exception as exc:
                    stats["errors"].append(f"worker异常: {exc}")
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
    logger.info("UP mid=%s 采集完成: 视频=%s 实际采集=%s 评论=%s 弹幕=%s",
                mid, stats["videos"], stats["fetched"], stats["comments"], stats["danmaku"])
    return stats


def _resolve_meta(client, bvid: str, *, need_cid: bool,
                  hint: dict | None = None) -> tuple[dict, bool]:
    """解析视频元信息，按性价比取路：

    1) 库内已有 → 直接复用（0 网络请求）；
    2) 列表页 hint 足够且不需要 cid → 直接用列表信息（0 网络请求）；
    3) 确实缺 cid（采弹幕）或两项都没有 → 才请求 /view 元信息接口。

    返回 (meta, from_db) where from_db=True 表示库内复用（无需再写入）。
    """
    from storage import dao

    try:
        row = dao.find_video_by_bvid(bvid)
    except Exception:
        row = None
    if row and row.get("aid") and (not need_cid or row.get("cid")):
        return (dict(row), True)

    if hint and hint.get("aid") and not need_cid:
        meta = {
            "aid": hint.get("aid") or 0,
            "bvid": bvid,
            "cid": hint.get("cid") or 0,
            "title": hint.get("title", "") or "",
            "owner_uid": hint.get("owner_uid", 0) or 0,
            "owner_name": hint.get("owner_name", "") or "",
            "pubdate": hint.get("pubdate", 0) or 0,
            "part": hint.get("part", 1) or 1,
        }
        return (meta, False)

    from crawler.tasks.full import fetch_video_meta

    return (fetch_video_meta(client, bvid), False)


def _crawl_one(client, bvid, aid=None, *, with_comments, with_danmaku, incremental,
               item_cb, progress_cb, stop_flag, meta_hint: dict | None = None) -> dict:
    """采集单个视频，评论/弹幕经异步写线程落库（采集线程不被 DB 阻塞）。

    meta_hint：调用方可传入视频列表页已返回的信息（标题/时间/owner），
    配合"库内已有则复用"，让评论-only 采集彻底免去 /view 元信息请求。
    """
    from crawler.pipelines.clean import normalize_comment
    from storage import dao
    from storage.async_writer import AsyncDbWriter

    stats = {"comments": 0, "danmaku": 0, "errors": []}
    writer = AsyncDbWriter()
    try:
        meta, from_db = _resolve_meta(client, bvid, need_cid=with_danmaku,
                                      hint=meta_hint)
        aid = meta["aid"]
        if not from_db:
            dao.upsert_video([meta])

        if with_comments:
            if progress_cb:
                progress_cb(f"  采评论（增量={incremental}）…")
            fetcher = CommentFetcher(client)
            min_ctime = dao.get_max_comment_ctime(aid, ref_type=1) if incremental else 0
            if incremental and min_ctime:
                if progress_cb:
                    progress_cb(f"  增量起点：上次已采到评论时刻")
            comments = 0

            def _page_cb(page_no: int, count: int) -> None:
                if progress_cb:
                    try:
                        progress_cb(f"  评论 第{page_no}页 已拉取 {count} 条")
                    except Exception:
                        pass

            def on_cb(kind: str, raw: dict) -> None:
                if kind != "comment":
                    return
                nonlocal comments
                comments += 1
                stats["comments"] = comments
                item = fetcher._normalize(raw, aid)
                clean = normalize_comment(item, aid)
                writer.add_comment(clean)  # 非阻塞：攒批落库
                if item_cb:
                    try:
                        item_cb("comment", item)
                    except Exception:
                        pass

            try:
                fetcher.collect(aid, with_sub=True, item_cb=on_cb,
                                stop_flag=stop_flag,
                                mode=2 if incremental else 3,
                                min_ctime=min_ctime or None,
                                progress_cb=_page_cb)
            except Exception as exc:
                stats["errors"].append(f"评论: {exc}")
                logger.error("评论采集失败 aid=%s: %s", aid, exc)

        if with_danmaku:
            r = _crawl_danmaku(client, meta, item_cb, progress_cb, stop_flag,
                               writer)
            stats["danmaku"] += r
    except Exception as exc:
        stats["errors"].append(f"整体 {bvid}: {exc}")
        logger.error("采集失败 bvid=%s: %s", bvid, exc)
        if progress_cb:
            progress_cb(f"错误：{exc}")
    finally:
        # 用户停止时不等队列：采集线程立即返回（数据由写线程继续落库），
        # 正常完成则 flush 排空（保证任务结束语义）。
        writer.close(wait=not (stop_flag is not None and stop_flag.is_set()))
    return stats


def _crawl_danmaku(client, meta, item_cb, progress_cb, stop_flag,
                   writer=None) -> int:
    from crawler.api.danmaku import DanmakuFetcher
    from crawler.pipelines.clean import normalize_danmaku
    from storage import dao

    aid = meta["aid"]
    if progress_cb:
        progress_cb("  采弹幕（每日拉取）…")
    count = 0
    fetcher = DanmakuFetcher(client)

    def on_cb(kind: str, raw: dict) -> None:
        nonlocal count
        count += 1
        if item_cb:
            try:
                item_cb("danmaku", raw)
            except Exception:
                pass

    try:
        items = fetcher.collect(aid, meta.get("cid") or 0, meta.get("pubdate") or 0,
                                item_cb=on_cb, stop_flag=stop_flag,
                                progress_cb=lambda t: progress_cb and progress_cb(t))
        clean = [normalize_danmaku(d, aid) for d in items]
        if writer is not None:
            writer.add_danmaku_batch(clean)  # 复用同一后台写线程
        else:
            dao.upsert_danmaku(clean)
    except Exception as exc:
        logger.error("弹幕采集失败 aid=%s: %s", aid, exc)
    return count