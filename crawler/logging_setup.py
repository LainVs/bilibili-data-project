"""日志配置：输出到控制台 + logs/ 文件（异步落盘，不阻塞采集线程）。

磁盘占用策略（对齐"安装体积小"的项目约定）：
- crawler.log 轮转上限 5MB × 3 份 ≈ 20MB 封顶；
- setup_logging 时在后台线程做一次陈旧日志清扫：logs/ 下已知日志模式
  且超过保留期的文件直接删除（研报 HTML 等用户数据不受影响）。
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import time

from config import ROOT_DIR

LOGS_DIR = ROOT_DIR / "logs"

# 轮转体量：单文件 5MB，保留 3 份备份（当前 + 3 备份 ≈ 20MB 封顶）
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# 杂散陈旧日志的清理：仅匹配已知运行日志文件名模式，保留期 7 天。
# 研报（report_*.html）、图表目录等用户产物一律不碰。
_STALE_PATTERNS = ("run*.out", "run*.err", "*.log", "*.log.*")
_STALE_AFTER_DAYS = 7

_listener = None


class _FlushRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """每条落盘后 flush（跑在监听线程，不阻塞采集线程），tail 日志实时可见。"""

    def emit(self, record):
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def cleanup_stale_logs(max_age_days: int = _STALE_AFTER_DAYS) -> int:
    """清扫 logs/ 下超过保留期的已知日志文件，返回删除数（异常静默）。"""
    removed = 0
    try:
        if not LOGS_DIR.is_dir():
            return 0
        cutoff = time.time() - max_age_days * 86400
        for pat in _STALE_PATTERNS:
            for f in LOGS_DIR.glob(pat):
                try:
                    # 只删"文件"且超期；stats_charts/ 等子目录与研报 HTML 不在模式内
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    continue
    except Exception:
        pass
    return removed


def _cleanup_async() -> None:
    """后台线程清扫：绝不让磁盘 IO 拖慢启动路径。"""
    import threading

    threading.Thread(target=cleanup_stale_logs, daemon=True).start()


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
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
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

        # 磁盘瘦身：超期陈旧日志后台清扫（不阻塞启动）
        _cleanup_async()
