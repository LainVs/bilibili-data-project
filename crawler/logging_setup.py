"""日志配置：输出到控制台 + logs/ 文件。"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from config import ROOT_DIR

LOGS_DIR = ROOT_DIR / "logs"


def setup_logging(level: int = logging.INFO) -> None:
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
        file_handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / "crawler.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)