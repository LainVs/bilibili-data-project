"""WebView 前端 ↔ Python 后端桥：复用 Tk 版同一套业务逻辑。

所有函数只做「调后端 + 整形成 JSON 安全 dict」，不含业务判断，
保证 Tk 引擎与 Web 引擎行为永远一致（单一事实来源在 storage/dao）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def check_databases() -> dict:
    """双库 D/E 状态检测（与 gui.connection_panel 同一套判定）。"""
    from config import get_config
    from gui.connection_panel import _test_conn

    cfg = get_config()
    w = getattr(cfg, "mysql_write", cfg.mysql)
    r = getattr(cfg, "mysql_archive", cfg.mysql)

    ok_d, msg_d = _test_conn(w)
    ok_e, msg_e = _test_conn(r)
    return {
        "ok": True,
        "d": {"addr": f"{w['host']}:{w['port']}/{w['database']}",
              "online": ok_d, "msg": msg_d},
        "e": {"addr": f"{r['host']}:{r['port']}/{r['database']}",
              "online": ok_e, "msg": msg_e},
    }


def read_overview() -> dict:
    """总览关键数字（优先读 stats_cache 单行 JSON，秒回不触发聚合）。"""
    from storage import dao

    try:
        cached = dao.load_stats_cache()  # 单行 JSON，无则 None
    except Exception:
        cached = None
    if isinstance(cached, dict):
        inner = cached.get("stats")
        if isinstance(inner, dict) and inner:
            return {"ok": True, "stats": inner, "cached": True}
    return {"ok": True, "stats": None, "cached": False}


def check_archive_resume() -> dict:
    """归档断点检测（与 connection_panel._check_resume 同逻辑）。"""
    try:
        from storage.archive_sync import _load_progress

        prog = _load_progress()
        if prog and prog.get("shard"):
            offs = prog.get("offsets", {})
            total = sum(offs.values()) if offs else 0
            return {"ok": True, "has_resume": True,
                    "shard": prog["shard"], "rows": total}
    except Exception:
        pass
    return {"ok": True, "has_resume": False}


def run_search(keyword: str, kind: str = "comments", limit: int = 100,
               start: str = "", end: str = "", exclude: str = "",
               min_like: int = 0) -> dict:
    """跨仓检索（后台线程中执行，返回一整页结果）。

    与 Tk 版 search_panel 同一数据源（dao.search_*），前端只负责渲染。
    """
    from storage import dao

    keyword = (keyword or "").strip()
    if not keyword:
        return {"ok": True, "rows": [], "total": 0}

    stop = __import__("threading").Event()

    def _cb(_label: str, _rows: list) -> None:
        pass  # 流式回调在 Web 版折叠为一次性返回（页面本身秒开，无需分批画）

    if kind == "danmaku":
        res = dao.search_danmaku(keyword, start=start, end=end,
                                 exclude=exclude, limit=limit, stop_event=stop)
    elif kind == "videos":
        res = dao.search_videos(keyword, start=start, end=end, limit=limit)
    else:
        res = dao.search_comments(keyword, start=start, end=end,
                                  exclude=exclude, limit=limit,
                                  progress_cb=_cb, min_like=min_like, stop_event=stop)

    rows = res.get("rows") or []
    # JSON 安全化：datetime/Decimal 等统一转字符串
    import datetime as _dt
    import decimal as _dec

    def _safe(v):
        if isinstance(v, (_dt.datetime, _dt.date)):
            return v.strftime("%Y-%m-%d %H:%M")
        if isinstance(v, _dec.Decimal):
            return float(v)
        return v

    clean = [{k: _safe(v) for k, v in r.items()} for r in rows]
    return {"ok": True, "rows": clean,
            "total": res.get("total", len(clean)),
            "failed": res.get("failed") or []}


def generate_report_task(bvid: str, out_name: str = "", limit: int = 0,
                         sections: dict | None = None,
                         auto_sync: bool = True,
                         progress_cb=None) -> dict:
    """生成单文件离线 HTML 研报（与 report_panel 同一引擎）。"""
    import os

    from reporting import ReportSections, generate_report

    bvid = (bvid or "").strip()
    if not bvid:
        return {"ok": False, "error": "请输入要生成研报的视频 BV 号"}

    try:
        limit = max(0, int(limit or 0))
    except (TypeError, ValueError):
        limit = 0

    out_name = (out_name or "").strip() or f"report_{bvid}.html"
    if not out_name.endswith(".html"):
        out_name += ".html"
    if not os.path.isabs(out_name):
        out_name = os.path.abspath(os.path.join("logs", out_name))

    s = sections or {}
    sec = ReportSections(
        overview=bool(s.get("overview", True)),
        trends=bool(s.get("trends", True)),
        sentiment=bool(s.get("sentiment", True)),
        terms=bool(s.get("terms", True)),
        water_army=bool(s.get("water_army", True)),
        top_comments=bool(s.get("top_comments", True)),
        interactive_table=bool(s.get("interactive", True)),
    )

    if progress_cb:
        try:
            progress_cb(f"正在处理 {bvid} ...")
        except Exception:
            pass

    path = generate_report(bvid, out_name, limit=limit, sections=sec,
                           auto_sync=auto_sync)
    size_kb = os.path.getsize(path) / 1024 if os.path.exists(path) else 0
    return {"ok": True, "path": path, "size_kb": round(size_kb, 1)}


def export_bundle_task(bvid: str, path: str, fmt: str = "sqlite") -> dict:
    """打包视频全量数据为 SQLite / XLSX（与 report_panel 同一引擎）。"""
    import etl

    bvid = (bvid or "").strip()
    if not bvid:
        return {"ok": False, "error": "请输入要打包导出的视频 BV 号"}
    res = etl.export_video_bundle(bvid, path, format=fmt, limit=0)
    return {"ok": True, "path": res["path"],
            "total_records": res.get("total_records", 0)}


def load_video_options(limit: int = 500) -> dict:
    """已采集视频下拉选项（复用 gui.util.load_videos 与 video_summary）。"""
    from gui.util import load_videos, video_summary

    try:
        videos = load_videos()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "items": []}

    items = []
    for v in videos[:limit]:
        items.append({
            "bvid": v.get("bvid", ""),
            "label": video_summary(v),
            "comment_count": v.get("comment_count", 0),
            "danmaku_count": v.get("danmaku_count", 0),
        })
    return {"ok": True, "items": items, "total": len(videos)}


def load_up_stats(uid_filter: str = "") -> dict:
    """UP 主采集统计表（复用 dao.query_up_stats）。"""
    from storage import dao

    try:
        rows = dao.query_up_stats()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "rows": []}

    uid_filter = (uid_filter or "").strip()
    out = []
    for r in rows:
        mid = str(r.get("mid", ""))
        if uid_filter and uid_filter not in mid and uid_filter not in str(r.get("uname") or ""):
            continue
        out.append({
            "uid": f"{mid}({r.get('uname') or ''})" if r.get("uname") else mid,
            "total": r.get("video_total", 0),
            "fetched": f"{r.get('video_fetched', 0)}/{r.get('video_total', 0)}",
            "comment_total": r.get("comment_total", 0),
        })
    return {"ok": True, "rows": out}


def load_stored_content(bvid: str, limit: int = 200) -> dict:
    """查看库内已存的该视频评论/弹幕（复用 dao 查询）。"""
    from storage import dao

    bvid = (bvid or "").strip()
    if not bvid:
        return {"ok": False, "error": "请输入 BV 号"}

    def _fmt(rows: list) -> list:
        out = []
        for r in rows:
            item = {}
            for k, v in r.items():
                if hasattr(v, "strftime"):
                    item[k] = v.strftime("%Y-%m-%d %H:%M")
                elif isinstance(v, (bytes, bytearray)):
                    item[k] = v.decode("utf-8", "ignore")
                else:
                    item[k] = v
            out.append(item)
        return out

    try:
        meta = dao.find_video_by_bvid(bvid)
        aid = int((meta or {}).get("aid") or 0)
    except Exception:
        aid = 0
    if not aid:
        return {"ok": False, "error": f"库内未找到视频 {bvid}，请先采集"}

    try:
        comments = dao.query_comments_by_video(aid, limit=limit)
    except Exception:
        comments = []
    try:
        danmaku = dao.query_danmaku_by_video(aid, limit=limit)
    except Exception:
        danmaku = []
    return {"ok": True, "comments": _fmt(comments), "danmaku": _fmt(danmaku),
            "title": (meta or {}).get("title", "")}


def extract_target_bvid(text: str) -> dict:
    """离线解析输入目标：BV 号或 b23.tv 短链（短链需前端二次调用解析）。"""
    from gui.util import extract_bvid, find_short_link

    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "请输入目标"}
    bvid = extract_bvid(text)
    if bvid:
        return {"ok": True, "bvid": bvid, "kind": "bvid"}
    link = find_short_link(text)
    if link:
        return {"ok": True, "kind": "shortlink", "link": link}
    return {"ok": False, "error": "无法识别 BV 号（示例：BV1xx4y1z7Ab）"}


def resolve_shortlink(url: str) -> dict:
    """联网解析 b23.tv 短链（可能耗时数秒，需在前端 await 中调用）。"""
    from gui.util import resolve_short_link

    bvid = resolve_short_link(url)
    if bvid:
        return {"ok": True, "bvid": bvid}
    return {"ok": False, "error": "短链解析失败，请改用完整 BV 号"}
