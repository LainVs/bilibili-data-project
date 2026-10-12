"""归档模块纯函数单测：无需数据库。"""
from storage.archive_sync import _TABLES, _choose_clear_strategy, _fmt_eta


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


def test_choose_clear_strategy():
    # 数量异常（无新增但对不上）：保守逐批删
    assert _choose_clear_strategy(90, 100) == "chunked"
    # 小漂移：TRUNCATE+回插快路径
    assert _choose_clear_strategy(120, 100) == "reinsert"
    assert _choose_clear_strategy(11868059, 11413106) == "reinsert"  # 本次真实漂移45万
    assert _choose_clear_strategy(11413106 + 1_000_000, 11413106) == "reinsert"  # 边界
    # 大漂移：逐批删
    assert _choose_clear_strategy(11413106 + 1_000_001, 11413106) == "chunked"
