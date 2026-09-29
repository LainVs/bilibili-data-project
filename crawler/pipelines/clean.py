"""清洗 / 规范化管道：评论、弹幕、用户、视频数据入库前的装配。"""
from __future__ import annotations

from typing import Iterable

from etl.cleaner import normalize_ip, sanitize_user_profile

_BV_TABLE = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
_BV_S = [11, 10, 3, 8, 4, 6]
_BV_XOR = 177451812
_BV_ADD = 8728348608


def bvid_to_aid(bvid: str) -> int | None:
    """B站官方数学双射：BV 号 -> 数字 avid。零网络开销，微秒级完成。"""
    if not bvid or len(bvid) != 12:
        return None
    try:
        tr = {c: i for i, c in enumerate(_BV_TABLE)}
        r = 0
        for i in range(6):
            r += tr[bvid[_BV_S[i]]] * (58 ** i)
        return (r - _BV_ADD) ^ _BV_XOR
    except Exception:
        return None


def normalize_video(meta: dict) -> dict:
    """视频元信息规范化（兼容 key 缺省）。"""
    return {
        "aid": int(meta.get("aid", 0)),
        "bvid": str(meta.get("bvid", "")),
        "cid": int(meta.get("cid", 0)),
        "title": str(meta.get("title", "")).strip(),
        "owner_uid": int(meta.get("owner_uid", 0) or 0),
        "pubdate": int(meta.get("pubdate", 0) or 0),
        "part": int(meta.get("part", 1)),
    }


def normalize_danmaku(raw: dict, aid: int) -> dict:
    """弹幕规范化；mid_hash 去空格小写；ctime 转 MySQL datetime。"""
    return {
        "id": int(raw.get("id", 0)),
        "video_aid": int(aid),
        "uid": 0,  # 后续由反查填充
        "mid_hash": str(raw.get("mid_hash", "")).strip().lower(),
        "content": str(raw.get("content", ""))[:4000],
        "progress": float(raw.get("progress", 0.0)),
        "mode": int(raw.get("mode", 1)),
        "color": int(raw.get("color", 16777215) or 16777215),
        "ctime": _ts_to_dt(raw.get("ctime", 0)),
    }


def _ts_to_dt(ts: int) -> str:
    """unix 时间戳 → MySQL datetime 字符串。"""
    if not ts:
        return "1970-01-01 00:00:00"
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def normalize_comment(raw: dict, oid: int) -> dict:
    """评论规范化；拆出 member 信息。oid 为对象ID（视频aid/动态id/专栏cvid）。"""
    member = raw.get("member") or {}
    return {
        "rpid": int(raw.get("rpid", 0)),
        "video_aid": int(oid),
        "ref_type": int(raw.get("ref_type", 1) or 1),
        "parent": int(raw.get("parent", 0) or 0),
        "uid": int(raw.get("uid", 0) or 0),
        "content": str(raw.get("content", ""))[:4000],
        "like_num": int(raw.get("like_num", 0) or 0),
        "ctime": raw.get("ctime", 0),
        "ip_location": normalize_ip(raw.get("ip_location")),
        "member": member,
    }


def build_user_records(comments: Iterable[dict], danmaku_uids: Iterable[tuple[int, dict]]) -> list[dict]:
    """从评论 member + 弹幕反查 uid 汇总构建用户维度记录。"""
    user_map: dict[int, dict] = {}
    for c in comments:
        member = c.get("member") or {}
        uid = c.get("uid", 0)
        if uid:
            user_map.setdefault(
                uid,
                sanitize_user_profile({
                    "uid": uid,
                    "uname": str(member.get("uname", "")),
                    "level": int(member.get("level", 0) or 0),
                    "sex": str(member.get("sex", "")),
                    "is_vip": int(member.get("is_vip", 0) or 0),
                    "signature": str(member.get("signature", "")),
                    "fans": 0,
                }),
            )
    for uid, _unused in danmaku_uids:
        if uid and uid not in user_map:
            user_map[uid] = {
                "uid": uid,
                "uname": "",
                "level": 0,
                "sex": "保密",
                "is_vip": 0,
                "signature": "",
                "fans": 0,
            }
    return list(user_map.values())