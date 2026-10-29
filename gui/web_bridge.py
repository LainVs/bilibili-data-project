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


def query_user_history(uid: int, limit: int = 500) -> dict:
    """用户画像 + 发言历史（复用 dao.query_user / by_uid）。"""
    from gui.util import fmt_dt
    from storage import dao

    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "UID 需为纯数字"}

    try:
        user = dao.query_user(uid)
    except Exception:
        user = None
    try:
        comments = dao.query_comments_by_uid(uid, limit=limit)
    except Exception:
        comments = []
    try:
        danmaku = dao.query_danmaku_by_uid(uid, limit=limit)
    except Exception:
        danmaku = []

    profile = None
    if user:
        profile = {
            "uid": user.get("uid", uid),
            "uname": user.get("uname") or "(未命名)",
            "level": user.get("level", 0),
            "vip": "大会员" if user.get("is_vip") else "普通用户",
            "sex": user.get("sex") or "-",
            "signature": (user.get("signature") or "")[:80],
        }

    def _fmt(rows: list, kind: str) -> list:
        out = []
        for r in rows:
            out.append({
                "kind": kind,
                "ctime": fmt_dt(r.get("ctime")),
                "content": str(r.get("content", ""))[:300],
                "ip_location": r.get("ip_location") or "-",
                "like_num": r.get("like_num", 0),
                "bvid": r.get("bvid", ""),
                "title": str(r.get("title") or "")[:40],
                "progress": r.get("progress", 0),
            })
        return out

    return {"ok": True, "uid": uid, "profile": profile,
            "comments": _fmt(comments, "评论"),
            "danmaku": _fmt(danmaku, "弹幕")}


def run_increment_once(progress_cb=None, item_cb=None, stop_flag=None) -> dict:
    """执行一次全库增量补采（与 update_panel._worker_run 完全同一路径）。"""
    from crawler.api.client import ApiClient
    from crawler.tasks.increment import incremental_update
    from storage import dao

    client = ApiClient()
    try:
        aids = dao.list_all_video_aids()
        if not aids:
            return {"ok": True, "skipped": True, "msg": "库中暂无已采集视频，跳过"}
        incremental_update(client, aids, progress_cb=progress_cb,
                           item_cb=item_cb, stop_flag=stop_flag)
        return {"ok": True, "videos": len(aids),
                "msg": f"增量补采完成：处理 {len(aids)} 个视频"}
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_ups_batch(max_pages: int = 100, progress_cb=None, item_cb=None,
                  stop_flag=None) -> dict:
    """批量增量更新历史博主（与 update_panel._worker_ups_batch 同一路径）。"""
    from crawler.api.client import ApiClient
    from crawler.tasks.increment import update_ups_batch
    from storage import dao

    rows = dao.query_up_stats()
    uids = [r["mid"] for r in rows]
    if not uids:
        return {"ok": True, "skipped": True, "msg": "库中暂无已记录的博主，跳过"}

    client = ApiClient()
    try:
        result = update_ups_batch(client, uids, max_pages=max(1, int(max_pages or 100)),
                                  progress_cb=progress_cb, item_cb=item_cb,
                                  stop_flag=stop_flag)
        return {"ok": True, "result": result, "ups": len(uids)}
    finally:
        try:
            client.close()
        except Exception:
            pass


def load_videos_for_update() -> dict:
    """增量轮询页的已采集视频表（含完成状态标记）。"""
    from gui.util import load_videos

    try:
        videos = load_videos()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "rows": []}

    rows = []
    for v in videos:
        c = v.get("comment_count", 0)
        d = v.get("danmaku_count", 0)
        c_done = v.get("comment_done")
        d_done = v.get("danmaku_done")
        flag = ""
        if c_done or d_done:
            flag = "完成" if (c_done or not c) and (d_done or not d) else "未完"
        rows.append({
            "bvid": v.get("bvid", ""),
            "title": str(v.get("title", ""))[:60],
            "comment_count": c,
            "danmaku_count": d,
            "flag": flag,
        })
    return {"ok": True, "rows": rows, "total": len(rows)}


def get_db_size_mb() -> dict:
    """当前库占用（MB），用于广泛采集页的存储护栏显示。"""
    from storage import dao

    try:
        mb = dao.get_db_size() / (1024 * 1024)
        return {"ok": True, "mb": round(mb, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def seed_from_library(count: int = 10) -> dict:
    """从库内精选活跃 UP 作为种子（跨全库 Top 随机抽）。"""
    from storage import dao

    try:
        uids = dao.query_top_up_uids(max(1, int(count)))
        return {"ok": True, "uids": list(uids or [])}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "uids": []}


def seed_from_hot(count: int = 10) -> dict:
    """从 B站热搜榜取 UP 去重后作为种子（联网，与 broad_panel._seed_from_hot 同路径）。"""
    from crawler.api.client import ApiClient
    from crawler.api.popular import PopularFetcher

    want = max(1, min(100, int(count)))
    client = ApiClient()
    try:
        fetcher = PopularFetcher(client)
        items = fetcher.collect(count=max(20, want * 2))
        uids: list[int] = []
        seen: set[int] = set()
        for v in items:
            uid = int(v.get("owner_uid") or 0)
            if uid and uid not in seen:
                seen.add(uid)
                uids.append(uid)
            if len(uids) >= want:
                break
        if not uids:
            return {"ok": False, "error": "热搜未取到 UP", "uids": []}
        return {"ok": True, "uids": uids}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "uids": []}
    finally:
        try:
            client.close()
        except Exception:
            pass
