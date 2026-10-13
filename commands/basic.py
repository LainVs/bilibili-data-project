"""基础命令：init（初始化库表）、version、gui（图形界面）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VERSION = "bilibili-data-project 0.1.0"


def run_init(args) -> int:
    from storage.db import init_db

    init_db()
    logger.info("数据库初始化完成")
    return 0


def run_version(args) -> int:
    print(VERSION)
    return 0


def run_gui(args) -> int:
    from gui.main_window import run as run_gui

    run_gui()
    return 0
