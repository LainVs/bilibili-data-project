"""B站 UP 主空间 API：视频列表、专栏列表。

space 相关的接口需要 wbi 签名。为保持与评论模块一致，
这里复用 comments.py 的签名方案，抽取为独立签名工具。
"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher

logger = logging.getLogger(__name__)

SPACE_VIDEO_API = "https://api.bilibili.com/x/space/wbi/arc/search"
SPACE_ARTICLE_API = "https://api.bilibili.com/x/space/wbi/article"


class SpaceFetcher:
    """抓取某 UP 主（mid）的视频列表与专栏列表。"""

    def __init__(self, client: ApiClient) -> None:
        self.client = client
        self._signer = CommentFetcher(client)

    def _sign(self, params: dict) -> dict:
        return self._signer._sign(params)

    def list_videos(self, mid: int, *, max_pages: int = 20,
                    progress_cb=None, all_done_check=None,
                    stop_flag=None) -> list[dict]:
        """分页抓取 UP 主全部投稿视频。

        第一页确定 total 后，剩余页按空间列表并发拉取
        （pn 页码式分页天然可并行；全局 QPS 仍兜底总速率）。

        all_done_check(items)：可选回调，每页(或每并发批次)拉取后调用，
        若返回 True 视为"该页全部已在库且采集完成"，后续更旧的页直接不再拉取
        ——续跑时只拉最新几页即可跳到已完成边界。可据此把"全量重拉"变成"增量续拉"。

        返回 [{bvid, aid, cid?, title, pubdate}]；
        bvid/aid 即用于后续全量采集。
        实例属性：last_total=发布总数(page.count)，early_stopped=是否提前停页。
        """
        from config import get_config

        videos: list[dict] = []
        # 各列表接口服务端墙不同，按类型独立取值；未配置时回退 list_page_workers
        cfg = get_config()
        workers = getattr(cfg, "space_list_workers", None) or cfg.list_page_workers
        self.early_stopped = False
        self.last_total = 0

        def _fetch_page_raw(client, pn: int) -> dict:
            params = CommentFetcher(client)._sign({
                "mid": mid,
                "ps": 30,
                "pn": pn,
                "order": "pubdate",
                "order_avoided": "",
                "tid": 0,
                "keyword": "",
            })
            payload = client.get_json(SPACE_VIDEO_API, params=params)
            return payload.get("data") or {}

        def _parse(data: dict) -> list[dict]:
            items = (data.get("list") or {}).get("vlist") or []
            return [{
                "bvid": it.get("bvid", ""),
                "aid": it.get("aid", 0),
                "title": it.get("title", ""),
                "owner_uid": it.get("mid", 0),
                "owner_name": it.get("author", ""),
                "pubdate": it.get("created", 0),
                "length": it.get("length", ""),
                "play": it.get("play", 0),
            } for it in items]

        def _fetch_page(client, pn: int) -> list[dict]:
            return _parse(_fetch_page_raw(client, pn))

        # 第 1 页：拿 total 与首批数据
        if progress_cb:
            progress_cb("读取UP视频列表 第1页")
        first_data = _fetch_page_raw(self.client, 1)
        first = _parse(first_data)
        if not first:
            return videos
        videos.extend(first)
        # 总数优先取 data.page.count（接口字段），data.count 兜底
        total = (first_data.get("page") or {}).get("count", 0) or first_data.get("count", 0)
        self.last_total = total or len(first) or 0

        total_pages = min(max_pages, (total + 29) // 30) if total else max_pages
        remaining = list(range(2, total_pages + 1))
        if not remaining:
            return videos
        if workers <= 1 or len(remaining) == 1:
            if progress_cb:
                progress_cb(f"读取UP视频列表 剩余 {len(remaining)} 页…")
            for pn in remaining:
                if stop_flag is not None and stop_flag.is_set():
                    return videos
                page_items = _fetch_page(self.client, pn)
                videos.extend(page_items)
                if all_done_check and page_items and all_done_check(page_items):
                    self.early_stopped = True
                    return videos
        else:
            from crawler.api.client import fetch_pages_parallel

            for batch_start in range(0, len(remaining), workers):
                if stop_flag is not None and stop_flag.is_set():
                    return videos
                batch = remaining[batch_start:batch_start + workers]
                if progress_cb:
                    progress_cb(f"读取UP视频列表 并行第{batch[0]}-{batch[-1]}页")
                results = fetch_pages_parallel(
                    _fetch_page, batch, workers=workers,
                    client_factory=type(self.client), stop_flag=stop_flag)
                for _pn, items in results:
                    videos.extend(items)
                    if total and len(videos) >= total:
                        return videos
                # 整批页全部成功且每页均命中"已采集完成"→ 后续更旧页无需再拉
                if all_done_check and len(results) == len(batch):
                    if all(all_done_check(it) for _pn, it in results):
                        self.early_stopped = True
                        return videos
        return videos

    def fetch_fans(self, mid: int) -> int:
        """取 UP 粉丝数（用于种子按粉丝排序）。失败返回 0。"""
        try:
            payload = self.client.get_json(
                "https://api.bilibili.com/x/relation/stat",
                params={"vmid": mid})
            data = payload.get("data") or {}
            return int(data.get("follower") or data.get("fans") or 0)
        except Exception:
            pass
        try:
            payload = self.client.get_json(
                "https://api.bilibili.com/x/space/acc/info",
                params={"mid": mid})
            data = payload.get("data") or {}
            return int(data.get("follower") or data.get("fans") or 0)
        except Exception:
            return 0

    def list_articles(self, mid: int, max_pages: int = 10,
                      progress_cb=None) -> list[dict]:
        """分页抓取 UP 主全部专栏文章。

        返回 [{id(cvid), title, pubdate, view}]。
        """
        articles: list[dict] = []
        pn = 1
        while pn <= 1 or pn <= max_pages:
            if progress_cb:
                progress_cb(f"拉取 UP 专栏文章 第{pn}页")
            params = self._sign({"mid": mid, "pn": pn, "ps": 30, "sort": "publish_time"})
            payload = self.client.get_json(SPACE_ARTICLE_API, params=params)
            data = payload.get("data") or {}
            articles_data = data.get("articles") or data.get("list") or []
            if not articles_data:
                break
            for it in articles_data:
                articles.append({
                    "id": it.get("id", 0),
                    "cvid": it.get("id", 0),
                    "title": it.get("title", ""),
                    "pubdate": it.get("publish_time", 0),
                    "view": it.get("view", 0),
                })
            if len(articles_data) < 30:
                break
            pn += 1
        return articles