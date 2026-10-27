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
