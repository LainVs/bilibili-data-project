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
    python main.py resolve BV1xx               # 对已入库弹幕执行 mid_hash->UID 反查

命令实现按族拆分在 commands/ 包中，本文件只负责解析参数与分派。
"""
from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B站弹幕/评论采集与存储")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化数据库与表结构")

    p_crawl = sub.add_parser("crawl", help="全量采集")
    p_crawl.add_argument("bvids", nargs="+", help="一个或多个 BVID")
    grp = p_crawl.add_mutually_exclusive_group()
    grp.add_argument("--comments-only", action="store_true", help="仅采评论")
    grp.add_argument("--danmaku-only", action="store_true", help="仅采弹幕")
    p_crawl.add_argument("--bvid-file", help="从文件读取 BV 列表(每行一个)")
    p_crawl.add_argument("--incremental", action="store_true", help="增量：只采新评论并跳过已完成")

    sub.add_parser("increment", help="增量采集所有已采集视频")

    sub.add_parser("serve", help="启动定时增量调度")

    sub.add_parser("version", help="显示版本")

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

    p_export = sub.add_parser("export", help="数据导出 (CSV / JSONL / XLSX / SQLite)")
    p_export.add_argument("--type", choices=["comments", "danmaku", "videos", "users"], default="comments", help="导出数据类型")
    p_export.add_argument("--format", choices=["csv", "jsonl", "xlsx", "sqlite"], default="csv", help="导出格式")
    p_export.add_argument("--bvid", default="", help="限定视频 BV")
    p_export.add_argument("--uid", type=int, default=0, help="限定用户 UID")
    p_export.add_argument("--output", required=True, help="导出文件路径")
    p_export.add_argument("--limit", type=int, default=0, help="上限条数(0=不限)")

    p_detect = sub.add_parser("detect", help="水军/刷屏/控评检测")
    p_detect.add_argument("--bvid", default="", help="限定视频 BV（留空=最新采样兜底）")
    p_detect.add_argument("--limit", type=int, default=5000, help="分析发言上限（默认5000）")
    p_detect.add_argument("--top", type=int, default=50, help="输出可疑人数上限（默认50）")
    p_detect.add_argument("--repeat", type=float, default=0.5, help="复读率阈值（默认0.5）")
    p_detect.add_argument("--burst", type=int, default=8, help="10分钟爆发条数阈值（默认8）")
    p_detect.add_argument("--export", default="", help="导出CSV路径")

    p_report = sub.add_parser("report", help="一键生成独立单文件 HTML 网页研报")
    p_report.add_argument("--bvid", required=True, help="视频 BV 号")
    p_report.add_argument("--output", default="", help="导出 HTML 文件路径（默认 logs/report_<BVID>.html）")
    p_report.add_argument("--limit", type=int, default=0, help="分析条数上限(默认 0 为全量分析)")
    p_report.add_argument("--no-trends", action="store_true", help="不包含走势板块")
    p_report.add_argument("--no-sentiment", action="store_true", help="不包含情感板块")
    p_report.add_argument("--no-terms", action="store_true", help="不包含词频板块")
    p_report.add_argument("--no-detect", action="store_true", help="不包含水军检测板块")
    p_report.add_argument("--no-comments", action="store_true", help="不包含精选神评板块")
    p_report.add_argument("--no-interactive", action="store_true", help="不包含交互检索表格与时间滑块")
    p_report.add_argument("--sync", dest="auto_sync", action="store_true", default=True, help="生成前自动增量补采最新数据与官方指标（默认开启）")
    p_report.add_argument("--no-sync", dest="auto_sync", action="store_false", help="仅基于本地已有数据离线生成，不联网补采")

    p_iptrace = sub.add_parser("iptrace", help="IP属地迁移轨迹与异地跳跃瞬移排查")
    p_iptrace.add_argument("--uid", type=int, default=0, help="分析指定用户 UID")
    p_iptrace.add_argument("--bvid", default="", help="分析指定视频 BV（批量排查观众跨域跳跃）")
    p_iptrace.add_argument("--limit", type=int, default=5000, help="分析发言上限条数（默认5000）")
    p_iptrace.add_argument("--top", type=int, default=30, help="输出异常人数上限（默认30）")

    p_search = sub.add_parser("search", help="跨仓检索（评论/弹幕/视频，支持 AND/OR/引号短语）")
    p_search.add_argument("keyword", help="关键词（空格/逗号=同时包含，OR/|=任一，\"引号\"=精确短语）")
    p_search.add_argument("--type", choices=["comments", "danmaku", "videos", "all"],
                          default="comments", help="检索类型（默认 comments）")
    p_search.add_argument("--start", default="", help="起始时间 YYYY-MM-DD")
    p_search.add_argument("--end", default="", help="结束时间 YYYY-MM-DD")
    p_search.add_argument("--exclude", default="", help="排除词（逗号分隔，防子串误伤）")
    p_search.add_argument("--min-like", dest="min_like", type=int, default=0, help="最低点赞数")
    p_search.add_argument("--limit", type=int, default=100, help="结果上限（默认100，0=不限）")
    p_search.add_argument("--top", type=int, default=20, help="终端打印条数（默认20）")
    p_search.add_argument("--export", default="", help="顺带导出到 .xlsx/.csv")
    return parser.parse_args(argv)


def _dispatch(command: str):
    """命令名 -> run(args)->int 处理器；未知命令返回 None。"""
    from commands import analyze, basic, crawl, export, search, stats, tools

    return {
        "init": basic.run_init,
        "version": basic.run_version,
        "gui": basic.run_gui,
        "crawl": crawl.run_crawl,
        "increment": crawl.run_increment,
        "serve": crawl.run_serve,
        "up": crawl.run_up,
        "post": crawl.run_post,
        "stats": stats.run_stats,
        "resolve": stats.run_resolve,
        "crack": tools.run_crack,
        "doctor": tools.run_doctor,
        "export": export.run_export,
        "search": search.run_search,
        "detect": analyze.run_detect,
        "report": analyze.run_report,
        "iptrace": analyze.run_iptrace,
    }.get(command)


def main(argv: list[str] | None = None) -> int:
    from crawler.logging_setup import setup_logging

    setup_logging()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # 无子命令等价于启动 GUI（保持历史行为）
    handler = _dispatch(args.command or "gui")
    if handler is None:
        logger.error("未知命令: %s", args.command)
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
