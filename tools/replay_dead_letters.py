"""坏账回放工具：把 storage/dead_letters/ 下写库失败的记录重新入库。

设计原则（与 async_writer 的坏账语义对齐）：
- 只做幂等重放：DAO 层本身是 upsert，重复回放不会产生脏数据；
- 默认先 dry-run 统计，加 --apply 才真正写库；
- 回放成功后把文件移到 dead_letters/replayed/，失败的原样保留便于排查；
- 全程不删数据：即使 --apply 也只移动，不 unlink。

用法:
    python -m tools.replay_dead_letters              # 只统计，不写库
    python -m tools.replay_dead_letters --apply      # 真正回放
    python -m tools.replay_dead_letters --apply --limit 50000
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEAD_DIR = Path(__file__).resolve().parent.parent / "storage" / "dead_letters"
REPLAYED_DIR = DEAD_DIR / "replayed"

# 文件名前缀 → 数据类型
KIND_MAP = {
    "comments": "comments",
    "danmaku": "danmaku",
    "users": "users",
}


def _iter_files(kinds: set[str] | None = None):
    """遍历坏账文件，按时间升序（先写的先回放）。"""
    if not DEAD_DIR.is_dir():
        return
    files = sorted(DEAD_DIR.glob("*.json"), key=lambda p: p.name)
    for f in files:
        kind = f.name.split("_", 1)[0]
        if kind not in KIND_MAP:
            continue
        if kinds and kind not in kinds:
            continue
        yield kind, f


def _load_rows(path: Path) -> list[dict]:
    """读坏账文件，兼容 {error,count,rows} 与裸数组两种历史格式。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取失败 %s: %s", path.name, exc)
        return []
    if isinstance(data, dict):
        rows = data.get("rows") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def _archive(path: Path, ok: bool) -> None:
    """回放成功后移入 replayed/；失败留在原地。"""
    if not ok:
        return
    try:
        REPLAYED_DIR.mkdir(exist_ok=True)
        shutil.move(str(path), str(REPLAYED_DIR / path.name))
    except Exception as exc:
        logger.warning("归档坏账文件失败 %s: %s", path.name, exc)


def _norm_comment(r: dict) -> dict:
    """坏账里的 comment 是 API 原始结构，补上 DAO 需要的字段。"""
    out = dict(r)
    out.pop("member", None)  # member 子结构由 _extract_users 单独处理
    return out


def _extract_users(rows: list[dict]) -> list[dict]:
    """从评论的 member 子结构提取用户维度（补上 uid 键）。"""
    users = []
    for r in rows:
        member = r.get("member")
        if not isinstance(member, dict):
            continue
        uid = member.get("uid") or r.get("uid")
        if not uid:
            continue
        u = dict(member)
        u["uid"] = uid
        users.append(u)
    return users


def replay(apply: bool = False, limit: int = 0,
           kinds: set[str] | None = None) -> dict:
    """回放坏账。apply=False 时只统计。"""
    stats = {"files": 0, "rows": 0, "ok": 0, "failed": 0,
             "by_kind": {}, "errors": []}

    pending = list(_iter_files(kinds))
    for kind, path in pending:
        rows = _load_rows(path)
        if not rows:
            continue
        if limit and stats["rows"] + len(rows) > limit:
            rows = rows[: max(0, limit - stats["rows"])]
            if not rows:
                break
        stats["files"] += 1
        stats["rows"] += len(rows)
        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + len(rows)

        if not apply:
            continue

        ok = _replay_one(kind, rows, stats)
        _archive(path, ok)
        if ok:
            stats["ok"] += len(rows)
        else:
            stats["failed"] += len(rows)
    return stats


def _replay_one(kind: str, rows: list[dict], stats: dict) -> bool:
    """单文件回放。异常不抛出，计入 stats["errors"]。"""
    try:
        from storage import dao

        if kind == "comments":
            clean = [_norm_comment(r) for r in rows]
            dao.upsert_comments(clean)
            # 评论里携带的 member 结构顺带补用户维度
            users = _extract_users(rows)
            if users:
                try:
                    dao.upsert_users(users)
                except Exception as exc:
                    logger.warning("回放用户维度失败: %s", exc)
        elif kind == "danmaku":
            dao.upsert_danmaku(rows)
        elif kind == "users":
            dao.upsert_users(rows)
        else:
            return False
        return True
    except Exception as exc:
        msg = f"{kind}: {exc}"
        logger.error("回放失败 %s", msg)
        stats["errors"].append(msg[:200])
        return False


def main(argv: list[str] | None = None) -> int:
    from crawler.logging_setup import setup_logging

    setup_logging()
    ap = argparse.ArgumentParser(description="坏账回放：重新写入失败的数据")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    ap.add_argument("--limit", type=int, default=0, help="最多回放条数（0=不限）")
    ap.add_argument("--kind", default="", help="只回放指定类型 comments/danmaku/users")
    args = ap.parse_args(argv)

    kinds = {args.kind} if args.kind else None
    stats = replay(apply=args.apply, limit=args.limit, kinds=kinds)

    print(f"坏账文件: {stats['files']} 个")
    print(f"总记录数: {stats['rows']:,} 条")
    for k, n in sorted(stats["by_kind"].items()):
        print(f"  {k}: {n:,} 条")
    if args.apply:
        print(f"回放成功: {stats['ok']:,} 条")
        print(f"回放失败: {stats['failed']:,} 条")
        if stats["errors"]:
            print("错误样本:")
            for e in stats["errors"][:5]:
                print(f"  - {e}")
    else:
        print("\n[干跑] 未写入数据库。加 --apply 真正回放。")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
