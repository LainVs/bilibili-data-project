"""ETL 清洗、聚合与导出单元测试。"""
import json

from etl.aggregator import (
    aggregate_ip_distribution,
    aggregate_user_engagement,
    aggregate_video_summary,
)
from etl.cleaner import clean_text, normalize_ip, sanitize_user_profile
from etl.exporter import (
    COMMENT_FIELDS,
    DANMAKU_FIELDS,
    export_to_csv,
    export_to_jsonl,
)


def test_clean_text_comprehensive():
    raw = "【前排】[doge] https://b23.tv/abc @哔哩哔哩 #热门话题# 这视频太棒了！ &lt;script&gt;"
    cleaned = clean_text(raw, remove_emoji=True)
    assert "[doge]" not in cleaned
    assert "https://" not in cleaned
    assert "@" not in cleaned
    assert "#热门话题#" not in cleaned
    assert "这视频太棒了！" in cleaned
    assert "<script>" in cleaned  # HTML 反转义成功


def test_normalize_ip():
    assert normalize_ip("IP属地：北京") == "北京"
    assert normalize_ip("属地: 广东") == "广东"
    assert normalize_ip("来自四川") == "四川"
    assert normalize_ip(None) == "未知"
    assert normalize_ip("") == "未知"


def test_sanitize_user_profile():
    u = {"uid": "12345", "uname": " 测试用户 ", "level": 10, "sex": "其他", "is_vip": 2}
    res = sanitize_user_profile(u)
    assert res["uid"] == 12345
    assert res["uname"] == "测试用户"
    assert res["level"] == 6  # 限制在 0-6
    assert res["sex"] == "保密"
    assert res["is_vip"] == 1


def test_aggregate_ip_distribution():
    comments = [
        {"ip_location": "北京"},
        {"ip_location": "北京"},
        {"ip_location": "上海"},
        {"ip_location": ""},
    ]
    dist = aggregate_ip_distribution(comments)
    assert dist[0]["ip"] == "北京"
    assert dist[0]["count"] == 2
    assert dist[0]["ratio"] == 0.5


def test_aggregate_user_engagement():
    comments = [
        {"uid": 1001, "uname": "alice", "like_num": 10, "video_aid": 1},
        {"uid": 1001, "uname": "alice", "like_num": 5, "video_aid": 2},
        {"uid": 1002, "uname": "bob", "like_num": 2, "video_aid": 1},
    ]
    danmaku = [
        {"uid": 1001, "video_aid": 1},
    ]
    users = aggregate_user_engagement(comments, danmaku)
    assert len(users) == 2
    assert users[0]["uid"] == 1001
    assert users[0]["comments_count"] == 2
    assert users[0]["danmaku_count"] == 1
    assert users[0]["total_likes"] == 15
    assert users[0]["active_videos"] == 2


def test_aggregate_video_summary():
    videos = [{"aid": 1, "bvid": "BV1", "title": "V1", "owner_name": "UP1"}]
    comments = [{"video_aid": 1, "like_num": 10}, {"video_aid": 1, "like_num": 5}]
    danmaku = [{"video_aid": 1}]
    summary = aggregate_video_summary(videos, comments, danmaku)
    assert len(summary) == 1
    assert summary[0]["comments"] == 2
    assert summary[0]["danmaku"] == 1
    assert summary[0]["comment_likes"] == 15


def test_export_csv_and_jsonl(tmp_path):
    rows = [
        {"rpid": 1, "uid": 100, "content": "测试评论", "like_num": 9},
        {"rpid": 2, "uid": 101, "content": "第二条", "like_num": 0},
    ]
    csv_file = str(tmp_path / "test.csv")
    jsonl_file = str(tmp_path / "test.jsonl")

    count_csv = export_to_csv(rows, csv_file, field_mapping=[("rpid", "ID"), ("content", "内容")])
    assert count_csv == 2
    with open(csv_file, encoding="utf-8-sig") as f:
        content = f.read()
        assert "ID,内容" in content
        assert "1,测试评论" in content

    count_jsonl = export_to_jsonl(rows, jsonl_file)
    assert count_jsonl == 2
    with open(jsonl_file, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
        assert len(lines) == 2
        assert lines[0]["rpid"] == 1
