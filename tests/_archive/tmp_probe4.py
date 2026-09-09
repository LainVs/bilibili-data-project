"""临时探测：索引接口是否需 wbi 签名。"""
from __future__ import annotations

from crawler.api.client import ApiClient
from crawler.api.wbi import enc_wbi, get_wbi_keys

client = ApiClient()

keys = get_wbi_keys(client)
if keys:
    img_key, sub_key = keys
    base = {"type": 1, "oid": 144541892, "month": "2020-01"}
    signed, _ = enc_wbi(base, img_key, sub_key)
    try:
        payload = client.get_json("https://api.bilibili.com/x/v2/dm/history/index", params=signed)
        print("signed:", payload.get("code"), payload.get("message"), str(payload.get("data"))[:80])
    except Exception as exc:
        print("signed FAIL:", exc)
else:
    print("no wbi keys")

try:
    payload = client.get_json(
        "https://api.bilibili.com/x/v2/dm/history/index",
        params={"type": 1, "oid": 144541892, "month": "2020-01"},
    )
    print("raw:", payload.get("code"), payload.get("message"), str(payload.get("data"))[:80])
except Exception as exc:
    print("raw FAIL:", exc)