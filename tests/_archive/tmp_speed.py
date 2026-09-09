"""临时：评论链路快速自检——拉2页观察耗时与错误。"""
from __future__ import annotations
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(name)s: %(message)s")
from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher

c = ApiClient()
cf = CommentFetcher(c)
oid = 117042774546015

next_offset = ""
total = 0
for page in range(6):
    base = {
        "oid": oid,
        "type": 1,
        "mode": 3,
        "plat": 1,
        "pagination_str": cf._make_pagination(next_offset),
        "web_location": 1315875,
    }
    params = cf._sign(base)
    t0 = time.time()
    payload = c.get_json("https://api.bilibili.com/x/v2/reply/wbi/main", params=params)
    data = payload.get("data") or {}
    reps = data.get("replies") or []
    total += len(reps)
    cursor = data.get("cursor") or {}
    next_offset = (cursor.get("pagination_reply") or {}).get("next_offset") or ""
    print(f"page={page} took={time.time()-t0:.1f}s replies={len(reps)} total={total} next={'Y' if next_offset else 'N'}")
    if not next_offset:
        break
    time.sleep(0.5)