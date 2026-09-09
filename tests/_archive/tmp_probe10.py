"""临时：探测各 segment_index 返回情况。"""
from __future__ import annotations
import time
import requests
from crawler.api.client import ApiClient, _throttle

client = ApiClient()
url = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"
for idx in (1, 2, 3, 4):
    _throttle()
    t0 = time.time()
    try:
        resp = requests.get(url, params={"type": 1, "oid": 144541892, "date": "2020-01-21", "segment_index": idx},
                            headers=client._headers(), timeout=15)
        print(f"idx={idx} status={resp.status_code} len={len(resp.content)} time={time.time()-t0:.1f}s")
    except Exception as exc:
        print(f"idx={idx} FAIL {type(exc).__name__}: {str(exc)[:80]}")