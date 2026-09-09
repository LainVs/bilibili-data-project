"""临时：快速验证 DanmakuFetcher 对已有视频的采样+逐日采集（只跑较少天数）。"""
from __future__ import annotations
import datetime

from crawler.api.client import ApiClient
from crawler.api.danmaku import DanmakuFetcher, parse_dms

client = ApiClient()
fetcher = DanmakuFetcher(client)

# BV1bW411n7fn:aid=19390885 cid=31621851 pubdate=2018-02-10
aid, cid, pubdate = 19390885, 31621851, 1518221237
start = datetime.date.fromtimestamp(pubdate)
print("start:", start, "today:", datetime.date.today())

has = fetcher._has_any_danmaku(cid, start, datetime.date.today())
print("has_any_danmaku:", has)