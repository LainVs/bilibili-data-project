"""B站采集速率上限扫描探针（混合接口，模拟真实负载）。

设计初衷
--------
单纯高频压一个 seg.so 端点会触发"端点级频控"(-702)，而真实采集是
多接口混打（弹幕历史 + 评论 + UP主空间分页），频率预算被分摊到多个端点，
因此"混合负载"才代表真实可达到的速率上限。

原理
----
crawler 的全局闸门每秒读取 config.global_qps，本探针在同一进程内逐档改写
该值并压测。抖动与风控退避保持与生产一致，不放大伪造计数。

用法:
  python tools/rate_limit_probe.py                # 自动挑库内目标
  python tools/rate_limit_probe.py --no-apply    # 只出报告，不改配置
  python tools/rate_limit_probe.py --max-qps 20  # 扫到 20 qps 为止
"""
from __future__ import annotations

import argparse
import datetime
import sys
import threading
import time

from concurrent.futures import ThreadPoolExecutor, as_completed

# 使 tools/ 下的脚本可 import 项目根模块
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from config import get_config
from crawler.api.client import ApiClient, ApiError
from crawler.api.space import SpaceFetcher
from crawler.api.wbi import MIXIN_KEY_ENC, enc_wbi, get_wbi_keys

HISTORY_API = "https://api.bilibili.com/x/v2/dm/web/history/seg.so"
REPLY_MAIN = "https://api.bilibili.com/x/v2/reply/wbi/main"
SPACE_VIDEO = "https://api.bilibili.com/x/space/wbi/arc/search"

# 风控/频控错误码：出现一次即说明已贴近或越过安全线
RISK_CODES = {-412, -403, -402, -702, -799}

# 探测用抖动压制（生产为 0.4~0.9，这里压小让总速率只受 global_qps 约束）
SMALL_JITTER_LO = 0.02
SMALL_JITTER_HI = 0.06


def _pick_targets(limit: int = 6) -> list[dict]:
    """从库中挑弹幕日期跨度最长、较新的视频作为轮转目标。"""
    from storage.db import get_conn

    conn = get_conn()
    rows: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.aid, v.cid, v.pubdate, v.owner_uid, v.title
                FROM meta_video v
                WHERE v.pubdate > 0
                ORDER BY (v.pubdate / 86400) DESC
                LIMIT %s
                """,
                (limit * 4,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows[:limit]


def _build_targets(rows: list[dict]) -> list[dict]:
    targets: list[dict] = []
    for r in rows:
        pub = int(r.get("pubdate") or 0) or int(time.time()) - 30 * 86400
        targets.append({
            "aid": int(r["aid"]),
            "cid": int(r.get("cid") or 0),
            "uid": int(r.get("owner_uid") or 0),
            "bvid": r.get("title") or str(r["aid"]),
            "start": datetime.date.fromtimestamp(pub),
            "end": datetime.date.today(),
        })
    return targets


def _build_jobs(targets: list[dict], kinds: set[str] | None = None) -> list[dict]:
    """构造混合请求池（弹幕历史 × N + 评论 + UP空间分页）。

    kinds：需要包含的接口集合，None 表示全部（弹幕/评论/空间）。
    """
    kinds = kinds or {"dan", "comment", "space"}
    jobs: list[dict] = []
    mids = {t["uid"] for t in targets if t["uid"]}
    if "dan" in kinds:
        for t in targets:
            # 弹幕历史：每视频轮取 60 天（覆盖 2 个月跨度）
            day = t["start"]
            guard = 0
            while guard < 60:
                jobs.append({"kind": "dan", "cid": t["cid"],
                             "date": day.strftime("%Y-%m-%d")})
                day += datetime.timedelta(days=1)
                guard += 1
    if "comment" in kinds:
        for t in targets:
            # 视频评论主接口（oid=aid) 3 页深翻
            jobs.append({"kind": "comment", "oid": t["aid"]})
            jobs.append({"kind": "comment", "oid": t["aid"]})
            jobs.append({"kind": "comment", "oid": t["aid"]})
    if "space" in kinds:
        for mid in (mids or {t["uid"] for t in targets} or {2}):
            for pn in range(1, 4):
                jobs.append({"kind": "space", "mid": mid, "pn": pn, "ps": 25})
    if not jobs:
        jobs = [{"kind": "dan", "cid": targets[0]["cid"],
                 "date": str(datetime.date.today())}]
    return jobs


class _Signer:
    """复用评论签名器的 wbi 签名（与生产一致的加签行为）。"""

    def __init__(self, client: ApiClient) -> None:
        self.cf = SpaceFetcher(client)._signer

    def sign(self, base: dict) -> dict:
        return self.cf._sign(base)


class _CachedSigner(_Signer):
    """探针专用：每 worker 只拉一次 wbi key，之后签名不再打 /nav。

    对照实验，验证"每次签名都调 /nav"是否为吞吐瓶颈。
    """

    def __init__(self, client: ApiClient) -> None:
        super().__init__(client)
        self._keys = None
        # 立即拉一次，让首次 /nav 成本进探测
        ks = get_wbi_keys(client)
        self._keys = ks or (MIXIN_KEY_ENC[:16], MIXIN_KEY_ENC[16:32])

    def sign(self, base: dict) -> dict:
        signed, _ = enc_wbi(base, *self._keys)
        return signed


def _send(client: ApiClient, signer: _Signer, job: dict):
    """按 job 类型发一次真实请求。"""
    kind = job["kind"]
    if kind == "dan":
        return client.get_bytes(
            HISTORY_API,
            params={"type": 1, "oid": job["cid"],
                    "date": job["date"], "segment_index": 1},
        )
    if kind == "comment":
        base = {"oid": job["oid"], "type": 1, "mode": 3, "plat": 1,
                "pagination_str": '{"offset":""}', "web_location": 1315875}
        return client.get_json(REPLY_MAIN, params=signer.sign(base))
    return client.get_json(SPACE_VIDEO,
                           params=signer.sign({"mid": job["mid"],
                                               "pn": job["pn"], "ps": 25}))


def _set_runtime_qps(qps: float, jitter_min: float, jitter_max: float,
                     backoff: float) -> None:
    """原地改写全局配置单例（全局闸门每次请求都读它，无需重启）。"""
    cfg = get_config()
    cfg.global_qps = qps
    cfg.request_delay_min = jitter_min
    cfg.request_delay_max = jitter_max
    cfg.risk_backoff_seconds = backoff


def _probe_tier(jobs: list[dict], qps: float, n_requests: int,
                workers: int, backoff: float,
                cache_wbi: bool = False) -> dict:
    """在固定 qps 档位下并发发 n 次真实请求，统计风控码与吞吐。"""
    _set_runtime_qps(qps, SMALL_JITTER_LO, SMALL_JITTER_HI, backoff)

    idx = 0
    n = 0
    lock = threading.Lock()
    stats = {"ok": 0, "risk": 0, "net_err": 0, "risk_errors": {}}
    wall_start = time.monotonic()

    def _worker() -> None:
        nonlocal idx, n
        client = ApiClient()
        signer = _CachedSigner(client) if cache_wbi else _Signer(client)
        while True:
            with lock:
                if n >= n_requests:
                    return
                job = jobs[idx % len(jobs)]
                idx += 1
                n += 1
            try:
                _send(client, signer, job)
                with lock:
                    stats["ok"] += 1
            except ApiError as exc:
                code = getattr(exc, "code", -1)
                with lock:
                    if code in RISK_CODES:
                        stats["risk"] += 1
                        stats["risk_errors"][code] = stats["risk_errors"].get(code, 0) + 1
                    else:
                        stats["net_err"] += 1
            except Exception:
                with lock:
                    stats["net_err"] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_worker) for _ in range(workers)]
        for _ in as_completed(futs):
            pass

    wall = time.monotonic() - wall_start
    stats["wall"] = wall
    stats["requests"] = n
    stats["qps_actual"] = round(n / wall, 2) if wall > 0 else 0.0
    return stats


def _tier_ladder(start_qps: float, max_qps: float) -> list[float]:
    """几何序列档位（约每档 ×1.4）。"""
    tiers: list[float] = []
    q = start_qps
    while q <= max_qps + 1e-9:
        tiers.append(round(q, 1))
        q *= 1.4
    return tiers


def _apply_ini(qps: float) -> None:
    """备份 settings.ini 并写回 global_qps。"""
    from config import CONFIG_FILE

    p = CONFIG_FILE
    text = p.read_text(encoding="utf-8")
    backup = p.with_suffix(".ini.bak-probe")
    backup.write_text(text, encoding="utf-8")
    out_lines: list[str] = []
    replaced = False
    for line in text.splitlines():
        if line.lstrip().startswith("global_qps"):
            out_lines.append(f"global_qps = {qps}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(f"global_qps = {qps}")
    p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"[apply] global_qps = {qps} → {p.name}（原配置备份 {backup.name}）")


def main() -> None:
    ap = argparse.ArgumentParser(description="B站混合接口速率上限扫描")
    ap.add_argument("--start-qps", type=float, default=None, help="起始档（默认当前值）")
    ap.add_argument("--max-qps", type=float, default=30.0, help="扫描上限（默认30）")
    ap.add_argument("--per-tier", type=int, default=200, help="每档请求数（默认200）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--risk-tolerate", type=int, default=2,
                    help="允许的偶然风控码数上限（默认2）")
    ap.add_argument("--cooldown", type=float, default=15.0)
    ap.add_argument("--backoff", type=float, default=None,
                    help="风控退避（默认用 settings.ini 的 risk_backoff_seconds）")
    ap.add_argument("--no-apply", action="store_true", help="只出报告不改配置")
    ap.add_argument("--kinds", default="dan,comment,space",
                    help="要压的接口子集，逗号分隔 dan/comment/space（默认全开）")
    ap.add_argument("--cache-wbi", action="store_true",
                    help="对照实验：每 worker 缓存 wbi key，签名不再打 /nav")
    args = ap.parse_args()

    cfg = get_config()
    start_qps = args.start_qps or cfg.global_qps
    backoff = args.backoff if args.backoff is not None else cfg.risk_backoff_seconds

    rows = _pick_targets(6)
    targets = _build_targets(rows) if rows else []
    if not targets:
        print("未能在库中找到候选视频。请先用 GUI 采集至少 1 个视频。")
        sys.exit(2)
    jobs = _build_jobs(targets, kinds=set(k.strip() for k in args.kinds.split(",")))

    print("=" * 62)
    print(f"轮转目标 {len(targets)} 个: " +
          ", ".join(t["bvid"] for t in targets))
    print(f"混合 job 池 {len(jobs)} 条（{args.kinds}）  每档请求 {args.per_tier}，"
          f"worker {args.workers}")
    print("=" * 62)

    tiers = _tier_ladder(start_qps, args.max_qps)
    print("扫描档位:", "  ".join(map(str, tiers)))
    print()

    # 并发能力须 > 目标 qps，否则瓶颈是抖动而非闸门
    def _workers_for(qps: float) -> int:
        return max(args.workers, int(qps / 1.2) + 1)

    safe_qps = start_qps
    report: list[dict] = []
    for tier in tiers:
        print(f"\n[probe] qps={tier}（{_workers_for(tier)} workers, backoff={backoff}s）…",
              flush=True)
        st = _probe_tier(jobs, tier,
                         n_requests=args.per_tier, workers=_workers_for(tier),
                         backoff=backoff, cache_wbi=args.cache_wbi)
        verdict = "OK"
        risk_threshold = max(2, args.per_tier // 5)
        if st["risk"] > args.risk_tolerate or st["net_err"] >= risk_threshold:
            verdict = "危险·停止"
        st["tier"] = tier
        st["verdict"] = verdict
        report.append(st)
        codes = ",".join(f"{k}x{v}" for k, v in st["risk_errors"].items()) or "-"
        print(f"   ok={st['ok']} 风控码={st['risk']} [{codes}] "
              f"网络/业务错={st['net_err']} 实际≈{st['qps_actual']} req/s"
              f"（{st['wall']:.1f}s）  {verdict}", flush=True)
        if verdict == "OK":
            safe_qps = tier
        else:
            break
        if args.cooldown > 0:
            print(f"   … 冷却 {args.cooldown}s …", flush=True)
            time.sleep(args.cooldown)

    print("\n" + "=" * 62)
    print("档位   实际req/s   风险码   网络错   判定")
    for r in report:
        codes = ",".join(f"{k}x{v}" for k, v in r["risk_errors"].items()) or "-"
        print(f"{r['tier']:>6}   {r['qps_actual']:>8}    {r['risk']:>3}     "
              f"{r['net_err']:>4}   {r['verdict']}  [{codes}]")
    print("=" * 62)
    print(f"建议安全值: global_qps = {safe_qps}")

    if not args.no_apply and safe_qps and abs(safe_qps - start_qps) > 1e-6:
        _apply_ini(safe_qps)
    else:
        print("[skip] 未写回（--no-apply 或取值未变化）")


if __name__ == "__main__":
    main()