"""跨仓检索命令：search（评论 / 弹幕 / 视频）。

把原本只在 GUI 与 export 内部可用的跨仓检索（D 写缓冲 + E 全部分仓，
密度自适应执行计划 + 新鲜度倒序 + 精确早停）暴露为命令行入口。

用法:
    python main.py search 关键词                       # 默认搜评论
    python main.py search 关键词 --type danmaku        # 搜弹幕
    python main.py search 关键词 --type all            # 评论+弹幕+视频
    python main.py search "a,b" --type comments        # 多词 AND
    python main.py search "a OR b" --start 2026-01-01 --limit 200
    python main.py search 关键词 --export out.xlsx     # 顺带导出
"""
from __future__ import annotations


def _print_rows(rows: list, kind: str, limit: int) -> None:
    shown = rows[:limit]
    for i, r in enumerate(shown, 1):
        if kind == "评论":
            uid = r.get("uid", "")
            uname = (r.get("uname") or "")[:14]
            ctime = str(r.get("ctime") or "")[:19]
            ip = r.get("ip_location") or "-"
            like = r.get("like_num", 0)
            bvid = r.get("bvid") or ""
            content = str(r.get("content") or "").replace("\n", " ")[:60]
            print(f"  {i:>3}. [{ctime}] {ip} {uname}(uid={uid}) 赞{like} {bvid}")
            print(f"       {content}")
        elif kind == "弹幕":
            ctime = str(r.get("ctime") or "")[:19]
            prog = r.get("progress", 0)
            content = str(r.get("content") or "").replace("\n", " ")[:60]
            print(f"  {i:>3}. [{ctime}] {float(prog or 0):>7.1f}s  {content}")
        else:  # 视频
            bvid = r.get("bvid") or ""
            title = str(r.get("title") or "")[:50]
            owner = (r.get("owner_name") or "")[:14]
            print(f"  {i:>3}. {bvid} {title}  @{owner}")


def _summarize(res: dict, kind: str, top: int) -> None:
    rows = res.get("rows") or []
    total = res.get("total")
    tail = ""
    if total is None:
        tail = "  (已截断：命中过多，仅返回前若干条)"
    failed = res.get("failed") or []
    if failed:
        tail += f"  ⚠ {len(failed)} 个仓读取失败"
    print(f"【{kind}】命中 {len(rows)} 条{tail}")
    if rows:
        _print_rows(rows, kind, top)


def run_search(args) -> int:
    import sys

    from storage import dao

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass

    keyword = (args.keyword or "").strip()
    if not keyword:
        print("请提供检索关键词。")
        return 2

    kind = args.type
    top = max(1, int(args.top))
    kw = {"start": args.start, "end": args.end, "exclude": args.exclude,
          "limit": args.limit, "min_like": args.min_like}

    print(f"检索: {keyword!r}  类型={kind}  上限={args.limit}"
          f"{'  起始' + args.start if args.start else ''}"
          f"{'  结束' + args.end if args.end else ''}")

    collected: dict[str, list] = {}
    if kind in ("comments", "all"):
        res = dao.search_comments(keyword, **kw)
        collected["comments"] = res.get("rows") or []
        _summarize(res, "评论", top)
    if kind in ("danmaku", "all"):
        res = dao.search_danmaku(keyword, **kw)
        collected["danmaku"] = res.get("rows") or []
        _summarize(res, "弹幕", top)
    if kind in ("videos", "all"):
        res = dao.search_videos(keyword, start=args.start, end=args.end, limit=args.limit)
        collected["videos"] = res.get("rows") or []
        _summarize(res, "视频", top)

    if args.export:
        _export(collected, kind, args.export)
    return 0


def _export(collected: dict, kind: str, path: str) -> None:
    import etl

    try:
        if path.endswith(".xlsx"):
            sheets = {}
            if collected.get("comments"):
                sheets["评论"] = (etl.COMMENT_FIELDS, collected["comments"])
            if collected.get("danmaku"):
                sheets["弹幕"] = (etl.DANMAKU_FIELDS, collected["danmaku"])
            if collected.get("videos"):
                sheets["视频"] = (etl.VIDEO_FIELDS, collected["videos"])
            if not sheets:
                print("无结果可导出。")
                return
            n = etl.export_to_styled_xlsx(sheets, path)
            print(f"[OK] 已导出 {n} 条到 {path}")
        else:
            rows = collected.get("comments") or collected.get("danmaku") or collected.get("videos") or []
            mapping = etl.COMMENT_FIELDS if collected.get("comments") else None
            n = etl.export_to_csv(rows, path, field_mapping=mapping)
            print(f"[OK] 已导出 {n} 条到 {path}")
    except Exception as exc:
        print(f"导出失败: {exc}")
