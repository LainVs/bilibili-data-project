"""分析族命令：detect（水军检测）、report（HTML 研报）、iptrace（IP 轨迹）。"""
from __future__ import annotations

import sys


def _force_utf8_replace_stdout() -> None:
    """Windows 控制台编码不稳时，替换非法字符而非崩溃。"""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass


def run_detect(args) -> int:
    from analysis import stats_service as _svc

    _force_utf8_replace_stdout()

    res = _svc.detect_for_video(
        bvid=args.bvid, limit=args.limit, top=args.top,
        repeat_threshold=args.repeat, burst_threshold=args.burst,
    )
    meta = res.get("meta") or {}
    if meta and meta.get("bvid"):
        print(f"【视频】{meta.get('bvid', '')} {(meta.get('title') or '')[:40]}")
    print("【检测】" + _svc.detect_summary(res))
    for r in (res.get("results") or [])[: args.top]:
        hits = "+".join(r.get("hits") or []) or "-"
        uname = str(r.get("uname") or "")
        print(f"  UID={r['uid']:<10} {uname:<16} "
              f"发言{r.get('total'):<4} 复读率{r.get('repeat_rate'):<6} "
              f"爆发{r.get('burst_max'):<3} 属地{str(r.get('ip_top') or '-'):<4}{r.get('ip_ratio', 0):<5} "
              f"评分{r.get('score'):<5} [{r.get('level'):<7}] {hits}")
    if args.export:
        _svc.export_detect_csv(res.get("results") or [], args.export)
        print(f"已导出：{args.export}")
    return 0


def run_report(args) -> int:
    from reporting import ReportSections, generate_report

    sec = ReportSections(
        overview=True,
        trends=not args.no_trends,
        sentiment=not args.no_sentiment,
        terms=not args.no_terms,
        water_army=not args.no_detect,
        top_comments=not args.no_comments,
        interactive_table=not args.no_interactive,
    )
    out = args.output.strip() or f"logs/report_{args.bvid}.html"
    path = generate_report(args.bvid, out, limit=args.limit, sections=sec, auto_sync=args.auto_sync)
    print(f"[OK] 研报已成功生成: {path}")
    return 0


def run_iptrace(args) -> int:
    from analysis.stats_service import get_user_ip_trajectory, get_video_ip_trajectories

    _force_utf8_replace_stdout()

    if args.uid > 0:
        traj = get_user_ip_trajectory(args.uid)
        uname = traj.get("uname") or f"UID:{args.uid}"
        print(f"=== 用户属地迁移轨迹分析: {uname} (UID: {args.uid}) ===")
        print(f"【概况】{traj['summary']}")
        print(f"【指标】总发言: {traj['total_records']} | 有效属地: {traj['valid_records']} | "
              f"涉及属地: {traj['distinct_count']} 个 | 迁移次数: {traj['jump_count']} | "
              f"瞬移次数: {traj['teleport_count']} | 风险评级: [{traj['risk_level']}]")
        if traj.get("transitions"):
            print("\n【迁移跳转时间链】")
            for i, tr in enumerate(traj["transitions"], 1):
                tp_flag = " [!] 疑似瞬移" if tr["is_teleport"] else ""
                print(f"  {i}. {tr['from_ip']} -> {tr['to_ip']}{tp_flag}")
                print(f"     起止: {tr['from_time']} -> {tr['to_time']} (间隔 {tr['interval_hours']:.1f}h)")
                print(f"     距离: {tr['distance_km']:.0f} km | "
                      f"隐含时速: {tr['speed_kmh']:,.0f} km/h | 判定: {tr['flag']}")
        return 0

    bvid = args.bvid.strip()
    trajs = get_video_ip_trajectories(bvid=bvid, limit=args.limit, top=args.top)
    label = f"视频 {bvid}" if bvid else "库内抽样"
    print(f"=== {label} 观众群 IP 属地跳跃与瞬移批量排查 ===")
    print(f"共发现 {len(trajs)} 名存在多地流转/异常跳跃的用户 (按瞬移次数与迁移频度降序):")
    for t in trajs:
        chain = " -> ".join(t.get("distinct_ips", []))
        tp = f" [瞬移 {t['teleport_count']} 次]" if t["teleport_count"] > 0 else ""
        print(f"  UID={t['uid']:<10} {str(t.get('uname') or ''):<16} 流转{t['jump_count']:<2}次{tp} "
              f"最高时速{t['max_speed_kmh']:<8,.0f} km/h [{t['risk_level']:<6}] 轨迹: {chain}")
    return 0
