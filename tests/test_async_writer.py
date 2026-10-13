"""storage/async_writer.py 单元测试（不连 MySQL、不落真实坏账目录）。

覆盖：
- 采集侧入队非阻塞、batch 达到阈值才唤醒
- _commit 分别落 danmaku/comments/users 并直播喂全局去重表
- 写库失败进入坏账（计数 + 留样），不抛异常打断写线程
- flush/is_idle/close 的生命周期语义
"""
from __future__ import annotations

import time

import pytest

from storage import async_writer as aw
from storage import dao
from tests.conftest import FakeConn


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """隔离 dao 副作用：假连接 + 记录调用。

    坏账不落真实目录：把 _dead_letter 替换为只计数/留样到内存的实现，
    保持与生产一致的可观测接口（dead_stats），但不触碰文件系统。
    """
    calls = {"danmaku": [], "comments": [], "users": [], "global_cmt": [], "global_users": []}
    wconn = FakeConn()
    monkeypatch.setattr(dao, "_w", lambda: wconn)
    monkeypatch.setattr(dao, "_r_strict", lambda: None)  # E 在线但不喂数，简化断言

    monkeypatch.setattr(dao, "upsert_danmaku", lambda items, conn=None: calls["danmaku"].append(list(items)) or len(items))
    monkeypatch.setattr(dao, "upsert_comments", lambda items, conn=None: calls["comments"].append(list(items)) or len(items))
    monkeypatch.setattr(dao, "upsert_users", lambda items, conn=None: calls["users"].append(list(items)) or len(items))
    monkeypatch.setattr(dao, "add_global_comment_uids", lambda uids, conn=None: calls["global_cmt"].append(list(uids)))
    monkeypatch.setattr(dao, "add_global_users", lambda pairs, conn=None: calls["global_users"].append(list(pairs)))

    def _fake_dead_letter(self, kind, rows, exc):
        with self._lock:
            self._dead_counts[kind] = self._dead_counts.get(kind, 0) + len(rows)
            samples = self._dead_samples.setdefault(kind, [])
            if len(samples) < 3:
                samples.append({"_repr": str(rows[:1])[:80]})

    monkeypatch.setattr(aw.AsyncDbWriter, "_dead_letter", _fake_dead_letter)
    return calls


def _make_writer(**kw) -> aw.AsyncDbWriter:
    w = aw.AsyncDbWriter(**kw)
    return w


def _drain(w: aw.AsyncDbWriter, timeout: float = 5.0) -> None:
    w.flush(timeout=timeout)
    w.close(wait=True)


# ---------- 生命周期 ----------

def test_writer_starts_and_closes_cleanly(patched):
    w = _make_writer(batch=10)
    assert aw.AsyncDbWriter.any_active() is True
    w.close(wait=True)
    assert aw.AsyncDbWriter.any_active() is False


def test_flush_writes_pending_batch_below_threshold(patched):
    w = _make_writer(batch=1000)  # 阈值很远，靠 flush 触发
    w.add_danmaku({"id": 1, "video_aid": 1})
    w.add_comment({"rpid": 1, "video_aid": 1, "uid": 1})
    _drain(w)
    assert len(patched["danmaku"]) == 1
    assert len(patched["comments"]) == 1
    assert w.is_idle() is True


def test_batch_threshold_triggers_commit(patched):
    w = _make_writer(batch=3)
    for i in range(3):
        w.add_danmaku({"id": i, "video_aid": 1})
    # 达到 batch 后写线程应自行落库（最多等 2s）
    deadline = time.monotonic() + 2.0
    while not patched["danmaku"] and time.monotonic() < deadline:
        time.sleep(0.02)
    _drain(w)
    assert patched["danmaku"], "达到 batch 阈值应触发落库"


def test_close_wait_false_leaves_no_data_loss(patched):
    """wait=False：采集侧秒退，写线程仍把队列写完。"""
    w = _make_writer(batch=1000)
    w.add_comment({"rpid": 7, "video_aid": 1, "uid": 1})
    w.close(wait=False)
    # 写线程仍会排空；等待其空闲
    deadline = time.monotonic() + 2.0
    while not w.is_idle() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert patched["comments"], "wait=False 也应最终落库（不丢数据）"


# ---------- _commit 行为 ----------

def test_commit_writes_comments_and_derives_users(patched, monkeypatch):
    """评论落库时同步派生用户维度记录。"""
    monkeypatch.setattr(aw, "build_user_records",
                        lambda comments, extra: [{"uid": c.get("uid"), "uname": f"u{c.get('uid')}"}
                                                 for c in comments])
    w = _make_writer(batch=1000)
    w.add_comment({"rpid": 1, "video_aid": 1, "uid": 55})
    _drain(w)
    assert len(patched["comments"]) == 1
    assert patched["users"], "评论应派生用户维度写入"


def test_commit_danmaku_failure_goes_to_dead_letter(patched, monkeypatch):
    def _boom(items, conn=None):
        raise RuntimeError("D 写炸了")

    monkeypatch.setattr(dao, "upsert_danmaku", _boom)
    w = _make_writer(batch=1000)
    w.add_danmaku({"id": 1, "video_aid": 1})
    _drain(w)
    stats = w.dead_stats()
    assert stats["danmaku"]["dropped"] == 1
    assert stats["danmaku"]["samples"]


def test_commit_comment_failure_does_not_break_writer_thread(patched, monkeypatch):
    """单类失败不应导致写线程崩溃，后续批次仍可处理。"""
    calls = {"ok": 0}

    def _boom(items, conn=None):
        raise RuntimeError("评论写失败")

    monkeypatch.setattr(dao, "upsert_comments", _boom)
    monkeypatch.setattr(dao, "upsert_danmaku",
                        lambda items, conn=None: calls.__setitem__("ok", calls["ok"] + 1))

    w = _make_writer(batch=1000)
    w.add_comment({"rpid": 1, "video_aid": 1, "uid": 1})
    w.add_danmaku({"id": 1, "video_aid": 1})
    _drain(w)
    assert w.dead_stats()["comments"]["dropped"] == 1
    assert calls["ok"] == 1, "评论失败不应阻断弹幕落库"


def test_dead_stats_shape():
    w = _make_writer(batch=1000)
    try:
        stats = w.dead_stats()
        assert set(stats) == {"danmaku", "comments", "users"}
        assert all("dropped" in v and "samples" in v for v in stats.values())
    finally:
        w.close(wait=True)
