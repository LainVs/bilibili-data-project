"""写库解耦：采集线程只产数据，后台写线程批量落库。

背景：`up_crawl` 评论回调里每攒 200 条就同步跑两次事务
（`upsert_comments` + `upsert_users`），采集线程（即网络 I/O
线程）被 DB 阻塞，造成吞吐浪费。本模块把"积攒+落库"移出采集线程：

  writer = AsyncDbWriter()
  writer.add_comment(item) / add_danmaku(item)
  writer.flush()     # 结束前等队列落空
  writer.stop()      # 退出前关闭写线程

线程安全：锁 + 条件变量，写线程单点消费。
"""
from __future__ import annotations

import logging
import threading
import time

from crawler.pipelines.clean import build_user_records
from storage import dao

logger = logging.getLogger(__name__)

_COMMIT_BATCH = 200

# 活跃写线程注册表：close(wait=False) 后采集线程已返回，但写线程仍在
# 排空队列；强引用防止 writer 被 GC（daemon 写线程随之中断丢数据），
# 供关窗流程等待全部落库完成。
_registry: set["AsyncDbWriter"] = set()
_registry_lock = threading.Lock()


class AsyncDbWriter:
    """后台批量写库。采集回调非阻塞入队，写线程落库。"""

    def __init__(self, batch: int = _COMMIT_BATCH) -> None:
        self._batch = batch
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._comments: list[dict] = []
        self._danmaku: list[dict] = []
        self._users: list[dict] = []
        self._closed = False
        # 坏账：写不进库的批次不静默丢，记数+留样+落文件
        self._dead_counts: dict[str, int] = {"danmaku": 0, "comments": 0, "users": 0}
        self._dead_samples: dict[str, list] = {"danmaku": [], "comments": [], "users": []}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="async-db-writer")
        self._thread.start()
        with _registry_lock:
            _registry.add(self)

    def dead_stats(self) -> dict:
        """坏账统计：{kind: {"dropped": n, "samples": [...]}}，供 GUI/日志展示。"""
        with self._lock:
            return {k: {"dropped": self._dead_counts.get(k, 0),
                        "samples": list(self._dead_samples.get(k, []))[:3]}
                    for k in ("danmaku", "comments", "users")}

    @classmethod
    def any_active(cls) -> bool:
        """是否仍有写线程在排空数据（供关窗等待）。"""
        with _registry_lock:
            return bool(_registry)

    # ---- 采集线程侧（非阻塞） ----
    def add_comment(self, item: dict) -> None:
        with self._lock:
            self._comments.append(item)
            if len(self._comments) >= self._batch:
                self._cv.notify()

    def add_comment_batch(self, items: list[dict]) -> None:
        with self._lock:
            self._comments.extend(items)
            if len(self._comments) >= self._batch:
                self._cv.notify()

    def add_danmaku_batch(self, items: list[dict]) -> None:
        with self._lock:
            self._danmaku.extend(items)
            if len(self._danmaku) >= self._batch:
                self._cv.notify()

    def add_user(self, item: dict) -> None:
        with self._lock:
            self._users.append(item)
            if len(self._users) >= 500:
                self._cv.notify()

    def add_danmaku(self, item: dict) -> None:
        with self._lock:
            self._danmaku.append(item)
            if len(self._danmaku) >= self._batch:
                self._cv.notify()

    # ---- 收集侧控制 ----
    def flush(self, timeout: float = 30.0) -> None:
        """通知写线程把剩余批次写完，并等队列落空后返回。"""
        with self._lock:
            self._cv.notify_all()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not (self._comments or self._danmaku or self._users):
                    return
            time.sleep(0.05)
        logger.warning("异步写线程队列未排空超时")

    def close(self, wait: bool = True) -> None:
        """关闭写线程。

        wait=True：先 flush 排空队列再停线程（任务正常完成语义）；
        wait=False：立即标记关闭并返回，写线程继续把剩余队列写完
        （用于“用户停止”：采集线程秒退，数据不丢，进程存活期间落库）。
        """
        if wait:
            self.flush()
        with self._lock:
            self._closed = True
            self._cv.notify()
        self._thread.join(timeout=5)

    def is_idle(self) -> bool:
        """队列是否已排空（供关窗等待：数据全部落库才算安全退出）。"""
        with self._lock:
            return not (self._comments or self._danmaku or self._users)

    # ---- 写线程内部 ----
    def _run(self) -> None:
        try:
            while True:
                with self._lock:
                    while not (self._comments or self._danmaku or self._users) \
                            and not self._closed:
                        self._cv.wait(0.5)
                    if self._closed and not (self._comments or self._danmaku
                                             or self._users):
                        return
                    comments = self._comments
                    danmaku = self._danmaku
                    users = self._users
                    self._comments = []
                    self._danmaku = []
                    self._users = []
                self._commit(comments, danmaku, users)
        finally:
            with _registry_lock:
                _registry.discard(self)

    def _dead_letter(self, kind: str, rows: list, exc: Exception) -> None:
        """坏账：计数+留样（内存只留前 3 条样）+全量落文件，不静默丢。"""
        try:
            with self._lock:
                self._dead_counts[kind] = self._dead_counts.get(kind, 0) + len(rows)
                keep = self._dead_samples.setdefault(kind, [])
                for r in rows[:3]:
                    if len(keep) < 3:
                        try:
                            keep.append({k: (str(v)[:80] if isinstance(v, str) and len(v) > 80 else v)
                                         for k, v in (r.items() if isinstance(r, dict) else {})})
                        except Exception:
                            keep.append({"_repr": str(r)[:200]})
            # 全量落文件（best-effort，坏账目录按日封顶 50 个文件防爆盘）
            import json as _json
            from pathlib import Path as _Path
            d = _Path(__file__).with_name("dead_letters")
            d.mkdir(exist_ok=True)
            try:
                olds = sorted(d.glob(f"{kind}_*.json"))
                for p in olds[:-49]:
                    try: p.unlink()
                    except Exception: pass
            except Exception:
                pass
            fp = d / f"{kind}_{time.strftime('%Y%m%d_%H%M%S')}_{len(rows)}.json"
            try:
                fp.write_text(_json.dumps({"error": str(exc)[:500], "count": len(rows),
                                           "rows": rows[:200]}, ensure_ascii=False, default=str),
                              encoding="utf-8")
            except Exception:
                pass
            logger.warning("写库失败已入坏账(%s %s 条): %s（见 %s）",
                           kind, len(rows), exc, fp.name)
        except Exception as e2:
            logger.warning("坏账记录失败(%s): %s", kind, e2)

    def _commit(self, comments, danmaku, users) -> None:
        """批量落库；失败进坏账（计数+留样+落文件），不静默丢整批。

        成功后顺手把 uid 直播喂进 E 全局去重表（best-effort，E 离线自跳，
        归档 hook 会补，不影响主链）。
        """
        try:
            if danmaku:
                dao.upsert_danmaku(danmaku)
        except Exception as exc:
            self._dead_letter("danmaku", danmaku, exc)
        try:
            if comments:
                dao.upsert_comments(comments)
                try:
                    dao.add_global_comment_uids([c.get("uid", 0) for c in comments
                                                 if isinstance(c, dict)])
                except Exception:
                    pass
        except Exception as exc:
            self._dead_letter("comments", comments, exc)
        if comments:
            try:
                recs = build_user_records(comments, [])
                dao.upsert_users(recs)
                try:
                    dao.add_global_users([(r.get("uid", 0), r.get("uname", ""))
                                          for r in recs if isinstance(r, dict)])
                except Exception:
                    pass
            except Exception as exc:
                try:
                    self._dead_letter("users", build_user_records(comments, []), exc)
                except Exception:
                    logger.warning("用户维度写入失败: %s", exc)
        if users:
            try:
                dao.upsert_users(users)
                try:
                    dao.add_global_users([(u.get("uid", 0), u.get("uname", ""))
                                          for u in users if isinstance(u, dict)])
                except Exception:
                    pass
            except Exception as exc:
                self._dead_letter("users", users, exc)