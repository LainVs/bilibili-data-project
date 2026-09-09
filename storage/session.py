"""GUI 会话状态持久化：面板表单 + 上次任务断点，重启自动恢复。

- 面板表单（模式/输入/勾选）在关窗与任务启动时落盘为 session.json；
- last_task 记录最近一次启动的采集任务，done=False 表示中断，
  下次启动 GUI 时据此提示断点续采（增量模式自动跳过已完成部分）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_FILE = Path(__file__).resolve().parent / "session.json"


def load_session() -> dict:
    """读取会话状态；文件缺失/损坏时返回空 dict（不影响启动）。"""
    try:
        raw = SESSION_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("读取会话状态失败（忽略，按新会话启动）: %s", exc)
        return {}


def save_session(data: dict) -> None:
    """原子写入会话状态：先写临时文件再 os.replace，避免写一半损坏。"""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(SESSION_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SESSION_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("保存会话状态失败（忽略）: %s", exc)
