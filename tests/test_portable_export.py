"""单文件便携 SQLite / 格式化 Excel 导出单测。"""
import os
import sqlite3
import tempfile
import openpyxl

import etl
from etl.exporter import (
    COMMENT_FIELDS,
    DANMAKU_FIELDS,
    USER_FIELDS,
    VIDEO_FIELDS,
    export_to_sqlite,
    export_to_styled_xlsx,
    export_video_bundle,
)


def _sample_bundle_data():
    meta = [{"aid": 100, "bvid": "BV1test", "cid": 200, "title": "测试视频", "owner_name": "测试UP", "pubdate": 1700000000}]
    comments = [
        {"rpid": 1, "video_aid": 100, "uid": 501, "content": "第一条评论", "like_num": 10, "ctime": "2026-09-01", "ip_location": "北京"},
        {"rpid": 2, "video_aid": 100, "uid": 502, "content": "第二条评论", "like_num": 5, "ctime": "2026-09-02", "ip_location": "上海"},
    ]
    danmaku = [
        {"id": 10, "video_aid": 100, "uid": 501, "content": "前方高能", "progress": 12.5, "ctime": "2026-09-01"},
    ]
    users = [
        {"uid": 501, "uname": "观众A", "level": 6, "sex": "男", "is_vip": 1},
        {"uid": 502, "uname": "观众B", "level": 4, "sex": "女", "is_vip": 0},
    ]
    return meta, comments, danmaku, users


def test_export_to_sqlite():
    meta, comments, danmaku, users = _sample_bundle_data()
    tables = {
        "meta_video": meta,
        "raw_comments": comments,
        "raw_danmaku": danmaku,
        "dim_user": users,
    }

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "test_portable.sqlite")
        counts = export_to_sqlite(tables, db_path)

        assert os.path.exists(db_path)
        assert counts["meta_video"] == 1
        assert counts["raw_comments"] == 2
        assert counts["raw_danmaku"] == 1
        assert counts["dim_user"] == 2

        # 验证 SQLite 独立连库查询与索引
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT title, owner_name FROM meta_video WHERE aid = 100")
        r_meta = cur.fetchone()
        assert r_meta == ("测试视频", "测试UP")

        cur.execute("SELECT COUNT(*) FROM raw_comments WHERE video_aid = 100")
        assert cur.fetchone()[0] == 2

        cur.execute("SELECT content FROM raw_danmaku WHERE id = 10")
        assert cur.fetchone()[0] == "前方高能"

        # 验证索引已正确建立
        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        idx_names = [r[0] for r in cur.fetchall()]
        assert "idx_comments_aid" in idx_names
        assert "idx_danmaku_aid" in idx_names

        conn.close()


def test_export_to_styled_xlsx():
    meta, comments, danmaku, users = _sample_bundle_data()
    sheets = {
        "视频元数据": (VIDEO_FIELDS, meta),
        "评论明细": (COMMENT_FIELDS, comments),
        "弹幕列表": (DANMAKU_FIELDS, danmaku),
        "互动用户": (USER_FIELDS, users),
    }

    with tempfile.TemporaryDirectory() as td:
        xlsx_path = os.path.join(td, "test_styled.xlsx")
        total = export_to_styled_xlsx(sheets, xlsx_path)

        assert os.path.exists(xlsx_path)
        assert total == 6  # 1 + 2 + 1 + 2

        # 验证生成的 Excel 结构与样式
        wb = openpyxl.load_workbook(xlsx_path)
        assert "视频元数据" in wb.sheetnames
        assert "评论明细" in wb.sheetnames
        assert "弹幕列表" in wb.sheetnames
        assert "互动用户" in wb.sheetnames

        ws_cmt = wb["评论明细"]
        # 表头行高度与冻结
        assert ws_cmt.freeze_panes == "A2"
        assert ws_cmt.cell(row=1, column=1).value == "评论ID"
        assert ws_cmt.cell(row=2, column=1).value == "1"

        wb.close()
