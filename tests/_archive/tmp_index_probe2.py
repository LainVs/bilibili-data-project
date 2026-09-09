"""临时探测：修正后的弹幕日期索引接口。"""
from __future__ import annotations

from crawler.api.client import ApiClient

client = ApiClient()

for oid in (279786, 243922477, 144541892):
    try:
        payload = client.get_json(
            "https://api.bilibili.com/x/v2/dm/history/index",
            params={"type": 1, "oid": oid, "month": "2020-01"},
        )
        data = payload.get("data")
        print(f"oid={oid} code={payload.get('code')} msg={payload.get('message')} dates={data!r}"[:160])
    except Exception as exc:
        print(f"oid={oid} FAIL: {exc}")