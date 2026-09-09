"""临时探测：验证 protobuf 解析对真实历史弹幕数据是否正确。"""
from __future__ import annotations
import requests

from crawler.api.client import ApiClient, _throttle
from crawler.api.danmaku import parse_dms

client = ApiClient()
url = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"

_throttle()
resp = requests.get(
    url,
    params={"type": 1, "oid": 144541892, "date": "2020-01-21", "segment_index": 1},
    headers=client._headers(),
    timeout=15,
)
data = resp.content
print(f"raw bytes len={len(data)}")
dms = parse_dms(data)
print(f"parsed {len(dms)} danmaku")
for dm in dms[:5]:
    print(dm)