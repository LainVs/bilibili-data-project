"""临时探测：索引接口的所有可能调用形式。"""
from __future__ import annotations

from crawler.api.client import ApiClient

client = ApiClient()

candidates = [
    ("https://api.bilibili.com/x/v2/dm/history/index", {"type": 1, "oid": 144541892, "month": "2020-01"}),
    ("https://api.bilibili.com/x/v2/dm/history/index", {"type": 1, "oid": 144541892, "month": "202001"}),
    ("http://api.bilibili.com/x/v2/dm/history/index", {"type": 1, "oid": 144541892, "month": "2020-01"}),
    ("https://api.bilibili.com/x/v2/dm/history/index", {"type": 1, "oid": "144541892", "month": "2020-01"}),
    ("https://api.bilibili.com/x/v2/dm/web/history/index", {"type": 1, "oid": 144541892, "month": "2020-01"}),
]
for url, params in candidates:
    try:
        payload = client.get_json(url, params=params)
        print(f"{url.split('/x/')[1]} -> code={payload.get('code')} msg={payload.get('message')} data={str(payload.get('data'))[:40]}")
    except Exception as exc:
        print(f"{url.split('/x/')[1]} -> FAIL {str(exc)[:120]}")