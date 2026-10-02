"""可视化研报引擎单测。"""
import os
import tempfile

from reporting import (
    ReportData,
    ReportSections,
    build_html_report,
    collect_report_data,
    generate_report,
    render_svg_hours_bar,
    render_svg_line_chart,
    render_svg_sentiment_donut,
)


def _sample_rows():
    return [
        {"uid": 1001, "content": "这个视频太好看了！强烈推荐", "ctime": "2026-09-01 20:00:00", "like_num": 100, "ip_location": "北京"},
        {"uid": 1002, "content": "很一般，内容有点水", "ctime": "2026-09-01 20:30:00", "like_num": 5, "ip_location": "上海"},
        {"uid": 1003, "content": "打卡支持！", "ctime": "2026-09-02 10:00:00", "like_num": 2, "ip_location": "广东"},
        {"uid": 1004, "content": "水军复读内容测试", "ctime": "2026-09-02 12:00:00", "like_num": 0, "ip_location": "北京"},
        {"uid": 1004, "content": "水军复读内容测试", "ctime": "2026-09-02 12:01:00", "like_num": 0, "ip_location": "北京"},
        {"uid": 1004, "content": "水军复读内容测试", "ctime": "2026-09-02 12:02:00", "like_num": 0, "ip_location": "北京"},
    ]


def test_svg_renderers():
    svg_line = render_svg_line_chart([{"day": "09-01", "cnt": 10}, {"day": "09-02", "cnt": 20}])
    assert "<svg" in svg_line and "lineGrad" in svg_line

    svg_hours = render_svg_hours_bar([{"hour": f"{i:02d}", "cnt": i} for i in range(24)])
    assert "<svg" in svg_hours and "<rect" in svg_hours

    svg_donut = render_svg_sentiment_donut({"pos": 10, "neu": 5, "neg": 2})
    assert "<svg" in svg_donut and "#22c55e" in svg_donut


def test_build_html_report_selective():
    data = ReportData(
        meta={"bvid": "BV1test", "title": "测试报告视频", "aid": 12345, "owner_name": "测试UP"},
        comments=_sample_rows(),
        danmaku=[],
        stats={"bvid": "BV1test", "title": "测试报告视频", "total_comments": 6, "total_danmaku": 0, "total_likes": 107, "unique_users": 4},
        daily=[{"day": "2026-09-01", "cnt": 3}, {"day": "2026-09-02", "cnt": 3}],
        hours=[{"hour": f"{i:02d}", "cnt": 1} for i in range(24)],
        sentiment={"pos": 2, "neu": 3, "neg": 1},
        terms=[{"term": "视频", "cnt": 5}, {"term": "推荐", "cnt": 3}],
        suspects=[{"uid": 1004, "total": 3, "repeat_rate": 1.0, "burst_max": 3, "ip_top": "北京", "ip_ratio": 1.0, "score": 65.0, "level": "high"}],
        top_liked=_sample_rows()[:3],
    )

    # 1. 全板块
    html_all = build_html_report(data)
    assert "核心指标概览" in html_all
    assert "互动传播与时间节律" in html_all
    assert "舆论倾向与情感基调" in html_all
    assert "讨论热词与观众地域" in html_all
    assert "水军刷屏与控评可疑名单" in html_all
    assert "精选高赞热评" in html_all

    # 2. 选择性关闭某些板块
    sec = ReportSections(trends=False, water_army=False, sentiment=False)
    html_partial = build_html_report(data, sections=sec)
    assert "核心指标概览" in html_partial
    assert "互动传播与时间节律" not in html_partial
    assert "水军刷屏与控评可疑名单" not in html_partial
    assert "舆论倾向与情感基调" not in html_partial


def test_generate_report_disk():
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "report_test.html")
        path = generate_report("BV1tJ4m1T7QL", out_path, limit=200)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 500
        content = open(path, encoding="utf-8").read()
        assert "<!DOCTYPE html>" in content
        assert "BILIBILI DATA INTELLIGENCE REPORT" in content
