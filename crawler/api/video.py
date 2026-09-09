"""B站视频信息接口。"""
from __future__ import annotations


def parse_info(data: dict) -> dict:
    """从视频详情接口结果提取结构化字段。"""
    pages = data.get("pages") or []
    if not pages:
        return {}
    first = pages[0]
    owner = data.get("owner") or {}
    return {
        "aid": data.get("aid", 0),
        "bvid": data.get("bvid", ""),
        "cid": first.get("cid", 0),
        "title": data.get("title", ""),
        "owner_uid": owner.get("mid", 0),
        "owner_name": owner.get("name", ""),
        "pubdate": data.get("pubdate", 0),
        "part": first.get("page", 1),
    }