"""ETL 聚合模块：按维度汇总计算（用户行为、属地分布、视频互动度）。"""
from __future__ import annotations

from collections import Counter
from typing import Any

from etl.cleaner import normalize_ip


def aggregate_ip_distribution(comments: list[dict[str, Any]],
                              top_n: int = 34) -> list[dict[str, Any]]:
    """统计评论 IP 属地分布，返回 [{ip, count, ratio}] 降序列表。"""
    c: Counter = Counter()
    total = 0
    for r in comments or []:
        ip = normalize_ip(r.get("ip_location"))
        c[ip] += 1
        total += 1
    out: list[dict[str, Any]] = []
    for ip, cnt in c.most_common(max(1, top_n)):
        ratio = round(cnt / total, 4) if total > 0 else 0.0
        out.append({"ip": ip, "count": cnt, "ratio": ratio})
    return out


def aggregate_user_engagement(comments: list[dict[str, Any]],
                              danmaku: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总已知用户的活跃互动画像（评论数、弹幕数、总点赞、活跃视频数）。"""
    stats: dict[int, dict[str, Any]] = {}

    for c in comments or []:
        uid = int(c.get("uid", 0) or 0)
        if uid <= 0:
            continue
        st = stats.setdefault(uid, {
            "uid": uid,
            "uname": str(c.get("uname") or c.get("member", {}).get("uname", "") or ""),
            "comments_count": 0,
            "danmaku_count": 0,
            "total_likes": 0,
            "videos_involved": set(),
        })
        st["comments_count"] += 1
        st["total_likes"] += int(c.get("like_num", 0) or 0)
        aid = c.get("video_aid")
        if aid:
            st["videos_involved"].add(aid)

    for d in danmaku or []:
        uid = int(d.get("uid", 0) or 0)
        if uid <= 0:
            continue
        st = stats.setdefault(uid, {
            "uid": uid,
            "uname": str(d.get("uname") or ""),
            "comments_count": 0,
            "danmaku_count": 0,
            "total_likes": 0,
            "videos_involved": set(),
        })
        st["danmaku_count"] += 1
        aid = d.get("video_aid")
        if aid:
            st["videos_involved"].add(aid)

    out: list[dict[str, Any]] = []
    for uid, st in stats.items():
        out.append({
            "uid": uid,
            "uname": st["uname"],
            "comments_count": st["comments_count"],
            "danmaku_count": st["danmaku_count"],
            "total_likes": st["total_likes"],
            "active_videos": len(st["videos_involved"]),
        })

    # 按综合互动量降序
    out.sort(key=lambda x: x["comments_count"] * 2 + x["danmaku_count"] + x["total_likes"] * 0.5, reverse=True)
    return out


def aggregate_video_summary(videos: list[dict[str, Any]],
                            comments: list[dict[str, Any]],
                            danmaku: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总各视频维度表现指标。"""
    c_map: Counter = Counter()
    like_map: Counter = Counter()
    for c in comments or []:
        aid = c.get("video_aid")
        if aid:
            c_map[aid] += 1
            like_map[aid] += int(c.get("like_num", 0) or 0)

    d_map: Counter = Counter()
    for d in danmaku or []:
        aid = d.get("video_aid")
        if aid:
            d_map[aid] += 1

    out = []
    for v in videos or []:
        aid = v.get("aid")
        bvid = v.get("bvid", "")
        title = v.get("title", "")
        up = v.get("owner_name", "")
        cnt_c = c_map.get(aid, 0)
        cnt_d = d_map.get(aid, 0)
        likes = like_map.get(aid, 0)
        out.append({
            "aid": aid,
            "bvid": bvid,
            "title": title,
            "owner": up,
            "comments": cnt_c,
            "danmaku": cnt_d,
            "comment_likes": likes,
            "pubdate": v.get("pubdate", 0),
        })
    out.sort(key=lambda x: x["comments"] + x["danmaku"], reverse=True)
    return out
