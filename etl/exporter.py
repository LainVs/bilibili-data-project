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
