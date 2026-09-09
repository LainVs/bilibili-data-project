"""B站用户社交关系 API：关注列表、粉丝列表。

公开接口（需登录 Cookie 数据更全）：
  /x/relation/followings - 用户关注列表
  /x/relation/followers  - 用户粉丝列表
两者均返回 {list: [{mid, uname, ...}], total}。
"""
from __future__ import annotations

import logging

from crawler.api.client import ApiClient

logger = logging.getLogger(__name__)

FOLLOWINGS_API = "https://api.bilibili.com/x/relation/followings"
FOLLOWERS_API = "https://api.bilibili.com/x/relation/followers"


def _get_page_1(client, api: str, mid: int) -> dict:
    """拉取第一页原始响应（列表类接口 total 只在此页返回）。"""
    return client.get_json(api, params={"vmid": mid, "pn": 1, "ps": 50, "order": "desc"})


class RelationFetcher:
    """分页拉取某用户（mid）的关注/粉丝列表。"""

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def list_followings(self, mid: int, *, max_users: int = 200,
                        progress_cb=None, stop_flag=None) -> list[dict]:
        """分页拉取关注列表，返回 [{mid, uname}]。"""
        return self._list(FOLLOWINGS_API, mid, max_users=max_users,
                          progress_cb=progress_cb, label="关注",
                          stop_flag=stop_flag)

    def list_followers(self, mid: int, *, max_users: int = 200,
                       progress_cb=None, stop_flag=None) -> list[dict]:
        """分页拉取粉丝列表，返回 [{mid, uname}]。"""
        return self._list(FOLLOWERS_API, mid, max_users=max_users,
                          progress_cb=progress_cb, label="粉丝",
                          stop_flag=stop_flag)

    def _list(self, api: str, mid: int, *, max_users: int, progress_cb,
              label: str, all_done_check=None, stop_flag=None) -> list[dict]:
        """分页拉取关注/粉丝列表。

        第一页拿到 total 后，剩余页按关系列表并发拉取；
        total 未知或并发非可用时回退逐页串行（保持旧行为）。

        all_done_check(items)：可选回调，每页(或每并发批次)拉取后调用，
        返回 True 视为"该页全部已在库且采集完成"，后续更早关注的页不再拉取。

        返回 [{mid, uname}]。
        实例属性：last_total=关系总数，early_stopped=是否提前停页。
        """
        from config import get_config

        users: list[dict] = []
        # 各列表接口服务端墙不同，按类型独立取值；未配置时回退 list_page_workers
        cfg = get_config()
        workers = getattr(cfg, "relation_list_workers", None) or cfg.list_page_workers
        self.early_stopped = False
        self.last_total = 0

        def _fetch_page(client, pn: int) -> list[dict]:
            params = {"vmid": mid, "pn": pn, "ps": 50, "order": "desc"}
            payload = client.get_json(api, params=params)
            return _parse((payload.get("data") or {}))

        def _parse(data: dict) -> list[dict]:
            out: list[dict] = []
            for it in data.get("list") or []:
                uid = it.get("mid") or 0
                if not uid:
                    continue
                out.append({"mid": uid, "uname": it.get("uname", "")})
            return out

        # 第 1 页：拿 total + 首批数据（一次请求）
        if progress_cb:
            progress_cb(f"读取{label}列表 第1页")
        data = _get_page_1(self.client, api, mid).get("data") or {}
        total = data.get("total") or 0
        self.last_total = total or 0
        first = _parse(data)
        users.extend(first)

        if max_users and len(users) >= max_users:
            return users[:max_users]
        if not first:
            return users

        # 并发优先：total 已知时把剩余页全并行拉取（页数按 max_users 截断）
        if total and workers > 1:
            from crawler.api.client import fetch_pages_parallel

            fetch_count = max_users if max_users else total
            pages = list(range(2, min(200, ((fetch_count + 49) // 50) + 1)))
            for batch_start in range(0, len(pages), workers):
                if stop_flag is not None and stop_flag.is_set():
                    return users[:max_users] if max_users else users
                batch = pages[batch_start:batch_start + workers]
                if progress_cb:
                    progress_cb(f"读取{label}列表 并行第{batch[0]}-{batch[-1]}页")
                for _pn, items in fetch_pages_parallel(
                        _fetch_page, batch, workers=workers,
                        client_factory=type(self.client), stop_flag=stop_flag):
                    users.extend(items)
                    if max_users and len(users) >= max_users:
                        return users[:max_users]
                    if total and len(users) >= total:
                        return users
            return users[:max_users] if max_users else users

        # 串行兜底（total 未知 / 并发不可用）
        pn = 2
        while True:
            if stop_flag is not None and stop_flag.is_set():
                break
            if max_users and len(users) >= max_users:
                break
            if progress_cb:
                progress_cb(f"读取{label}列表 第{pn}页")
            items = _fetch_page(self.client, pn)
            if not items:
                break
            users.extend(items)
            if total and len(users) >= total:
                break
            if pn >= 200:  # 保守上限：80万关注/粉丝也不无限拉
                break
            pn += 1
        logger.info("用户 %s %s列表 共 %s 个", mid, label, len(users))
        if progress_cb:
            progress_cb(f"{label}共 {len(users)} 人")
        return users[:max_users] if max_users else users

    def sample(self, mid: int, *, limit: int = 5) -> dict:
        """一次性探测关注/粉丝接口是否开放，返回两列表前若干条。"""
        out: dict = {"followings": [], "followers": []}
        try:
            out["followings"] = self.list_followings(mid, max_users=limit)
        except Exception as exc:
            out["followings_error"] = str(exc)
        try:
            out["followers"] = self.list_followers(mid, max_users=limit)
        except Exception as exc:
            out["followers_error"] = str(exc)
        return out