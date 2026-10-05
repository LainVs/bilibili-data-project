"""ETL 导出器：将采集或加工后的数据流式写入 CSV (UTF-8 BOM) 或 JSONL。"""
from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable
from typing import Any


def _ensure_dir(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    return path


def export_to_csv(rows: Iterable[dict[str, Any]], path: str,
                  field_mapping: list[tuple[str, str]] | None = None) -> int:
    """流式导出到 CSV，带 UTF-8 BOM（Excel 中文不乱码）。

    field_mapping: [(内部字段名, CSV表头名), ...]
    若未指定，则以首行所有 key 为表头。
    返回导出记录数。
    """
    _ensure_dir(path)
    count = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer: csv.writer | None = None
        keys: list[str] = []

        for row in rows:
            if not row:
                continue
            if writer is None:
                writer = csv.writer(fh)
                if field_mapping:
                    keys = [k for k, _ in field_mapping]
                    headers = [h for _, h in field_mapping]
                else:
                    keys = list(row.keys())
                    headers = keys
                writer.writerow(headers)

            vals = [str(row.get(k, "") if row.get(k) is not None else "") for k in keys]
            writer.writerow(vals)
            count += 1

    return count


def export_to_jsonl(rows: Iterable[dict[str, Any]], path: str) -> int:
    """流式导出到 JSONL（每行一条独立 JSON，适合大数据管道/训练）。"""
    _ensure_dir(path)
    count = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            if not row:
                continue
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


def export_to_sqlite(
    tables: dict[str, list[dict[str, Any]]],
    path: str,
) -> dict[str, int]:
    """将多表数据打包导出为独立的单文件 SQLite 便携数据库 (.sqlite / .db)。

    无视 MySQL 环境，任何机器使用 Navicat / DBeaver / SQLiteStudio 双击秒开。
    标准支持 meta_video, raw_comments, raw_danmaku, dim_user 及其关键索引。
    返回 {table_name: exported_count}。
    """
    _ensure_dir(path)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass

    import sqlite3
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    counts: dict[str, int] = {}

    ddls = {
        "meta_video": """
            CREATE TABLE IF NOT EXISTS meta_video (
                aid INTEGER PRIMARY KEY,
                bvid TEXT,
                cid INTEGER,
                title TEXT,
                owner_uid INTEGER,
                owner_name TEXT,
                pubdate INTEGER,
                part INTEGER,
                create_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_video_bvid ON meta_video(bvid);
        """,
        "raw_comments": """
            CREATE TABLE IF NOT EXISTS raw_comments (
                rpid INTEGER PRIMARY KEY,
                video_aid INTEGER,
                ref_type INTEGER,
                parent INTEGER,
                uid INTEGER,
                content TEXT,
                like_num INTEGER,
                ctime TEXT,
                ip_location TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_comments_aid ON raw_comments(video_aid);
            CREATE INDEX IF NOT EXISTS idx_comments_uid ON raw_comments(uid);
        """,
        "raw_danmaku": """
            CREATE TABLE IF NOT EXISTS raw_danmaku (
                id INTEGER PRIMARY KEY,
                video_aid INTEGER,
                uid INTEGER,
                mid_hash TEXT,
                content TEXT,
                progress REAL,
                mode INTEGER,
                color INTEGER,
                ctime TEXT,
                is_parsed INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_danmaku_aid ON raw_danmaku(video_aid);
            CREATE INDEX IF NOT EXISTS idx_danmaku_uid ON raw_danmaku(uid);
        """,
        "dim_user": """
            CREATE TABLE IF NOT EXISTS dim_user (
                uid INTEGER PRIMARY KEY,
                uname TEXT,
                level INTEGER,
                sex TEXT,
                is_vip INTEGER,
                signature TEXT,
                fans INTEGER,
                update_time TEXT
            );
        """,
    }

    try:
        for tbl_name, rows in tables.items():
            if not rows:
                counts[tbl_name] = 0
                continue
            if tbl_name in ddls:
                for stmt in ddls[tbl_name].strip().split(";"):
                    if stmt.strip():
                        cur.execute(stmt)
                cur.execute(f"PRAGMA table_info({tbl_name})")
                col_defs = [r[1] for r in cur.fetchall()]
            else:
                first = rows[0]
                col_defs = list(first.keys())
                col_sql = ", ".join(f'"{c}" TEXT' for c in col_defs)
                cur.execute(f'CREATE TABLE IF NOT EXISTS "{tbl_name}" ({col_sql})')

            placeholders = ", ".join(["?"] * len(col_defs))
            cols_quoted = ", ".join(f'"{c}"' for c in col_defs)
            insert_sql = f'INSERT OR REPLACE INTO "{tbl_name}" ({cols_quoted}) VALUES ({placeholders})'

            def _sqlite_val(v):
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False)
                return v

            data_tuples = [tuple(_sqlite_val(r.get(c)) for c in col_defs) for r in rows]
            cur.executemany(insert_sql, data_tuples)
            counts[tbl_name] = len(data_tuples)

        conn.commit()
    finally:
        conn.close()

    return counts


def export_to_styled_xlsx(
    sheets: dict[str, tuple[list[tuple[str, str]] | None, list[dict[str, Any]]]],
    path: str,
) -> int:
    """生成带有 Claude 质感排版、冻结表头与斑马纹的多 Sheet 格式化 Excel 工作簿 (.xlsx)。

    sheets: {sheet_name: (field_mapping, rows)}
    返回总导出记录数。
    """
    _ensure_dir(path)
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    if wb.active:
        wb.remove(wb.active)

    header_fill = PatternFill(start_color="191816", end_color="191816", fill_type="solid")
    header_font = Font(name="Microsoft YaHei UI", size=10, bold=True, color="FFFFFF")
    stripe_fill = PatternFill(start_color="FCFBFA", end_color="FCFBFA", fill_type="solid")
    body_font = Font(name="Microsoft YaHei UI", size=9, color="191816")
    thin_border = Border(
        left=Side(style="thin", color="E7E5DC"),
        right=Side(style="thin", color="E7E5DC"),
        top=Side(style="thin", color="E7E5DC"),
        bottom=Side(style="thin", color="E7E5DC"),
    )

    total_rows = 0
    import re

    for sheet_name, (mapping, rows) in sheets.items():
        clean_title = re.sub(r'[\\/*?:\[\]]', '_', str(sheet_name))[:31].strip() or "Sheet"
        ws = wb.create_sheet(title=clean_title)
        if not rows:
            ws.append(["暂无数据"])
            continue

        if mapping:
            keys = [k for k, _ in mapping]
            headers = [h for _, h in mapping]
        else:
            keys = list(rows[0].keys())
            headers = keys

        ws.append(headers)
        ws.row_dimensions[1].height = 24
        ws.freeze_panes = "A2"

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(vertical="center", horizontal="left")
            cell.border = thin_border

        for row_idx, r in enumerate(rows, start=2):
            vals = [str(r.get(k, "") if r.get(k) is not None else "") for k in keys]
            ws.append(vals)
            ws.row_dimensions[row_idx].height = 20
            is_odd = (row_idx % 2 == 1)
            for col_idx in range(1, len(keys) + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.font = body_font
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")
                if is_odd:
                    cell.fill = stripe_fill
            total_rows += 1

        for col_idx, col_name in enumerate(headers, start=1):
            max_len = len(str(col_name)) * 2
            for r in rows[:100]:
                val_str = str(r.get(keys[col_idx - 1], "") or "")
                max_len = max(max_len, min(len(val_str), 36))
            col_letter = get_column_letter(col_idx)
            ws.column_dimensions[col_letter].width = max(11, min(max_len + 3, 45))

    wb.save(path)
    return total_rows


def export_video_bundle(
    bvid: str,
    output_path: str,
    format: str = "sqlite",
    limit: int = 0,
) -> dict[str, Any]:
    """将指定视频的完整生态数据（元信息、全量评论、全量弹幕、互动用户）一键打包为便携格式。

    format: "sqlite" | "xlsx" | "csv" | "jsonl"
    limit: 0 代表全量无截断导出。
    返回导出统计。
    """
    from storage import dao

    meta = dao.find_video_by_bvid(bvid) or {}
    aid = int(meta.get("aid", 0) or 0)
    if not aid:
        raise ValueError(f"未在数据库中找到视频 {bvid}，请确认已采集。")

    fetch_limit = None if limit <= 0 else limit
    comments = dao.query_comments_by_video(aid, limit=fetch_limit) or []
    danmaku = dao.query_danmaku_by_video(aid, limit=fetch_limit) or []

    def _safe_uid(v: Any) -> int | None:
        try:
            val = int(v)
            return val if val > 0 else None
        except (ValueError, TypeError):
            return None

    uids = list({u for c in comments if (u := _safe_uid(c.get("uid")))} |
                {u for d in danmaku if (u := _safe_uid(d.get("uid")))})
    users = []
    if uids:
        sub_uids = uids[:500]
        try:
            from storage.db import get_conn
            for role in ("write", "read"):
                try:
                    conn = get_conn(role)
                    try:
                        with conn.cursor() as cur:
                            fmt_u = ",".join(["%s"] * len(sub_uids))
                            cur.execute(
                                f"SELECT uid, uname, level, sex, is_vip, signature, fans FROM dim_user WHERE uid IN ({fmt_u})",
                                tuple(sub_uids),
                            )
                            found = list(cur.fetchall())
                            if found:
                                users.extend(found)
                                break
                    finally:
                        conn.close()
                except Exception:
                    pass
        except Exception:
            pass

    fmt = format.lower().strip()
    if fmt in ("sqlite", "db"):
        if not output_path.endswith((".sqlite", ".db")):
            output_path += ".sqlite"
        counts = export_to_sqlite({
            "meta_video": [meta],
            "raw_comments": comments,
            "raw_danmaku": danmaku,
            "dim_user": users,
        }, output_path)
        return {
            "path": os.path.abspath(output_path),
            "format": "sqlite",
            "counts": counts,
            "total_records": sum(counts.values()),
        }
    elif fmt in ("xlsx", "excel"):
        if not output_path.endswith(".xlsx"):
            output_path += ".xlsx"
        total = export_to_styled_xlsx({
            "视频元数据": (VIDEO_FIELDS, [meta]),
            "评论列表": (COMMENT_FIELDS, comments),
            "弹幕列表": (DANMAKU_FIELDS, danmaku),
            "互动用户": (USER_FIELDS, users),
        }, output_path)
        return {
            "path": os.path.abspath(output_path),
            "format": "xlsx",
            "counts": {
                "meta_video": 1,
                "raw_comments": len(comments),
                "raw_danmaku": len(danmaku),
                "dim_user": len(users),
            },
            "total_records": total,
        }
    elif fmt == "jsonl":
        if not output_path.endswith(".jsonl"):
            output_path += ".jsonl"
        tot = export_to_jsonl(comments, output_path)
        return {"path": os.path.abspath(output_path), "format": "jsonl", "total_records": tot}
    else:  # csv
        if not output_path.endswith(".csv"):
            output_path += ".csv"
        tot = export_to_csv(comments, output_path, COMMENT_FIELDS)
        return {"path": os.path.abspath(output_path), "format": "csv", "total_records": tot}


# 预设字段映射表
COMMENT_FIELDS: list[tuple[str, str]] = [
    ("rpid", "评论ID"),
    ("video_aid", "视频AID"),
    ("bvid", "BV号"),
    ("title", "视频标题"),
    ("owner_name", "UP主"),
    ("uid", "用户UID"),
    ("uname", "用户昵称"),
    ("content", "评论内容"),
    ("like_num", "点赞数"),
    ("ip_location", "IP属地"),
    ("ctime", "发表时间"),
]

DANMAKU_FIELDS: list[tuple[str, str]] = [
    ("id", "弹幕ID"),
    ("video_aid", "视频AID"),
    ("bvid", "BV号"),
    ("title", "视频标题"),
    ("progress", "播放进度(秒)"),
    ("content", "弹幕内容"),
    ("uid", "解析UID"),
    ("mid_hash", "发送者哈希"),
    ("uname", "发送者昵称"),
    ("ctime", "发送时间"),
]

VIDEO_FIELDS: list[tuple[str, str]] = [
    ("aid", "AID"),
    ("bvid", "BV号"),
    ("title", "视频标题"),
    ("owner_uid", "UP主UID"),
    ("owner_name", "UP主昵称"),
    ("pubdate", "发布时间戳"),
    ("comment_count", "已采评论数"),
    ("danmaku_count", "已采弹幕数"),
]

USER_FIELDS: list[tuple[str, str]] = [
    ("uid", "UID"),
    ("uname", "昵称"),
    ("level", "等级"),
    ("sex", "性别"),
    ("is_vip", "大会员"),
    ("fans", "粉丝数"),
    ("signature", "个性签名"),
]
