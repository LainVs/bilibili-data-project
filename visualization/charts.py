"""图表渲染：matplotlib 可选依赖，无库时降级为空并提示安装。

所有函数均为“保存 PNG 文件”语义（不直接弹窗、不依赖 Tk），
GUI 用 PhotoImage 展示文件，CLI 直接拿文件路径。
中文字体：优先微软雅黑，缺失时回退 DejaVu（中文显示为方框但不报错）。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def available() -> bool:
    try:
        import matplotlib  # noqa: F401

        return True
    except Exception:
        return False


def _setup_font():
    import matplotlib
    import matplotlib.font_manager as _fm

    matplotlib.rcParams["axes.unicode_minus"] = False
    for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"):
        try:
            if any(name.lower() in (f.name or "").lower() for f in _fm.fontManager.ttflist):
                matplotlib.rcParams["font.sans-serif"] = [name]
                return name
        except Exception:
            continue
    return None


def _new_fig(w: float = 8.0, h: float = 3.2):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _setup_font()
    fig, ax = plt.subplots(figsize=(w, h), dpi=110)
    return fig, ax, plt


def _save(fig, plt, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def line_daily(daily: list[dict], path: str, title: str = "每日评论量") -> str | None:
    """折线：每日评论量。无数据返回 None（调用方显示占位）。"""
    if not available():
        logger.warning("matplotlib 未安装，跳过图表（pip install matplotlib）")
        return None
    daily = [d for d in (daily or []) if d.get("cnt") is not None]
    if not daily:
        return None
    fig, ax, plt = _new_fig()
    ax.plot([d["day"][5:] for d in daily], [d["cnt"] for d in daily],
            marker="o", markersize=3, linewidth=1.5)
    ax.set_title(title)
    ax.set_xlabel("日期(MM-DD)")
    ax.set_ylabel("条数")
    ax.tick_params(axis="x", rotation=45, labelsize=7)
    ax.grid(alpha=0.3)
    return _save(fig, plt, path)


def bar_hours(hours: list[dict], path: str, title: str = "24小时活跃分布") -> str | None:
    """柱状：24 小时分布。"""
    if not available():
        return None
    if not hours:
        return None
    fig, ax, plt = _new_fig()
    ax.bar([h["hour"] for h in hours], [h["cnt"] for h in hours])
    ax.set_title(title)
    ax.set_xlabel("小时")
    ax.set_ylabel("条数")
    ax.set_xticks(range(0, 24, 2))
    ax.grid(alpha=0.3, axis="y")
    return _save(fig, plt, path)


def bar_terms(terms: list[dict], path: str, title: str = "高频词Top",
              top: int = 15) -> str | None:
    """横向条形：高频词。"""
    if not available():
        return None
    terms = list((terms or [])[: max(1, top)])
    if not terms:
        return None
    terms = list(reversed(terms))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _setup_font()
    fig, ax = plt.subplots(figsize=(8.0, max(2.6, 0.32 * len(terms) + 1.2)), dpi=110)
    ax.barh([str(t.get("term")) for t in terms], [t.get("cnt", 0) for t in terms])
    ax.set_title(f"{title}{len(terms)}")
    ax.set_xlabel("次数")
    return _save(fig, plt, path)


def render_all(insights: dict, out_dir: str) -> dict:
    """一键渲染三张图：返回 {daily, hours, terms} 路径（缺失为 None）。"""
    os.makedirs(out_dir, exist_ok=True)
    ins = insights or {}
    return {
        "daily": line_daily(ins.get("daily") or [],
                            os.path.join(out_dir, "daily.png")),
        "hours": bar_hours(ins.get("hours") or [],
                           os.path.join(out_dir, "hours.png")),
        "terms": bar_terms(ins.get("terms") or [],
                           os.path.join(out_dir, "terms.png")),
    }
