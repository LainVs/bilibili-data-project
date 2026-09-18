"""统计服务层：GUI / CLI 共用的统计口径与缓存。

设计目标：
1. 把“怎么算”（纯内存聚合）与“从哪取”（DAO 查库）解耦——
   GUI 只负责展示，CLI 可脚本化调用，单测无需数据库。
2. 趋势类查询失败只降级为空（返回 []），不抛异常拖死面板。
3. 缓存沿用 storage.dao.save/load_stats_cache（payload 新增 insights 段，
   老缓存无该段时按缺失处理，前向兼容）。
"""
from __future__ import annotations

import csv
import logging
import time
from typing import Any

from analysis.text import (
    daily_series,
    hour_dist,
    ip_dist,
    length_dist,
    like_dist,
    sentiment_dist,
    top_terms,
)

logger = logging.getLogger(__name__)

CACHE_VERSION = 2


def compute_insights(rows: list[dict], *, top: int = 20,
                     days: int = 30) -> dict:
    """对一批评论/弹幕行做内存聚合（纯函数，无 DB）。

    rows 每行需含 content/ctime，可选 like_num/ip_location。
    返回 {"terms","sentiment","daily","hours","length","likes","ips","total"}。
    """
    rows = list(rows or [])
    texts = [str((r or {}).get("content") or "") for r in rows]
    return {
        "total": len(rows),
        "terms": top_terms(texts, top=top),
        "sentiment": sentiment_dist(texts),
        "daily": daily_series(rows, days=days),
        "hours": hour_dist(rows),
        "length": length_dist(texts),
        "likes": like_dist(rows),
        "ips": ip_dist(rows),
    }


def insights_summary(ins: dict) -> str:
    """洞察一句话摘要（面板/CLI 共用展示）。"""
    if not isinstance(ins, dict) or not ins.get("total"):
        return "暂无数据：先采集/搜索一批评论后再看洞察。"
    s = ins.get("sentiment") or {}
    terms = ins.get("terms") or []
    hours = ins.get("hours") or []
    tot = ins.get("total") or 0
    pos = s.get("pos", 0)
    neg = s.get("neg", 0)
    top3 = "、".join(f"{t['term']}×{t['cnt']}" for t in terms[:3]) or "—"
    peak = max(hours, key=lambda x: x.get("cnt", 0)) if hours else {}
    peak_txt = f"{peak.get('hour', '?')}点({peak.get('cnt', 0)}条)" if peak else "—"
    return (f"共 {tot} 条 | 正向 {pos} / 负向 {neg} | "
            f"高频词：{top3} | 最活跃时段：{peak_txt}")


# ---------------- 取数层（查库，失败降级） ----------------

def get_overview(progress_cb=None, stop_event=None,
                 stat_done_cb=None) -> dict:
    """总览 10 项（复用 dao.get_collection_stats，HDD 友好）。"""
    from storage import dao

    return dao.get_collection_stats(progress_cb=progress_cb,
                                    stop_event=stop_event,
                                    stat_done_cb=stat_done_cb)


def get_rankings(**kwargs) -> dict:
    """排行榜（透传 dao.get_rankings，失败返回 {}）。"""
    from storage import dao

    try:
        return dao.get_rankings(**kwargs)
    except getattr(dao, "StatsCancelled", Exception):
        raise
    except Exception as exc:
        logger.warning("排行榜查询失败(服务层降级): %s", exc)
        return {}


def get_daily_comments(days: int = 30) -> list[dict]:
    """近 N 天每日评论量（DAO 新增轻量查询；异常/缺表返回 []）。"""
    try:
        from storage import dao

        fn = getattr(dao, "get_daily_comment_counts", None)
        if callable(fn):
            return fn(days=days) or []
    except Exception as exc:
        logger.warning("每日评论量查询失败: %s", exc)
    return []


def get_hourly_comments() -> list[dict]:
    """24 小时评论分布（DAO 轻量查询；异常返回 []）。"""
    try:
        from storage import dao

        fn = getattr(dao, "get_hourly_comment_counts", None)
        if callable(fn):
            return fn() or []
    except Exception as exc:
        logger.warning("小时分布查询失败: %s", exc)
    return []


def sample_texts_for_insight(limit: int = 5000,
                             keyword: str = "") -> list[dict]:
    """洞察用抽样：优先关键词搜索结果，否则取最新评论。

    保证 GUI“趋势洞察”页在不输入关键词时也有数据可看。
    失败返回 []（面板显示占位，不报错）。
    """
    try:
        from storage import dao

        if keyword.strip():
            res = dao.search_comments(keyword.strip(), limit=limit)
            return res.get("rows", []) if isinstance(res, dict) else []
        # 无关键词：各库取最新拼满 limit
        rows: list[dict] = []
        try:
            conn = dao.get_conn("write")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT rpid, uid, content, like_num, ctime, ip_location,"
                        " video_aid FROM raw_comments "
                        "ORDER BY ctime DESC LIMIT %s", (int(limit),))
                    rows.extend(list(cur.fetchall()))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("抽样查 D 失败: %s", exc)
        if len(rows) < limit:
            try:
                res = dao.search_comments("的", limit=limit - len(rows))
                if isinstance(res, dict):
                    seen = {r.get("rpid") for r in rows}
                    rows.extend(r for r in res.get("rows", [])
                                if r.get("rpid") not in seen)
            except Exception:
                pass
        return rows[:limit]
    except Exception as exc:
        logger.warning("洞察抽样失败: %s", exc)
        return []


# ---------------- 缓存（复用 dao.stats_cache，v2 段） ----------------

def load_cached_insights() -> tuple[dict | None, str]:
    """读缓存中的 insights 段：返回 (insights|None, 更新时间)。"""
    try:
        from storage import dao

        cached = dao.load_stats_cache() or {}
        ins = cached.get("insights")
        if isinstance(ins, dict) and ins.get("total"):
            return ins, str(cached.get("time", ""))
    except Exception as exc:
        logger.warning("洞察缓存读取失败: %s", exc)
    return None, ""


def save_cached_insights(insights: dict) -> None:
    """把 insights 并入现有缓存（保留 stats/ranks，只更新 insights 段）。"""
    try:
        from storage import dao

        prev = dao.load_stats_cache() or {}
        payload = {"stats": prev.get("stats"), "ranks": prev.get("ranks"),
                   "insights": insights,
                   "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "v": CACHE_VERSION}
        # stats 为空时 dao 会拒绝保存坏快照——此时只写本地文件兜底
        if not isinstance(payload["stats"], dict):
            import json
            from pathlib import Path

            p = Path(dao.__file__).with_name("stats_cache.json")
            try:
                local = __import__("json").loads(p.read_text(encoding="utf-8"))
            except Exception:
                local = {}
            local["insights"] = insights
            p.write_text(json.dumps(local, ensure_ascii=False, default=str),
                         encoding="utf-8")
            return
        dao.save_stats_cache(payload)
    except Exception as exc:
        logger.warning("洞察缓存保存失败: %s", exc)


# ---------------- 导出 ----------------

def export_insights_csv(insights: dict, path: str) -> str:
    """导出洞察为 CSV（terms/daily/hours 三段纵排），返回实际路径。"""
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "k", "v"])
        for t in (insights or {}).get("terms", []) or []:
            w.writerow(["term", t.get("term"), t.get("cnt")])
        for d in (insights or {}).get("daily", []) or []:
            w.writerow(["daily", d.get("day"), d.get("cnt")])
        for h in (insights or {}).get("hours", []) or []:
            w.writerow(["hour", h.get("hour"), h.get("cnt")])
        s = (insights or {}).get("sentiment", {}) or {}
        for k in ("pos", "neu", "neg", "total"):
            w.writerow(["sentiment", k, s.get(k, 0)])
    return path


def describe_overview(stats: dict) -> str:
    """总览一句话（CLI 输出用）。"""
    if not isinstance(stats, dict):
        return "总览为空"
    keys = ["videos", "comments", "danmaku", "users", "comment_uids"]
    return " / ".join(f"{k}={stats.get(k, 0):,}" for k in keys)


def overview_export_rows(stats: dict) -> list[tuple]:
    """总览导出行 [(指标, 数值)]。"""
    labels = [("采集视频数", "videos"), ("有评论的视频数", "video_with_comments"),
              ("已采评论条数", "comments"), ("已采弹幕条数", "danmaku"),
              ("用户维度人数", "users"), ("去重评论UID数", "comment_uids"),
              ("已解析UID弹幕", "danmaku_parsed"), ("未解析弹幕", "danmaku_unparsed"),
              ("反查命中hash", "crc_hits"), ("反查缓存总数", "crc_total")]
    return [(name, (stats or {}).get(key, 0)) for name, key in labels]


def export_overview_csv(stats: dict, path: str) -> str:
    """导出总览为 CSV，返回实际路径。"""
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["指标", "数值"])
        w.writerows(overview_export_rows(stats))
    return path
