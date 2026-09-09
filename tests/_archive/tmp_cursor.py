"""临时：打印 cursor 结构,判断期望的 pagination_str 格式。"""
from __future__ import annotations
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher

c = ApiClient()
cf = CommentFetcher(c)
oid = 116591031289981

base = {
    "oid": oid,
    "type": 1,
    "mode": 3,
    "plat": 1,
    "pagination_str": json.dumps({"offset": ""}, separators=(",", ":")),
    "web_location": 1315875,
}
params = cf._sign(base)
payload = c.get_json("https://api.bilibili.com/x/v2/reply/wbi/main", params=params)
data = payload.get("data") or {}
print("cursor keys:", list((data.get("cursor") or {}).keys()))
print("cursor full:", json.dumps(data.get("cursor"), ensure_ascii=False)[:600])
pr = (data.get("cursor") or {}).get("pagination_reply") or {}
print("next_offset:", json.dumps(pr.get("next_offset"), ensure_ascii=False))
print("is_end:", pr.get("is_end"))
print("--- raw pagination_str we sent ---")
print(cf._make_pagination(""))