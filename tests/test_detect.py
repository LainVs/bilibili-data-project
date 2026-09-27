"""水军检测单测：纯函数，无需数据库。"""
from analysis.detect import (
    burst_max,
    detect_water_army,
    ip_concentration,
    normalize_content,
    parse_ts,
    score_user,
)


def _row(uid, content, ctime="2026-09-01 20:00:00", ip="", uname=""):
    return {"uid": uid, "content": content, "ctime": ctime,
            "ip_location": ip, "uname": uname}


def test_normalize_content_groups_repeats():
    assert normalize_content("好看！ https://b23.tv/x  ") == normalize_content("好看！")
    assert normalize_content("  Hello   World ") == "hello world"


def test_parse_ts_formats():
    assert parse_ts(1725225600) == 1725225600.0
    assert parse_ts("2026-09-01 20:00:00") is not None
    assert parse_ts("") is None
    assert parse_ts(None) is None


def test_burst_max_sliding_window():
    base = parse_ts("2026-09-01 20:00:00")
    ts = [base + i * 60 for i in range(8)]  # 8 条落在 8 分钟内
    assert burst_max(ts, window=600) == 8
    assert burst_max([base, base + 3600], window=600) == 1


def test_ip_concentration():
    ip, ratio = ip_concentration(["北京", "北京", "北京", "上海"])
    assert ip == "北京" and ratio == 0.75


def test_score_user_levels():
    s, hits, level = score_user(10, 0.9, 3, 0.2)
    assert level in ("high", "suspect") and "复读" in hits
    s2, hits2, level2 = score_user(10, 0.1, 12, 0.1)
    assert "爆发" in hits2
    _, _, lv3 = score_user(2, 0.0, 1, 0.0)
    assert lv3 == "normal"


def test_detect_water_army_repeater_flagged():
    rows = [_row(1001, "支持支持！必看", ip="北京", uname="a") for _ in range(8)]
    rows += [_row(1002, f"正常评论{i}", ip="上海") for i in range(8)]
    res = detect_water_army(rows)
    by_uid = {r["uid"]: r for r in res}
    assert by_uid[1001]["level"] in ("high", "suspect")
    assert by_uid[1001]["repeat_rate"] == 1.0
    assert res[0]["uid"] == 1001  # 按 score 降序
