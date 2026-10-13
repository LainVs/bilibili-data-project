"""CRC32 极速破解引擎单元测试。"""
import zlib

import pytest

from crc32_crack.core import (
    UidResolver,
    crack,
    crack_candidates,
    crack_fast,
    uid_to_mid_hash,
)


def test_uid_to_mid_hash():
    assert uid_to_mid_hash(1) == "83dcefb7"
    assert uid_to_mid_hash(12345) == "cbf53a1c"
    assert uid_to_mid_hash(546195) == "190a48b8"


@pytest.mark.parametrize("uid", [
    2,
    99,
    1234,
    546195,
    12345678,
    483920194,
    354655389,
])
def test_crack_fast_known_uids(uid):
    h = uid_to_mid_hash(uid)
    res = crack_fast(h)
    cands = crack_candidates(h)
    assert res is not None
    assert uid in cands
    # 检验反查结果的 CRC32 确实完全等于目标 hash
    for c in cands:
        assert (zlib.crc32(str(c).encode()) & 0xFFFFFFFF) == int(h, 16)


def test_crack_backward_compatibility():
    h = uid_to_mid_hash(546195)
    assert crack(h) == 546195


def test_crack_invalid_hash():
    assert crack_fast("") is None
    assert crack_fast("xyz") is None
    assert crack_candidates("1234567g") == []


def test_uid_resolver_auto_crack():
    resolver = UidResolver(use_db_cache=False, auto_crack=True)
    target_uid = 12345678
    h = uid_to_mid_hash(target_uid)
    uid, ok = resolver.resolve(h)
    assert ok
    assert uid == target_uid
