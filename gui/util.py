"""GUI 通用工具：视频列表加载、内容格式化。"""
from __future__ import annotations

import datetime
import logging
import re

from storage import dao

logger = logging.getLogger(__name__)

_BV_RE = re.compile(r"(?<![0-9a-zA-Z])(BV1[0-9A-Za-z]{9})(?![0-9a-zA-Z])", re.IGNORECASE)
_SHORTLINK_RE = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+")


def extract_bvid(text: str) -> str:
    """从任意文本中**离线**提取 BV 号（纯正则，不发起网络请求）。

    支持：裸 BV 号、含 BV 的链接/文案。若只有 b23.tv 短链而无 BV，
    返回空串——短链需联网解析，请用 resolve_short_link()（放后台线程调）。
    """
    text = (text or "").strip()
    if not text:
        return ""
    m = _BV_RE.search(text)
    if m:
        return "BV" + m.group(1)[2:]
    return ""


def find_short_link(text: str) -> str:
    """从文本中找出 b23.tv / bili2233.cn 短链，没有则返回空串。"""
    m = _SHORTLINK_RE.search(text or "")
    return m.group(0) if m else ""


def resolve_short_link(url: str) -> str:
    """联网解析短链得到 BV 号（**阻塞，必须在后台线程调用**）。

    失败或无法提取时返回空串，绝不抛异常。
    """
    try:
        import requests

        resp = requests.head(url, allow_redirects=True, timeout=5,
                             headers={"User-Agent": "Mozilla/5.0"})
        return extract_bvid(resp.url)
    except Exception as exc:
        logger.warning("短链解析失败 %s: %s", url, exc)
        return ""


def ping_main(widget, label: str) -> None:
    """通知主窗口某面板工作完成（亮小红点）。线程安全，找不到主窗口则静默。"""
    try:
        top = widget.winfo_toplevel()
        notify = getattr(top, "notify_done", None)
        if not callable(notify):
            return
        try:
            widget.after(0, lambda: notify(label))
        except Exception:
            try:
                notify(label)
            except Exception:
                pass
    except Exception:
        pass


def load_videos() -> list[dict]:
    """返回已采集视频列表（附 评论数/弹幕数/完成状态），全量。"""
    try:
        return dao.query_recent_videos()
    except Exception:
        return []


def video_summary(v: dict) -> str:
    """已采集视频下拉标签：BV 标题 | UP名(uid) | 评论N/弹幕M [已完成/未完]"""
    c = v.get("comment_count", 0)
    d = v.get("danmaku_count", 0)
    c_done = v.get("comment_done")
    d_done = v.get("danmaku_done")
    flag = ""
    if c_done or d_done:
        done = "完成" if (c_done or not c) and (d_done or not d) else "未完整"
        flag = f" [{done}]"
    owner = v.get("owner_name") or ""
    uid = v.get("owner_uid") or 0
    up = f" {owner}({uid})" if owner else f" UP{uid}"
    return f"{v.get('bvid', '')} {str(v.get('title', ''))[:22]} |{up} | 评{c} 弹{d}{flag}"


def fmt_dt(ts) -> str:
    """兼容时间戳/字符串。"""
    if not ts:
        return ""
    if isinstance(ts, datetime.datetime):
        return ts.strftime("%Y-%m-%d %H:%M")
    if isinstance(ts, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return str(ts)
    return str(ts)


def format_danmaku_line(data: dict) -> str:
    """弹幕 → 显示行。"""
    return (
        f"[弹幕] {fmt_dt(data.get('ctime'))} | "
        f"{float(data.get('progress', 0) or 0):>8.1f}s | "
        f"{data.get('mid_hash', '') or '-':<8} | {str(data.get('content', ''))[:200]}"
    )


def format_comment_line(data: dict) -> str:
    """评论 → 显示行。"""
    loc = ("[" + str(data.get("ip_location", "")) + "]") if data.get("ip_location") else ""
    return (
        f"[评论] {fmt_dt(data.get('ctime'))} | likes={data.get('like_num', 0)} "
        f"| uid={data.get('uid', 0)} {loc} | {str(data.get('content', ''))}"
    )
