"""ETL 清洗模块：文本清洗、属地规范化、表情包与特殊符号净化。"""
from __future__ import annotations

import html
import re
from typing import Any

# 匹配 Bilibili 表情标识，如 [doge]、[妙看]、[tv_doge]
_EMOJI_RE = re.compile(r"\[[a-zA-Z0-9_\u4e00-\u9fa5]+\]")
# URL 匹配
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
# @ 用户匹配
_AT_RE = re.compile(r"@[^\s:,，：]+")
# 话题标签 #xxx#
_TOPIC_RE = re.compile(r"#[^#\s]+#?")
# 属地清理前缀
_IP_PREFIX_RE = re.compile(r"^(?:IP属地|属地|来自)\s*[:：]?\s*")


def clean_text(raw: str, *, remove_emoji: bool = False,
               strip_urls: bool = True) -> str:
    """清理评论与弹幕文本：反转义 HTML、去 URL、去 @，可选去除表情包。"""
    if not raw:
        return ""
    text = str(raw)
    # HTML 实体反转义（如 &amp; -> &）
    text = html.unescape(text)
    if strip_urls:
        text = _URL_RE.sub("", text)
        text = _AT_RE.sub("", text)
        text = _TOPIC_RE.sub("", text)
    if remove_emoji:
        text = _EMOJI_RE.sub("", text)
    # 合并连续换行与空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_ip(raw: str | None) -> str:
    """规范化 IP 属地：去除“IP属地：”前缀，空值返回“未知”。"""
    if not raw:
        return "未知"
    s = str(raw).strip()
    s = _IP_PREFIX_RE.sub("", s).strip()
    return s if s else "未知"


def sanitize_user_profile(user: dict[str, Any]) -> dict[str, Any]:
    """清洗并规整用户维度数据。"""
    if not isinstance(user, dict):
        return {}
    uid = int(user.get("uid", 0) or 0)
    uname = str(user.get("uname", "") or "").strip()[:128]
    level = max(0, min(6, int(user.get("level", 0) or 0)))
    sex = str(user.get("sex", "") or "").strip()
    if sex not in ("男", "女", "保密"):
        sex = "保密"
    is_vip = 1 if int(user.get("is_vip", 0) or 0) > 0 else 0
    signature = clean_text(str(user.get("signature", "") or ""), strip_urls=False)[:500]
    fans = max(0, int(user.get("fans", 0) or 0))
    return {
        "uid": uid,
        "uname": uname,
        "level": level,
        "sex": sex,
        "is_vip": is_vip,
        "signature": signature,
        "fans": fans,
    }
