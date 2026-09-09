"""临时：验证评论分页游标是否真正翻页。"""
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
oid = 116591031289981  # BV1qnLH6AEBb

seen = set()
dup = 0
next_offset = ""
for page in range(8):
    base = {
        "oid": oid,
        "type": 1,
        "mode": 3,
        "plat": 1,
        "pagination_str": cf._make_pagination(next_offset),
        "web_location": 1315875,
    }
    params = cf._sign(base)
    payload = c.get_json("https://api.bilibili.com/x/v2/reply/wbi/main", params=params)
    data = payload.get("data") or {}
    reps = data.get("replies") or []
    cursor = data.get("cursor") or {}
    pr = cursor.get("pagination_reply") or {}
    new_offset = pr.get("next_offset") or ""
    rpids = [r["rpid"] for r in reps]
    newp = len(set(rpids) - seen)
    dup += len(set(rpids) & seen)
    seen.update(rpids)
    print(f"page={page} replies={len(reps)} new_in_this_page={newp} dup_total={dup} next_offset={'Y' if new_offset else 'N'} next_len={len(str(new_offset))}")
    next_offset = new_offset
    if not next_offset:
        break
    time.sleep(0.5)