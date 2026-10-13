"""系统体检与环境诊断工具（Doctor）。

用于一键诊断采集系统全链路健康状况：
1. Python 环境与关键依赖（requests, pymysql, matplotlib, jieba）
2. 缓冲库 D (3306) 与归档库 E (3307) 连接状态、延迟及核心表数据量
3. B 站 Cookie 有效性（登录状态、昵称、硬币、大会员）
4. 磁盘空间（工作盘与 E 盘）
5. CRC32 弹幕反查算力跑分（哈希/秒）
"""
from __future__ import annotations

import shutil
import sys
import time
from typing import Any

from config import get_config


def run_doctor() -> dict[str, Any]:
    report: dict[str, Any] = {
        "env": _check_env(),
        "db": _check_databases(),
        "cookie": _check_cookie(),
        "disk": _check_disk(),
        "benchmark": _check_benchmark(),
    }
    return report


def print_doctor_report(report: dict[str, Any] | None = None) -> None:
    if report is None:
        report = run_doctor()

    print("=" * 60)
    print("      B站弹幕/评论采集系统 - 全面体检报告 (Doctor)      ")
    print("=" * 60)

    # 1. 运行环境
    env = report["env"]
    print("\n[1] 运行环境与依赖:")
    print(f"  Python: {env['python_version']} ({env['platform']})")
    for lib, ok in env["dependencies"].items():
        status = "[OK]" if ok else "[MISSING]"
        print(f"  {lib:<14}: {status}")

    # 2. 数据库
    db = report["db"]
    print("\n[2] 数据库服务 (D / E):")
    d = db["d"]
    if d["ok"]:
        print(f"  缓冲库 D (3306): [OK] 连通 (延迟 {d['latency_ms']}ms, 版本 {d['version']}, 评论 {d.get('comments_count', 0):,} 条)")
    else:
        print(f"  缓冲库 D (3306): [FAIL] 连接失败 ({d['error']})")

    e = db["e"]
    if e["ok"]:
        print(f"  归档库 E (3307): [OK] 连通 (延迟 {e['latency_ms']}ms, 版本 {e['version']}, 分仓 {e.get('shards_count', 0)} 个)")
    else:
        print(f"  归档库 E (3307): [WARN] 未连通 ({e['error']} - 查询回退到 D)")

    # 3. Cookie
    ck = report["cookie"]
    print("\n[3] B站 Cookie 凭证状态:")
    if ck["ok"]:
        print(f"  登录状态: [OK] 已登录 - 昵称: {ck.get('uname')} (UID: {ck.get('uid')}, VIP: {'是' if ck.get('is_vip') else '否'})")
    else:
        print(f"  登录状态: [WARN] {ck['msg']}")

    # 4. 磁盘
    disk = report["disk"]
    print("\n[4] 存储空间与目录:")
    for path, info in disk.items():
        if info["exists"]:
            free_gb = round(info["free_bytes"] / (1024 ** 3), 2)
            total_gb = round(info["total_bytes"] / (1024 ** 3), 2)
            print(f"  {path:<12}: [OK] 可用 {free_gb} GB / 总计 {total_gb} GB")
        else:
            print(f"  {path:<12}: [FAIL] 不存在或不可访问")

    # 5. 算力跑分
    bm = report["benchmark"]
    print("\n[5] 算法引擎跑分 (CRC32 逆向):")
    print(f"  平均破解耗时: {bm['avg_ms']} ms/次 | 估算算力: {bm['qps']} 样本/秒")

    print("\n" + "=" * 60)
    # 汇总结论
    issues = []
    if not d["ok"]:
        issues.append("缓冲库 D 未连通")
    if not ck["ok"]:
        issues.append("B站 Cookie 未配置或已失效")
    if not env["dependencies"].get("matplotlib"):
        issues.append("缺少 matplotlib，趋势图无法绘制（pip install matplotlib）")

    if issues:
        print(f"诊断结论: 发现 {len(issues)} 项待注意问题:")
        for idx, iss in enumerate(issues, 1):
            print(f"  {idx}. {iss}")
    else:
        print("诊断结论: 全部核心组件运行正常！")
    print("=" * 60)


def _check_env() -> dict[str, Any]:
    deps = {}
    for mod in ("requests", "pymysql", "APScheduler", "matplotlib", "jieba"):
        try:
            __import__(mod)
            deps[mod] = True
        except ImportError:
            deps[mod] = False

    return {
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "dependencies": deps,
    }


def _check_databases() -> dict[str, Any]:
    import pymysql
    cfg = get_config()
    w = getattr(cfg, "mysql_write", cfg.mysql)
    r = getattr(cfg, "mysql_archive", cfg.mysql)

    def _test(conf: dict) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            conn = pymysql.connect(
                host=conf["host"], port=conf["port"], user=conf["user"],
                password=conf["password"], database=conf["database"],
                connect_timeout=4, read_timeout=5,
            )
            lat = round((time.monotonic() - t0) * 1000, 1)
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                ver = cur.fetchone()[0]
                cur.execute("SHOW TABLES LIKE 'raw_comments'")
                has_cmt = bool(cur.fetchone())
                cmt_cnt = 0
                if has_cmt:
                    cur.execute("SELECT table_rows FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='raw_comments'")
                    r = cur.fetchone()
                    cmt_cnt = int(r[0] or 0) if r else 0
                cur.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name LIKE 'bilibili_%'")
                shards = cur.fetchone()[0]
            conn.close()
            return {"ok": True, "latency_ms": lat, "version": ver,
                    "comments_count": cmt_cnt, "shards_count": shards}
        except Exception as exc:
            return {"ok": False, "error": str(exc).splitlines()[0][:120]}

    return {"d": _test(w), "e": _test(r)}


def _check_cookie() -> dict[str, Any]:
    cfg = get_config()
    cookie = cfg.cookie
    if not cookie:
        return {"ok": False, "msg": "未检测到配置的 Cookie（可在 settings.ini 或 bili_cookie.txt 配置）"}

    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
    }
    try:
        resp = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=headers, timeout=5)
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
            d = data["data"]
            return {
                "ok": True,
                "uid": d.get("mid"),
                "uname": d.get("uname"),
                "is_vip": bool(d.get("vipType", 0) > 0),
            }
        else:
            return {"ok": False, "msg": f"Cookie 无效或已过期 (code: {data.get('code')})"}
    except Exception as exc:
        return {"ok": False, "msg": f"测试网络异常: {exc}"}


def _check_disk() -> dict[str, Any]:
    res = {}
    for p in (".", "E:\\"):
        try:
            usage = shutil.disk_usage(p)
            res[p] = {"exists": True, "free_bytes": usage.free, "total_bytes": usage.total}
        except Exception:
            res[p] = {"exists": False, "free_bytes": 0, "total_bytes": 0}
    return res


def _check_benchmark() -> dict[str, Any]:
    from crc32_crack.core import crack_fast, uid_to_mid_hash
    test_uids = [546195, 12345678, 483920194, 987654321, 666666]
    hashes = [uid_to_mid_hash(u) for u in test_uids]

    t0 = time.monotonic()
    runs = 0
    for _ in range(5):
        for h in hashes:
            crack_fast(h)
            runs += 1
    cost = max(0.001, time.monotonic() - t0)
    avg = round((cost / runs) * 1000, 2)
    qps = round(runs / cost, 1)
    return {"avg_ms": avg, "qps": qps}
