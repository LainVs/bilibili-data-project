"""B站 WBI 签名。

评论接口（/x/v2/reply/wbi/main 等）需要 wts + w_rid 签名。
参考 bilibili-API-collect 的实现。

注意：本实现提供了一个公开的备选 mixin key（MIXIN_KEY_ENC）用于兜底；
实际运行时优先从 /nav 接口动态获取 img_key/sub_key 计算。
"""
from __future__ import annotations

import functools
import hashlib
import threading
import time
import urllib.parse

# 从 https://api.bilibili.com/x/web-interface/nav 的 wbi_img 可动态更新这两项。
# 这里给一个历史的公开备选值；进程内会优先尝试动态获取。
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
# 公开备选 key（可能失效，动态获取失败时兜底）
MIXIN_KEY_ENC = "ea1db124af3c7062474693fa704f4ff8"

# 动态 key 缓存 TTL：B站 wbi key 一般半天内有效，取 30 分钟足够安全
_WBI_KEY_TTL = 1800.0
_wbi_lock = threading.Lock()
_wbi_keys: tuple[str, str] | None = None
_wbi_fetched_at = 0.0


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """由 img_key + sub_key 混合生成真正的 mixin key。"""
    origin = img_key + sub_key
    mixin = "".join(origin[i] for i in MIXIN_KEY_ENC_TAB)  # type: ignore[index]
    return mixin[:32]


def _fetch_wbi_keys(client: object) -> tuple[str, str] | None:
    """从 /x/web-interface/nav 动态获取 img_key 与 sub_key。"""
    try:
        payload = client.get_json("https://api.bilibili.com/x/web-interface/nav")
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_url = (wbi_img.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_url = (wbi_img.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if img_url and sub_url:
            return img_url, sub_url
    except Exception:
        pass
    return None


def get_wbi_keys(client: object) -> tuple[str, str] | None:
    """按 TTL 缓存的 key 获取：第一次从 /nav 拉取，之后 30 分钟内复用。

    并发下由锁保证只触发一次 /nav（评论翻页等高频签名不再打 /nav，
    吞吐瓶颈从"每请求一次 nav"降为"每 30 分钟一次"）。

    动态获取失败时返回兜底备选 key（可能失效，但保留调用方行为不变）。
    """
    global _wbi_keys, _wbi_fetched_at
    now = time.monotonic()
    if _wbi_keys is not None and now - _wbi_fetched_at < _WBI_KEY_TTL:
        return _wbi_keys
    with _wbi_lock:
        now = time.monotonic()
        if _wbi_keys is not None and now - _wbi_fetched_at < _WBI_KEY_TTL:
            return _wbi_keys  # 双检：另一个线程刚刷新过
        keys = _fetch_wbi_keys(client)
        if keys is None:
            # 拉取失败：过期缓存仍可用则继续用，避免一失败就全停
            if _wbi_keys is not None:
                return _wbi_keys
            return None
        _wbi_keys = keys
        _wbi_fetched_at = now
        return keys


def reset_wbi_keys_cache() -> None:
    """测试/换号场景强制让缓存立即过期。"""
    global _wbi_keys, _wbi_fetched_at
    with _wbi_lock:
        _wbi_keys = None
        _wbi_fetched_at = 0.0


@functools.lru_cache(maxsize=1)
def get_mixin_key_cached() -> str:
    """缓存计算可复用的 mixin key（常规 key 半天换一次）。"""
    # 无法访问 client 自动刷新，这里返回内置备选；
    # 生产上建议改为带超时的缓存 + 定时刷新。
    return MIXIN_KEY_ENC


def enc_wbi(params: dict, img_key: str, sub_key: str) -> tuple[dict, int]:
    """对参数字典进行 wbi 签名，返回 (带 wts/w_rid 的 dict)。"""
    mixin_key = get_mixin_key(img_key, sub_key)
    # 过滤 value 为特殊字符的
    params = {k: v for k, v in params.items() if v not in ("", "undefined", "null")}
    params["wts"] = int(time.time())
    # 值必须为字符串再拼接
    query = urllib.parse.urlencode({k: str(v) for k, v in sorted(params.items())})
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params, params["wts"]
