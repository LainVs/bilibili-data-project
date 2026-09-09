"""临时：用接口返回的 next_offset 直接放 offset 验证翻页。"""
from __future__ import annotations
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
from crawler.api.client import ApiClient
from crawler.api.comments import CommentFetcher

c = ApiClient()
cf = CommentFetcher(c)
oid = 116591031289981

next_offset = ""
seen = set()
for page in range(10):
    pagination = json.dumps({"offset": next_offset}, separators=(",", ":"))
    base = {
        "oid": oid,
        "type": 1,
        "mode": 3,
        "plat": 1,
        "pagination_str": pagination,
        "web_location": 1315875,
    }
    params = cf._sign(base)
    payload = c.get_json("https://api.bilibili.com/x/v2/reply/wbi/main", params=params)
    data = payload.get("data") or {}
    reps = data.get("replies") or []
    rpids = [r["rpid"] for r in reps]
    new = [x for x in rpids if x not in seen]
    seen.update(rpids)
    pr = (data.get("cursor") or {}).get("pagination_reply") or {}
    next_offset = pr.get("next_offset") or ""
    print(f"page={page} replies={len(reps)} new={len(new)} cursor_next={'Y' if next_offset else 'N'} all_count={(data.get('cursor') or {}).get('all_count')}")
    if not next_offset:
        break
    time.sleep(0.5)