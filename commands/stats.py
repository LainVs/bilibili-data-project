"""统计与反查命令：stats（总览/洞察/导出/图表）、resolve（mid_hash->UID）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_stats(args) -> int:
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


def run_resolve(args) -> int:
    from crawler.tasks.resolve_uid import resolve_all_from_library, resolve_batch

    if args.all or not args.bvids:
        result = resolve_all_from_library()
        logger.info("库内反查结果: %s", result)
        return 0
    for bvid in args.bvids:
        result = resolve_batch(bvid)
        logger.info("反查结果 %s: %s", bvid, result)
    return 0
