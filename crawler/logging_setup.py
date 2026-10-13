"""日志配置：输出到控制台 + logs/ 文件（异步落盘，不阻塞采集线程）。"""
from __future__ import annotations

import logging
import logging.handlers
import queue

from config import ROOT_DIR

LOGS_DIR = ROOT_DIR / "logs"

_listener = None


class _FlushRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """每条落盘后 flush（跑在监听线程，不阻塞采集线程），tail 日志实时可见。"""

    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def setup_logging(level: int = logging.INFO) -> None:
    """异步日志：调用方线程只入队，专职监听线程刷盘。"""
    global _listener
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if not root.handlers:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)

        LOGS_DIR.mkdir(exist_ok=True)
        file_handler = _FlushRotatingFileHandler(
            LOGS_DIR / "crawler.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        # 文件落盘走后台监听线程（daemon，随进程退出，不丢已入队记录）
        q: queue.Queue = queue.Queue(-1)
        qh = logging.handlers.QueueHandler(q)
        root.addHandler(qh)
        global _listener
        _listener = logging.handlers.QueueListener(
            q, file_handler, respect_handler_level=True)
        _listener.start()
        # 进程退出时排空队列再走（daemon 线程否则直接被杀丢尾巴）
        import atexit as _atexit

        def _stop_listener(_l=_listener):
            try:
                _l.stop()
            except Exception:
                pass

        _atexit.register(_stop_listener)
