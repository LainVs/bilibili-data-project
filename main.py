"""B站弹幕/评论采集项目入口。

用法:
    python main.py init                       # 初始化数据库表结构
    python main.py full BV1xxxx [BV2xxxx...]  # 全量采集指定视频(弹幕+评论)
    python main.py crawl --comments-only BV1xx # 只采评论
    python main.py crawl --danmaku-only BV1xx  # 只采弹幕
    python main.py crawl --incremental BV1xx   # 增量去重采集
    python main.py increment                   # 对所有已采集视频增量更新
    python main.py serve                       # 定时增量后台运行
    python main.py up <UID>                    # 按 UP 主批量采集其全部视频评论
    python main.py post <链接或ID>              # 采集帖子/动态/专栏评论
    python main.py resolvecache BV1xx          # 对已入库弹幕执行 mid_hash->UID 反查
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B站弹幕/评论采集与存储")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="初始化数据库与表结构")

    p_crawl = sub.add_parser("crawl", help="全量采集")
    p_crawl.add_argument("bvids", nargs="+", help="一个或多个 BVID")
    grp = p_crawl.add_mutually_exclusive_group()
    grp.add_argument("--comments-only", action="store_true", help="仅采评论")
    grp.add_argument("--danmaku-only", action="store_true", help="仅采弹幕")
    p_crawl.add_argument("--bvid-file", help="从文件读取 BV 列表(每行一个)")
    p_crawl.add_argument("--incremental", action="store_true", help="增量：只采新评论并跳过已完成")

    p_incr = sub.add_parser("increment", help="增量采集所有已采集视频")

    p_sched = sub.add_parser("serve", help="启动定时增量调度")

    p_ver = sub.add_parser("version", help="显示版本")

    p_resolve = sub.add_parser("resolve", help="对已入库弹幕执行 mid_hash->UID 反查")
    p_resolve.add_argument("bvids", nargs="*", help="一个或多个 BVID；省略则对全库未解析弹幕执行库内 UID 反查")
    p_resolve.add_argument("--all", action="store_true", help="全库执行库内 UID->mid_hash 建表反查")

    p_up = sub.add_parser("up", help="按 UP 主批量采集其全部视频")
    p_up.add_argument("uid", type=int, help="UP 主 UID")
    grp2 = p_up.add_mutually_exclusive_group()
    grp2.add_argument("--comments-only", action="store_true", help="仅采评论")
    grp2.add_argument("--danmaku-only", action="store_true", help="仅采弹幕")
    p_up.add_argument("--full", action="store_true", help="非增量：全量重采已完成的视频")
    p_up.add_argument("--max-pages", type=int, default=20, help="视频列表最大页数(每页30)")

    p_post = sub.add_parser("post", help="采集帖子/动态/专栏评论")
    p_post.add_argument("targets", nargs="+", help="帖子链接或ID，可多个")
    p_post.add_argument("--incremental", action="store_true", help="增量：只采新评论")

    sub.add_parser("gui", help="启动图形界面（定向采集/UP主/帖子/增量轮询/用户查询）")

    p_stats = sub.add_parser("stats", help="统计：总览/趋势洞察/导出CSV")
    p_stats.add_argument("--overview", action="store_true", help="只打总览10项")
    p_stats.add_argument("--keyword", default="", help="洞察抽样关键词（留空=最新采样）")
    p_stats.add_argument("--days", type=int, default=30, help="趋势近N天（默认30）")
    p_stats.add_argument("--top", type=int, default=20, help="高频词TopN（默认20）")
    p_stats.add_argument("--limit", type=int, default=5000, help="洞察抽样上限（默认5000）")
    p_stats.add_argument("--export", default="", help="导出CSV路径（总览+洞察三段）")
    p_stats.add_argument("--charts", default="", help="渲染趋势图到指定目录")

    p_crack = sub.add_parser("crack", help="mid_hash -> UID 极速逆向破解")
    p_crack.add_argument("hashes", nargs="+", help="一个或多个 8 位十六进制 mid_hash")
    p_crack.add_argument("--all", action="store_true", help="显示所有可能的哈希碰撞候选")

    sub.add_parser("doctor", help="系统全链路健康体检（DB连接/Cookie/磁盘/算法性能）")

    p_export = sub.add_parser("export", help="数据导出 (CSV / JSONL)")
    p_export.add_argument("--type", choices=["comments", "danmaku", "videos", "users"], default="comments", help="导出数据类型")
    p_export.add_argument("--format", choices=["csv", "jsonl"], default="csv", help="导出格式")
    p_export.add_argument("--bvid", default="", help="限定视频 BV")
    p_export.add_argument("--uid", type=int, default=0, help="限定用户 UID")
    p_export.add_argument("--output", required=True, help="导出文件路径")
    p_export.add_argument("--limit", type=int, default=0, help="上限条数(0=不限)")
    return parser.parse_args(argv)


def read_bvid_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]


def main(argv: list[str] | None = None) -> int:
    from crawler.logging_setup import setup_logging

    setup_logging()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.command == "init":
        from storage.db import init_db

        init_db()
        logger.info("数据库初始化完成")
        return 0

    if args.command == "version":
        print("bilibili-data-project 0.1.0")
        return 0

    if args.command == "gui":
        from gui.main_window import run as run_gui

        run_gui()
        return 0

    if args.command == "stats":
        from analysis import stats_service as _svc

        if args.overview and not args.keyword and not args.export and not args.charts:
            stats = _svc.get_overview()
            print(_svc.describe_overview(stats))
            for name, val in _svc.overview_export_rows(stats):
                print(f"{name}: {val:,}")
            return 0
        stats = _svc.get_overview()
        print("【总览】" + _svc.describe_overview(stats))
        rows = _svc.sample_texts_for_insight(limit=args.limit, keyword=args.keyword)
        ins = _svc.compute_insights(rows, top=args.top, days=args.days)
        print("【洞察】" + _svc.insights_summary(ins))
        for t in (ins.get("terms") or [])[: args.top]:
            print(f"  {t['term']}: {t['cnt']}")
        if args.export:
            _svc.export_overview_csv(stats, args.export.replace(".csv", "_overview.csv")
                                     if "_overview" not in args.export and "_insight" not in args.export
                                     else args.export)
            _svc.export_insights_csv(ins, args.export.replace(".csv", "_insights.csv")
                                     if "_overview" not in args.export and "_insight" not in args.export
                                     else args.export)
            print(f"已导出：{args.export}")
        if args.charts:
            from visualization import charts as _charts
            paths = _charts.render_all(ins, args.charts)
            print("图表：" + str(paths))
        return 0

    if args.command == "up":
        from crawler.api.client import ApiClient
        from crawler.tasks.up_crawl import crawl_up_videos
        from config import get_config

        cfg = get_config()
        if not cfg.cookie:
            logger.warning("未配置 Cookie（评论接口需要登录）。请在 config/settings.ini 或环境变量 BILI_COOKIE 中配置")
        client = ApiClient()
        with_comments = not args.danmaku_only
        with_danmaku = not args.comments_only
        result = crawl_up_videos(
            client, args.uid,
            with_comments=with_comments,
            with_danmaku=with_danmaku,
            incremental=not args.full,
            max_pages=args.max_pages,
            workers=cfg.up_workers,
        )
        logger.info("UP采集结果 %s: 视频=%s 新采=%s 评论=%s 弹幕=%s 错误=%s",
                    args.uid, result["videos"], result["fetched"],
                    result["comments"], result["danmaku"], result["errors"])
        return 0

    if args.command == "post":
        from crawler.api.client import ApiClient
        from crawler.tasks.post_crawl import crawl_post_link

        client = ApiClient()
        total = 0
        for target in args.targets:
            try:
                result = crawl_post_link(client, target, incremental=args.incremental)
                total += result.get("comments", 0)
                logger.info("帖子采集 %s 评论 %s 条", target, result.get("comments", 0))
            except Exception as exc:
                logger.error("帖子采集失败 %s: %s", target, exc)
        return 0

    if args.command in ("crawl", "increment", "serve"):
        from crawler.api.client import ApiClient
        from config import get_config

        cfg = get_config()
        if not cfg.cookie:
            logger.warning("未配置 Cookie（评论接口需要登录）。请在 config/settings.ini 或环境变量 BILI_COOKIE 中配置")
        client = ApiClient()

        if args.command == "crawl":
            bvids = list(args.bvids or [])
            if args.bvid_file:
                bvids.extend(read_bvid_file(args.bvid_file))
            if not bvids:
                logger.error("请提供至少一个 BV 或 --bvid-file")
                return 2
            from crawler.tasks.full import crawl_one_video

            with_comments = not args.danmaku_only
            with_danmaku = not args.comments_only
            for bvid in bvids:
                if args.incremental:
                    from storage import dao

                    meta = dao.find_video_by_bvid(bvid)
                    if meta:
                        row = dao.get_progress(meta["aid"]) or {}
                        done = (not with_comments or bool(row.get("comment_done"))) and \
                               (not with_danmaku or bool(row.get("danmaku_done")))
                        if done:
                            logger.info("跳过已完成 %s (增量模式)", bvid)
                            continue
                stats = crawl_one_video(client, bvid, with_comments=with_comments, with_danmaku=with_danmaku,
                                        incremental=args.incremental)
                logger.info("采集结果 %s: 评论=%s 弹幕=%s 错误=%s",
                            bvid, stats["comments"], stats["danmaku"], stats["errors"])
            return 0

        if args.command == "increment":
            from crawler.tasks.increment import incremental_update
            from storage import dao

            aids = dao.list_all_video_aids()
            if not aids:
                logger.info("无已采集视频，跳过增量")
                return 0
            incremental_update(client, aids)
            return 0

        if args.command == "serve":
            from crawler.tasks.scheduler import start_scheduler

            scheduler = start_scheduler(client)
            logger.info("后台增量调度运行中，Ctrl-C 退出")
            try:
                import time

                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                scheduler.shutdown(wait=False)
                return 0

    if args.command == "resolve":
        from crawler.tasks.resolve_uid import resolve_all_from_library, resolve_batch

        if args.all or not args.bvids:
            result = resolve_all_from_library()
            logger.info("库内反查结果: %s", result)
            return 0
        for bvid in args.bvids:
            result = resolve_batch(bvid)
            logger.info("反查结果 %s: %s", bvid, result)
        return 0

    if args.command == "crack":
        from crc32_crack.core import crack_candidates, crack_fast
        for h in args.hashes:
            h = h.strip().lower()
            if args.all:
                cands = crack_candidates(h)
                if cands:
                    print(f"{h} -> {len(cands)} 个候选: {cands} (推荐主候选: {cands[0]})")
                else:
                    print(f"{h} -> 未能破解")
            else:
                uid = crack_fast(h)
                if uid is not None:
                    print(f"{h} -> UID: {uid}")
                else:
                    print(f"{h} -> 未能破解")
        return 0

    if args.command == "doctor":
        from tools.doctor import print_doctor_report
        print_doctor_report()
        return 0

    if args.command == "export":
        from storage import dao
        import etl

        rows = []
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
                rows = [{"uid": r["mid"], "uname": r.get("uname", ""), "fans": 0, "level": 0, "sex": "", "is_vip": 0, "signature": ""} for r in top_users[:lim]]

        if args.format == "csv":
            count = etl.export_to_csv(rows, args.output, field_mapping=mapping)
        else:
            count = etl.export_to_jsonl(rows, args.output)
        print(f"成功导出 {count} 条记录到 {args.output}")
        return 0

    logger.error("未知命令: %s", args.command)
    return 2


if __name__ == "__main__":
    sys.exit(main())