"""归档模块纯函数单测：无需数据库。"""
from storage.archive_sync import _TABLES, _fmt_eta


def test_fmt_eta_seconds():
    assert _fmt_eta(0) == "0秒"
    assert _fmt_eta(45) == "45秒"
    assert _fmt_eta(-5) == "0秒"
    assert _fmt_eta("bad") == "--"


def test_fmt_eta_minutes():
    assert _fmt_eta(60) == "1分00秒"
    assert _fmt_eta(90) == "1分30秒"
    assert _fmt_eta(3599) == "59分59秒"


def test_fmt_eta_hours():
    assert _fmt_eta(3600) == "1小时00分"
    assert _fmt_eta(5040) == "1小时24分"
    assert _fmt_eta(26798) == "7小时26分"


def test_clear_tables_order():
    # 清D顺序：小表先行，大表收尾（meta -> dim -> danmaku -> comments）
    names = [t for t, _, _ in _TABLES]
    assert names == ["meta_video", "dim_user", "raw_danmaku", "raw_comments"]
