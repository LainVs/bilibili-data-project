"""D->E 拷文件归档：D每攒20G，拷文件在E建新小仓库，快且不怕掉盘。"""
from __future__ import annotations
import logging, threading, time, os, csv, uuid
from datetime import datetime
import pymysql
logger = logging.getLogger(__name__)
_stop = threading.Event()
_thread = None

def _get_conn(role: str):
    from storage.db import get_conn
    return get_conn(role)
def _same_dsn() -> bool:
    from config import get_config
    cfg = get_config()
    w = getattr(cfg, "mysql_write", cfg.mysql)
    r = getattr(cfg, "mysql_archive", cfg.mysql)
    return w["host"] == r["host"] and w["port"] == r["port"] and w["database"] == r["database"]

_SHARD_PREFIX = "bilibili_"
_TABLES = [
    ("meta_video", "aid", ["aid","bvid","cid","title","owner_uid","owner_name","pubdate","part","create_time"]),
    ("dim_user", "uid", ["uid","uname","level","sex","is_vip","signature","fans","update_time"]),
    ("raw_danmaku", "id", ["id","video_aid","uid","mid_hash","content","progress","mode","color","ctime","is_parsed"]),
    ("raw_comments", "rpid", ["rpid","video_aid","ref_type","parent","uid","content","like_num","ctime","ip_location"]),
]
OUT_DIR = r"C:\ProgramData\MySQL\MySQL Server 8.0\Uploads"

def _fmt(v):
    if v is None: return "\\N"
    if isinstance(v, datetime): return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)

def list_shards():
    try:
        e = _get_conn("read")
        with e.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'bilibili\\_%' ORDER BY schema_name")
            raw = cur.fetchall()
            rows = []
            for r in raw:
                if isinstance(r, dict):
                    v = r.get("schema_name") or r.get("SCHEMA_NAME") or next(iter(r.values()), None)
                else:
                    v = r[0]
                if v:
                    rows.append(v)
            rows = [r for r in rows if r != "bilibili_data"]
        e.close()
        return rows
    except Exception:
        return []

_PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "shard_progress.json")
def _load_progress():
    try:
        import json
        if os.path.exists(_PROGRESS_FILE):
            with open(_PROGRESS_FILE, encoding="utf-8") as f: return json.load(f)
    except: pass
    return None
def _save_progress(d: dict):
    try:
        import json
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f: json.dump(d, f)
    except: pass
def shard_archive(progress_cb=None, stop_flag: threading.Event | None = None) -> dict:
    if _same_dsn():
        return {"ok": False, "msg": "D与E同库，不执行"}
    if not os.path.exists("E:\\"):
        return {"ok": False, "msg": "E盘不在"}
    prev = _load_progress()
    if prev and prev.get("shard"):
        # 续上次未完成的小仓库，不建新库
        try:
            e = _get_conn("read")
            with e.cursor() as cur:
                cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s", (prev["shard"],))
                if cur.fetchone():
                    shard = prev["shard"]
                    e.close()
                else:
                    shard = datetime.now().strftime(f"{_SHARD_PREFIX}%Y%m%d_%H%M%S")
                    e.close()
            if shard == prev["shard"]:
                copied_ids = {k: v for k,v in prev.get("copied_ids", {}).items()}
                offsets = prev.get("offsets", {})
            else:
                copied_ids, offsets = {}, {}
        except:
            shard = datetime.now().strftime(f"{_SHARD_PREFIX}%Y%m%d_%H%M%S")
            copied_ids, offsets = {}, {}
    else:
        shard = datetime.now().strftime(f"{_SHARD_PREFIX}%Y%m%d_%H%M%S")
        copied_ids, offsets = {}, {}
    # 小数据不建库：先攒一攒（但已有未完成 shard 则续拷，不受门槛限）
    is_resume = bool(prev and prev.get("shard") and shard == prev.get("shard"))
    if not is_resume:
        try:
            dc2 = _get_conn("write")
            with dc2.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM raw_comments")
                r = cur.fetchone(); tot2 = r["n"] if isinstance(r, dict) else r[0]
            dc2.close()
            if tot2 < 2000000:
                return {"ok": False, "msg": f"暂不满足建库容量门槛（D仅{tot2}行，≥200万再拷）", "shard": ""}
        except: pass
    def _report(s):
        if progress_cb:
            try: progress_cb(s)
            except: pass
        logger.info(s)
    _report(f"建小仓库 {shard}")
    # 1. E建库+建表
    e = _get_conn("read")
    try:
        with e.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{shard}` DEFAULT CHARACTER SET utf8mb4")
        e.commit()
        from storage.db import _ALL_DDL
        # 在shard库建表
        for ddl in _ALL_DDL:
            # 在shard库执行
            shard_ddl = ddl.replace("CREATE TABLE IF NOT EXISTS ", f"CREATE TABLE IF NOT EXISTS `{shard}`.")
            with e.cursor() as cur:
                cur.execute(shard_ddl)
        e.commit()
    finally:
        e.close()
    # 2. 对每张表：D读→CSV→LOAD到shard（空表极快）
    # keyset 分页（WHERE pk>last）：并发写入时 OFFSET 会跳行，keyset 只漏
    # “扫描位点之前的新小主键行”（极少），且漏的留在 D 由下次归档补。
    total = sum(len(v) for v in copied_ids.values()) if 'copied_ids' in locals() and copied_ids else 0
    if 'copied_ids' not in locals() or not isinstance(copied_ids, dict):
        copied_ids = {}
    # 续跑恢复：shard 里已有 pk 即已拷（防进程重启丢内存名单），只扫一次主键。
    # 进度文件里 copied_ids 可能为空（新格式只存 keypos），有 keypos 也要恢复。
    _has_keypos = bool(prev.get("keypos")) if isinstance(prev, dict) else False
    if is_resume and (copied_ids or _has_keypos):
        try:
            _re = _get_conn("read")
            with _re.cursor() as _rcur:
                for _t, _pk, _cols in _TABLES:
                    try:
                        _rcur.execute(f"SELECT `{_pk}` FROM `{shard}`.`{_t}`")
                        _have = [(_r[_pk] if isinstance(_r, dict) else _r[0])
                                 for _r in _rcur.fetchall()]
                        if _have:
                            copied_ids[_t] = _have
                    except Exception:
                        pass
            _re.close()
            total = sum(len(v) for v in copied_ids.values())
            _report(f"续跑：shard 已有 {total} 行，从断点继续")
        except Exception as exc:
            logger.warning(f"续跑恢复 shard 名单失败: {exc}")
    keypos = {}  # 表 -> 已扫到最大 pk（只存数字，进度文件不再存全量 id）
    for table, pk, cols in _TABLES:
        if stop_flag is not None and stop_flag.is_set():
            _report("已取消归档")
            return {"ok": False, "msg": "已取消归档", "shard": shard}
        col_list = ", ".join(cols)
        fname = f"shard_{table}_{uuid.uuid4().hex[:8]}.csv"
        out_path = os.path.join(OUT_DIR, fname)
        out_sql = out_path.replace("\\", "/")
        try:
            CHUNK = 100000
            ids_all = copied_ids.get(table, [])
            # 断点：shard 已有最大 pk（无则 0）；老格式 offsets 忽略（keyset 下无意义）
            last_pk = max(ids_all) if ids_all else 0
            loaded = len(ids_all)
            base_loaded = loaded  # 续跑已有的不计入本次新增，防“共N行”翻倍显示
            while True:
                if stop_flag is not None and stop_flag.is_set():
                    _save_progress({"shard": shard, "copied_ids": {t: [] for t in copied_ids},
                                    "keypos": keypos})
                    _report("已取消归档")
                    return {"ok": False, "msg": "已取消归档", "shard": shard}
                _save_progress({"shard": shard, "copied_ids": {},
                                "keypos": dict(keypos, **{table: {"last_pk": last_pk,
                                                                  "count": loaded}})})
                d = _get_conn("write")
                with d.cursor() as cur:
                    cur.execute(f"SELECT {col_list} FROM {table} WHERE {pk} > %s ORDER BY {pk} LIMIT %s", (last_pk, CHUNK))
                    rows = cur.fetchall()
                d.close()
                if not rows:
                    if not ids_all and loaded == base_loaded:
                        _report(f"{table}: 0行跳过")
                    break
                _report(f"{table}: 拷 {loaded+1}-{loaded+len(rows)} 行...")
                with open(out_path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    for r in rows:
                        w.writerow([_fmt(r[c]) for c in cols])
                e2 = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=1200, write_timeout=600)
                with e2.cursor() as cur:
                    cur.execute("SET SESSION innodb_lock_wait_timeout=60")
                    cur.execute("SET SESSION sql_mode=''")
                    load_sql = f"LOAD DATA INFILE %s IGNORE INTO TABLE {table} FIELDS TERMINATED BY ',' ENCLOSED BY '\"' LINES TERMINATED BY '\\n' ({col_list})"
                    try:
                        cur.execute(load_sql, (out_sql,))
                    except Exception as exc:
                        if table == "dim_user" and "Unknown column 'fans'" in str(exc):
                            # 旧小仓库无 fans 列，回退无 fans 列
                            cols2 = [c for c in cols if c != "fans"]
                            col_list2 = ", ".join(cols2)
                            # 重写 CSV 去掉 fans 列
                            with open(out_path, "r", encoding="utf-8") as rf:
                                import csv as _csv
                                rows2 = list(_csv.reader(rf))
                            with open(out_path, "w", newline="", encoding="utf-8") as wf:
                                w2 = _csv.writer(wf)
                                for r in rows2:
                                    # cols 顺序含 fans，需去掉对应位
                                    idx = cols.index("fans")
                                    w2.writerow(r[:idx] + r[idx+1:])
                            load_sql2 = f"LOAD DATA INFILE %s IGNORE INTO TABLE {table} FIELDS TERMINATED BY ',' ENCLOSED BY '\"' LINES TERMINATED BY '\\n' ({col_list2})"
                            cur.execute(load_sql2, (out_sql,))
                        else:
                            raise
                e2.commit()
                e2.close()
                try: os.remove(out_path)
                except: pass
                ids_all.extend(r[pk] if isinstance(r, dict) else r[0] for r in rows)
                loaded += len(rows)
                try:
                    last_pk = max(r[pk] if isinstance(r, dict) else r[0] for r in rows)
                except Exception:
                    pass
                copied_ids[table] = ids_all
                keypos[table] = {"last_pk": last_pk, "count": loaded}
                _save_progress({"shard": shard, "copied_ids": {},
                                "keypos": dict(keypos)})
                if len(rows) < CHUNK:
                    break
            total += (loaded - base_loaded)
            if loaded:
                _report(f"{table}: 拷完 {loaded} 行")
        except Exception as exc:
            logger.warning(f"拷 {table} 失败: {exc}")
            return {"ok": False, "msg": f"{table} 失败: {exc}", "shard": shard}
        finally:
            try: os.remove(out_path)
            except: pass
    # 3. 验对（HDD COUNT(*) 易 2013 超时，改用估算+重试，超时只警告不失败）
    _report("验对行数...")
    try:
        d = _get_conn("write")
        e3 = _get_conn("read")
        need_fix = []
        for table, pk, cols in _TABLES:
            exp = len(copied_ids.get(table, []))
            en = None
            for attempt in (1, 2):
                try:
                    # 优先用 information_schema 秒出，COUNT(*) 兜底
                    if attempt == 1:
                        ec = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=30, cursorclass=pymysql.cursors.DictCursor)
                        with ec.cursor() as cur:
                            cur.execute("SELECT table_rows AS n FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (shard, table))
                            r = cur.fetchone()
                            en = int(r["n"] or 0) if r else 0
                            # 估算误差大时再 COUNT(*)
                            if en == 0 or abs(en - exp) > max(100, exp * 0.05):
                                raise ValueError("estimate off")
                        ec.close()
                        _report(f"验对 {table} 估算 {en} / 期望 {exp}")
                    else:
                        ec = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=600, cursorclass=pymysql.cursors.DictCursor)
                        with ec.cursor() as cur:
                            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                            r = cur.fetchone(); en = r["n"] if isinstance(r, dict) else r[0]
                        ec.close()
                    break
                except Exception as exc:
                    try: ec.close()
                    except: pass
                    if "2013" in str(exc) or "Lost connection" in str(exc):
                        # fail-closed：验对超时不断言成功，保留断点可续，不清 D
                        _report(f"验对 {table} 超时(2013)，判失败不清空D（断点已存，可续）")
                        return {"ok": False, "msg": f"验对 {table} 超时，未清D（可续拷 {shard}）", "shard": shard}
                    if attempt == 1:
                        continue
                    raise
            if en is None:
                en = exp
            if exp != en:
                _report(f"验对差 {table} 期望{exp} shard={en} 差{exp-en} 补漏...")
                need_fix.append((table, pk, cols))
        d.close(); e3.close()
        # 补漏：对差的表逐行补 INSERT（仅64/10行，极快）
        for table, pk, cols in need_fix:
            ids = copied_ids.get(table, [])
            # 查shard已有ids
            ec = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=120)
            with ec.cursor() as cur:
                cur.execute(f"SELECT {pk} FROM {table}")
                have = set(r[0] if not isinstance(r, dict) else list(r.values())[0] for r in cur.fetchall())
            ec.close()
            miss = [i for i in ids if i not in have]
            if not miss: continue
            # 巨型 IN 一次发会超时/超包，按 5000 分批（之前 17 万一次发直接跨掉）
            col_list = ", ".join(cols)
            placeholders = ", ".join(["%s"]*len(cols))
            _fixed = 0
            d2 = _get_conn("write")
            try:
                for _bi in range(0, len(miss), 5000):
                    if stop_flag is not None and stop_flag.is_set():
                        _report("补漏中取消")
                        return {"ok": False, "msg": "补漏中取消", "shard": shard}
                    _batch = miss[_bi:_bi+5000]
                    with d2.cursor() as cur:
                        cur.execute(f"SELECT {col_list} FROM {table} WHERE {pk} IN ({','.join(['%s']*len(_batch))})", tuple(_batch))
                        rows = cur.fetchall()
                    if not rows: continue
                    em = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=300, write_timeout=300)
                    try:
                        with em.cursor() as cur:
                            cur.executemany(f"INSERT IGNORE INTO {table} ({col_list}) VALUES ({placeholders})", [tuple(r[c] if isinstance(r, dict) else r[_ci] for _ci, c in enumerate(cols)) for r in rows])
                        em.commit()
                    finally:
                        em.close()
                    _fixed += len(rows)
                    _report(f"{table} 补漏 {_fixed}/{len(miss)} 行...")
            finally:
                d2.close()
            _report(f"{table} 补 {len(miss)} 行缺口，实际补回 {_fixed} 行")
        # 二次验对：子集校验（shard ⊇ 已拷即可清 D；多出的行不影响清 D 安全）。
        # 之前用绝对相等，仓里多 1 行（并发写入/重复触发）就误判失败。
        ok = True
        for table, pk, cols in _TABLES:
            ids = copied_ids.get(table, [])
            exp = len(ids)
            if not ids:
                continue
            if stop_flag is not None and stop_flag.is_set():
                return {"ok": False, "msg": "二次验对中取消", "shard": shard}
            try:
                ec = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=600, cursorclass=pymysql.cursors.DictCursor)
                with ec.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                    r = cur.fetchone(); en = r["n"] if isinstance(r, dict) else r[0]
                ec.close()
            except Exception as exc:
                if "2013" in str(exc) or "Lost connection" in str(exc):
                    _report(f"二次验对 {table} 超时，判失败不清空D（断点已存，可续）")
                    return {"ok": False, "msg": f"二次验对 {table} 超时，未清D（可续拷 {shard}）", "shard": shard}
                raise
            if exp == en:
                continue
            # 数量不对：分块查到底是缺还是多。缺=危险，继续失败；纯多=安全，放行。
            _missing = 0
            try:
                ec2 = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database=shard, charset="utf8mb4", connect_timeout=10, read_timeout=600, cursorclass=pymysql.cursors.DictCursor)
                with ec2.cursor() as cur:
                    for _bi in range(0, len(ids), 5000):
                        if stop_flag is not None and stop_flag.is_set():
                            ec2.close()
                            return {"ok": False, "msg": "二次验对中取消", "shard": shard}
                        _batch = ids[_bi:_bi+5000]
                        cur.execute(f"SELECT {pk} FROM {table} WHERE {pk} IN ({','.join(['%s']*len(_batch))})", tuple(_batch))
                        _have2 = set(r[pk] if isinstance(r, dict) else r[0] for r in cur.fetchall())
                        _missing += sum(1 for _i in _batch if _i not in _have2)
                        if _missing:
                            break  # 有缺即停，不必扫完
                ec2.close()
            except Exception as exc:
                if "2013" in str(exc) or "Lost connection" in str(exc):
                    _report(f"二次验对 {table} 子集检查超时，判失败不清空D")
                    return {"ok": False, "msg": f"二次验对 {table} 超时，未清D（可续拷 {shard}）", "shard": shard}
                raise
            if _missing:
                ok = False; _report(f"二次验对仍缺 {table} {_missing} 行（期望{exp} 现有{en}）")
            else:
                _report(f"二次验对 {table} 行数多 {en-exp}（期望{exp} 现有{en}），无缺失，放行")
        if not ok:
            return {"ok": False, "msg": "二次验对仍有缺失，D未清（已补漏，可重试）", "shard": shard}
        # 2.5 全局去重并集：新 shard 内容并入 E 主库 globals（只增不改，幂等）。
        # 分块+逐块提交：单条大 INSERT 在 HDD 上易超时，回滚更慢还会锁表。
        # 失败只告警不挡归档（可用回填补），成功则统计的用户数天然去重。
        try:
            from storage.db import ensure_global_uid_tables
            try:
                ensure_global_uid_tables("read")
            except Exception:
                pass
            from storage.dao import backfill_one_shard

            def _hook_prog(d, t):
                _report(f"全局去重并集 {shard} ({d}/{t})")

            r = backfill_one_shard(shard, progress_cb=_hook_prog, stop_event=stop_flag)
            _report(f"全局去重并集 {shard} 完成（dim行约{r.get('uids_add', 0)}）")
        except Exception as e:
            _report(f"全局去重并集跳过（可用回填补）: {e}")
    except Exception as exc:
        # fail-closed：验对环节任何异常都不清 D，断点已存可续
        if "2013" in str(exc) or "Lost connection" in str(exc) or "timed out" in str(exc).lower():
            _report(f"验对超时判失败，未清D（可续）: {exc}")
            return {"ok": False, "msg": f"验对超时，未清D（可续拷 {shard}）: {exc}", "shard": shard}
        else:
            return {"ok": False, "msg": f"验对异常: {exc}", "shard": shard}
    # 4. 清D
    # 同步 progress 到 E 主库（永久）
    try:
        d2 = _get_conn("write")
        with d2.cursor() as cur:
            cur.execute("SELECT video_aid, danmaku_done, comment_done, last_error, update_time FROM crawl_progress")
            prows = cur.fetchall()
        d2.close()
        if prows:
            em = pymysql.connect(host="127.0.0.1", port=3307, user="root", password="REDACTED", database="bilibili_data", charset="utf8mb4", connect_timeout=10, read_timeout=60)
            with em.cursor() as cur:
                cur.executemany("REPLACE INTO crawl_progress (video_aid, danmaku_done, comment_done, last_error, update_time) VALUES (%s,%s,%s,%s,%s)",
                    [(r["video_aid"], r["danmaku_done"], r["comment_done"], r["last_error"], r["update_time"]) for r in prows])
            em.commit(); em.close()
    except Exception as e: _report(f"progress同步E主库跳过: {e}")
    _report("验对通过，清D...")
    d = _get_conn("write")
    try:
        with d.cursor() as cur:
            for table, pk, cols in _TABLES:
                ids = copied_ids.get(table, [])
                if not ids: continue
                # 快路径：D期间无新写入则TRUNCATE秒清
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                r = cur.fetchone(); cur_n = r["n"] if isinstance(r, dict) else r[0]
                if cur_n == len(ids):
                    _report(f"{table}: TRUNCATE 秒清")
                    cur.execute(f"TRUNCATE TABLE {table}")
                else:
                    _kept = cur_n - len(ids)
                    # 估算漂移可能导致负数，钳到 0（COUNT(*) 精确，负数只可能是估算口径）
                    _report(f"{table}: 增量DELETE {len(ids)} 行（期间新增{max(0, _kept)}行保留）")
                    for i in range(0, len(ids), 1000):
                        chunk = ids[i:i+1000]
                        fmt = ",".join(["%s"]*len(chunk))
                        cur.execute(f"DELETE FROM {table} WHERE {pk} IN ({fmt})", tuple(chunk))
            cur.execute("TRUNCATE TABLE crawl_progress")
            cur.execute("TRUNCATE TABLE stat_comment_uids")
            cur.execute("TRUNCATE TABLE stat_comment_aids")
        d.commit()
    finally:
        d.close()
    try: os.remove(_PROGRESS_FILE)
    except: pass
    _report(f"完成，小仓库 {shard} 共 {total} 行")
    return {"ok": True, "msg": f"完成 {shard} {total}行", "shard": shard}

# 兼容旧接口
def _sync_table(*a, **kw): return 0
def _sync_table_bulk(*a, **kw): return 0
def _one_round(bulk=False, progress_cb=None): return 0
def _loop(): pass
def start(): pass
def stop(): pass
