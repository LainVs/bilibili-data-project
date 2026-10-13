"""快速验证 B站 Cookie（登录态）是否有效。

用法:
    python tools/check_login.py

逻辑:
  - 从配置读取 cookie（env > bili_cookie.txt > settings.ini）
  - 请求 /x/web-interface/nav 判断 isLogin
  - 顺带测试评论接口权限码（-352=未登录/失效）
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许 `python tools/check_login.py` 直接运行（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from config import get_config  # noqa: E402


def check() -> None:
    cfg = get_config()
    cookie = cfg.cookie

    if not cookie:
        print("[cookie] 未配置！")
        print("  请将浏览器复制的完整 Cookie 粘贴到 config/bili_cookie.txt")
        print("  或设置环境变量 BILI_COOKIE，或填入 config/settings.ini")
        return

    headers = {
        "User-Agent": cfg.user_agents[0],
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
    }

    # ---- 1. 登录态校验 ----
    try:
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=headers, timeout=15,
        )
        nav = r.json()
        code = nav.get("code", -999)
        data = nav.get("data") or {}
        if code == 0 and data.get("isLogin"):
            print("[OK] 已登录")
            print(f"[OK] UID     : {data.get('mid')}")
            print(f"[OK] 用户名  : {data.get('uname')}")
            print(f"[OK] 等级    : {data.get('level_info', {}).get('current_level')}")
            print(f"[INFO] wbi_img: {bool((data.get('wbi_img') or {}).get('img_url'))}")
        else:
            print(f"[失败] 未登录或 Cookie 失效 (code={code}, isLogin={data.get('isLogin')})")
            print(f"       message={nav.get('message')}")
    except Exception as exc:
        print(f"[错误] nav 接口请求异常: {exc}")

    # ---- 2. 评论接口授权校验（观察 -352） ----
    try:
        r2 = requests.get(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            params={"oid": 577961621, "type": 1, "mode": 3},
            headers={**headers, "Origin": "https://www.bilibili.com"},
            timeout=15,
        )
        reply = r2.json()
        print(f"[评论接口] code={reply.get('code')} message={reply.get('message')}")
    except Exception as exc:
        print(f"[错误] 评论接口请求异常: {exc}")

    # ---- 3. Cookie 关键字段概览 ----
    for key in ("SESSDATA", "bili_jct", "DedeUserID"):
        found = any(seg.strip().startswith(key + "=") for seg in cookie.split(";"))
        print(f"[字段] {key}: {'存在' if found else '缺失'}")


if __name__ == "__main__":
    check()
