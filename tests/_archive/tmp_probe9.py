"""临时：逐步定位 _fetch_day_all 超时点。"""
from __future__ import annotations
import datetime, time

from crawler.api.client import ApiClient, _throttle
from crawler.api.danmaku import parse_dms
import requests

client = ApiClient()
url = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"
params = {"type": 1, "oid": 144541892, "date": "2020-01-21", "segment_index": 1}

# 1) 单段请求耗时
t0 = time.time()
_throttle()
resp = requests.get(url, params=params, headers=client._headers(), timeout=15)
data = resp.content
t1 = time.time()
print(f"req ok status={resp.status_code} len={len(data)} bytes time={t1-t0:.1f}s")

# 2) 解析耗时
t2 = time.time()
dms = parse_dms(data)
t3 = time.time()
print(f"parse {len(dms)} dms time={t3-t2:.1f}s")
print("sample:", dms[0] if dms else None)