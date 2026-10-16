"""跨仓检索缓存单测：纯逻辑，无需数据库。"""
import time

from storage.dao import search as S


def test_cache_key_normalizes():
    k1 = S._search_cache_key("cmt", "  视频  ", "2026-01-01", "", "", 100, 0)
    k2 = S._search_cache_key("cmt", "视频", "2026-01-01", "", "", 100, 0)
    assert k1 == k2  # 关键词两端空白被规整
    k3 = S._search_cache_key("dmk", "视频", "2026-01-01", "", "", 100, 0)
    assert k3 != k1   # 类型不同即不同键


def test_cache_put_get_roundtrip():
    S.clear_search_cache()
    key = S._search_cache_key("cmt", "测试词", "", "", "", 50, 0)
    payload = {"rows": [{"rpid": 1}], "total": 1}
    S._search_cache_put(key, payload)
    got = S._search_cache_get(key)
    assert got is not None and got["total"] == 1
    # 返回的是副本语义由调用方负责，这里只验证命中
    S.clear_search_cache()


def test_cache_expires():
    S.clear_search_cache()
    key = S._search_cache_key("cmt", "过期词", "", "", "", 50, 0)
    S._search_cache_put(key, {"rows": [], "total": 0})
    # 手动把时间戳拨回过去，模拟 TTL 过期
    ts, payload = S._SEARCH_CACHE[key]
    S._SEARCH_CACHE[key] = (ts - S._SEARCH_CACHE_TTL - 1, payload)
    assert S._search_cache_get(key) is None
    S.clear_search_cache()


def test_cache_lru_eviction():
    S.clear_search_cache()
    for i in range(S._SEARCH_CACHE_MAX + 5):
        k = S._search_cache_key("cmt", f"词{i}", "", "", "", 10, 0)
        S._search_cache_put(k, {"rows": [], "total": 0})
    assert len(S._SEARCH_CACHE) == S._SEARCH_CACHE_MAX
    # 最早的几个应被淘汰
    oldest = S._search_cache_key("cmt", "词0", "", "", "", 10, 0)
    assert S._search_cache_get(oldest) is None
    S.clear_search_cache()


def test_clear_search_cache():
    key = S._search_cache_key("cmt", "x", "", "", "", 1, 0)
    S._search_cache_put(key, {"rows": [], "total": 0})
    n = S.clear_search_cache()
    assert n >= 1
    assert len(S._SEARCH_CACHE) == 0
