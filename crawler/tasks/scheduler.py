"""定时增量调度：用 APScheduler 在进程内定时执行增量采集。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from config import get_config

logger = logging.getLogger(__name__)


def start_scheduler(client) -> BackgroundScheduler:
    """启动后台定时调度，周期性执行增量任务。

    每 increment_interval_minutes 分钟运行一次。
    """
    from crawler.tasks.increment import incremental_update

    cfg = get_config()
    scheduler = BackgroundScheduler()
    interval_minutes = max(cfg.increment_interval_minutes, 5)

    def job() -> None:
        try:
            from storage import dao

            aids = dao.list_all_video_aids()
            if not aids:
                logger.info("调度任务跳过：无已采集视频")
                return
            incremental_update(client, aids)
        except Exception as exc:
            logger.error("定时增量任务异常: %s", exc)

    scheduler.add_job(job, "interval", minutes=interval_minutes, id="increment")
    scheduler.start()
    logger.info("定时调度已启动，间隔 %s 分钟", interval_minutes)
    return scheduler