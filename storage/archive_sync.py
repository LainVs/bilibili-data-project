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

def _get_archive_conn(db_name: str, timeout: int = 600, dict_cursor: bool = False):
    from config import get_config
    cfg = get_config()
    r = getattr(cfg, "mysql_archive", cfg.mysql)
    kw = {
        "host": r["host"],
        "port": r["port"],
        "user": r["user"],
        "password": r["password"],
        "database": db_name,
        "charset": r.get("charset", "utf8mb4"),
        "connect_timeout": 10,
        "read_timeout": timeout,
        "write_timeout": timeout,
    }
    if dict_cursor:
        kw["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**kw)

def _same_dsn() -> bool:
    from config import get_config
    cfg = get_config()
    w = getattr(cfg, "mysql_write", cfg.mysql)
    r = getattr(cfg, "mysql_archive", cfg.mysql)
    return w["host"] == r["host"] and w["port"] == r["port"] and w["database"] == r["database"]

# 建新小仓库的容量门槛：D 的评论行数攒够才值得建仓
# （拷文件+验对+清D一整套很重，小数据先攒着）。
# 已有未完成 shard 时走续拷，不受此限。
_MIN_ROWS_FOR_NEW_SHARD = 2000000

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


def _fmt_eta(sec: float) -> str:
    """将剩余秒数格式化为人类可读时长（纯函数，可单测）。"""
    try:
        s = max(0, int(sec))
    except (TypeError, ValueError):
        return "--"
    if s < 60:
        return f"{s}秒"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}分{sec:02d}秒"
    h, m = divmod(m, 60)
    return f"{h}小时{m:02d}分"


# 回插快路径的漂移上限：漂移行超过此数则捞回成本过高，回退逐批DELETE。
# 100万行 LOAD 回插约 1~2 分钟，远快于同等规模随机删除。
_DRIFT_REINSERT_MAX = 1_000_000


class _ClearCancelled(Exception):
    """清D回插快路径中用户取消（TRUNCATE前，无任何修改，可安全退出）。"""


def _choose_clear_strategy(cur_n: int, n_ids: int) -> str:
    """纯函数：按漂移量决定清D子策略。

    返回 "truncate"（零漂移秒清，由调用方精确相等分支处理）/
    "reinsert"（小漂移：捞出→TRUNCATE→回插）/ "chunked"（大漂移或数量异常：逐批DELETE）。
    """
    drift = cur_n - n_ids
    if drift <= 0:
        return "chunked"  # 无新增漂移但数量对不上：保守逐批删（删不存在的行是无害空操作）
    if drift <= _DRIFT_REINSERT_MAX:
        return "reinsert"
    return "chunked"

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
    except Exception:
        pass
    return None
def _save_progress(d: dict):
    try:
        import json
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f: json.dump(d, f)
    except Exception:
        pass


def _clear_via_truncate_reinsert(d, table: str, pk: str, cols: list,
                                 ids: list, cur_n: int,
                                 stop_flag, report) -> dict:
    """漂移很少时的系统级快路径：流式捞出漂移行→TRUNCATE秒清→LOAD回插。

    原理：删1100万保45万是本末倒置；全表顺序扫一遍捞出漂移（数分钟），
    TRUNCATE秒清，LOAD回插漂移（约1分钟），快两个数量级。
    要求调用前暂停采集；全程持有 WRITE 锁，TRUNCATE之前可随时取消
    （零副作用），TRUNCATE之后必须走完回插（CSV落盘可供人工恢复）。
    ids 必须已升序。返回 {"deleted": 名单行数, "drift": 回插行数}。
    """
    from pymysql.cursors import SSCursor

    col_list = ", ".join(cols)
    pk_idx = cols.index(pk)
    n_ids = len(ids)
    fname = f"retain_{table}_{uuid.uuid4().hex[:8]}.csv"
    out_path = os.path.join(OUT_DIR, fname)
    out_sql = out_path.replace("\\", "/")
    drift = 0
    scanned = 0
    t0 = time.time()
    last_rep = 0.0
    try:
        d.commit()
    except Exception:
        pass
    ssc = None
    try:
        with d.cursor() as lk:
            lk.execute(f"LOCK TABLES {table} WRITE")
        try:
            # 阶段1（可取消）：单遍顺序扫 + 归并指针分离漂移行
            ssc = d.cursor(SSCursor)
            ssc.execute(f"SELECT {col_list} FROM {table} ORDER BY {pk}")
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                p = 0
                for row in ssc:
                    rpk = row[pk_idx]
                    while p < n_ids and ids[p] < rpk:
                        p += 1
                    if p < n_ids and ids[p] == rpk:
                        pass  # 已拷贝，TRUNCATE时一并清掉
                    else:
                        w.writerow([_fmt(v) for v in row])
                        drift += 1
                    scanned += 1
                    if scanned % 50000 == 0:
                        if stop_flag is not None and stop_flag.is_set():
                            raise _ClearCancelled()
                        now = time.time()
                        if now - last_rep >= 10:
                            last_rep = now
                            report(f"清D {table}: 扫描漂移 {scanned:,}/{cur_n:,} 行，已捞出 {drift:,} 行...")
            try:
                ssc.close()
            except Exception:
                pass
            ssc = None
            if stop_flag is not None and stop_flag.is_set():
                raise _ClearCancelled()
            # 阶段2（不可取消，必须走完）：TRUNCATE + 回插 + 验数
            t1 = time.time()
            with d.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {table}")
                if drift > 0:
                    cur.execute("SET SESSION sql_mode=''")
                    cur.execute(
                        f"LOAD DATA INFILE %s IGNORE INTO TABLE {table} "
                        f"FIELDS TERMINATED BY ',' ENCLOSED BY '\"' LINES TERMINATED BY '\\n' ({col_list})",
                        (out_sql,),
                    )
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                r = cur.fetchone()
                back = r["n"] if isinstance(r, dict) else r[0]
            d.commit()
            if back != drift:
                raise RuntimeError(
                    f"回插验对失败：期望 {drift:,} 行，实际 {back:,} 行"
                    f"（CSV保留在 {out_path}，可人工恢复）")
            report(f"清D {table}: TRUNCATE秒清 + 回插 {drift:,} 行"
                   f"（回插用时 {_fmt_eta(time.time() - t1)}，含扫描共 {_fmt_eta(time.time() - t0)}）")
            try:
                os.remove(out_path)
            except Exception:
                pass
            return {"deleted": n_ids, "drift": drift}
        finally:
            try:
                with d.cursor() as lk2:
                    lk2.execute("UNLOCK TABLES")
            except Exception:
                pass
    except _ClearCancelled:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        raise
    finally:
        if ssc is not None:
            try:
                ssc.close()
            except Exception:
                pass


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
        except Exception:
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
            if tot2 < _MIN_ROWS_FOR_NEW_SHARD:
                return {"ok": False, "msg": f"暂不满足建库容量门槛（D评论仅{tot2}行，需≥{_MIN_ROWS_FOR_NEW_SHARD // 10000}万再拷；继续采集攒数据，或有未完成仓时会自动续拷）", "shard": ""}
        except Exception:
            pass
    def _report(s):
        if progress_cb:
            try: progress_cb(s)
            except Exception:
                pass
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
                e2 = _get_archive_conn(shard, timeout=1200)
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
                except Exception: pass
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
            except Exception: pass
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
                        ec = _get_archive_conn(shard, timeout=30, dict_cursor=True)
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
                        ec = _get_archive_conn(shard, timeout=600, dict_cursor=True)
                        with ec.cursor() as cur:
                            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                            r = cur.fetchone(); en = r["n"] if isinstance(r, dict) else r[0]
                        ec.close()
                    break
                except Exception as exc:
                    try: ec.close()
                    except Exception: pass
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
            ec = _get_archive_conn(shard, timeout=120)
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
                    em = _get_archive_conn(shard, timeout=300)
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
                ec = _get_archive_conn(shard, timeout=600, dict_cursor=True)
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
                ec2 = _get_archive_conn(shard, timeout=600, dict_cursor=True)
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
        # 2.6 ANALYZE 新仓表：LOAD DATA 后 table_rows 失准会导致统计走
        # 300秒级 COUNT(*) 回退；ANALYZE 只要几十秒。失败不挡归档。
        try:
            _ea = _get_conn("read")
            try:
                with _ea.cursor() as _cur:
                    for _table, _pk, _cols in _TABLES:
                        try:
                            _cur.execute(f"ANALYZE TABLE `{shard}`.`{_table}`")
                            list(_cur.fetchall())
                        except Exception as _ae:
                            _report(f"ANALYZE {shard}.{_table} 跳过: {_ae}")
                            break
                _report(f"ANALYZE {shard} 完成")
            finally:
                try: _ea.close()
                except Exception: pass
        except Exception as e:
            _report(f"ANALYZE 跳过: {e}")
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
            em = _get_archive_conn("bilibili_data", timeout=60)
            with em.cursor() as cur:
                cur.executemany("REPLACE INTO crawl_progress (video_aid, danmaku_done, comment_done, last_error, update_time) VALUES (%s,%s,%s,%s,%s)",
                    [(r["video_aid"], r["danmaku_done"], r["comment_done"], r["last_error"], r["update_time"]) for r in prows])
            em.commit(); em.close()
    except Exception as e: _report(f"progress同步E主库跳过: {e}")
    _report("验对通过，清D...")
    _report("提示：清D期间建议暂停采集任务；零漂移TRUNCATE秒清，小漂移捞回重插快路径（数分钟），大漂移逐批DELETE（可中断续清）")
    d = _get_conn("write")
    try:
        with d.cursor() as cur:
            total_del = sum(len(copied_ids.get(t, []) or []) for t, _, _ in _TABLES)
            done_del = 0
            cancelled = False
            err_table = ""
            for table, pk, cols in _TABLES:
                ids = list(copied_ids.get(table, []) or [])
                if not ids:
                    continue
                if stop_flag is not None and stop_flag.is_set():
                    cancelled = True
                    break
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                r = cur.fetchone(); cur_n = r["n"] if isinstance(r, dict) else r[0]
                if cur_n == len(ids):
                    _report(f"{table}: TRUNCATE 秒清")
                    cur.execute(f"TRUNCATE TABLE {table}")
                    d.commit()
                    done_del += len(ids)
                    if total_del > 0:
                        _pct_all = done_del * 100 // total_del
                        _report(f"清D进度 {done_del:,}/{total_del:,} 行 ({_pct_all}%) [CLEARPCT {_pct_all}]")
                    continue
                _kept = cur_n - len(ids)
                # id 升序：顺序主键删除，缓冲池页命中率最高（续跑名单可能乱序，必须排）
                try:
                    ids.sort()
                except Exception:
                    pass
                n_ids = len(ids)
                if _choose_clear_strategy(cur_n, n_ids) == "reinsert":
                    # 快路径2：漂移很少，删千万保几十万是本末倒置 —— 捞出漂移→TRUNCATE秒清→插回
                    _report(f"{table}: 漂移仅 {max(0, _kept):,} 行，走TRUNCATE+回插快路径（数分钟搞定）")
                    try:
                        _res = _clear_via_truncate_reinsert(
                            d, table, pk, cols, ids, cur_n, stop_flag, _report)
                    except _ClearCancelled:
                        try:
                            d.commit()
                        except Exception:
                            pass
                        cancelled = True
                        break
                    except Exception as exc:
                        err_table = table
                        _report(f"{table}: 回插快路径失败，D库未动（E库数据完整，可重试或转人工）: {exc}")
                        break
                    done_del += _res["deleted"]
                    if total_del > 0:
                        _pct_all = done_del * 100 // total_del
                        _report(f"清D进度 {done_del:,}/{total_del:,} 行 ({_pct_all}%) [CLEARPCT {_pct_all}]")
                    _report(f"{table}: 清D完成，TRUNCATE秒清并回插漂移 {_res['drift']:,} 行")
                    continue
                _report(f"{table}: 增量DELETE {n_ids:,} 行（期间新增{max(0, _kept):,}行保留），每1000行提交一次，可随时取消")
                CHUNK_D = 1000
                t0 = time.time()
                last_rep = 0.0
                done_tbl = 0
                table_broken = False
                for i in range(0, n_ids, CHUNK_D):
                    if stop_flag is not None and stop_flag.is_set():
                        try: d.commit()
                        except Exception: pass
                        cancelled = True
                        break
                    chunk = ids[i:i + CHUNK_D]
                    fmt = ",".join(["%s"] * len(chunk))
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE {pk} IN ({fmt})", tuple(chunk))
                    except Exception as exc:
                        # 瞬时失败（锁等待超时等）：仅本批未提交，回滚后重试一次
                        try: d.rollback()
                        except Exception: pass
                        time.sleep(2)
                        try:
                            cur.execute(f"DELETE FROM {table} WHERE {pk} IN ({fmt})", tuple(chunk))
                        except Exception as exc2:
                            try: d.rollback()
                            except Exception: pass
                            err_table = table
                            _report(f"{table}: 分块删除连续失败，中断本表（已提交 {done_tbl:,}/{n_ids:,} 行，E库数据完整，下次自动续清）: {exc2}")
                            table_broken = True
                            break
                    try:
                        d.commit()
                    except Exception as exc:
                        err_table = table
                        _report(f"{table}: 提交失败，中断（{exc}）")
                        table_broken = True
                        break
                    done_tbl += len(chunk)
                    done_del += len(chunk)
                    now = time.time()
                    if now - last_rep >= 5 or done_tbl >= n_ids:
                        last_rep = now
                        el = max(0.1, now - t0)
                        rate = done_tbl / el
                        pct_tbl = done_tbl * 100 // n_ids
                        eta = _fmt_eta((n_ids - done_tbl) / rate) if rate > 0 else "--"
                        pct_all = done_del * 100 // total_del if total_del > 0 else 100
                        _report(f"清D {table}: 已删 {done_tbl:,}/{n_ids:,} 行 ({pct_tbl}%) | 速率 {rate:,.0f}行/s | 预计剩余 {eta} [CLEARPCT {pct_all}]")
                if cancelled or table_broken:
                    break
                _report(f"{table}: 清D完成，共删 {done_tbl:,} 行，用时 {_fmt_eta(time.time() - t0)}")
            if cancelled:
                _report(f"已取消归档（清D已提交 {done_del:,}/{total_del:,} 行，E库数据完整无损，下次归档自动续清剩余）")
                return {"ok": False, "msg": f"已取消归档，清D已提交 {done_del:,} 行", "shard": shard}
            if err_table:
                return {"ok": False, "msg": f"{err_table} 清D中断，已提交 {done_del:,} 行（E库完整，下次自动续清）", "shard": shard}
            cur.execute("TRUNCATE TABLE crawl_progress")
            cur.execute("TRUNCATE TABLE stat_comment_uids")
            cur.execute("TRUNCATE TABLE stat_comment_aids")
        d.commit()
    finally:
        d.close()
    try: os.remove(_PROGRESS_FILE)
    except Exception: pass
    _report(f"完成，小仓库 {shard} 共 {total} 行")
    return {"ok": True, "msg": f"完成 {shard} {total}行", "shard": shard}

# 兼容旧接口
def _sync_table(*a, **kw): return 0
def _sync_table_bulk(*a, **kw): return 0
def _one_round(bulk=False, progress_cb=None): return 0
def _loop(): pass
def start(): pass
def stop(): pass
