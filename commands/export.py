"""数据导出命令：export（CSV / JSONL / XLSX / SQLite）。"""
from __future__ import annotations


def _collect_rows(args):
    """按 --type/--bvid/--uid 取数，返回 (rows, field_mapping)。"""
    import etl
    from storage import dao

    rows: list = []
    mapping = None
    t = args.type
    lim = args.limit if args.limit > 0 else 500000

    if t == "comments":
        mapping = etl.COMMENT_FIELDS
        if args.uid:
            rows = dao.query_comments_by_uid(args.uid, limit=lim)
        elif args.bvid:
            meta = dao.find_video_by_bvid(args.bvid)
            if meta:
                rows = dao.query_comments_by_video(meta["aid"], limit=lim)
        else:
            res = dao.search_comments("的", limit=lim)
            rows = res.get("rows", [])
    elif t == "danmaku":
        mapping = etl.DANMAKU_FIELDS
        if args.uid:
            rows = dao.query_danmaku_by_uid(args.uid, limit=lim)
        elif args.bvid:
            meta = dao.find_video_by_bvid(args.bvid)
            if meta:
                rows = dao.query_danmaku_by_video(meta["aid"], limit=lim)
        else:
            res = dao.search_danmaku("的", limit=lim)
            rows = res.get("rows", [])
    elif t == "videos":
        mapping = etl.VIDEO_FIELDS
        rows = dao.query_recent_videos(limit=lim if lim < 500000 else None)
    elif t == "users":
        mapping = etl.USER_FIELDS
        if args.uid:
            u = dao.query_user(args.uid)
            rows = [u] if u else []
        else:
            top_users = dao.query_up_stats()
            rows = [{"uid": r["mid"], "uname": r.get("uname", ""), "fans": 0, "level": 0,
                     "sex": "", "is_vip": 0, "signature": ""} for r in top_users[:lim]]
    return rows, mapping


def run_export(args) -> int:
    import etl

    rows, mapping = _collect_rows(args)
    t = args.type

    if args.format == "csv":
        count = etl.export_to_csv(rows, args.output, field_mapping=mapping)
        print(f"[OK] 成功导出 {count} 条记录到 {args.output}")
    elif args.format == "jsonl":
        count = etl.export_to_jsonl(rows, args.output)
        print(f"[OK] 成功导出 {count} 条记录到 {args.output}")
    elif args.format in ("sqlite", "db"):
        if args.bvid:
            bundle = etl.export_video_bundle(args.bvid, args.output, format="sqlite", limit=args.limit)
            print(f"[OK] 成功打包视频完整 SQLite 数据库到 {bundle['path']} "
                  f"(总计 {bundle['total_records']} 条记录, 表明细: {bundle['counts']})")
        else:
            counts = etl.export_to_sqlite({t: rows}, args.output)
            print(f"[OK] 成功导出 SQLite 数据库到 {args.output} (明细: {counts})")
    elif args.format in ("xlsx", "excel"):
        if args.bvid:
            bundle = etl.export_video_bundle(args.bvid, args.output, format="xlsx", limit=args.limit)
            print(f"[OK] 成功导出多 Sheet 样式 Excel 到 {bundle['path']} "
                  f"(总计 {bundle['total_records']} 条记录)")
        else:
            count = etl.export_to_styled_xlsx({t: (mapping, rows)}, args.output)
            print(f"[OK] 成功导出样式 Excel 到 {args.output} (共 {count} 条)")
    return 0
