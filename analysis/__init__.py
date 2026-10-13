"""数据分析与挖掘层（含文本/统计/水军检测/IP属地轨迹）。"""
from analysis.detect import detect_water_army, score_user
from analysis.ip_trajectory import (
    PROVINCE_COORDS,
    analyze_user_ip_trajectory,
    batch_detect_ip_trajectories,
    calc_ip_distance,
    haversine_km,
    match_standard_province,
)

__all__ = [
    "detect_water_army",
    "score_user",
    "PROVINCE_COORDS",
    "match_standard_province",
    "haversine_km",
    "calc_ip_distance",
    "analyze_user_ip_trajectory",
    "batch_detect_ip_trajectories",
]
