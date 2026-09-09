"""GUI 后台采集线程。

复用 crawler 的评论/弹幕采集，通过 fetcher 的 item_cb 回调
把每条抓取到的内容实时推送到 GUI 队列；同时支持软停止。
"""
from __future__ import annotations

import logging
import queue
import threading

from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher
from crawler.api.danmaku import DanmakuFetcher
from config import get_config
from storage import dao

logger = logging.getLogger(__name__)

# 消息类型
MSG_META = "meta"          # {"aid","bvid","title"}
MSG_PROGRESS = "progress"  # {"phase","count","text"}
MSG_ITEM = "item"          # {"kind","data"}  kind=danmaku|comment
MSG_ERROR = "error"        # {"text"}
MSG_DONE = "done"          # {"stats"}


def _new_writer():
    from storage.async_writer import AsyncDbWriter
    return AsyncDbWriter()


class CrawlWorker(threading.Thread):
    """采集单个视频（评论+弹幕可选）的后台线程。"""

    def __init__(self, out_q: queue.Queue, *, bvid: str,
                 comments: bool = True, danmaku: bool = True) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.bvid = bvid
        self.do_comments = comments
        self.do_danmaku = danmaku
        self._stop = threading.Event()
        self.stats: dict = {"comments": 0, "danmaku": 0, "errors": []}

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        client = ApiClient(stop_event=self._stop)
        self.writer = None
        try:
            from crawler.tasks.full import fetch_video_meta

            meta = fetch_video_meta(client, self.bvid)
            dao.upsert_video([meta])
            aid = meta["aid"]
            self._emit(MSG_META, aid=aid, bvid=self.bvid, title=meta.get("title", ""))
            self._emit(MSG_PROGRESS, phase="元信息", text=f"开始采集 {self.bvid} aid={aid}")

            self.writer = _new_writer()
            if self.do_comments:
                self._collect_comments(client, aid)
            if self.do_danmaku:
                self._collect_danmaku(client, aid, meta)
            self.writer.close(wait=not self._stop.is_set())

            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("采集线程异常")
            self._emit(MSG_ERROR, text=f"采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)
        finally:
            client.close()

    def _collect_comments(self, client: ApiClient, aid: int) -> None:
        from crawler.pipelines.clean import normalize_comment

        fetcher = CommentFetcher(client)

        def on_cb(kind: str, raw: dict) -> None:
            if kind != "comment":
                return
            item = fetcher._normalize(raw, aid)
            clean = normalize_comment(item, aid)
            self.writer.add_comment(clean)  # 非阻塞：攒批异步落库
            self.stats["comments"] += 1
            self._emit(MSG_ITEM, kind="comment", data=item)

        try:
            fetcher.collect(aid, with_sub=True, item_cb=on_cb, stop_flag=self._stop)
            if self._stop.is_set():
                self._emit(MSG_PROGRESS, phase="评论", text="评论采集被停止（已采集数据后台落库中）")
        except Exception as exc:
            self.stats["errors"].append(f"评论: {exc}")
            self._emit(MSG_ERROR, text=f"评论出错: {exc}")
        self._emit(MSG_PROGRESS, phase="评论", text=f"评论采集完毕，共 {self.stats['comments']} 条")

    def _collect_danmaku(self, client: ApiClient, aid: int, meta: dict) -> None:
        from crawler.pipelines.clean import normalize_danmaku

        fetcher = DanmakuFetcher(client)

        def on_cb(kind: str, raw: dict) -> None:
            if kind != "danmaku":
                return
            self.writer.add_danmaku(normalize_danmaku(raw, aid))
            self.stats["danmaku"] += 1
            self._emit(MSG_ITEM, kind="danmaku", data=raw)

        def on_progress(text: str) -> None:
            self._emit(MSG_PROGRESS, phase="弹幕", text=text)

        try:
            fetcher.collect(
                aid, meta.get("cid") or 0, meta.get("pubdate") or 0,
                item_cb=on_cb, stop_flag=self._stop, progress_cb=on_progress,
            )
            if self._stop.is_set():
                self._emit(MSG_PROGRESS, phase="弹幕", text="弹幕采集被停止（已采集数据后台落库中）")
        except Exception as exc:
            self.stats["errors"].append(f"弹幕: {exc}")
            self._emit(MSG_ERROR, text=f"弹幕出错: {exc}")
        self._emit(MSG_PROGRESS, phase="弹幕", text=f"弹幕采集完毕，共 {self.stats['danmaku']} 条")


class UpCrawlWorker(threading.Thread):
    """按 UID 批量采集该 UP 主的全部视频（可选评论/弹幕、增量去重）。"""

    def __init__(self, out_q: queue.Queue, *, uid: int,
                 comments: bool = True, danmaku: bool = False,
                 incremental: bool = True, max_pages: int = 20) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.uid = uid
        self.do_comments = comments
        self.do_danmaku = danmaku
        self.incremental = incremental
        self.max_pages = max_pages
        self._stop = threading.Event()
        self.stats: dict = {"videos": 0, "fetched": 0, "comments": 0, "danmaku": 0}

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        client = ApiClient(stop_event=self._stop)
        try:
            from crawler.tasks.up_crawl import crawl_up_videos

            def on_item(kind: str, raw: dict) -> None:
                if kind == "comment" and self.do_comments:
                    # raw 已是 _normalize 后的评论 item
                    self._emit(MSG_ITEM, kind="comment", data=raw)
                elif kind == "danmaku" and self.do_danmaku:
                    self._emit(MSG_ITEM, kind="danmaku", data=raw)

            result = crawl_up_videos(
                client, self.uid,
                with_comments=self.do_comments,
                with_danmaku=self.do_danmaku,
                incremental=self.incremental,
                max_pages=self.max_pages,
                item_cb=on_item,
                progress_cb=lambda t: self._emit(MSG_PROGRESS, phase="UP", text=t),
                stop_flag=self._stop,
                workers=get_config().up_workers,
            )
            self.stats.update(result)
            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("UP采集线程异常")
            self._emit(MSG_ERROR, text=f"UP采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)
        finally:
            client.close()


class FavCrawlWorker(threading.Thread):
    """按用户收藏夹：列出全部收藏夹视频，逐视频采集评论（可选弹幕、增量去重）。"""

    def __init__(self, out_q: queue.Queue, *, uid: int,
                 comments: bool = True, danmaku: bool = False,
                 incremental: bool = True, max_pages: int = 50) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.uid = uid
        self.do_comments = comments
        self.do_danmaku = danmaku
        self.incremental = incremental
        self.max_pages = max_pages
        self._stop = threading.Event()
        self.stats: dict = {"mid": uid, "folders": 0, "videos": 0, "fetched": 0,
                            "comments": 0, "danmaku": 0}

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        try:
            client = ApiClient(stop_event=self._stop)
            from crawler.tasks.fav_crawl import crawl_user_favorites

            def on_item(kind: str, raw: dict) -> None:
                if kind == "comment" and self.do_comments:
                    self._emit(MSG_ITEM, kind="comment", data=raw)
                elif kind == "danmaku" and self.do_danmaku:
                    self._emit(MSG_ITEM, kind="danmaku", data=raw)

            result = crawl_user_favorites(
                client, self.uid,
                with_comments=self.do_comments,
                with_danmaku=self.do_danmaku,
                incremental=self.incremental,
                max_pages=self.max_pages,
                item_cb=on_item,
                progress_cb=lambda t: self._emit(MSG_PROGRESS, phase="收藏夹", text=t),
                stop_flag=self._stop,
                workers=get_config().up_workers,
            )
            self.stats.update(result)
            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("收藏夹采集线程异常")
            self._emit(MSG_ERROR, text=f"收藏夹采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)


class RelationCrawlWorker(threading.Thread):
    """按社交关系采集：目标用户关注/粉丝列表中每人，采集其视频评论（可选弹幕、增量）。"""

    def __init__(self, out_q: queue.Queue, *, uid: int,
                 direction: str = "both", max_users: int = 0,
                 comments: bool = True, danmaku: bool = False,
                 incremental: bool = True, max_pages: int = 100) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.uid = uid
        self.direction = direction
        self.max_users = max_users
        self.do_comments = comments
        self.do_danmaku = danmaku
        self.incremental = incremental
        self.max_pages = max_pages
        self._stop = threading.Event()
        self.stats: dict = {"mid": uid, "related": 0, "ups": 0, "ups_ok": 0,
                            "videos": 0, "fetched": 0, "comments": 0,
                            "danmaku": 0}

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        client = ApiClient(stop_event=self._stop)
        try:
            from crawler.tasks.relation_crawl import crawl_relation_videos

            def on_item(kind: str, raw: dict) -> None:
                if kind == "comment" and self.do_comments:
                    self._emit(MSG_ITEM, kind="comment", data=raw)
                elif kind == "danmaku" and self.do_danmaku:
                    self._emit(MSG_ITEM, kind="danmaku", data=raw)

            result = crawl_relation_videos(
                client, self.uid,
                direction=self.direction, max_users=self.max_users,
                with_comments=self.do_comments,
                with_danmaku=self.do_danmaku,
                incremental=self.incremental,
                max_pages=self.max_pages,
                item_cb=on_item,
                progress_cb=lambda t: self._emit(MSG_PROGRESS, phase="关系", text=t),
                stop_flag=self._stop,
                workers=get_config().up_workers,
            )
            self.stats.update(result)
            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("关系采集线程异常")
            self._emit(MSG_ERROR, text=f"关系采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)
        finally:
            client.close()


class BatchUpWorker(threading.Thread):
    """自由批量采集：队列驱动的混合任务批量执行。

    每个任务 = {"kind": "up"|"fav"|"rel", "uid": int, "direction": str|None}。
    - up  : 采集该 UP 的视频评论/弹幕（crawl_up_videos）
    - fav : 采集该用户全部收藏夹内的视频（crawl_user_favorites）
    - rel : 采集该用户关注/粉丝列表中每人的视频（crawl_relation_videos）
    运行中可随时 add_jobs 追加（接续），队列空时保持空闲等待直到 stop()。
    """

    def __init__(self, out_q: queue.Queue, *,
                 comments: bool = True, danmaku: bool = False,
                 incremental: bool = True, max_pages: int = 100,
                 max_users: int = 0) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.do_comments = comments
        self.do_danmaku = danmaku
        self.incremental = incremental
        self.max_pages = max_pages
        self.max_users = max_users
        self._stop = threading.Event()
        self._job_q: queue.Queue[dict] = queue.Queue()
        self.queued = 0       # 已提交（未完成）数量
        self.processed = 0    # 已完成数量
        self.stats: dict = {"jobs": 0, "ups": 0, "favs": 0, "rels": 0,
                            "fetched": 0, "videos": 0,
                            "comments": 0, "danmaku": 0, "errors": []}
        self.current_job: dict | None = None  # 正在执行的任务（供 GUI 面包屑）

    def stop(self) -> None:
        self._stop.set()

    def add_jobs(self, jobs: list[dict]) -> None:
        """向队列追加一批任务（运行中可随时调用，实现接续采集）。

        job: {"kind": "up"|"fav"|"rel", "uid": int, "direction"?: str}
        """
        for j in jobs:
            kind = j.get("kind", "up")
            uid = int(j.get("uid") or 0)
            if not uid or kind not in ("up", "fav", "rel"):
                continue
            self._job_q.put({"kind": kind, "uid": uid,
                             "direction": j.get("direction") or "both"})
            self.queued += 1
            self.stats["jobs"] += 1

    @property
    def remaining(self) -> int:
        return self.queued - self.processed

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        idle_reported = False
        while not self._stop.is_set():
            try:
                job = self._job_q.get_nowait()
            except queue.Empty:
                if not idle_reported:
                    self._emit(MSG_PROGRESS, phase="批量",
                               text="[批量] 队列已空，等待追加任务…（可粘贴任务点“追加”）")
                    idle_reported = True
                if self._stop.wait(2.0):
                    break
                continue
            idle_reported = False
            self._process(job)
        self._emit(MSG_DONE, stats=self.stats)

    def _process(self, job: dict) -> None:
        from crawler.api.client import ApiClient

        kind = job.get("kind", "up")
        uid = job["uid"]
        self.current_job = job
        self.processed += 1
        kind_text = {"up": "UP", "fav": "收藏夹", "rel": "关注/粉丝"}.get(kind, kind)
        key = "favs" if kind == "fav" else ("rels" if kind == "rel" else "ups")
        self.stats[key] += 1
        self._emit(MSG_PROGRESS, phase="批量",
                   text=f"[批量 {self.processed}/{self.queued}] [{kind_text}] UID={uid} 开始…")
        try:
            client = ApiClient(stop_event=self._stop)

            def on_item(kind_: str, raw: dict) -> None:
                if not self.do_comments and kind_ == "comment":
                    return
                if not self.do_danmaku and kind_ == "danmaku":
                    return
                self._emit(MSG_ITEM, kind=kind_, data=raw)

            r = self._run_one(client, kind, uid, job.get("direction") or "both", on_item)
            self.stats["fetched"] += r.get("fetched", 0)
            self.stats["videos"] += r.get("videos", 0)
            self.stats["comments"] += r.get("comments", 0)
            self.stats["danmaku"] += r.get("danmaku", 0)
            if r.get("errors"):
                self.stats["errors"].extend(r["errors"])
            self._emit(MSG_PROGRESS, phase="批量",
                       text=f"[批量 {self.processed}/{self.queued}] [{kind_text}] UID={uid} 完成 "
                            f"(评论+{r.get('comments', 0)} 弹幕+{r.get('danmaku', 0)})")
        except Exception as exc:
            self.stats["errors"].append(f"{kind_text} UID{uid}: {exc}")
            self._emit(MSG_ERROR, text=f"批量 [{kind_text}] UID={uid} 失败: {exc}")
        finally:
            client.close()
            self.current_job = None

    def _run_one(self, client, kind: str, uid: int, direction: str, on_item) -> dict:
        common = dict(
            with_comments=self.do_comments, with_danmaku=self.do_danmaku,
            incremental=self.incremental, max_pages=self.max_pages,
            item_cb=on_item,
            progress_cb=lambda t: self._emit(MSG_PROGRESS, phase="批量", text=t),
            stop_flag=self._stop, workers=get_config().up_workers,
        )
        if kind == "fav":
            from crawler.tasks.fav_crawl import crawl_user_favorites
            return crawl_user_favorites(client, uid, **common)
        if kind == "rel":
            from crawler.tasks.relation_crawl import crawl_relation_videos
            return crawl_relation_videos(
                client, uid, direction=direction or "both",
                max_users=self.max_users, **common)
        from crawler.tasks.up_crawl import crawl_up_videos
        return crawl_up_videos(client, uid, **common)


class BroadCrawlWorker(threading.Thread):
    """广泛采集：以种子 UID 起 BFS 滚动扩展，逐 UP 采评论并反推新 UP。"""

    def __init__(self, out_q: queue.Queue, *, seeds: list[int],
                 max_depth: int = 3, max_ups: int = 30, max_pages: int = 20,
                 top_n: int = 10, min_comments: int = 3,
                 max_storage_mb: int = 0, perpetual: bool = False) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.seeds = seeds
        self.max_depth = max_depth
        self.max_ups = max_ups
        self.max_pages = max_pages
        self.top_n = top_n
        self.min_comments = min_comments
        self.max_storage_mb = max_storage_mb
        self.perpetual = perpetual
        self._stop = threading.Event()
        self._pause = threading.Event()
        self.stats: dict = {}
        self.status: dict = {}  # 实时状态快照（供 GUI 状态栏轮询读取）

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        if self._pause.is_set():
            self._pause.clear()
        else:
            self._pause.set()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        from crawler.tasks.broad_crawl import broad_crawl
        # 启动即置初值，避免 GUI 轮询到空 status
        self.status.update({"mode": "广泛", "phase": "collecting", "action": "正在启动…",
                            "queue": len(self.seeds), "processed": 0, "max_ups": self.max_ups})
        self.stats.update({"videos": 0, "comments": 0, "candidates": 0, "errors": []})

        def on_item(kind: str, raw: dict) -> None:
            if kind == "comment":
                self._emit(MSG_ITEM, kind="comment", data=raw)

        try:
            def _on_status(s):
                self.status.update(s)
                for k in ("videos", "comments", "candidates"):
                    if k in s:
                        self.stats[k] = s[k]
                # errors 在 broad_crawl 快照里是计数 int，保持 stats 里也是计数，避免 list/int 混用
                if "errors" in s:
                    self.stats["errors"] = s["errors"]
            result = broad_crawl(
                self.seeds,
                max_depth=self.max_depth, max_ups=self.max_ups,
                max_pages=self.max_pages, top_n=self.top_n,
                min_comments=self.min_comments,
                max_storage_mb=self.max_storage_mb,
                perpetual=self.perpetual,
                item_cb=on_item,
                progress_cb=lambda t: self._emit(MSG_PROGRESS, phase="广泛", text=t),
                status_cb=_on_status,
                stop_flag=self._stop,
                pause_flag=self._pause,
                workers=get_config().up_workers,
            )
            self.stats = result
            self.stats.setdefault("fetched", result.get("ups", 0))
            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("广泛采集线程异常")
            self._emit(MSG_ERROR, text=f"广泛采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)


class PostCrawlWorker(threading.Thread):
    """采集一个帖子/动态/专栏的评论（支持增量）。"""

    def __init__(self, out_q: queue.Queue, *, target: str,
                 comments: bool = True, incremental: bool = False) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.target = target
        self.do_comments = comments
        self.incremental = incremental
        self._stop = threading.Event()
        self.stats: dict = {"post": 0, "kind": "", "comments": 0}

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        from crawler.api.client import ApiClient
        from crawler.api.comments import CommentFetcher, REPLY_TYPE_ARTICLE, REPLY_TYPE_DYNAMIC
        from crawler.api.posts import parse_post_link
        from crawler.tasks.post_crawl import crawl_one_post

        client = ApiClient(stop_event=self._stop)
        try:
            parsed = parse_post_link(self.target)
            if not parsed:
                self._emit(MSG_ERROR, text=f"无法识别的帖子链接/ID: {self.target}")
                self._emit(MSG_DONE, stats=self.stats)
                return
            ref_type = REPLY_TYPE_DYNAMIC if parsed["kind"] == "dynamic" else REPLY_TYPE_ARTICLE
            self.stats["kind"] = parsed["kind"]
            self.stats["post"] = parsed["id"]
            self._emit(MSG_META, kind=parsed["kind"], post_id=parsed["id"],
                       title=f"{'动态' if parsed['kind'] == 'dynamic' else '专栏'} {parsed['id']}")

            def on_cb(kind: str, raw: dict) -> None:
                if kind == "comment" and self.do_comments:
                    self.stats["comments"] += 1
                    try:
                        item = CommentFetcher(client)._normalize(raw, parsed["id"], ref_type=ref_type)
                    except Exception:
                        item = raw
                    self._emit(MSG_ITEM, kind="comment", data=item)

            result = crawl_one_post(
                client, parsed["kind"], parsed["id"],
                with_sub=True,
                incremental=self.incremental,
                item_cb=on_cb,
                stop_flag=self._stop,
                progress_cb=lambda t: self._emit(MSG_PROGRESS, phase="帖子", text=t),
            )
            self.stats["comments"] = result.get("comments", 0)
            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("帖子采集线程异常")
            self._emit(MSG_ERROR, text=f"帖子采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)
        finally:
            client.close()


class PopularCrawlWorker(threading.Thread):
    """一键采集 B 站热门榜视频的评论。

    拉取当前热门榜前 count 个视频，逐视频增量采集评论（已完成自动跳过），
    实时推送进度与评论流。
    """

    def __init__(self, out_q: queue.Queue, *, count: int = 20,
                 incremental: bool = True) -> None:
        super().__init__(daemon=True)
        self.out_q = out_q
        self.count = count
        self.incremental = incremental
        self._stop = threading.Event()
        self.stats: dict = {"kind": "hot", "videos": 0, "fetched": 0,
                            "comments": 0, "errors": []}
        self.status: dict = {"mode": "热搜", "phase": "", "uid": 0,
                             "uname": "", "vtitle": "", "action": "",
                             "videos": 0, "fetched": 0, "comments": 0,
                             "errors": 0, "queue": 0}

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, msg_type: str, **fields) -> None:
        try:
            self.out_q.put({"type": msg_type, **fields})
        except Exception:
            pass

    def run(self) -> None:
        from crawler.api.client import ApiClient
        from crawler.api.comments import CommentFetcher
        from crawler.api.popular import PopularFetcher
        from crawler.pipelines.clean import normalize_comment
        from storage import dao
        from storage.async_writer import AsyncDbWriter

        client = ApiClient(stop_event=self._stop)
        writer = AsyncDbWriter()
        try:
            self.status.update({"phase": "collecting", "action": "拉取热门榜中…"})
            items = PopularFetcher(client).collect(count=self.count,
                                                   stop_flag=self._stop)
            if not items:
                self._emit(MSG_ERROR, text="热门榜拉取为空（可能被风控或接口变更）")
                self._emit(MSG_DONE, stats=self.stats)
                return
            self.stats["videos"] = len(items)
            self.status.update({"phase": "collecting", "action": "开始逐视频采评论"})
            self._emit(MSG_PROGRESS, phase="热搜",
                       text=f"热门榜拉到 {len(items)} 个视频，开始逐视频采评论")
            for idx, v in enumerate(items, start=1):
                if self._stop.is_set():
                    break
                aid = v.get("aid") or 0
                bvid = v.get("bvid") or ""
                title = str(v.get("title", "") or "")[:40]
                self.status.update({"uid": v.get("owner_uid") or 0,
                                    "uname": v.get("owner_name") or "",
                                    "vtitle": title, "action": "采评论中…",
                                    "videos": len(items),
                                    "fetched": self.stats["fetched"],
                                    "comments": self.stats["comments"],
                                    "errors": len(self.stats["errors"])})
                self._emit(MSG_PROGRESS, phase="热搜",
                           text=f"[{idx}/{len(items)}] {title} 采评论中…")
                if not aid:
                    self.stats["errors"].append(f"{bvid}: 无 aid，跳过")
                    continue
                try:
                    meta = dict(v)
                    dao.upsert_video([meta])
                    min_ctime = (dao.get_max_comment_ctime(aid, ref_type=1)
                                 if self.incremental else 0)

                    def on_cb(kind: str, raw: dict) -> None:
                        if kind == "comment":
                            item = CommentFetcher(client)._normalize(raw, aid)
                            # 非阻塞：攒批异步落库（停止后线程秒退，数据后台写完）
                            writer.add_comment(normalize_comment(item, aid))
                            self.stats["comments"] += 1
                            self._emit(MSG_ITEM, kind="comment", data=item)

                    def _page_cb(p: int, n: int) -> None:
                        self.status.update(
                            {"action": f"评论 第{p}页 已拉 {n} 条",
                             "comments": self.stats["comments"]})
                        self._emit(MSG_PROGRESS, phase="热搜",
                                   text=f"[{idx}/{len(items)}] {title} "
                                        f"评论 第{p}页 已拉 {n} 条")

                    CommentFetcher(client).collect(
                        aid, with_sub=True, item_cb=on_cb,
                        stop_flag=self._stop,
                        mode=2 if min_ctime else 3,
                        min_ctime=min_ctime or None,
                        progress_cb=_page_cb,
                    )
                    self.stats["fetched"] += 1
                    self.status.update(
                        {"fetched": self.stats["fetched"],
                         "comments": self.stats["comments"],
                         "action": f"评论 +{self.stats['comments']} 条（异步落库中）"})
                    self._emit(MSG_PROGRESS, phase="热搜",
                               text=f"[{idx}/{len(items)}] {title} 已采 "
                                    f"{self.stats['comments']} 条（后台落库）")
                except Exception as exc:
                    self.stats["errors"].append(f"{bvid}: {exc}")
                    self._emit(MSG_ERROR, text=f"热搜视频 {bvid} 评论失败: {exc}")
            self.status.update({"phase": "done", "action": "任务完成"})
            self._emit(MSG_DONE, stats=self.stats)
        except Exception as exc:
            logger.exception("热搜采集线程异常")
            self._emit(MSG_ERROR, text=f"热搜采集失败: {exc}")
            self._emit(MSG_DONE, stats=self.stats)
        finally:
            # 用户停止时不等队列：线程立即退出，数据由写线程继续落库
            writer.close(wait=not self._stop.is_set())
            client.close()