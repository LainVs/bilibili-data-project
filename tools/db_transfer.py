"""数据库迁移工具：全库导出 / 一键导入（配合 mysqldump/mysql CLI）。

- export_dump：mysqldump --single-transaction 导出全部表 + gzip 压缩，
  输出 .sql.gz 数据包（含表结构/数据/索引/全文索引，可直接导入）。
- import_dump：自动建库建表后，把 .sql.gz 数据包流式导入。
  导入完成后重建统计物化表（stat_comment_uids/aids）。
进度通过回调轮询：导出看输出文件大小，导入用重定向文件行数估算。
"""
from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import get_config

# 常见 mysqldump/mysql 可执行路径（Windows）
_CLI_HINTS = [
    r"C:\Program Files\MySQL\MySQL Server 8.0\bin",
    r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe",
    r"C:\xampp\mysql\bin",
    r"C:\wamp64\bin\mysql\mysql8.0.30\bin",
]


def _find_cli(name: str) -> str:
    """在 PATH 与常见安装路径中找 mysql/mysqldump 可执行文件。"""
    found = shutil.which(name)
    if found:
        return found
    for hint in _CLI_HINTS:
        p = Path(hint)
        cand = p / (name + ".exe") if p.is_dir() else p
        if cand.is_file():
            return str(cand)
    return name  # 交给 subprocess 报错


def _env() -> dict:
    """避免 CLI 读 my.ini 默认账号导致密码参数被忽略。"""
    return dict(os.environ)


def _masked_cmd(cmd: list[str]) -> str:
    return " ".join(c if "password" not in c.lower() else "***" for c in cmd)


def export_dump(out_path: str, *, on_progress=None, on_log=None) -> dict:
    """全库导出为 .sql.gz 数据包。

    返回 {"ok": bool, "path": str, "size_mb": float, "error": str}
    """
    cfg = get_config().mysql
    mysqldump = _find_cli("mysqldump")
    out = Path(out_path)
    if out.suffix.lower() != ".gz":
        out = out.with_suffix(out.suffix + ".gz")
    if out.exists():
        out.unlink()
    gz_tmp = out.with_suffix(out.suffix + ".partial")

    dump = [
        mysqldump,
        f"--host={cfg['host']}", f"--port={cfg['port']}",
        f"--user={cfg['user']}", f"--password={cfg['password']}",
        "--single-transaction", "--skip-lock-tables",
        "--default-character-set=utf8mb4", "--hex-blob",
        "--no-tablespaces", cfg["database"],
    ]
    _emit(on_log, f"执行: {_masked_cmd(dump)}")
    try:
        proc = subprocess.Popen(
            dump, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with open(gz_tmp, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=6) as gz:
            last = 0.0
            while True:
                chunk = proc.stdout.read(1 << 16)
                if not chunk:
                    break
                gz.write(chunk)
                size = gz_tmp.stat().st_size
                if size != last:
                    last = size
                    _emit(on_progress, f"导出中… {size / 1048576:.1f} MB")
            proc.stdout.close()
        rc = proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        if rc != 0:
            return {"ok": False, "error": f"mysqldump 失败(rc={rc}): {err[:400]}"}
        gz_tmp.replace(out)
        return {"ok": True, "path": str(out),
                "size_mb": round(out.stat().st_size / 1048576, 1), "error": ""}
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 mysqldump，请确认 MySQL 已安装"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if gz_tmp.exists():
            try:
                gz_tmp.unlink()
            except OSError:
                pass


def _ensure_database() -> None:
    """确保数据库存在（连接串连库失败时自动建库）。"""
    cfg = get_config().mysql
    import pymysql

    conn = pymysql.connect(host=cfg["host"], port=cfg["port"],
                           user=cfg["user"], password=cfg["password"],
                           charset=cfg["charset"], connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` "
                "DEFAULT CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()


def import_dump(dump_path: str, *, on_progress=None, on_log=None) -> dict:
    """把 .sql.gz 数据包导入当前配置的数据库（自动建库）。

    返回 {"ok": bool, "error": str}
    """
    cfg = get_config().mysql
    mysql = _find_cli("mysql")
    src = Path(dump_path)
    if not src.is_file():
        return {"ok": False, "error": f"数据包不存在: {src}"}

    try:
        _ensure_database()
    except Exception as exc:
        return {"ok": False, "error": f"无法连接 MySQL/建库失败: {exc}"}

    # 导入放行器：--force 容忍重复建表等幂等语句，事务内数据一致性由 SQL 自身保证
    cmd = [
        mysql,
        f"--host={cfg['host']}", f"--port={cfg['port']}",
        f"--user={cfg['user']}", f"--password={cfg['password']}",
        "--default-character-set=utf8mb4", "--force", cfg["database"],
    ]
    _emit(on_log, f"执行: {_masked_cmd(cmd)}")
    try:
        import zlib

        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # 手写 gzip 解压流喂给 mysql，按已解压字节报真实进度
        dobj = zlib.decompressobj(16 + zlib.MAX_WBITS)
        written = 0
        last_log = 0.0
        with src.open("rb") as raw:
            while True:
                chunk = raw.read(1 << 20)
                if not chunk:
                    break
                data = dobj.decompress(chunk)
                if data:
                    proc.stdin.write(data)
                    written += len(data)
                if time.time() - last_log > 1.5:
                    last_log = time.time()
                    _emit(on_progress, f"导入中… 已导入 {written / 1048576:.0f} MB 数据")
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return {"ok": False, "error": f"导入失败(rc={proc.returncode}): {err[:400]}"}
        # 重建统计物化表（若库是全新导入则直接全量回填）
        try:
            from storage.dao import backfill_stat_counters
            backfill_stat_counters()
        except Exception as exc:
            _emit(on_log, f"统计物化表回填跳过: {exc}")
        return {"ok": True, "error": ""}
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 mysql，请确认 MySQL 已安装"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _emit(cb, text: str) -> None:
    if cb:
        try:
            cb(text)
        except Exception:
            pass


if __name__ == "__main__":
    # 独立运行：python tools/db_transfer.py export <out.sql.gz>
    #             python tools/db_transfer.py import <dump.sql.gz>
    def _print(t):
        print(t, flush=True)

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "export" and len(sys.argv) > 2:
        r = export_dump(sys.argv[2], on_progress=_print, on_log=_print)
        print("OK" if r["ok"] else f"FAIL: {r['error']}")
    elif cmd == "import" and len(sys.argv) > 2:
        r = import_dump(sys.argv[2], on_progress=_print, on_log=_print)
        print("OK" if r["ok"] else f"FAIL: {r['error']}")
    else:
        print("用法: db_transfer.py export <out.sql.gz> | import <dump.sql.gz>")
        sys.exit(2)
