"""可视化网页研报引擎：零外部依赖、100% 离线可用单文件 HTML 报告生成。"""
from __future__ import annotations

import html
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from analysis.detect import detect_water_army
from analysis.text import daily_series, hour_dist, sentiment_dist, top_terms
from etl.aggregator import aggregate_ip_distribution
from storage import dao

logger = logging.getLogger(__name__)


@dataclass
class ReportSections:
    """研报可选板块配置。"""
    overview: bool = True       # 核心资产与基础指标
    trends: bool = True         # 互动走势与活跃时段 (含交互式时间滑块)
    sentiment: bool = True      # 舆论情感倾向分布
    terms: bool = True          # 词频透视与核心热词
    water_army: bool = True     # 水军与控评可疑名单
    top_comments: bool = True   # 精选高赞神评展示
    interactive_table: bool = True  # 内嵌全功能交互式可检索/排序评论表格与时间滑块联动


@dataclass
class ReportData:
    """研报数据集。"""
    meta: dict[str, Any]
    comments: list[dict[str, Any]]
    danmaku: list[dict[str, Any]]
    stats: dict[str, Any] = field(default_factory=dict)
    daily: list[dict[str, Any]] = field(default_factory=list)
    hours: list[dict[str, Any]] = field(default_factory=list)
    sentiment: dict[str, Any] = field(default_factory=dict)
    terms: list[dict[str, Any]] = field(default_factory=list)
    ips: list[dict[str, Any]] = field(default_factory=list)
    suspects: list[dict[str, Any]] = field(default_factory=list)
    top_liked: list[dict[str, Any]] = field(default_factory=list)
    interactive_comments: list[dict[str, Any]] = field(default_factory=list)


def collect_report_data(
    bvid: str,
    limit: int = 0,
    sections: ReportSections | None = None,
    auto_sync: bool = False,
) -> ReportData:
    """从数据库采集并加工研报所需全部数据（按勾选版块选择性计算）。

    auto_sync: 若为 True，生成前自动联网增量同步该视频官方最新指标与新评论/弹幕。
    limit: 深度分析样本上限。若传 0 则为 100% 全量分析。
    """
    sections = sections or ReportSections()

    # 0. 可选前置：自动增量补采最新数据
    meta = dao.find_video_by_bvid(bvid) or {}
    if auto_sync and bvid.strip():
        try:
            from crawler.api.client import ApiClient
            from crawler.tasks.full import crawl_one_video, fetch_video_meta
            client = ApiClient()
            try:
                latest_meta = fetch_video_meta(client, bvid.strip())
                if latest_meta and latest_meta.get("aid"):
                    dao.upsert_video([latest_meta])
                    meta = latest_meta
                crawl_one_video(client, bvid.strip(), incremental=True, with_comments=True, with_danmaku=True)
            finally:
                client.close()
        except Exception as exc:
            logger.warning("研报前自动增量补采跳过/异常: %s", exc)

    if not meta:
        meta = dao.find_video_by_bvid(bvid) or {}
    aid = int(meta.get("aid", 0) or 0)

    comments: list[dict[str, Any]] = []
    danmaku: list[dict[str, Any]] = []
    fetch_limit = 0 if limit <= 0 else max(100, int(limit))

    if aid:
        try:
            comments = dao.query_comments_by_video(aid, limit=fetch_limit) or []
        except Exception as exc:
            logger.warning("研报获取评论失败: %s", exc)
        try:
            danmaku = dao.query_danmaku_by_video(aid, limit=fetch_limit) or []
        except Exception as exc:
            logger.warning("研报获取弹幕失败: %s", exc)
    else:
        # 若未按 BV 找到，尝试按全库采样兜底
        try:
            from analysis.stats_service import sample_texts_for_insight
            comments = sample_texts_for_insight(limit=min(fetch_limit, 5000))
        except Exception:
            pass

    data = ReportData(meta=meta, comments=comments, danmaku=danmaku)

    # 1. 基础汇总指标（区分真实全库总数、官方实时总数、本次分析采样量）
    db_counts = dao.count_video_comments_and_danmaku(aid) if aid else {}
    db_total_comments = db_counts.get("comments", 0) or len(comments)
    db_total_danmaku = db_counts.get("danmaku", 0) or len(danmaku)

    total_likes = sum(int(c.get("like_num", 0) or 0) for c in comments)
    unique_users = len({c.get("uid") for c in comments if c.get("uid")} |
                       {d.get("uid") for d in danmaku if d.get("uid") and d.get("uid") > 0})

    data.stats = {
        "bvid": bvid,
        "title": meta.get("title") or bvid,
        "aid": aid,
        "owner": meta.get("owner_name") or "未知UP主",
        "pubdate": datetime.fromtimestamp(meta.get("pubdate", 0)).strftime("%Y-%m-%d %H:%M") if meta.get("pubdate") else "未知",
        "db_total_comments": db_total_comments,
        "db_total_danmaku": db_total_danmaku,
        "stat_reply": int(meta.get("stat_reply", 0) or 0),
        "stat_danmaku": int(meta.get("stat_danmaku", 0) or 0),
        "stat_view": int(meta.get("stat_view", 0) or 0),
        "stat_like": int(meta.get("stat_like", 0) or 0),
        "sample_comments_analyzed": len(comments),
        "sample_danmaku_analyzed": len(danmaku),
        "total_likes": total_likes,
        "unique_users": unique_users,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    texts = [str(c.get("content") or "") for c in comments]

    # 2. 传播走势与时段分布
    if sections.trends:
        try:
            data.daily = daily_series(comments, days=30)
            data.hours = hour_dist(comments)
        except Exception as exc:
            logger.warning("走势计算异常: %s", exc)

    # 3. 情感倾向
    if sections.sentiment:
        try:
            data.sentiment = sentiment_dist(texts)
        except Exception as exc:
            logger.warning("情感计算异常: %s", exc)

    # 4. 词频与热词
    if sections.terms:
        try:
            data.terms = top_terms(texts, top=25)
            data.ips = aggregate_ip_distribution(comments, top_n=10)
        except Exception as exc:
            logger.warning("词频计算异常: %s", exc)

    # 5. 水军与刷屏检测
    if sections.water_army:
        try:
            merged_rows = list(comments) + list(danmaku)
            data.suspects = detect_water_army(
                merged_rows, min_texts=3, repeat_threshold=0.5, burst_threshold=6, top=30
            )
            # 补齐部分昵称
            for s in data.suspects[:15]:
                if not s.get("uname"):
                    u = dao.query_user(s["uid"])
                    if u and u.get("uname"):
                        s["uname"] = u["uname"]
        except Exception as exc:
            logger.warning("水军检测异常: %s", exc)

    # 6. 精选高赞神评
    if sections.top_comments:
        try:
            sorted_by_like = sorted(
                comments, key=lambda c: int(c.get("like_num", 0) or 0), reverse=True
            )
            data.top_liked = sorted_by_like[:15]
        except Exception as exc:
            logger.warning("神评筛选异常: %s", exc)

    # 7. 交互式数据检索表格样本准备（抽取高赞与最新代表样本供前端即时过滤下钻）
    if sections.interactive_table:
        try:
            from analysis.text import sentiment_of
            sorted_likes = sorted(
                comments, key=lambda c: int(c.get("like_num", 0) or 0), reverse=True
            )[:300]
            sorted_recent = sorted(
                comments, key=lambda c: str(c.get("ctime", "") or ""), reverse=True
            )[:200]
            merged_dict = {}
            for c in sorted_likes + sorted_recent:
                rpid = c.get("rpid")
                if rpid and rpid not in merged_dict:
                    merged_dict[rpid] = c
            sample_src = list(merged_dict.values())
            if not sample_src and comments:
                sample_src = comments[:500]

            interactive_list = []
            for c in sample_src:
                txt = str(c.get("content") or "")
                s_val = sentiment_of(txt)
                s_label = "pos" if s_val > 0 else ("neg" if s_val < 0 else "neu")
                uname = str(c.get("uname") or c.get("member", {}).get("uname", "") or f"UID:{c.get('uid')}")
                interactive_list.append({
                    "rpid": int(c.get("rpid", 0) or 0),
                    "uid": int(c.get("uid", 0) or 0),
                    "uname": uname,
                    "ctime": str(c.get("ctime") or "")[:19],
                    "content": txt,
                    "like_num": int(c.get("like_num", 0) or 0),
                    "ip": str(c.get("ip_location") or "-"),
                    "sentiment": s_label,
                })
            data.interactive_comments = interactive_list
        except Exception as exc:
            logger.warning("交互表格数据准备异常: %s", exc)

    return data


# ── SVG 图表生成器（零依赖，纯矢量，嵌入式渲染） ──

def render_svg_line_chart(series: list[dict], width: int = 760, height: int = 240) -> str:
    """生成每日趋势折线图 SVG。"""
    if not series or len(series) < 2:
        return '<div style="color:#8c877d;padding:40px;text-align:center;">暂无足够跨度数据</div>'

    padding_x, padding_y = 50, 30
    w = width - padding_x * 2
    h = height - padding_y * 2

    counts = [int(s.get("cnt", 0) or 0) for s in series]
    max_c = max(counts) if max(counts) > 0 else 1
    n = len(series)

    points = []
    for i, c in enumerate(counts):
        x = padding_x + (i / (n - 1)) * w
        y = height - padding_y - (c / max_c) * h
        points.append((x, y))

    poly_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area_pts = f"{padding_x},{height - padding_y} {poly_pts} {width - padding_x},{height - padding_y}"

    svg = [
        f'<svg width="100%" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="overflow:visible;">',
        '<defs>',
        '<linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0%" stop-color="#da7756" stop-opacity="0.25"/>',
        '<stop offset="100%" stop-color="#da7756" stop-opacity="0.0"/>',
        '</linearGradient>',
        '</defs>',
        # 网格基准线
        f'<line x1="{padding_x}" y1="{height - padding_y}" x2="{width - padding_x}" y2="{height - padding_y}" stroke="#e7e5dc" stroke-width="1"/>',
        f'<line x1="{padding_x}" y1="{padding_y}" x2="{width - padding_x}" y2="{padding_y}" stroke="#f0eee6" stroke-dasharray="4" stroke-width="1"/>',
        f'<text x="{padding_x - 8}" y="{padding_y + 4}" fill="#8c877d" font-size="11" text-anchor="end">{max_c}</text>',
        f'<text x="{padding_x - 8}" y="{height - padding_y + 4}" fill="#8c877d" font-size="11" text-anchor="end">0</text>',
        # 渐变填充阴影与折线
        f'<polygon points="{area_pts}" fill="url(#lineGrad)"/>',
        f'<polyline points="{poly_pts}" fill="none" stroke="#da7756" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>',
    ]

    # 数据点与时间轴标签
    step = max(1, n // 7)
    for i, (x, y) in enumerate(points):
        if i % step == 0 or i == n - 1:
            day_str = series[i].get("day", "")[-5:]
            svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#ffffff" stroke="#da7756" stroke-width="2"/>')
            svg.append(f'<text x="{x:.1f}" y="{height - 10}" fill="#8c877d" font-size="11" text-anchor="middle">{day_str}</text>')

    svg.append('</svg>')
    return "".join(svg)


def render_svg_hours_bar(hours: list[dict], width: int = 760, height: int = 200) -> str:
    """生成 24 小时活跃柱状图 SVG。"""
    if not hours:
        return '<div style="color:#8c877d;padding:40px;text-align:center;">暂无时段数据</div>'

    padding_x, padding_y = 40, 25
    w = width - padding_x * 2
    h = height - padding_y * 2
    max_c = max((h_item.get("cnt", 0) for h_item in hours), default=1) or 1
    bar_w = w / 24

    svg = [
        f'<svg width="100%" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">',
        f'<line x1="{padding_x}" y1="{height - padding_y}" x2="{width - padding_x}" y2="{height - padding_y}" stroke="#e7e5dc" stroke-width="1"/>',
    ]

    for i, item in enumerate(hours):
        cnt = item.get("cnt", 0)
        bar_h = (cnt / max_c) * h if max_c > 0 else 0
        x = padding_x + i * bar_w + 3
        y = height - padding_y - bar_h
        hour_label = f"{i:02d}"

        # 峰值高亮
        is_peak = (cnt == max_c and cnt > 0)
        fill = "#da7756" if is_peak else "#f1d4c7"

        svg.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(2.0, bar_w - 6):.1f}" height="{bar_h:.1f}" rx="2" fill="{fill}">')
        svg.append(f'<title>{hour_label}点: {cnt} 条</title>')
        svg.append('</rect>')

        if i % 3 == 0:
            svg.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" fill="#8c877d" font-size="10" text-anchor="middle">{hour_label}</text>')

    svg.append('</svg>')
    return "".join(svg)


def render_svg_sentiment_donut(sent: dict, size: int = 180) -> str:
    """生成情感环形图 SVG。"""
    pos = sent.get("pos", 0)
    neu = sent.get("neu", 0)
    neg = sent.get("neg", 0)
    total = pos + neu + neg

    if total == 0:
        return '<div style="color:#8c877d;padding:20px;text-align:center;">暂无情感样本</div>'

    p_pos = pos / total
    p_neu = neu / total
    p_neg = neg / total

    r = 65
    c = 2 * 3.14159265 * r
    center = size / 2

    # 计算 stroke-dasharray 偏移
    len_pos = c * p_pos
    len_neu = c * p_neu
    len_neg = c * p_neg

    off_pos = 0.0
    off_neu = -len_pos
    off_neg = -(len_pos + len_neu)

    return f"""
    <div style="display:flex;align-items:center;justify-content:center;gap:30px;flex-wrap:wrap;">
      <svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" style="transform:rotate(-90deg);">
        <circle cx="{center}" cy="{center}" r="{r}" fill="none" stroke="#f3f2ee" stroke-width="22"/>
        <circle cx="{center}" cy="{center}" r="{r}" fill="none" stroke="#22c55e" stroke-width="22" stroke-dasharray="{len_pos:.1f} {c:.1f}" stroke-dashoffset="{off_pos:.1f}"/>
        <circle cx="{center}" cy="{center}" r="{r}" fill="none" stroke="#94a3b8" stroke-width="22" stroke-dasharray="{len_neu:.1f} {c:.1f}" stroke-dashoffset="{off_neu:.1f}"/>
        <circle cx="{center}" cy="{center}" r="{r}" fill="none" stroke="#ef4444" stroke-width="22" stroke-dasharray="{len_neg:.1f} {c:.1f}" stroke-dashoffset="{off_neg:.1f}"/>
      </svg>
      <div style="display:flex;flex-direction:column;gap:10px;">
        <div style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:#22c55e;"></span>
          <span style="color:#615d56;">正面点赞认同:</span> <b>{pos:,} ({p_pos * 100:.1f}%)</b>
        </div>
        <div style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:#94a3b8;"></span>
          <span style="color:#615d56;">中性陈述探讨:</span> <b>{neu:,} ({p_neu * 100:.1f}%)</b>
        </div>
        <div style="display:flex;align-items:center;gap:8px;font-size:13px;">
          <span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:#ef4444;"></span>
          <span style="color:#615d56;">负面批评争议:</span> <b>{neg:,} ({p_neg * 100:.1f}%)</b>
        </div>
      </div>
    </div>
    """


# ── 主 HTML 模板构建 ──

CLIENT_INTERACTIVE_JS = """
(function() {
  const rawDataEl = document.getElementById("report-raw-data");
  if (!rawDataEl) return;
  let rawData = {};
  try {
    rawData = JSON.parse(rawDataEl.textContent || "{}");
  } catch (e) {
    console.error("Failed to parse report data", e);
    return;
  }

  const allDaily = rawData.daily || [];
  const allComments = rawData.comments || [];

  let minDayIdx = 0;
  let maxDayIdx = allDaily.length > 0 ? allDaily.length - 1 : 0;
  let cmtPage = 1;
  const pageSize = 15;
  let filteredComments = [...allComments];

  // 1. 初始化 IP 属地下拉筛选框
  const ipSelect = document.getElementById("cmt-filter-ip");
  if (ipSelect) {
    const rawIps = allComments.map(c => c.ip).filter(ip => ip && ip !== "-" && ip !== "未知");
    const uniqueIps = Array.from(new Set(rawIps)).sort();
    uniqueIps.forEach(ip => {
      const opt = document.createElement("option");
      opt.value = ip;
      opt.textContent = ip;
      ipSelect.appendChild(opt);
    });
  }

  // 2. 动态 SVG 趋势图与双向时间滑块
  function updateTrendChart() {
    if (allDaily.length === 0) return;
    const minSlider = document.getElementById("time-slider-min");
    const maxSlider = document.getElementById("time-slider-max");
    if (!minSlider || !maxSlider) return;

    let iMin = parseInt(minSlider.value, 10);
    let iMax = parseInt(maxSlider.value, 10);
    if (iMin > iMax) {
      if (document.activeElement === minSlider) {
        maxSlider.value = iMin;
        iMax = iMin;
      } else {
        minSlider.value = iMax;
        iMin = iMax;
      }
    }
    minDayIdx = iMin;
    maxDayIdx = iMax;

    const subSeries = allDaily.slice(minDayIdx, maxDayIdx + 1);
    const startDay = subSeries[0] ? subSeries[0].day : "";
    const endDay = subSeries[subSeries.length - 1] ? subSeries[subSeries.length - 1].day : "";

    const rangeText = document.getElementById("trend-range-text");
    if (rangeText) {
      rangeText.textContent = `${startDay} 至 ${endDay} (共 ${subSeries.length} 天)`;
    }

    const totalCnt = subSeries.reduce((acc, s) => acc + (s.cnt || 0), 0);
    const avgCnt = subSeries.length > 0 ? Math.round(totalCnt / subSeries.length) : 0;
    const rangeStats = document.getElementById("trend-range-stats");
    if (rangeStats) {
      rangeStats.textContent = `区间互动量: ${totalCnt.toLocaleString()} 条 · 日均: ${avgCnt.toLocaleString()} 条`;
    }

    renderDynamicLineChart(subSeries);
    applyCommentFilters();
  }

  function renderDynamicLineChart(series) {
    const container = document.getElementById("daily-chart-container");
    if (!container || series.length === 0) return;

    const width = 760, height = 240;
    const paddingX = 50, paddingY = 30;
    const w = width - paddingX * 2;
    const h = height - paddingY * 2;

    const counts = series.map(s => s.cnt || 0);
    const maxC = Math.max(...counts, 1);
    const n = series.length;

    const points = series.map((s, i) => {
      const x = paddingX + (n > 1 ? (i / (n - 1)) * w : w / 2);
      const y = height - paddingY - ((s.cnt || 0) / maxC) * h;
      return [x, y];
    });

    const polyPts = points.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const areaPts = n > 1
      ? `${paddingX},${height - paddingY} ${polyPts} ${width - paddingX},${height - paddingY}`
      : `${paddingX},${height - paddingY} ${points[0][0].toFixed(1)},${points[0][1].toFixed(1)} ${width - paddingX},${height - paddingY}`;

    let svgHtml = `<svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" style="overflow:visible;">
      <defs>
        <linearGradient id="lineGradDyn" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#da7756" stop-opacity="0.25"/>
          <stop offset="100%" stop-color="#da7756" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
      <line x1="${paddingX}" y1="${height - paddingY}" x2="${width - paddingX}" y2="${height - paddingY}" stroke="#e7e5dc" stroke-width="1"/>
      <line x1="${paddingX}" y1="${paddingY}" x2="${width - paddingX}" y2="${paddingY}" stroke="#f0eee6" stroke-dasharray="4" stroke-width="1"/>
      <text x="${paddingX - 8}" y="${paddingY + 4}" fill="#8c877d" font-size="11" text-anchor="end">${maxC}</text>
      <text x="${paddingX - 8}" y="${height - paddingY + 4}" fill="#8c877d" font-size="11" text-anchor="end">0</text>
      <polygon points="${areaPts}" fill="url(#lineGradDyn)"/>
      <polyline points="${polyPts}" fill="none" stroke="#da7756" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>`;

    const step = Math.max(1, Math.floor(n / 7));
    points.forEach((p, i) => {
      if (i % step === 0 || i === n - 1) {
        const dayStr = (series[i].day || "").slice(-5);
        svgHtml += `<circle cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="4" fill="#ffffff" stroke="#da7756" stroke-width="2">
          <title>${series[i].day}: ${series[i].cnt} 条</title>
        </circle>
        <text x="${p[0].toFixed(1)}" y="${height - 10}" fill="#8c877d" font-size="11" text-anchor="middle">${dayStr}</text>`;
      }
    });
    svgHtml += `</svg>`;
    container.innerHTML = svgHtml;
  }

  window.setTrendRangeDays = function(days, btn) {
    const minSlider = document.getElementById("time-slider-min");
    const maxSlider = document.getElementById("time-slider-max");
    if (!minSlider || !maxSlider || allDaily.length === 0) return;

    document.querySelectorAll(".quick-btns .qbtn").forEach(b => b.classList.remove("active"));
    if (btn) {
      btn.classList.add("active");
    } else if (window.event && window.event.target) {
      window.event.target.classList.add("active");
    }

    if (days === 0 || days >= allDaily.length) {
      minSlider.value = 0;
      maxSlider.value = allDaily.length - 1;
    } else {
      minSlider.value = Math.max(0, allDaily.length - days);
      maxSlider.value = allDaily.length - 1;
    }
    updateTrendChart();
  };

  window.onTimeSliderChange = function() {
    document.querySelectorAll(".quick-btns .qbtn").forEach(btn => btn.classList.remove("active"));
    updateTrendChart();
  };

  // 3. 交互式多维发言检索与表格分页
  window.applyCommentFilters = function() {
    const searchInput = document.getElementById("cmt-search");
    const searchVal = (searchInput ? searchInput.value : "").trim().toLowerCase();
    const sentSelect = document.getElementById("cmt-filter-sentiment");
    const sentVal = sentSelect ? sentSelect.value : "";
    const ipSelect = document.getElementById("cmt-filter-ip");
    const ipVal = ipSelect ? ipSelect.value : "";
    const sortSelect = document.getElementById("cmt-sort");
    const sortVal = sortSelect ? sortSelect.value : "like";

    let startDayStr = "", endDayStr = "";
    if (allDaily.length > 0 && minDayIdx >= 0 && maxDayIdx < allDaily.length) {
      startDayStr = allDaily[minDayIdx].day;
      endDayStr = allDaily[maxDayIdx].day;
    }

    filteredComments = allComments.filter(c => {
      if (searchVal) {
        const inContent = (c.content || "").toLowerCase().includes(searchVal);
        const inUname = (c.uname || "").toLowerCase().includes(searchVal);
        const inUid = String(c.uid || "").includes(searchVal);
        const inIp = (c.ip || "").toLowerCase().includes(searchVal);
        if (!inContent && !inUname && !inUid && !inIp) return false;
      }
      if (sentVal && c.sentiment !== sentVal) return false;
      if (ipVal && c.ip !== ipVal) return false;
      if (startDayStr && endDayStr) {
        const cDate = (c.ctime || "").slice(0, 10);
        if (cDate && (cDate < startDayStr || cDate > endDayStr)) return false;
      }
      return true;
    });

    if (sortVal === "like") {
      filteredComments.sort((a, b) => (b.like_num || 0) - (a.like_num || 0));
    } else if (sortVal === "time_desc") {
      filteredComments.sort((a, b) => (b.ctime || "").localeCompare(a.ctime || ""));
    } else if (sortVal === "time_asc") {
      filteredComments.sort((a, b) => (a.ctime || "").localeCompare(b.ctime || ""));
    } else if (sortVal === "length") {
      filteredComments.sort((a, b) => (b.content || "").length - (a.content || "").length);
    }

    cmtPage = 1;
    renderCommentsTable(searchVal);
  };

  window.onFilterComments = function() {
    applyCommentFilters();
  };

  window.changeCmtPage = function(delta) {
    const totalPages = Math.max(1, Math.ceil(filteredComments.length / pageSize));
    const newPage = cmtPage + delta;
    if (newPage >= 1 && newPage <= totalPages) {
      cmtPage = newPage;
      const searchInput = document.getElementById("cmt-search");
      const searchVal = (searchInput ? searchInput.value : "").trim().toLowerCase();
      renderCommentsTable(searchVal);
    }
  };

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
  }

  function highlightMatch(text, query) {
    if (!query) return escapeHtml(text);
    const escaped = escapeHtml(text);
    const qEscaped = escapeRegExp(escapeHtml(query));
    try {
      const reg = new RegExp(`(${qEscaped})`, "gi");
      return escaped.replace(reg, '<mark style="background:#fef08a;color:#713f12;border-radius:2px;padding:0 2px;">$1</mark>');
    } catch (e) {
      return escaped;
    }
  }

  function renderCommentsTable(searchQuery) {
    const tbody = document.getElementById("interactive-comments-tbody");
    const countEl = document.getElementById("cmt-filter-count");
    const pageInfo = document.getElementById("cmt-page-info");
    const prevBtn = document.getElementById("btn-prev-page");
    const nextBtn = document.getElementById("btn-next-page");

    if (!tbody) return;

    if (countEl) {
      countEl.innerHTML = `共检索到 <b>${filteredComments.length.toLocaleString()}</b> 条发言`;
    }

    const totalPages = Math.max(1, Math.ceil(filteredComments.length / pageSize));
    if (pageInfo) pageInfo.textContent = `第 ${cmtPage} / ${totalPages} 页`;
    if (prevBtn) prevBtn.disabled = (cmtPage <= 1);
    if (nextBtn) nextBtn.disabled = (cmtPage >= totalPages);

    if (filteredComments.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:#8c877d;padding:40px;">未找到匹配条件的发言记录</td></tr>`;
      return;
    }

    const startIdx = (cmtPage - 1) * pageSize;
    const pageRows = filteredComments.slice(startIdx, startIdx + pageSize);

    let rowsHtml = "";
    pageRows.forEach((c, idx) => {
      const globalIdx = startIdx + idx + 1;
      const sentBadge = c.sentiment === "pos"
        ? '<span class="badge" style="background:#e6f9ed;color:#166534;">🟢 正面</span>'
        : (c.sentiment === "neg"
            ? '<span class="badge" style="background:#fef2f2;color:#991b1b;">🔴 负面</span>'
            : '<span class="badge" style="background:#f1f5f9;color:#475569;">⚪ 中性</span>');

      const unameEsc = highlightMatch(c.uname || "", searchQuery);
      const contentEsc = highlightMatch(c.content || "", searchQuery);

      rowsHtml += `<tr>
        <td style="color:#8c877d;font-size:12px;">${globalIdx}</td>
        <td><b>${unameEsc}</b><br><small style="color:#8c877d;">UID:${c.uid}</small></td>
        <td>${escapeHtml(c.ip || "-")}</td>
        <td style="font-size:12px;color:#615d56;">${escapeHtml(c.ctime || "")}</td>
        <td><b style="color:#da7756;">👍 ${(c.like_num || 0).toLocaleString()}</b></td>
        <td>${sentBadge}</td>
        <td style="line-height:1.5;">
          <div>${contentEsc}</div>
        </td>
      </tr>`;
    });

    tbody.innerHTML = rowsHtml;
  }

  // 4. 初始化加载与首屏呈现
  if (allDaily.length > 0) {
    const minSlider = document.getElementById("time-slider-min");
    const maxSlider = document.getElementById("time-slider-max");
    if (minSlider && maxSlider) {
      minSlider.max = allDaily.length - 1;
      maxSlider.max = allDaily.length - 1;
      minSlider.value = 0;
      maxSlider.value = allDaily.length - 1;
    }
  }
  updateTrendChart();
  applyCommentFilters();
})();
"""

def build_html_report(data: ReportData, sections: ReportSections | None = None) -> str:
    """生成排版精美、完全自包含、可打印的单文件 HTML 报告。"""
    sections = sections or ReportSections()
    st = data.stats

    # HTML 转义安全处理
    safe_title = html.escape(st.get("title", ""))
    safe_bvid = html.escape(st.get("bvid", ""))
    safe_owner = html.escape(st.get("owner", ""))

    content_blocks = []

    # 1. 核心指标卡片
    if sections.overview:
        db_c = st.get("db_total_comments", 0)
        off_c = st.get("stat_reply", 0)
        sample_c = st.get("sample_comments_analyzed", 0)

        db_d = st.get("db_total_danmaku", 0)
        off_d = st.get("stat_danmaku", 0)
        sample_d = st.get("sample_danmaku_analyzed", 0)

        off_v = st.get("stat_view", 0)
        off_l = st.get("stat_like", 0)

        disp_c = off_c if off_c > 0 else db_c
        if off_c > 0:
            rate = f"收录率 {min(100.0, db_c / off_c * 100):.1f}%" if off_c > 0 else ""
            sub_c = f"库内已收录 {db_c:,} 条 · 分析样本 {sample_c:,} 条 ({rate})"
        else:
            sub_c = f"全库总计 {db_c:,} 条 · 本次深度分析样本 {sample_c:,} 条"

        disp_d = off_d if off_d > 0 else db_d
        sub_d = f"库内已收录 {db_d:,} 条 · 样本 {sample_d:,} 条" if off_d > 0 else f"分析样本 {sample_d:,} 条"

        if off_v > 0:
            label_3 = "全网播放热度"
            disp_3 = f"{off_v:,}"
            sub_3 = f"官方点赞 {off_l:,} 次 · 活跃观众 {st.get('unique_users', 0):,} 人"
        else:
            label_3 = "独立参与用户"
            disp_3 = f"{st.get('unique_users', 0):,}"
            sub_3 = "真实去重发言用户数"

        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">01 / 核心指标概览</h2>
          <div class="kpi-grid">
            <div class="kpi-card">
              <div class="kpi-label">全网评论总量</div>
              <div class="kpi-val">{disp_c:,}</div>
              <div class="kpi-sub">{sub_c}</div>
            </div>
            <div class="kpi-card">
              <div class="kpi-label">全网弹幕总量</div>
              <div class="kpi-val">{disp_d:,}</div>
              <div class="kpi-sub">{sub_d}</div>
            </div>
            <div class="kpi-card">
              <div class="kpi-label">{label_3}</div>
              <div class="kpi-val">{disp_3}</div>
              <div class="kpi-sub">{sub_3}</div>
            </div>
            <div class="kpi-card">
              <div class="kpi-label">累计前台获赞</div>
              <div class="kpi-val">{st.get("total_likes", 0):,}</div>
              <div class="kpi-sub">分析样本前台点赞总和</div>
            </div>
          </div>
        </section>
        """)

    # 2. 互动走势与时段热力
    if sections.trends and (data.daily or data.hours):
        svg_daily = render_svg_line_chart(data.daily)
        svg_hours = render_svg_hours_bar(data.hours)
        n_days = len(data.daily)
        max_idx = max(0, n_days - 1)
        start_day = data.daily[0]["day"] if data.daily else ""
        end_day = data.daily[-1]["day"] if data.daily else ""

        trend_ctrl = ""
        if n_days >= 2:
            trend_ctrl = f"""
            <div class="trend-controls">
              <div class="range-display">
                <span class="range-tag">时间窗口</span>
                <b id="trend-range-text">{start_day} 至 {end_day} (共 {n_days} 天)</b>
                <span class="range-stats" id="trend-range-stats">拖动滑块可联动缩放折线图并筛选下方发言</span>
              </div>
              <div class="quick-btns">
                <button type="button" class="qbtn active" onclick="setTrendRangeDays(0, this)">全部</button>
                <button type="button" class="qbtn" onclick="setTrendRangeDays(7, this)">近7天</button>
                <button type="button" class="qbtn" onclick="setTrendRangeDays(14, this)">近14天</button>
                <button type="button" class="qbtn" onclick="setTrendRangeDays(30, this)">近30天</button>
              </div>
            </div>
            <div class="slider-wrapper">
              <input type="range" id="time-slider-min" min="0" max="{max_idx}" value="0" oninput="onTimeSliderChange()">
              <input type="range" id="time-slider-max" min="0" max="{max_idx}" value="{max_idx}" oninput="onTimeSliderChange()">
            </div>
            """

        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">02 / 互动传播与时间节律</h2>
          <div class="card-grid-2">
            <div class="card">
              <h3 class="card-title">每日评论/弹幕热度走势</h3>
              <p class="card-desc">互动波峰走势（支持双滑块无级缩放与快速视窗切换）</p>
              {trend_ctrl}
              <div class="chart-box" id="daily-chart-container">{svg_daily}</div>
            </div>
            <div class="card">
              <h3 class="card-title">24 小时活跃时段分布</h3>
              <p class="card-desc">观众讨论最为密集的时间区间（高亮为主峰）</p>
              <div class="chart-box">{svg_hours}</div>
            </div>
          </div>
        </section>
        """)

    # 3. 舆论情感倾向
    if sections.sentiment and data.sentiment:
        svg_donut = render_svg_sentiment_donut(data.sentiment)
        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">03 / 舆论倾向与情感基调</h2>
          <div class="card">
            <h3 class="card-title">观众情感极性分布</h3>
            <p class="card-desc">基于短文本词典法的语义倾向倾向度打分</p>
            <div style="padding:16px 0;">{svg_donut}</div>
          </div>
        </section>
        """)

    # 4. 高频热词与属地
    if sections.terms and (data.terms or data.ips):
        term_tags = "".join(
            f'<span class="tag"><b>{html.escape(t.get("term", ""))}</b> <small>{t.get("cnt", 0)}</small></span>'
            for t in data.terms[:24]
        )
        ip_rows = "".join(
            f'<tr><td>{html.escape(i.get("ip", ""))}</td><td><b>{i.get("count", 0):,}</b></td><td>{i.get("ratio", 0) * 100:.1f}%</td></tr>'
            for i in data.ips[:8]
        )
        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">04 / 讨论热词与观众地域</h2>
          <div class="card-grid-2">
            <div class="card">
              <h3 class="card-title">核心词频 Top 24</h3>
              <p class="card-desc">观众在评论区讨论最频繁的关键实体与表达</p>
              <div class="tag-cloud">{term_tags}</div>
            </div>
            <div class="card">
              <h3 class="card-title">IP 属地分布 Top 8</h3>
              <p class="card-desc">主要发声观众的省份与地域来源</p>
              <table class="report-table">
                <thead><tr><th>属地</th><th>发言条数</th><th>占比</th></tr></thead>
                <tbody>{ip_rows}</tbody>
              </table>
            </div>
          </div>
        </section>
        """)

    # 5. 水军与刷屏名单
    if sections.water_army:
        suspect_rows = []
        for s in data.suspects[:20]:
            hits = "+".join(s.get("hits") or []) or "综合评分"
            level = s.get("level")
            badge_class = "badge-danger" if level == "high" else "badge-warn"
            badge_text = "高危" if level == "high" else "存疑"
            sample = html.escape(s.get("top_text") or "-")
            suspect_rows.append(
                f"""<tr>
                  <td><b>{s.get('uid')}</b><br><small style="color:#8c877d;">{html.escape(s.get('uname') or '')}</small></td>
                  <td><span class="badge {badge_class}">{badge_text} ({s.get('score')})</span></td>
                  <td>{s.get('total')}条 / <b>{s.get('repeat_rate', 0)*100:.0f}%</b></td>
                  <td>峰值 {s.get('burst_max')}条 / {html.escape(s.get('ip_top') or '-')}</td>
                  <td><small style="color:#da7756;">{hits}</small><div style="font-size:12px;color:#615d56;margin-top:2px;">{sample}</div></td>
                </tr>"""
            )
        sus_tbody = "".join(suspect_rows) if suspect_rows else '<tr><td colspan="5" style="text-align:center;color:#8c877d;padding:20px;">未检出明显可疑账号</td></tr>'

        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">05 / 水军刷屏与控评可疑名单</h2>
          <div class="card">
            <h3 class="card-title">可疑账号矩阵 (按置信度排序)</h3>
            <p class="card-desc">基于复读率 ≥50%、短时 10 分钟爆发 ≥6 条、同属地聚集等综合加权排查</p>
            <table class="report-table">
              <thead><tr><th>用户 UID / 昵称</th><th>定级与分值</th><th>发言与复读率</th><th>爆发与属地</th><th>特征与示例发言</th></tr></thead>
              <tbody>{sus_tbody}</tbody>
            </table>
          </div>
        </section>
        """)

    # 6. 精选高赞神评
    if sections.top_comments and data.top_liked:
        top_list = []
        for rank, c in enumerate(data.top_liked[:10], 1):
            content = html.escape(str(c.get("content") or ""))
            uname = html.escape(str(c.get("uname") or c.get("member", {}).get("uname", "") or f"UID:{c.get('uid')}"))
            likes = int(c.get("like_num", 0) or 0)
            time_str = str(c.get("ctime") or "")
            ip = c.get("ip_location") or "-"
            top_list.append(f"""
            <div class="comment-item">
              <div class="comment-rank">#{rank}</div>
              <div class="comment-body">
                <div class="comment-header">
                  <b>{uname}</b> <span class="comment-meta">{ip} · {time_str}</span>
                  <span class="like-badge">👍 {likes:,}</span>
                </div>
                <div class="comment-text">{content}</div>
              </div>
            </div>
            """)
        top_html = "".join(top_list)
        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">06 / 精选高赞热评 (Top 10)</h2>
          <div class="card">
            <div class="comment-list">{top_html}</div>
          </div>
        </section>
        """)

    # 7. 交互式数据检索表格与数据下钻
    if sections.interactive_table:
        content_blocks.append("""
        <section class="section" id="section-interactive">
          <h2 class="section-title">07 / 互动发言检索透视与数据下钻</h2>
          <div class="card">
            <h3 class="card-title">发言样本多维即时检索</h3>
            <p class="card-desc">纯本地无网络离线计算：支持关键词毫秒级过滤、情感倾向筛选、省份属地交叉聚合与多种排序模式</p>

            <div class="filter-toolbar">
              <div class="search-box">
                <input type="text" id="cmt-search" placeholder="🔍 实时搜索发言内容、用户昵称、UID 或属地..." oninput="onFilterComments()">
              </div>
              <div class="filter-group">
                <select id="cmt-filter-sentiment" onchange="onFilterComments()">
                  <option value="">全部情感倾向</option>
                  <option value="pos">🟢 正向认同</option>
                  <option value="neu">⚪ 中性客观</option>
                  <option value="neg">🔴 负向争议</option>
                </select>
                <select id="cmt-filter-ip" onchange="onFilterComments()">
                  <option value="">全部 IP 属地</option>
                </select>
                <select id="cmt-sort" onchange="onFilterComments()">
                  <option value="like">按点赞数最高</option>
                  <option value="time_desc">按发布时间倒序 (最新)</option>
                  <option value="time_asc">按发布时间正序 (最早)</option>
                  <option value="length">按发言文本长度</option>
                </select>
              </div>
            </div>

            <div class="filter-summary">
              <span id="cmt-filter-count">正在载入数据样本...</span>
              <div class="pagination-controls">
                <button type="button" class="page-btn" id="btn-prev-page" onclick="changeCmtPage(-1)" disabled>‹ 上一页</button>
                <span id="cmt-page-info">第 1 / 1 页</span>
                <button type="button" class="page-btn" id="btn-next-page" onclick="changeCmtPage(1)" disabled>下一页 ›</button>
              </div>
            </div>

            <div class="table-container">
              <table class="report-table interactive-table" id="interactive-comments-table">
                <thead>
                  <tr>
                    <th style="width:48px;">#</th>
                    <th style="width:140px;">用户 (UID)</th>
                    <th style="width:70px;">属地</th>
                    <th style="width:140px;">发表时间</th>
                    <th style="width:80px;">点赞数</th>
                    <th style="width:75px;">情感倾向</th>
                    <th>发言正文内容</th>
                  </tr>
                </thead>
                <tbody id="interactive-comments-tbody">
                  <tr><td colspan="7" style="text-align:center;color:#8c877d;padding:30px;">正在加载...</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>
        """)

    rendered_body = "\n".join(content_blocks)

    raw_payload = {
        "daily": data.daily,
        "comments": data.interactive_comments,
    }
    json_str = json.dumps(raw_payload, ensure_ascii=False).replace("</script>", "<\\/script>")
    scripts_html = f"""
    <script id="report-raw-data" type="application/json">{json_str}</script>
    <script>{CLIENT_INTERACTIVE_JS}</script>
    """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>B站深度数据研报 - {safe_title}</title>
  <style>
    :root {{
      --bg-page: #faf9f5;
      --bg-card: #ffffff;
      --fg-primary: #191816;
      --fg-secondary: #615d56;
      --fg-muted: #8c877d;
      --accent: #da7756;
      --accent-light: #fbf2ed;
      --border: #e7e5dc;
      --radius: 8px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei UI", sans-serif;
      background: var(--bg-page);
      color: var(--fg-primary);
      line-height: 1.6;
      padding: 32px 16px;
    }}
    .container {{
      max-width: 960px;
      margin: 0 auto;
    }}
    header.report-header {{
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 32px;
    }}
    .badge-tag {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.5px;
      text-transform: uppercase;
      background: var(--accent-light);
      color: var(--accent);
      padding: 3px 8px;
      border-radius: 4px;
      margin-bottom: 8px;
    }}
    h1.report-title {{
      font-size: 26px;
      font-weight: 800;
      color: var(--fg-primary);
      margin-bottom: 8px;
      line-height: 1.3;
    }}
    .report-meta {{
      font-size: 13px;
      color: var(--fg-muted);
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }}
    .section {{
      margin-bottom: 40px;
    }}
    .section-title {{
      font-size: 14px;
      font-weight: 700;
      color: var(--fg-muted);
      letter-spacing: 0.5px;
      text-transform: uppercase;
      margin-bottom: 16px;
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
    }}
    .kpi-card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 18px;
    }}
    .kpi-label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--fg-muted);
      text-transform: uppercase;
    }}
    .kpi-val {{
      font-size: 26px;
      font-weight: 800;
      color: var(--fg-primary);
      margin: 4px 0;
    }}
    .kpi-sub {{
      font-size: 12px;
      color: var(--fg-muted);
    }}
    .card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 20px;
    }}
    .card-grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }}
    @media (max-width: 768px) {{
      .card-grid-2 {{ grid-template-columns: 1fr; }}
      h1.report-title {{ font-size: 20px; }}
    }}
    .card-title {{
      font-size: 15px;
      font-weight: 700;
      color: var(--fg-primary);
      margin-bottom: 2px;
    }}
    .card-desc {{
      font-size: 12px;
      color: var(--fg-muted);
      margin-bottom: 16px;
    }}
    .tag-cloud {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .tag {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--bg-page);
      border: 1px solid var(--border);
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 13px;
    }}
    .tag small {{
      color: var(--accent);
      font-weight: 700;
    }}
    table.report-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    table.report-table th, table.report-table td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }}
    table.report-table th {{
      color: var(--fg-muted);
      font-weight: 600;
      background: #fdfcfb;
    }}
    .badge {{
      display: inline-block;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 4px;
    }}
    .badge-danger {{ background: #fdf0ef; color: #b9382f; }}
    .badge-warn {{ background: #fff8e6; color: #a36814; }}
    .comment-list {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .comment-item {{
      display: flex;
      gap: 14px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }}
    .comment-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
    .comment-rank {{
      font-size: 16px;
      font-weight: 800;
      color: var(--accent);
      width: 32px;
      flex-shrink: 0;
    }}
    .comment-body {{ flex: 1; }}
    .comment-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .comment-meta {{ font-size: 11px; color: var(--fg-muted); }}
    .like-badge {{
      margin-left: auto;
      font-size: 12px;
      font-weight: 700;
      color: var(--accent);
      background: var(--accent-light);
      padding: 2px 8px;
      border-radius: 4px;
    }}
    .comment-text {{
      font-size: 13px;
      color: var(--fg-secondary);
      line-height: 1.5;
    }}
    /* 趋势控制栏与时间滑块 */
    .trend-controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
      padding: 10px 12px;
      background: var(--bg-page);
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    .range-display {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--fg-secondary);
      flex-wrap: wrap;
    }}
    .range-tag {{
      font-size: 11px;
      font-weight: 700;
      color: var(--accent);
      background: var(--accent-light);
      padding: 2px 6px;
      border-radius: 3px;
    }}
    .range-stats {{
      font-size: 11px;
      color: var(--fg-muted);
    }}
    .quick-btns {{
      display: inline-flex;
      gap: 4px;
    }}
    .qbtn {{
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 3px 8px;
      font-size: 11px;
      cursor: pointer;
      color: var(--fg-secondary);
      transition: all 0.15s;
    }}
    .qbtn:hover {{ border-color: var(--accent); color: var(--accent); }}
    .qbtn.active {{
      background: var(--fg-primary);
      color: #ffffff;
      border-color: var(--fg-primary);
      font-weight: 600;
    }}
    .slider-wrapper {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
      padding: 0 4px;
    }}
    .slider-wrapper input[type="range"] {{
      flex: 1;
      height: 6px;
      accent-color: var(--accent);
      cursor: pointer;
    }}

    /* 交互式表格与工具栏 */
    .filter-toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }}
    .search-box {{
      flex: 1;
      min-width: 240px;
    }}
    .search-box input {{
      width: 100%;
      padding: 8px 12px;
      font-size: 13px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--bg-page);
      color: var(--fg-primary);
      outline: none;
      transition: border-color 0.2s;
    }}
    .search-box input:focus {{ border-color: var(--accent); background: #ffffff; }}
    .filter-group {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .filter-group select {{
      padding: 7px 10px;
      font-size: 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--bg-page);
      color: var(--fg-primary);
      outline: none;
      cursor: pointer;
    }}
    .filter-group select:focus {{ border-color: var(--accent); }}
    .filter-summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
      font-size: 12px;
      color: var(--fg-muted);
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }}
    .pagination-controls {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .page-btn {{
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 3px 8px;
      font-size: 12px;
      cursor: pointer;
      color: var(--fg-secondary);
    }}
    .page-btn:disabled {{
      opacity: 0.4;
      cursor: not-allowed;
    }}
    .page-btn:not(:disabled):hover {{
      border-color: var(--accent);
      color: var(--accent);
    }}
    .table-container {{
      overflow-x: auto;
    }}
    .interactive-table td {{
      vertical-align: top;
    }}
    footer.report-footer {{
      margin-top: 48px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
      text-align: center;
      font-size: 12px;
      color: var(--fg-muted);
    }}
    @media print {{
      body {{ background: #ffffff; padding: 0; }}
      .card, .kpi-card {{ border: 1px solid #ccc; }}
      .trend-controls, .slider-wrapper, .filter-toolbar, .pagination-controls {{ display: none !important; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="report-header">
      <div class="badge-tag">BILIBILI DATA INTELLIGENCE REPORT</div>
      <h1 class="report-title">{safe_title}</h1>
      <div class="report-meta">
        <span><b>BV:</b> {safe_bvid}</span>
        <span><b>UP主:</b> {safe_owner}</span>
        <span><b>发布时间:</b> {st.get("pubdate")}</span>
        <span><b>生成时间:</b> {st.get("generated_at")}</span>
      </div>
    </header>

    {rendered_body}

    <footer class="report-footer">
      <p>由 Bilibili Data Project 自动生成 · 零外部网络依赖单文件离线报告</p>
    </footer>
  </div>
  {scripts_html}
</body>
</html>
"""


def generate_report(
    bvid: str,
    output_path: str,
    *,
    limit: int = 0,
    sections: ReportSections | None = None,
    auto_sync: bool = False,
) -> str:
    """采集并渲染单文件 HTML 研报至指定文件路径。"""
    sections = sections or ReportSections()
    data = collect_report_data(bvid, limit=limit, sections=sections, auto_sync=auto_sync)
    html_content = build_html_report(data, sections=sections)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return os.path.abspath(output_path)
