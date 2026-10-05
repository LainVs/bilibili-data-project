"""IP 属地迁移轨迹与异地跳跃瞬移检测单元测试。"""
from analysis.detect import detect_water_army, score_user
from analysis.ip_trajectory import (
    analyze_user_ip_trajectory,
    batch_detect_ip_trajectories,
    calc_ip_distance,
    haversine_km,
    match_standard_province,
)


def test_haversine_and_distance():
    # 北京 (39.9042, 116.4074) 到 上海 (31.2304, 121.4737) ~ 1067 km
    d = calc_ip_distance("北京", "上海")
    assert 1000.0 < d < 1200.0

    # 同属地返回 0.0
    assert calc_ip_distance("广东", "广东") == 0.0

    # 规范化省份名称
    assert match_standard_province("中国北京") == "北京"
    assert match_standard_province("IP属地：广东省") == "广东"
    assert match_standard_province("未知") == "未知"
    assert match_standard_province("") == "未知"


def test_analyze_user_ip_trajectory_clean():
    # 稳定同地用户
    rows = [
        {"uid": 1001, "ctime": "2026-09-01 10:00:00", "ip_location": "广东", "content": "评论1"},
        {"uid": 1001, "ctime": "2026-09-01 12:00:00", "ip_location": "广东", "content": "评论2"},
        {"uid": 1001, "ctime": "2026-09-02 18:00:00", "ip_location": "广东", "content": "评论3"},
    ]
    res = analyze_user_ip_trajectory(rows)
    assert res["valid_records"] == 3
    assert res["distinct_count"] == 1
    assert res["jump_count"] == 0
    assert res["teleport_count"] == 0
    assert res["risk_level"] == "CLEAN"


def test_analyze_user_ip_trajectory_normal_travel():
    # 正常差旅（间隔 3 天）
    rows = [
        {"uid": 1002, "ctime": "2026-09-01 10:00:00", "ip_location": "广东", "content": "出发广州"},
        {"uid": 1002, "ctime": "2026-09-04 12:00:00", "ip_location": "北京", "content": "到达北京"},
        {"uid": 1002, "ctime": "2026-09-08 15:00:00", "ip_location": "上海", "content": "转机上海"},
    ]
    res = analyze_user_ip_trajectory(rows)
    assert res["jump_count"] == 2
    assert res["teleport_count"] == 0
    assert res["risk_level"] == "MEDIUM"
    assert len(res["transitions"]) == 2
    tr1 = res["transitions"][0]
    assert tr1["from_ip"] == "广东" and tr1["to_ip"] == "北京"
    assert tr1["is_teleport"] is False
    assert tr1["interval_hours"] > 70.0


def test_analyze_user_ip_trajectory_teleport_detected():
    # 异常瞬移（15 分钟内 广东 -> 北京 约 1900 km）
    rows = [
        {"uid": 1003, "ctime": "2026-09-01 10:00:00", "ip_location": "广东", "content": "发言A"},
        {"uid": 1003, "ctime": "2026-09-01 10:15:00", "ip_location": "北京", "content": "发言B"},
    ]
    res = analyze_user_ip_trajectory(rows)
    assert res["jump_count"] == 1
    assert res["teleport_count"] == 1
    assert res["risk_level"] == "HIGH"
    assert res["max_speed_kmh"] > 2000.0
    tr = res["transitions"][0]
    assert tr["is_teleport"] is True
    assert "瞬移" in tr["flag"]


def test_batch_detect_ip_trajectories():
    rows = [
        # 用户 1：瞬移
        {"uid": 2001, "ctime": "2026-09-01 10:00:00", "ip_location": "广东", "uname": "机房号1"},
        {"uid": 2001, "ctime": "2026-09-01 10:10:00", "ip_location": "黑龙江", "uname": "机房号1"},
        # 用户 2：正常迁移
        {"uid": 2002, "ctime": "2026-09-01 10:00:00", "ip_location": "四川", "uname": "真实用户"},
        {"uid": 2002, "ctime": "2026-09-05 10:00:00", "ip_location": "重庆", "uname": "真实用户"},
        # 用户 3：单地点
        {"uid": 2003, "ctime": "2026-09-01 10:00:00", "ip_location": "浙江", "uname": "常住用户"},
    ]
    batch = batch_detect_ip_trajectories(rows, min_records=2)
    # 只有存在跳跃的用户会被返回 (2001, 2002)
    assert len(batch) == 2
    # 瞬移者排在最前
    assert batch[0]["uid"] == 2001
    assert batch[0]["teleport_count"] == 1


def test_detect_water_army_teleport_integration():
    # 测试水军检测模块联动：瞬移用户命中 "异地瞬移" 信号
    rows = [
        {"uid": 3001, "ctime": "2026-09-01 10:00:00", "ip_location": "广东", "content": "支持"},
        {"uid": 3001, "ctime": "2026-09-01 10:08:00", "ip_location": "北京", "content": "支持"},
        {"uid": 3001, "ctime": "2026-09-01 10:15:00", "ip_location": "上海", "content": "支持"},
    ]
    results = detect_water_army(rows, min_texts=2)
    assert len(results) == 1
    r = results[0]
    assert r["uid"] == 3001
    assert "异地瞬移" in r["hits"]
    assert r["teleport_count"] >= 1
    assert r["level"] in ("high", "suspect")


def test_analyze_user_ip_trajectory_sparse_uid():
    # 首行 uid 缺失或为 0 时，能自动扫描到后续有效 uid
    rows = [
        {"uid": 0, "ctime": "2026-09-01 10:00:00", "ip_location": "北京", "content": "c1"},
        {"uid": 8888, "ctime": "2026-09-01 11:00:00", "ip_location": "北京", "content": "c2"},
    ]
    res = analyze_user_ip_trajectory(rows)
    assert res["uid"] == 8888
    assert res["valid_records"] == 2
