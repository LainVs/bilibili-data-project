"""B站收藏夹 API：列出某用户全部收藏夹，并分页读取每夹内视频。

收藏夹相关接口需要 wbi 签名，复用 comments.py 的签名方案。
"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher

logger = logging.getLogger(__name__)

# 用户创建的收藏夹列表（up_mid 为对应用户）
FAV_FOLDER_API = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
# 某个收藏夹内的收藏内容（media_id 为收藏夹 id）
FAV_RESOURCE_API = "https://api.bilibili.com/x/v3/fav/resource/list"


class FavoritesFetcher:
    """抓取某用户（mid）的收藏夹及其视频列表。"""

    def __init__(self, client: ApiClient) -> None:
        self.client = client
        self._signer = CommentFetcher(client)

    def _sign(self, params: dict) -> dict:
        return self._signer._sign(params)

    def list_folders(self, mid: int, progress_cb=None) -> list[dict]:
        """列出某用户创建的公开收藏夹。

        返回 [{media_id, title, media_count}]。
        """
        folders: list[dict] = []
        params = self._sign({"up_mid": mid, "platform": "web"})
        payload = self.client.get_json(FAV_FOLDER_API, params=params)
        data = payload.get("data") or {}
        items = data.get("list") or data.get("llist") or []
        for it in items:
            mid_v = it.get("id") or 0
            cnt = it.get("media_count") or 0
            if not mid_v:
                continue
            folders.append({
                "media_id": mid_v,
                "fid": it.get("fid") or mid_v,
                "title": it.get("title", ""),
                "media_count": cnt,
            })
        folders.sort(key=lambda f: f.get("media_count", 0), reverse=True)
        if progress_cb:
            progress_cb(f"共 {len(folders)} 个收藏夹："
                        + "、".join(f"{f['title']}({f['media_count']})" for f in folders[:10]))
        return folders

    def list_folder_videos(self, media_id: int, *, max_pages: int = 50,
                           progress_cb=None, all_done_check=None,
                           stop_flag=None) -> list[dict]:
        """分页读取单个收藏夹内的全部视频。

        第一页拿到 media_count 后，剩余页按收藏夹列表并发拉取；
        total 未知或并发不可用时回退逐页串行。

        all_done_check(items)：可选回调，每页(或每并发批次)拉取后调用，
        返回 True 视为"该页全部已在库且采集完成"，后续更早收藏的页不再拉取
        ——续跑时只拉最新几页即可跳到已完成边界。

        返回 [{bvid, aid?, title, owner_uid, owner_name, fav_time}]。
        实例属性：last_total=夹内总数，early_stopped=是否提前停页。
        """
        from config import get_config

        videos: list[dict] = []
        # 各列表接口服务端墙不同，按类型独立取值；未配置时回退 list_page_workers
        cfg = get_config()
        workers = getattr(cfg, "fav_list_workers", None) or cfg.list_page_workers
        ps = 20
        self.early_stopped = False
        self.last_total = 0

        def _fetch_page(client, pn: int) -> list[dict]:
            params = CommentFetcher(client)._sign({
                "media_id": media_id,
                "pn": pn,
                "ps": ps,
                "platform": "web",
                "order": "mtime",
                "type": 0,
            })
            payload = client.get_json(FAV_RESOURCE_API, params=params)
            medias = (payload.get("data") or {}).get("medias") or []
            out: list[dict] = []
            for it in medias:
                bvid = it.get("bvid") or ""
                if not bvid:
                    continue
                upper = it.get("upper") or {}
                out.append({
                    "bvid": bvid,
                    "aid": it.get("id") or it.get("aid") or 0,
                    "title": it.get("title", ""),
                    "owner_uid": upper.get("mid") or 0,
                    "owner_name": upper.get("name") or "",
                    "fav_time": it.get("fav_time") or it.get("ctime") or 0,
                })
            return out

        # 第 1 页：拿 media_count + 首批
        if progress_cb:
            progress_cb(f"读取收藏夹 {media_id} 第1页")
        first_payload = self.client.get_json(
            FAV_RESOURCE_API,
            params=self._sign({"media_id": media_id, "pn": 1, "ps": ps,
                               "platform": "web", "order": "mtime", "type": 0}))
        first_data = first_payload.get("data") or {}
        medias = first_data.get("medias") or []
        total = (first_data.get("info") or {}).get("media_count", 0)
        if not medias:
            return []
        self.last_total = total or 0
        for it in medias:
            bvid = it.get("bvid") or ""
            if not bvid:
                continue
            upper = it.get("upper") or {}
            videos.append({
                "bvid": bvid,
                "aid": it.get("id") or it.get("aid") or 0,
                "title": it.get("title", ""),
                "owner_uid": upper.get("mid") or 0,
                "owner_name": upper.get("name") or "",
                "fav_time": it.get("fav_time") or it.get("ctime") or 0,
            })

        total_pages = min(max_pages, (total + ps - 1) // ps) if total else max_pages
        remaining = list(range(2, total_pages + 1))
        if not remaining:
            return videos

        if workers <= 1 or len(remaining) == 1:
            if progress_cb:
                progress_cb(f"读取收藏夹 {media_id} 剩余 {len(remaining)} 页…")
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
                    progress_cb(f"读取收藏夹 {media_id} 并行第{batch[0]}-{batch[-1]}页")
                results = fetch_pages_parallel(
                    _fetch_page, batch, workers=workers,
                    client_factory=type(self.client), stop_flag=stop_flag)
                for _pn, items in results:
                    videos.extend(items)
                    if total and len(videos) >= total:
                        return videos
                # 整批页全部成功且每页均命中"已采集完成"→ 后续更早收藏的页无需再拉
                if (all_done_check and len(results) == len(batch)
                        and all(all_done_check(it) for _pn, it in results)):
                    self.early_stopped = True
                    return videos
        return videos
