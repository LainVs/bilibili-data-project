"""水军 / 刷屏 / 控评检测纯函数（无 DB，可单测、GUI/CLI 共用）。

输入 rows：每行 dict，需含 uid/content，可选 ctime/ip_location/like_num/uname。
输出：按可疑度降序的 per-uid 记录，附命中信号与证据，便于人工复核。
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter
from typing import Any

from etl.cleaner import clean_text, normalize_ip


def normalize_content(text: str) -> str:
    """文案归一化：去 URL/@/表情、多余空白转单空格、小写英文，用于复读分组。"""
    t = clean_text(str(text or ""), remove_emoji=True, strip_urls=True)
    t = " ".join(t.split()).lower()
    return t[:200]


def parse_ts(value: Any) -> float | None:
    """ctime 兼容解析：epoch 数字 / datetime / 'YYYY-MM-DD HH:MM:SS' 字符串。"""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            return v if v > 0 else None
        if isinstance(value, _dt.datetime):
            return value.timestamp()
        s = str(value).strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _dt.datetime.strptime(s[:19] if len(fmt) > 10 else s[:10], fmt).timestamp()
            except ValueError:
                continue
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return None


def burst_max(timestamps: list[float], window: int = 600) -> int:
    """滑动窗口内最大发言数（默认 10 分钟窗口）。"""
    ts = sorted(t for t in timestamps if t is not None)
    if not ts:
        return 0
    best, left = 1, 0
    for right in range(len(ts)):
        while ts[right] - ts[left] > window:
            left += 1
        best = max(best, right - left + 1)
    return best


def ip_concentration(ips: list[str]) -> tuple[str, float]:
    """属地聚集度：返回 (Top1属地, Top1占比)。"""
    normed = [normalize_ip(i) for i in (ips or []) if str(i or "").strip()]
    if not normed:
        return ("-", 0.0)
    c = Counter(normed)
    top_ip, top_cnt = c.most_common(1)[0]
    return (top_ip, round(top_cnt / len(normed), 4))


def score_user(
    total: int,
    repeat_rate: float,
    burst: int,
    ip_ratio: float,
    *,
    min_texts: int = 5,
    repeat_threshold: float = 0.5,
    burst_threshold: int = 8,
    teleport_count: int = 0,
) -> tuple[float, list[str], str]:
    """综合打分（0~100）+ 命中信号 + 定级。"""
    hits: list[str] = []
    score = 0.0
    if total >= min_texts and repeat_rate >= repeat_threshold:
        hits.append("复读")
        score += 45.0 * min(1.0, repeat_rate)
    if burst >= burst_threshold:
        hits.append("爆发")
        score += 35.0 * min(1.0, burst / max(1, burst_threshold * 2))
    if total >= min_texts and ip_ratio >= 0.8:
        hits.append("同属地聚集")
        score += 20.0 * ip_ratio
    if teleport_count > 0:
        hits.append("异地瞬移")
        score += min(35.0, 20.0 + (teleport_count - 1) * 10.0)
    score = round(min(100.0, score), 1)
    if score >= 60 or len(hits) >= 2 or teleport_count >= 2:
        level = "high"
    elif score >= 30 or hits:
        level = "suspect"
    else:
        level = "normal"
    return (score, hits, level)


def detect_water_army(
    rows: list[dict[str, Any]],
    *,
    min_texts: int = 5,
    repeat_threshold: float = 0.5,
    burst_window: int = 600,
    burst_threshold: int = 8,
    top: int = 50,
) -> list[dict[str, Any]]:
    """对一批发言行做水军检测（纯内存聚合）。

    返回每项 {"uid","uname","total","repeat_rate","top_text","top_text_count",
    "burst_max","ip_top","ip_ratio","score","hits","level"}，按 score 降序截断 top。
    """
    groups: dict[int, dict[str, Any]] = {}
    for r in rows or []:
        try:
            uid = int((r or {}).get("uid", 0) or 0)
        except (TypeError, ValueError):
            continue
        if uid <= 0:
            continue
        g = groups.setdefault(uid, {
            "uid": uid,
            "uname": str((r or {}).get("uname") or ""),
            "texts": [],
            "timestamps": [],
            "ips": [],
            "raw_events": [],
        })
        if not g["uname"]:
            uname = str((r or {}).get("uname") or "")
            if uname:
                g["uname"] = uname
        g["texts"].append(normalize_content((r or {}).get("content")))
        ts = parse_ts((r or {}).get("ctime"))
        if ts is not None:
            g["timestamps"].append(ts)
        ip = (r or {}).get("ip_location")
        if ip:
            g["ips"].append(str(ip))
        if ts is not None and ip:
            g["raw_events"].append({"ctime": (r or {}).get("ctime"), "ip_location": ip})

    out: list[dict[str, Any]] = []
    from analysis.ip_trajectory import analyze_user_ip_trajectory

    for uid, g in groups.items():
        texts = [t for t in g["texts"] if t]
        total = len(g["texts"])
        if texts:
            c = Counter(texts)
            top_text, top_cnt = c.most_common(1)[0]
            repeat_rate = round(top_cnt / len(texts), 4)
        else:
            top_text, top_cnt, repeat_rate = "", 0, 0.0
        burst = burst_max(g["timestamps"], window=burst_window)
        ip_top, ip_ratio = ip_concentration(g["ips"])

        teleport_count = 0
        jump_count = 0
        if len(set(g["ips"])) >= 2 and len(g["raw_events"]) >= 2:
            try:
                traj = analyze_user_ip_trajectory(g["raw_events"])
                teleport_count = traj.get("teleport_count", 0)
                jump_count = traj.get("jump_count", 0)
            except Exception:
                pass

        score, hits, level = score_user(
            total, repeat_rate, burst, ip_ratio,
            min_texts=min_texts,
            repeat_threshold=repeat_threshold,
            burst_threshold=burst_threshold,
            teleport_count=teleport_count,
        )
        out.append({
            "uid": uid,
            "uname": g["uname"],
            "total": total,
            "repeat_rate": repeat_rate,
            "top_text": top_text[:80],
            "top_text_count": top_cnt,
            "burst_max": burst,
            "ip_top": ip_top,
            "ip_ratio": ip_ratio,
            "score": score,
            "hits": hits,
            "level": level,
            "teleport_count": teleport_count,
            "jump_count": jump_count,
        })
    out.sort(key=lambda x: (x["score"], x["total"]), reverse=True)
    return out[: max(1, top)]
