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
    engine = getattr(args, "engine", "web") or "web"
    if engine == "tk":
        from gui.main_window import run as run_tk

        run_tk()
        return 0
    from gui.webview_app import run as run_web

    return run_web()
