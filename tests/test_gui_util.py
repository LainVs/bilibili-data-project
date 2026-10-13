"""GUI 工具函数单元测试（不启动 Tk）。

重点覆盖 BV 号提取与短链识别——这是「开始采集」入口的第一道解析，
曾因在主线程同步 requests.head 解析短链导致界面冻结最多 5 秒。
"""
from __future__ import annotations

from gui.util import extract_bvid, find_short_link

# ---------- extract_bvid（离线，不联网） ----------

def test_extract_bvid_bare():
    assert extract_bvid("BV18q4y1V7pM") == "BV18q4y1V7pM"
    assert extract_bvid("  BV18q4y1V7pM  ") == "BV18q4y1V7pM"


def test_extract_bvid_from_full_url():
    assert extract_bvid("https://www.bilibili.com/video/BV18q4y1V7pM?spm_id_from=333") == "BV18q4y1V7pM"


def test_extract_bvid_embedded_in_chinese_text():
    # 前后粘连汉字/标点不应影响边界判定
    assert extract_bvid("看这个视频BV18q4y1V7pM超好笑") == "BV18q4y1V7pM"
    assert extract_bvid("【转载】BV18q4y1V7pM，推荐") == "BV18q4y1V7pM"


def test_extract_bvid_case_insensitive_prefix():
    # 前缀大小写不敏感（正则 IGNORECASE），统一规范为 "BV" 前缀；
    # 号段本体保持原样（B站 BV 号本身大小写敏感，不做改写）。
    assert extract_bvid("bv18q4y1v7pm") == "BV18q4y1v7pm"


def test_extract_bvid_empty_and_none():
    assert extract_bvid("") == ""
    assert extract_bvid("   ") == ""
    assert extract_bvid("没有 BV 号的一段话") == ""


def test_extract_bvid_short_link_is_empty_offline():
    """短链需联网解析，离线提取应返回空（由上层转后台线程处理）。"""
    assert extract_bvid("https://b23.tv/abc123") == ""


# ---------- find_short_link ----------

def test_find_short_link_detects_b23():
    assert find_short_link("https://b23.tv/abc123") == "https://b23.tv/abc123"
    assert find_short_link("分享 https://b23.tv/XyZ789 给你") == "https://b23.tv/XyZ789"


def test_find_short_link_detects_bili2233():
    assert find_short_link("https://bili2233.cn/abc") == "https://bili2233.cn/abc"


def test_find_short_link_absent():
    assert find_short_link("BV18q4y1V7pM") == ""
    assert find_short_link("https://www.bilibili.com/video/BV18q4y1V7pM") == ""
    assert find_short_link("") == ""


# ---------- friendly_error（异常 -> 可读中文） ----------

def test_friendly_error_db_down():
    from gui.util import friendly_error

    msg = friendly_error(Exception("(2003, \"Can't connect to MySQL server on '127.0.0.1' ([WinError 10061])\")"),
                         "启动写库 D")
    assert "启动写库 D失败" in msg
    assert "无法连接数据库服务" in msg


def test_friendly_error_access_denied():
    from gui.util import friendly_error

    msg = friendly_error(Exception("(1045, \"Access denied for user 'root'\")"), "连接")
    assert "账号或密码不正确" in msg


def test_friendly_error_unknown_database():
    from gui.util import friendly_error

    msg = friendly_error(Exception("(1049, \"Unknown database 'foo'\")"), "连接")
    assert "数据库不存在" in msg


def test_friendly_error_timeout():
    from gui.util import friendly_error

    msg = friendly_error(Exception("Connection timed out"), "导出")
    assert "超时" in msg


def test_friendly_error_fallback_truncates_and_hints_log():
    from gui.util import friendly_error

    msg = friendly_error(Exception("x" * 500), "操作")
    assert "操作失败" in msg
    assert "logs/" in msg
    assert len(msg) < 400  # 已截断，不刷屏


def test_friendly_error_no_action():
    from gui.util import friendly_error

    msg = friendly_error(Exception("boom"))
    assert msg.startswith("操作失败")
