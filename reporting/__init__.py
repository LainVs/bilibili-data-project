"""可视化网页研报引擎：零外部依赖、100% 离线可用单文件 HTML 报告生成。"""
from __future__ import annotations

import html
import json
import logging
import os
import re
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
    trends: bool = True         # 互动走势与活跃时段
    sentiment: bool = True      # 舆论情感倾向分布
    terms: bool = True          # 词频透视与核心热词
    water_army: bool = True     # 水军与控评可疑名单
    top_comments: bool = True   # 精选高赞神评展示


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
        f'<defs>',
        f'<linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">',
        f'<stop offset="0%" stop-color="#da7756" stop-opacity="0.25"/>',
        f'<stop offset="100%" stop-color="#da7756" stop-opacity="0.0"/>',
        f'</linearGradient>',
        f'</defs>',
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
        content_blocks.append(f"""
        <section class="section">
          <h2 class="section-title">02 / 互动传播与时间节律</h2>
          <div class="card-grid-2">
            <div class="card">
              <h3 class="card-title">每日评论/弹幕热度走势</h3>
              <p class="card-desc">近 30 天每日内容增量与互动波峰</p>
              <div class="chart-box">{svg_daily}</div>
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

    rendered_body = "\n".join(content_blocks)

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
