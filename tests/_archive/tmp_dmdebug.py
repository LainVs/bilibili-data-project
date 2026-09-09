"""临时：单测弹幕采样探测,定位静默超时。"""
from __future__ import annotations
import datetime
import logging
import sys

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from crawler.api.client import ApiClient
from crawler.api.danmaku import DanmakuFetcher

c = ApiClient()
f = DanmakuFetcher(c)
start = datetime.date(2026, 5, 18)
end = datetime.date.today()
print("start", start, "today", end, flush=True)
has = f._has_any_danmaku(38410325821, start, end)
print("has_any:", has, flush=True)