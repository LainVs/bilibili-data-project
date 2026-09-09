"""B站帖子类内容评论采集。

支持两类“帖子”：
  - 动态（space 动态 / t.bilibili.com）：标题/正文来自动态页，评论 type=17
  - 专栏文章（read.cv + 文章）：评论 type=12
评论数据源与视频共用 /x/v2/reply/wbi/main，仅 type 不同。
"""
from __future__ import annotations

import logging
import re

from crawler.api.client import ApiClient

logger = logging.getLogger(__name__)

# 动态详情（用于拿标题/正文）
DYNAMIC_DETAIL_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?timezone_offset=-480&id={dyn_id}"
# 专栏详情
ARTICLE_DETAIL_API = "https://api.bilibili.com/x/article/view?id={cvid}"

_RE_TBIL = re.compile(r"t\.bilibili\.com/(\d+)")
_RE_DYN = re.compile(r"(?:dynamic|opus)/?(\d+)")
_RE_READ = re.compile(r"read/cv(\d+)", re.IGNORECASE)

DYNAMIC_TYPE = 17
ARTICLE_TYPE = 12


def parse_post_link(text: str) -> dict:
    """解析帖子链接/ID，返回 {kind: 'dynamic'|'article', id: int} 或空 dict。"""
    text = text.strip()
    low = text.lower()
    if "read/cv" in low:
        m = _RE_READ.search(text)
        return {"kind": "article", "id": int(m.group(1))} if m else {}
    if "t.bilibili.com" in low or "dynamic" in low or "opus" in low:
        for pat in (_RE_TBIL, _RE_DYN):
            m = pat.search(text)
            if m:
                return {"kind": "dynamic", "id": int(m.group(1))}
    if text.isdigit():
        return {"kind": "dynamic", "id": int(text)}
    return {}


def fetch_post_meta(client: ApiClient, kind: str, pid: int) -> dict:
    """获取帖子标题等元信息（动态/专栏缺失时降级为 ID）。"""
    if kind == "article":
        try:
            payload = client.get_json(ARTICLE_DETAIL_API.format(cvid=pid))
            data = payload.get("data") or {}
            return {
                "kind": "article",
                "id": pid,
                "title": (data.get("title") or "")[:200],
                "owner_uid": int(((data.get("author") or {}).get("mid")) or 0),
                "pubdate": int(data.get("publish_time", 0) or 0),
            }
        except Exception as exc:
            logger.warning("专栏信息获取失败 id=%s: %s", pid, exc)
            return {"kind": "article", "id": pid, "title": f"专栏{pid}", "owner_uid": 0, "pubdate": 0}
    # 动态
    try:
        payload = client.get_json(DYNAMIC_DETAIL_API.format(dyn_id=pid))
        item = (payload.get("data") or {}).get("item") or {}
        content = ""
        module = item.get("modules") or {}
        dyn = module.get("module_dynamic") or {}
        if isinstance(dyn.get("desc"), dict):
            content = str(dyn["desc"].get("text", ""))
        author = (module.get("module_author") or {}).get("author") or {}
        return {
            "kind": "dynamic",
            "id": pid,
            "title": (content[:60] or f"动态{pid}"),
            "owner_uid": int(author.get("mid") or 0),
            "pubdate": int(author.get("pub_ts") or 0),
        }
    except Exception as exc:
        logger.warning("动态详情获取失败 id=%s: %s", pid, exc)
        return {"kind": "dynamic", "id": pid, "title": f"动态{pid}", "owner_uid": 0, "pubdate": 0}