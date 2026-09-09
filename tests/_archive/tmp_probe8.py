"""临时：验证 _fetch_day_all 翻页逻辑与入库字段。"""
from __future__ import annotations
import datetime

from crawler.api.client import ApiClient
from crawler.api.danmaku import DanmakuFetcher
from crawler.pipelines.clean import normalize_danmaku

client = ApiClient()
fetcher = DanmakuFetcher(client)

# 文档示例 cid=144541892 在 2020-01-21 有大量弹幕
day = datetime.date(2020, 1, 21)
items = fetcher._fetch_day_all(144541892, day)
print(f"day={day} fetched={len(items)}")
if items:
    dm = normalize_danmaku(items[0], 84271171)
    print("first normalized:", dm)
    # 抽样统计 mid_hash 非空率
    has_hash = sum(1 for x in items if x.get("mid_hash"))
    print("mid_hash present:", has_hash, "/", len(items))