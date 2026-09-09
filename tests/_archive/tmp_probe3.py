"""临时探测：历史弹幕 seg.so 接口 + 日期索引是否可用。"""
from __future__ import annotations

from crawler.api.client import ApiClient, _throttle
import requests
import sys

sys.path.insert(0, ".")

from crawler.api.client import ApiClient

client = ApiClient()

url = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"

# 用文档里已知有弹幕的示例: oid=144541892, date=2020-01-21
for oid, date in [(144541892, "2020-01-21"), (243922477, "2015-06-01")]:
    try:
        _throttle()
        resp = requests.get(
            url,
            params={"type": 1, "oid": oid, "date": date, "segment_index": 1},
            headers=client._headers(),
            timeout=15,
        )
        ctype = resp.headers.get("Content-Type", "")
        body = resp.content[:80]
        print(f"oid={oid} date={date} status={resp.status_code} ctype={ctype} len={len(resp.content)} head={body!r}")
    except Exception as exc:
        print(f"oid={oid} date={date} FAIL: {exc}")