"""DAO 基础设施：连接复用、shard 列表、跨库判定。

拆包后由 storage/dao/__init__.py 与 search/rankings 子模块共享。
此处的模块级状态（_tls / _SHARD_CACHE）是跨子模块的单一实例：
__init__ 通过 `from ... import` 复用同一对象，保证缓存与线程本地连接共享。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# 线程级读连接复用：同一线程内同 (role, db, 超时) 只建连一次，
# ping(reconnect=True) 保活；短生命周期的线程结束时由 GC 回收。
_tls = threading.local()

_SHARD_CACHE: dict = {"time": 0.0, "shards": [], "ok": False}
_SHARD_TTL = 60.0


class StatsCancelled(Exception):
    """统计过程中用户取消（stop_event 置位）。"""


def _open_conn(role: str, db: str, read_timeout: int):
    import pymysql

    from config import get_config
    if role == "write":
        base = getattr(get_config(), "mysql_write", get_config().mysql)
    else:
        base = getattr(get_config(), "mysql_archive", get_config().mysql)
    return pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
                           read_timeout=read_timeout)


def _rcached(role: str, db: str, read_timeout: int = 120):
    """取线程复用读连接；断线自动重连。调用方不关闭。"""
    cache = getattr(_tls, "conns", None)
    if cache is None:
        cache = {}
        _tls.conns = cache
    key = (role, db, read_timeout)
    conn = cache.get(key)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try: conn.close()
            except Exception: pass
    conn = _open_conn(role, db, read_timeout)
    cache[key] = conn
    return conn


def _drop_cached(role: str, db: str, read_timeout: int = 120) -> None:
    """丢掉线程复用池里坏掉的连接，下次重建。"""
    cache = getattr(_tls, "conns", None)
    if not cache:
        return
    conn = cache.pop((role, db, read_timeout), None)
    if conn is not None:
        try: conn.close()
        except Exception: pass


def _rclose_all() -> None:
    """关闭本线程全部复用读连接（如取消/收尾时调用，可选）。"""
    cache = getattr(_tls, "conns", None)
    if not cache:
        return
    for conn in list(cache.values()):
        try: conn.close()
        except Exception: pass
    cache.clear()


def _shard_dbs_trusted() -> tuple[list, bool]:
    """带可信标记的 shard 列表（60s 缓存）。

    返回 (dbs, ok)：ok=False 表示本次列表拉取失败（用了兜底/缓存），
    调用方不得把结果当全量存缓存。
    """
    import time as _t
    now = _t.monotonic()
    hit = _SHARD_CACHE.get("shards")
    if hit and (now - _SHARD_CACHE.get("time", 0)) < _SHARD_TTL:
        return list(hit), bool(_SHARD_CACHE.get("ok", True))
    try:
        from storage.archive_sync import list_shards
        shards = [s for s in (list_shards() or []) if s]
        base = "bilibili_data"
        uniq: list = []
        seen: set = set()
        for s in shards:
            if s not in seen:
                seen.add(s); uniq.append(s)
        if base not in seen:
            uniq = [base] + uniq
        _SHARD_CACHE.update({"time": now, "shards": list(uniq), "ok": True})
        return uniq, True
    except Exception as exc:
        logger.warning("shard 列表拉取失败: %s", exc)
        if hit:
            # 用上次可信列表，但标记不可信
            _SHARD_CACHE["ok"] = False
            return list(hit), False
        _SHARD_CACHE.update({"time": now, "shards": ["bilibili_data"], "ok": False})
        return ["bilibili_data"], False


def _shard_dbs():
    dbs, _ok = _shard_dbs_trusted()
    return dbs


def _uid_same_dsn() -> bool:
    """D 与 E 是否同库（单库模式）：是则只查一次，避免重复计数。"""
    try:
        from config import get_config
        cfg = get_config()
        w = getattr(cfg, "mysql_write", cfg.mysql)
        r = getattr(cfg, "mysql_archive", cfg.mysql)
        return w["host"] == r["host"] and w["port"] == r["port"] and w["database"] == r["database"]
    except Exception:
        return True

