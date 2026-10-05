"""IP 属地迁移轨迹与异地跳跃/瞬移检测引擎。

基于时序发言记录的 ip_location 变迁，精准识别：
1. 单用户属地流转时间轴（省份变更、停留时长、发言密度）；
2. 异常跳跃 / 瞬移判定（基于全国省会/行政区大地距离与隐含时速，识别代理池、脚本与异地号贩）；
3. 视频/群体级批量巡演排查（配合水军检测打分）。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from analysis.detect import parse_ts
from etl.cleaner import normalize_ip

# 全国 34 个省级行政区省会/中心点经纬度 (WGS84 逼近坐标)
PROVINCE_COORDS: dict[str, tuple[float, float]] = {
    "北京": (39.9042, 116.4074),
    "天津": (39.0842, 117.2009),
    "河北": (38.0428, 114.5149),
    "山西": (37.8706, 112.5489),
    "内蒙古": (40.8415, 111.7519),
    "辽宁": (41.8057, 123.4315),
    "吉林": (43.8171, 125.3235),
    "黑龙江": (45.7569, 126.6424),
    "上海": (31.2304, 121.4737),
    "江苏": (32.0617, 118.7632),
    "浙江": (30.2741, 120.1551),
    "安徽": (31.8612, 117.2849),
    "福建": (26.0789, 119.3062),
    "江西": (28.6832, 115.8582),
    "山东": (36.6512, 117.1201),
    "河南": (34.7579, 113.6654),
    "湖北": (30.5928, 114.3055),
    "湖南": (28.2282, 112.9388),
    "广东": (23.1291, 113.2644),
    "广西": (22.8170, 108.3665),
    "海南": (20.0440, 110.3492),
    "重庆": (29.5630, 106.5516),
    "四川": (30.5728, 104.0668),
    "贵州": (26.6477, 106.6302),
    "云南": (25.0406, 102.7123),
    "西藏": (29.6441, 91.1145),
    "陕西": (34.3416, 108.9398),
    "甘肃": (36.0611, 103.8343),
    "青海": (36.6232, 101.7789),
    "宁夏": (38.4872, 106.2309),
    "新疆": (43.8256, 87.6168),
    "香港": (22.3193, 114.1694),
    "澳门": (22.1987, 113.5439),
    "台湾": (25.0330, 121.5654),
}


def match_standard_province(raw_ip: str | None) -> str:
    """提取规范省份/地区名（如 '中国北京' -> '北京'，'广东省' -> '广东'）。"""
    ip = normalize_ip(raw_ip)
    if not ip or ip in ("未知", "unknown", "-"):
        return "未知"
    for prov in PROVINCE_COORDS:
        if prov in ip:
            return prov
    return ip


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """使用大圆航线半正矢公式 (Haversine) 计算两地间球面距离 (公里)。"""
    r = 6371.0  # 地球平均半径 (km)
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(r * c, 1)


def calc_ip_distance(ip_a: str, ip_b: str) -> float:
    """估算两属地之间的直线地理距离 (km)。同属地返回 0.0。未知或跨国默认返回 1500.0 km。"""
    p_a = match_standard_province(ip_a)
    p_b = match_standard_province(ip_b)
    if p_a == p_b:
        return 0.0
    c_a = PROVINCE_COORDS.get(p_a)
    c_b = PROVINCE_COORDS.get(p_b)
    if c_a and c_b:
        return haversine_km(c_a[0], c_a[1], c_b[0], c_b[1])
    # 涉外或未录入省份跨域跳转，按保守跨区域距离估计
    return 1500.0


def analyze_user_ip_trajectory(
    rows: list[dict[str, Any]],
    teleport_speed_kmh: float = 800.0,
    min_teleport_dist_km: float = 300.0,
) -> dict[str, Any]:
    """分析单一 UID 的 IP 属地时序演进与迁移轨迹。

    rows: 该用户的发言行列表，每项含 ctime, ip_location, 可选 content, bvid, title。
    返回结构包含 segments (分段聚集), transitions (跳跃明细), teleport_count (瞬移异动) 等。
    """
    valid_events: list[dict[str, Any]] = []
    for r in rows or []:
        raw_ip = (r or {}).get("ip_location")
        norm_ip = match_standard_province(raw_ip)
        if norm_ip == "未知":
            continue
        ts = parse_ts((r or {}).get("ctime"))
        if ts is None:
            continue
        valid_events.append({
            "ts": ts,
            "ctime": str((r or {}).get("ctime") or ""),
            "ip": norm_ip,
            "content": str((r or {}).get("content") or "")[:60],
            "bvid": str((r or {}).get("bvid") or ""),
            "title": str((r or {}).get("title") or "")[:40],
        })

    valid_events.sort(key=lambda x: x["ts"])

    uid = 0
    for r in rows or []:
        try:
            u = int((r or {}).get("uid", 0) or 0)
            if u > 0:
                uid = u
                break
        except (ValueError, TypeError):
            continue

    if not valid_events:
        return {
            "uid": uid,
            "total_records": len(rows or []),
            "valid_records": 0,
            "distinct_ips": [],
            "distinct_count": 0,
            "jump_count": 0,
            "teleport_count": 0,
            "max_speed_kmh": 0.0,
            "shortest_interval_hours": None,
            "segments": [],
            "transitions": [],
            "risk_level": "CLEAN",
            "summary": "无有效属地时序数据",
        }

    # 1. 连续同属地聚合为时段 (segments)
    segments: list[dict[str, Any]] = []
    curr_seg: dict[str, Any] | None = None
    for ev in valid_events:
        if curr_seg is None or curr_seg["ip"] != ev["ip"]:
            if curr_seg:
                curr_seg["duration_hours"] = round((curr_seg["end_ts"] - curr_seg["start_ts"]) / 3600.0, 2)
                segments.append(curr_seg)
            curr_seg = {
                "ip": ev["ip"],
                "start_ts": ev["ts"],
                "start_time": ev["ctime"],
                "end_ts": ev["ts"],
                "end_time": ev["ctime"],
                "count": 1,
                "first_content": ev["content"],
                "first_bvid": ev["bvid"],
            }
        else:
            curr_seg["end_ts"] = ev["ts"]
            curr_seg["end_time"] = ev["ctime"]
            curr_seg["count"] += 1
    if curr_seg:
        curr_seg["duration_hours"] = round((curr_seg["end_ts"] - curr_seg["start_ts"]) / 3600.0, 2)
        segments.append(curr_seg)

    # 2. 计算迁移跳转 (transitions)
    transitions: list[dict[str, Any]] = []
    teleport_count = 0
    max_speed_kmh = 0.0
    shortest_interval_sec: float | None = None

    for i in range(len(segments) - 1):
        s_from = segments[i]
        s_to = segments[i + 1]
        interval_sec = max(0.0, s_to["start_ts"] - s_from["end_ts"])
        interval_hours = interval_sec / 3600.0
        if shortest_interval_sec is None or interval_sec < shortest_interval_sec:
            shortest_interval_sec = interval_sec

        dist_km = calc_ip_distance(s_from["ip"], s_to["ip"])
        # 隐含时速：若间隔极短，以 0.005h (18秒) 为保底防除零
        speed_kmh = round(dist_km / max(0.005, interval_hours), 1)
        max_speed_kmh = max(max_speed_kmh, speed_kmh)

        # 瞬移判断规则：
        # 1. 距离 >= 300km 且 速度 > 800 km/h (超越高铁飞机常规极限)；
        # 2. 或者 距离 >= 400km 且 间隔 < 30 分钟 (无论航线多快均不可能完成机坪与安检)
        is_teleport = False
        if dist_km >= min_teleport_dist_km:
            if speed_kmh > teleport_speed_kmh or (interval_hours < 0.5 and dist_km >= 400.0):
                is_teleport = True

        if is_teleport:
            teleport_count += 1
            flag = "瞬移异动(疑似代理)"
        elif dist_km >= 200.0:
            flag = "跨省/长途迁移"
        else:
            flag = "周边迁移"

        transitions.append({
            "from_ip": s_from["ip"],
            "to_ip": s_to["ip"],
            "from_time": s_from["end_time"],
            "to_time": s_to["start_time"],
            "interval_sec": round(interval_sec, 1),
            "interval_hours": round(interval_hours, 2),
            "distance_km": dist_km,
            "speed_kmh": speed_kmh,
            "is_teleport": is_teleport,
            "flag": flag,
            "from_content": s_from.get("first_content", ""),
            "to_content": s_to.get("first_content", ""),
        })

    distinct_ips = [s["ip"] for s in segments]
    distinct_uniq = list(dict.fromkeys(distinct_ips))
    jump_count = len(transitions)

    # 风险定级
    if teleport_count > 0 or (jump_count >= 4 and len(distinct_uniq) >= 3):
        risk_level = "HIGH"
    elif jump_count >= 2 or len(distinct_uniq) >= 2:
        risk_level = "MEDIUM"
    elif jump_count == 1:
        risk_level = "LOW"
    else:
        risk_level = "CLEAN"

    # 生成叙述性摘要
    chain_str = " ➔ ".join(distinct_ips) if len(distinct_ips) <= 6 else f"{' ➔ '.join(distinct_ips[:5])} ...({len(distinct_ips)}次)"
    shortest_h = round(shortest_interval_sec / 3600.0, 2) if shortest_interval_sec is not None else None

    if jump_count == 0:
        summary = f"属地稳定为 [{distinct_uniq[0] if distinct_uniq else '未知'}]，未发生跨地跳跃。"
    else:
        summary = (
            f"发言涉及 {len(distinct_uniq)} 个属地，经历 {jump_count} 次流转 [{chain_str}]。"
        )
        if teleport_count > 0:
            summary += f" ⚠ 命中 {teleport_count} 次超速瞬移（最高隐含时速 {max_speed_kmh:,.0f} km/h），疑似机房代理池或异地号贩。"
        elif shortest_h is not None and shortest_h < 4.0:
            summary += f" 最短迁移间隔仅 {shortest_h:.1f} 小时，可能为高铁/民航频繁出行或多设备切换。"

    return {
        "uid": uid,
        "total_records": len(rows or []),
        "valid_records": len(valid_events),
        "distinct_ips": distinct_uniq,
        "distinct_count": len(distinct_uniq),
        "jump_count": jump_count,
        "teleport_count": teleport_count,
        "max_speed_kmh": max_speed_kmh,
        "shortest_interval_hours": shortest_h,
        "segments": segments,
        "transitions": transitions,
        "risk_level": risk_level,
        "summary": summary,
    }


def batch_detect_ip_trajectories(
    rows: list[dict[str, Any]],
    min_records: int = 2,
    top: int = 50,
) -> list[dict[str, Any]]:
    """群体发言 IP 属地跳跃批量筛查（适用于单视频或增量批次）。

    找出存在多属地流转或异地瞬移的可疑用户名单。按 (teleport_count, jump_count, max_speed_kmh) 降序。
    """
    by_uid: dict[int, list[dict[str, Any]]] = {}
    names: dict[int, str] = {}
    for r in rows or []:
        try:
            uid = int((r or {}).get("uid", 0) or 0)
        except (ValueError, TypeError):
            continue
        if uid <= 0:
            continue
        by_uid.setdefault(uid, []).append(r)
        if uid not in names and (r or {}).get("uname"):
            names[uid] = str(r["uname"])

    results: list[dict[str, Any]] = []
    for uid, urows in by_uid.items():
        if len(urows) < min_records:
            continue
        traj = analyze_user_ip_trajectory(urows)
        if traj["jump_count"] > 0:
            traj["uname"] = names.get(uid, "")
            results.append(traj)

    results.sort(
        key=lambda x: (
            x["teleport_count"],
            x["jump_count"],
            x["max_speed_kmh"],
            x["valid_records"],
        ),
        reverse=True,
    )
    return results[: max(1, top)]
