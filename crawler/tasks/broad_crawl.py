"""广泛采集：BFS 滚动扩展任务。

以若干种子 UID 为起点，逐个 UP 采集其全部视频评论；
每采完一个 UP，从该 UP 视频下最活跃的评论者中反推新的候选 UP，
自动向下扩散（广度优先），直至达到深度/数量上限或用户停止。

设计要点：
  - 复用 up_crawl.crawl_up_videos 做单 UP 全量采集（增量去重/断点续传）。
  - 扩展来源仅用评论 UID（真实 uid，可靠）；弹幕 uid 需 CRC32 反查，不做扩展源。
  - visited 集合 + 库内已采集 UP 双重去重，避免重复劳动。
  - 每轮按“已采视频数”决定反推阈值，控制扩散节奏。
"""
from __future__ import annotations

import logging
import time
from collections import deque

from crawler.api.client import ApiClient

logger = logging.getLogger(__name__)


def broad_crawl(seeds: list[int], *,
                max_depth: int = 999, max_ups: int = 999999,
                max_pages: int = 20, top_n: int = 10, min_comments: int = 3,
                max_storage_mb: int = 0,
                perpetual: bool = False,
                item_cb=None, progress_cb=None, status_cb=None, stop_flag=None,
                pause_flag=None,
                workers: int = 3) -> dict:
    """BFS 滚动采集一批 UP 的评论，并自动扩展发现新 UP。

    参数：
      seeds        初始 UID 列表（深度 0）
      max_depth    最大扩展深度（0=只采种子；1=种子+种子反推的UP，以此类推）
      max_ups      最多处理的 UP 总数（护栏）
      max_pages    每个 UP 最多拉取的视频列表页数
      top_n        每个 UP 最多反推几个候选评论者
      min_comments 候选者至少需在该UP视频下评论多少条
      max_storage_mb 存储上限（MB，0=不限制）。每个 UP 采完实时检查，
                     数据库总占用（data+index）超过该值即停止，防止存溢出
      status_cb(snapshot)：可选，UP 开始/结束时回调当前状态快照
                    {"phase","uid","depth","queue","processed","max_ups",
                     "videos","comments","candidates"}，供 GUI 状态栏展示
    返回 {"ups", "visited", "candidates", "videos", "comments", "errors"}
    """
    from storage import dao

    stats = {"ups": 0, "visited": list(seeds), "candidates": 0,
             "videos": 0, "comments": 0, "errors": []}
    client = ApiClient(stop_event=stop_flag)
    queue: deque[tuple[int, int]] = deque((s, 0) for s in seeds)  # (uid, depth)
    visited: set[int] = set(seeds)
    # 库内已采集过的 UP 不去重采（增量模式也由 crawl_up_videos 自行跳过）
    processed = 0
    # 自适应候选门槛：连续 N 个 UP 反推为零 → 门槛降到 1 条，防扩散断链
    effective_min = min_comments
    zero_streak = 0
    ZERO_STREAK_LIMIT = 5

    def _report(text: str) -> None:
        if progress_cb:
            try:
                progress_cb(text)
            except Exception:
                pass

    def _status(**fields) -> None:
        if not status_cb:
            return
        try:
            status_cb({
                "mode": "广泛",
                "phase": fields.get("phase", ""),
                "uid": fields.get("uid", 0),
                "uname": fields.get("uname", ""),
                "vtitle": fields.get("vtitle", ""),
                "action": fields.get("action", ""),
                "depth": fields.get("depth", 0),
                "queue": fields.get("queue", 0),
                "processed": fields.get("processed", processed),
                "max_ups": fields.get("max_ups", max_ups),
                "videos": stats["videos"], "comments": stats["comments"],
                "candidates": stats["candidates"], "errors": len(stats["errors"]),
            })
        except Exception:
            pass

    def _uname(uid: int) -> str:
        try:
            row = dao.query_user(uid)
            return (row or {}).get("uname") or ""
        except Exception:
            return ""

    def _storage_full() -> bool:
        if max_storage_mb <= 0:
            return False
        try:
            size_mb = dao.get_db_size() / (1024 * 1024)
            return size_mb > max_storage_mb
        except Exception:
            return False

    while not (stop_flag is not None and stop_flag.is_set()):
        if pause_flag is not None and pause_flag.is_set():
            _status(phase="paused", queue=len(queue), processed=processed)
            time.sleep(0.5)
            continue
        if not queue:
            if perpetual and processed < max_ups:
                try:
                    import random

                    from storage.dao import query_top_up_uids
                    # 永续：扩大池子避免永远前5，排除已采
                    pool = query_top_up_uids(limit=100)
                    seeds2 = [u for u in pool if u not in visited]
                    if not seeds2 and visited:
                        seeds2 = [random.choice(list(visited))]
                    # 打乱避免固定顺序
                    random.shuffle(seeds2)
                    for s in seeds2[:top_n]:
                        visited.add(s); queue.append((s, 0))
                    _report(f"[永续] 队列空，补种 {len(seeds2[:top_n])} 个UP")
                    if not queue:
                        break
                except Exception:
                    break
            else:
                break
        if processed >= max_ups:
            _report(f"达到 UP 处理上限 {max_ups}，停止扩展")
            break
        if _storage_full():
            _report(f"存储超过上限 {max_storage_mb}MB，停止扩展以防止存溢出")
            stats["errors"].append(f"存储超限 {max_storage_mb}MB，停止")
            break
        uid, depth = queue.popleft()
        if processed >= max_ups:
            break
        processed += 1
        uname = _uname(uid)
        _status(phase="collecting", uid=uid, depth=depth, queue=len(queue),
                processed=processed, uname=uname, action="拉取视频列表中")
        _report(f"[UP {processed}] UID={uid} 深度={depth} 队列剩余={len(queue)}")
        logger.info("广泛采集: 处理 UID=%s depth=%s", uid, depth)
        try:
            r = _crawl_up(client, uid, max_pages=max_pages,
                          item_cb=item_cb, progress_cb=progress_cb,
                          # lambda 默认参数绑定循环变量，避免回调逃逸后读到下一轮的值
                          video_cb=lambda i, n, t, _uid=uid, _depth=depth,
                          _proc=processed, _uname=uname: _status(
                              phase="collecting", uid=_uid, depth=_depth,
                              queue=len(queue), processed=_proc,
                              uname=_uname, vtitle=t, action=f"视频 {i}/{n}"),
                          stop_flag=stop_flag, pause_flag=pause_flag, workers=workers)
        except Exception as exc:
            stats["errors"].append(f"UP{uid}: {exc}")
            logger.exception("广泛采集 UID=%s 失败", uid)
            continue
        stats["videos"] += r.get("videos", 0)
        stats["comments"] += r.get("comments", 0)
        stats["ups"] += 1
        _report(f"  UID={uid} 完成：视频 {r.get('fetched', 0)}/{r.get('videos', 0)} "
                f"评论 {r.get('comments', 0)} 条")
        _status(phase="done", uid=uid, depth=depth, queue=len(queue),
                processed=processed, uname=uname,
                videos=stats["videos"], comments=stats["comments"],
                candidates=stats["candidates"])

        # 反推扩展（仅评论 UID；候选需在库内已实际采到评论才有效）
        if depth >= max_depth:
            continue
        try:
            candidates = dao.top_commenters(uid, excl_uids=visited,
                                            limit=top_n, min_comments=effective_min)
        except Exception as exc:
            logger.warning("反推评论者失败 UID=%s: %s", uid, exc)
            continue
        added = 0
        for cand in candidates:
            c_uid = cand["uid"]
            if c_uid in visited or c_uid == uid:
                continue
            visited.add(c_uid)
            queue.append((c_uid, depth + 1))
            added += 1
            _report(f"    反推候选 #{added}: UID={c_uid} {cand.get('uname', '')} "
                    f"(该UP视频下评论 {cand.get('n', 0)} 条)")
        stats["candidates"] += added
        if added:
            zero_streak = 0
            _report(f"  从 UID={uid} 反推 {added} 个候选 UP 加入队列")
        else:
            zero_streak += 1
            if zero_streak >= ZERO_STREAK_LIMIT and effective_min > 1:
                effective_min = 1
                zero_streak = 0
                _report("  [自适应] 连续 5 个 UP 无反推候选，"
                        "候选门槛自动降为 1 条评论（防止扩散断链）")
    if stop_flag is not None and stop_flag.is_set():
        stats["errors"].append("用户停止")
    stats["visited"] = list(visited)
    return stats


def _crawl_up(client, uid: int, *, max_pages: int, item_cb, progress_cb,
              video_cb=None, stop_flag=None, pause_flag=None, workers: int = 3) -> dict:
    """采集单个 UP 的全部视频评论（增量去重），返回统计。"""
    from crawler.tasks.up_crawl import crawl_up_videos

    return crawl_up_videos(
        client, uid,
        with_comments=True, with_danmaku=False,
        incremental=True, max_pages=max_pages,
        item_cb=item_cb, progress_cb=progress_cb, video_cb=video_cb,
        stop_flag=stop_flag, pause_flag=pause_flag, workers=workers,
    )
