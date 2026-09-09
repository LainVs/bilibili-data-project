"""mid_hash -> 真实 UID 反查（CRC32 破解）。

B站弹幕接口返回的 mid_hash 实际是 UID 字符串的 CRC32 值
（多项式 0xEDB88320, ITU I.363.5）。
本模块实现：
  1) 正向：uid_str -> crc32 hex（用于验证）
  2) 逆向：mid_hash -> 可能的 uid（暴力搜索 + 表缓存）
缓存实测 8~9 位 UID 命中率高；10 位以上可能失败。
"""
from __future__ import annotations

import logging
import struct
import zlib
from collections.abc import Iterable

logger = logging.getLogger(__name__)


def uid_to_mid_hash(uid: int) -> str:
    """UID -> mid_hash（正向校验用）。"""
    raw = str(uid).encode("utf-8")
    return format(zlib.crc32(raw) & 0xFFFFFFFF, "08x")


def build_forward_table(uids: Iterable[int]) -> dict[str, int]:
    """由已知 uid 集合构建 {mid_hash: uid} 正向表。

    原理：B站弹幕的 mid_hash 是 uid 的 CRC32。（单向、不可解码，
    所以用"评论里已知的 uid"反向凑出哈希倒排索引，命中即可回填。）
    """
    table: dict[str, int] = {}
    for uid in uids:
        h = uid_to_mid_hash(uid)
        table.setdefault(h, uid)
    return table


def crack(mid_hash: str, uid_max: int = 200_000_000) -> int | None:
    """暴力反查 uid：遍历 0..uid_max 找到首个 crc32(uid) == mid_hash。

    说明：CRC32 是单向的，只能靠穷举；uid_max 越大耗时越长。
    反向算法（像 crc2mid 那样的表优化）可作为后续增强。
    """
    target = int(mid_hash, 16)
    for uid in range(uid_max + 1):
        if (zlib.crc32(str(uid).encode()) & 0xFFFFFFFF) == target:
            return uid
    return None


class RainbowTable:
    """CRC32 -> UID 预计算表（离线生成）。

    生成方式：遍历目标 UID 范围（如 0..20_000_000），
    计算 crc32(str(uid))，存 uid -> mid_hash 或倒排 hash -> uid。
    用于快速反查 while 较大时减少暴力范围。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path

    def load(self) -> dict[str, int]:
        """从文件加载 {mid_hash: uid}。磁盘格式: 每行 `mid_hash,uid`。"""
        table: dict[str, int] = {}
        if not self.path:
            return table
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    mid_hash, uid = line.split(",")
                    table[mid_hash] = int(uid)
        except FileNotFoundError:
            logger.warning("彩虹表文件不存在: %s", self.path)
        return table


def make_rainbow_table(uid_max: int, path: str) -> None:
    """离线构建彩虹表（uid_min=0 到 uid_max）。会占用时间/空间，需注意规模。"""
    uid_min = 0
    total_memory_warning = (uid_max - uid_min) * len("ffffffff,2147483647\n")
    logger.info("表大小估算: %.1f MB", total_memory_warning / 1024 / 1024)
    with open(path, "w", encoding="utf-8") as fh:
        for uid in range(uid_min, uid_max + 1):
            h = uid_to_mid_hash(uid)
            fh.write(f"{h},{uid}\n")


class UidResolver:
    """结合彩虹表 + 内存缓存 + 数据库缓存的统一反查入口。"""

    def __init__(self, table: RainbowTable | None = None, use_db_cache: bool = True) -> None:
        self.table = table
        self._mem_cache: dict[str, tuple[int, bool]] = {}
        self._use_db_cache = use_db_cache

    def resolve(self, mid_hash: str) -> tuple[int, bool]:
        """反查 mid_hash，返回 (uid, resolved)。成功 resolved=True。"""
        if not mid_hash:
            return 0, False
        # 内存缓存
        cached = self._mem_cache.get(mid_hash)
        if cached is not None:
            return cached
        # DB 缓存
        if self._use_db_cache:
            try:
                from storage.dao import get_crc_cache

                row = get_crc_cache(mid_hash)
                if row:
                    result = (row["uid"], bool(row["resolved"]))
                    self._mem_cache[mid_hash] = result
                    return result
            except Exception:
                pass
        # 彩虹表
        if self.table:
            uid = self.table.load().get(mid_hash)
            if uid is not None:
                self._mem_cache[mid_hash] = (uid, True)
                return uid, True
        # 兜底：暴力范围过小会误判，返回未解析
        return 0, False

    def cache_result(self, mid_hash: str, uid: int, resolved: bool = True) -> None:
        """写回内存 + DB 缓存。"""
        self._mem_cache[mid_hash] = (uid, resolved)
        if self._use_db_cache and resolved:
            try:
                from storage.dao import set_crc_cache

                set_crc_cache(mid_hash, uid, resolved)
            except Exception:
                pass