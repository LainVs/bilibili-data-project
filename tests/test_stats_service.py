"""统计 V2 单测：纯函数，无需数据库。"""
import datetime

from analysis import stats_service as svc
from analysis.text import (
    clean_text,
    daily_series,
    hour_dist,
    ip_dist,
    length_dist,
    like_dist,
    sentiment_dist,
    tokenize,
    top_terms,
)


def _rows():
    return [
        {"content": "这个视频太好看了，强烈推荐！", "ctime": "2026-09-01 20:15:00",
         "like_num": 120, "ip_location": "北京"},
        {"content": "太烂了，避雷，难看", "ctime": "2026-09-01 21:00:00",
         "like_num": 0, "ip_location": "上海"},
        {"content": "哈哈哈笑死，前排支持", "ctime": "2026-09-02 08:00:00",
         "like_num": 5, "ip_location": ""},
        {"content": "", "ctime": None, "like_num": 0, "ip_location": ""},
    ]


def test_clean_text_strips_url_and_space():
    assert clean_text("看这个 https://b23.tv/x  好看  ") == "看这个 好看"


def test_tokenize_nonempty_zh():
    toks = tokenize("强烈推荐这个好看视频")
    assert isinstance(toks, list) and len(toks) > 0


def test_top_terms_counts():
    terms = top_terms(["好看 好看 推荐", "好看 拉胯"], top=2)
    assert terms[0]["term"] and terms[0]["cnt"] >= 2


def test_sentiment_dist_sums():
    d = sentiment_dist(["太好看了推荐", "太烂了避雷", "今天星期三"])
    assert d["total"] == 3 and d["pos"] >= 1 and d["neg"] >= 1


def test_hour_dist_peaks_evening():
    hs = hour_dist(_rows())
    assert len(hs) == 24 and hs[20]["cnt"] + hs[21]["cnt"] >= 2


def test_daily_series_fixed_window():
    rows = [{"ctime": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            for _ in range(3)]
    s = daily_series(rows, days=7)
    assert len(s) == 7 and s[-1]["cnt"] == 3


def test_like_and_length_and_ip():
    rows = _rows()
    lk = like_dist(rows)
    assert lk["total"] == 4 and lk["hot"] == 1
    ln = length_dist([r["content"] for r in rows])
    assert ln["total"] == 4
    ips = ip_dist(rows)
    assert ips and ips[0]["cnt"] >= 1


def test_compute_insights_and_summary():
    ins = svc.compute_insights(_rows(), top=5, days=7)
    assert ins["total"] == 4
    assert len(ins["daily"]) == 7 and len(ins["hours"]) == 24
    s = svc.insights_summary(ins)
    assert "共 4 条" in s


def test_overview_export_rows_shape():
    rows = svc.overview_export_rows({"videos": 1, "comments": 2})
    assert len(rows) == 10 and rows[0][0] == "采集视频数"


def test_export_csv_tmp(tmp_path):
    ins = svc.compute_insights(_rows(), top=5, days=7)
    p = str(tmp_path / "ins.csv")
    assert svc.export_insights_csv(ins, p) == p
    p2 = str(tmp_path / "ov.csv")
    assert svc.export_overview_csv({"videos": 3}, p2) == p2
