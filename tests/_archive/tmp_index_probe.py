"""临时探测：弹幕日期索引接口是否可用。"""
from __future__ import annotations

from crawler.api.client import ApiClient

client = ApiClient()

for oid in (279786, 243922477):
    try:
        payload = client.get_json(
            "https://api.bilibili.com/x/v2/dm/list/index",
            params={"type": 1, "oid": oid, "month": "201506"},
        )
        data = payload.get("data") or {}
        dates = data.get("dates") or data.get("x") or []
        print(f"oid={oid} code={payload.get('code')} msg={payload.get('message')} index_keys={list(data.keys())} dates_sample={list(dates)[:5]}")
    except Exception as exc:
        print(f"oid={oid} FAIL: {exc}")