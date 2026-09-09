"""临时：观察评论 rcount 分布与总量。"""
from __future__ import annotations
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(name)s: %(message)s")
from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher

c = ApiClient()
cf = CommentFetcher(c)
base = {
    "oid": 117042774546015,
    "type": 1,
    "mode": 3,
    "plat": 1,
    "pagination_str": json.dumps({"offset": ""}, separators=(",", ":")),
    "web_location": 1315875,
}
params = cf._sign(base)
p = c.get_json("https://api.bilibili.com/x/v2/reply/wbi/main", params=params)
data = p.get("data") or {}
replies = data.get("replies") or []
rcounts = [r.get("rcount", 0) for r in replies]
print("page1 replies:", len(replies))
print("rcounts top:", sorted(rcounts)[::-1][:10])
print("sum rcount:", sum(rcounts))