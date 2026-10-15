"""给归档分仓补 idx_ctime 索引（一次性 / 新增仓后补齐）。

用法:
    python tools/add_ctime_index.py            # 先体检，再逐个补（默认执行）
    python tools/add_ctime_index.py --dry-run  # 只看缺哪些仓，不动手
    python tools/add_ctime_index.py --db bilibili_20260902_172717  # 只补指定仓

背景:
    分仓建表用的 _COMMENT_DDL 里没有 idx_ctime，所以 `WHERE content LIKE '%词%'
    ORDER BY ctime DESC LIMIT n` 只能全表扫 + filesort，19 个仓串行累加成分钟级。
    storage/archive_sync.py 已改为「装载完成后建索引」，此脚本用于补齐历史仓。

    单仓实测（1144 万行）: 稠密词 15.0s → 0.0s，零命中词 12.8s → 49.9s。
    所以补索引后由 search.py 的密度自适应逻辑决定走索引还是走全表扫，两头不亏。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# 允许 `python tools/add_ctime_index.py` 直接运行（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymysql  # noqa: E402

from config import get_config  # noqa: E402

INDEX = "idx_ctime"
TABLE = "raw_comments"


def _connect(database: str | None = None):
    cfg = get_config().mysql_archive
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], database=database,
        charset=cfg.get("charset", "utf8mb4"),
        connect_timeout=10, read_timeout=7200, write_timeout=7200,
    )


def survey() -> list[tuple[str, int, bool]]:
    """返回 [(仓名, 估算行数, 是否已有 idx_ctime)]，按仓名升序。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name LIKE 'bilibili\\_%' ORDER BY schema_name")
            shards = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT TABLE_SCHEMA, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_NAME=%s AND TABLE_SCHEMA LIKE 'bilibili\\_%%'", (TABLE,))
            rows = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
            cur.execute(
                "SELECT DISTINCT TABLE_SCHEMA FROM information_schema.STATISTICS "
                "WHERE TABLE_NAME=%s AND INDEX_NAME=%s", (TABLE, INDEX))
            has = {r[0] for r in cur.fetchall()}
        return [(s, rows.get(s, 0), s in has) for s in shards]
    finally:
        conn.close()


def add_index(shard: str) -> tuple[bool, str]:
    conn = _connect(shard)
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE `{TABLE}` ADD INDEX {INDEX} (ctime)")
        conn.commit()
        return True, ""
    except Exception as exc:
        return False, str(exc)[:120]
    finally:
        conn.close()


def main() -> int:
    dry = "--dry-run" in sys.argv
    only = ""
    if "--db" in sys.argv:
        i = sys.argv.index("--db")
        if i + 1 < len(sys.argv):
            only = sys.argv[i + 1]

    info = survey()
    todo = [(s, n) for s, n, has in info if not has and (not only or s == only)]
    missing_rows = sum(n for _, n in todo)

    print(f"E 归档实例共 {len(info)} 个库；缺 {INDEX} 的 {len(todo)} 个")
    for s, n, has in info:
        print(f"  {s:<28} rows={n:<12,} {'OK' if has else '缺索引'}")
    print(f"\n待补 {len(todo)} 个仓，合计约 {missing_rows:,} 行"
          f"（预估新增占用 ~{missing_rows * 20 / 1024 ** 3:.2f} GB）")
    if not todo:
        print("无需处理。")
        return 0
    if dry:
        print("--dry-run：未执行。")
        return 0

    ok = 0
    for i, (s, n) in enumerate(todo, 1):
        t0 = time.time()
        good, err = add_index(s)
        if good:
            ok += 1
            print(f"[{i}/{len(todo)}] {s:<28} rows={n:<12,} {time.time() - t0:6.0f}s", flush=True)
        else:
            print(f"[{i}/{len(todo)}] {s:<28} 失败: {err}", flush=True)
    print(f"完成 {ok}/{len(todo)}。")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(main())
