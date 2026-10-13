"""B站热门/热搜视频：popular 榜单接口。"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient

logger = logging.getLogger(__name__)

POPULAR_API = "https://api.bilibili.com/x/web-interface/popular"


class PopularFetcher:
    """拉取当前热门榜视频列表（无需登录，公开接口）。"""

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def collect(self, *, count: int = 20, stop_flag=None) -> list[dict]:
        """取热门榜前 count 个视频。

        返回 [{aid, bvid, cid, title, pubdate, owner_uid, owner_name, duration}]，
        按榜单热度顺序（无需再排序）。
        """
        items: list[dict] = []
        pn = 1
        while len(items) < count:
            if stop_flag is not None and stop_flag.is_set():
                break
            payload = self.client.get_json(POPULAR_API, params={"pn": pn, "ps": 20})
            data = payload.get("data") or {}
            lst = data.get("list") or []
            if not lst:
                break
            for v in lst:
                owner = v.get("owner") or {}
                title = v.get("title", "") or ""
                if isinstance(title, dict):
                    title = title.get("title", "") or ""
                title = str(title).replace("<em class=\"keyword\">", "") \
                    .replace("</em>", "")
                items.append({
                    "aid": v.get("aid", 0) or 0,
                    "bvid": v.get("bvid", "") or "",
                    "cid": v.get("cid", 0) or 0,
                    "title": title,
                    "pubdate": v.get("pubdate", 0) or 0,
                    "owner_uid": owner.get("mid", 0) or 0,
                    "owner_name": owner.get("name", "") or "",
                    "duration": v.get("duration", "") or "",
                })
            pn += 1
            if len(lst) < 20:
                break
        return items[:count]
