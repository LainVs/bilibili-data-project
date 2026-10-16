"""搜索子系统：评论文本/弹幕/视频的跨仓检索（LIKE + 全文索引）。"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Iterable

from storage.dao._core import _open_conn, _shard_dbs, _uid_same_dsn, logger


def _like_esc(keyword: str) -> str:
    """转义 LIKE 元字符（% _ \\），防用户输入干扰通配。"""
    return (
        keyword
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


# ---------------------------------------------------------------------------
# 检索计划选择（密度自适应）
#
# 背景：归档分仓从第一天起就没有 idx_ctime（建仓用的 _COMMENT_DDL 里没写），
# 于是 `WHERE content LIKE '%词%' ORDER BY ctime DESC LIMIT n` 只能全表扫 +
# filesort，19 个仓串行累加成分钟级（实测 192s）。补上 idx_ctime 后 MySQL 可以
# 反扫索引 + 凑够 n 条即停，但代价是**稀疏词退化成整索引遍历**。1144 万行分仓
# 实测（LIMIT 303）：
#     稠密词"的"      15.0s → 0.0s
#     中等词"哈哈"    14.3s → 0.1s
#     零命中词        12.8s → 49.9s   ← 慢 3.9 倍
# 更麻烦的是优化器对 LIKE '%x%' 的选择率一律按 11% 估，**零命中词也会自发选
# 索引计划**，所以不能指望它，必须显式给提示词。
#
# 方案：先用 idx_ctime 反扫采样 _PROBE_ROWS 行数命中数（实测 0.06s）。这段采样
# 区间正是索引计划最先走的那一段，衡量的就是"凑够 limit 条要扫多少行"，比任何
# 统计估算都准。稠密 → FORCE INDEX（早停，秒级）；稀疏 → IGNORE INDEX（维持
# 全表扫，不退化）。两头都不亏。
_CTIME_IDX = "idx_ctime"
_PROBE_ROWS = 20000         # 采样行数：实测 0.06s
_INDEX_WALK_BUDGET = 0.25   # 索引反扫最多走全表 25%（实测交叉点约 26%）

_IDX_CACHE: dict = {}       # (role, db) -> 是否有 idx_ctime
_ROWS_CACHE: dict = {}      # (role, db) -> (采样时刻, 估算行数)
_MAXCTIME_CACHE: dict = {}  # db -> (采样时刻, (是否可信, 最新 ctime))
_CACHE_TTL = 300.0

# ---------------------------------------------------------------------------
# 检索结果进程内缓存
#
# 稀疏/零命中词是重灾区：无索引可早停，必须把 19 个仓全表扫一遍（机械硬盘上
# 十几分钟），而"同一个词再搜一次"是完全重复的昂贵劳动。这里用一个小容量
# LRU + 短 TTL 缓存**完整且无失败仓**的结果：
#   - 只缓存"完整结果"：早停（total=None，截断）或有失败仓时不缓存，
#     否则会把不完整结果固化。
#   - TTL 短（120s）：D 缓冲还在持续写入，过期即失效，避免陈旧。
#   - 容量小（64）：避免长期占内存。
_SEARCH_CACHE_TTL = 120.0
_SEARCH_CACHE_MAX = 64
_SEARCH_CACHE: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()


def _search_cache_key(kind: str, keyword: str, start: str, end: str,
                      exclude: str, limit: int, min_like: int) -> tuple:
    return (kind, (keyword or "").strip(), start or "", end or "",
            exclude or "", int(limit or 0), int(min_like or 0))


def _search_cache_get(key: tuple):
    hit = _SEARCH_CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if time.monotonic() - ts > _SEARCH_CACHE_TTL:
        _SEARCH_CACHE.pop(key, None)
        return None
    _SEARCH_CACHE.move_to_end(key)
    return payload


def _search_cache_put(key: tuple, payload: dict) -> None:
    _SEARCH_CACHE[key] = (time.monotonic(), payload)
    _SEARCH_CACHE.move_to_end(key)
    while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
        _SEARCH_CACHE.popitem(last=False)


def clear_search_cache() -> int:
    """清空检索缓存（供测试与"强制刷新"用）。返回清掉的条目数。"""
    n = len(_SEARCH_CACHE)
    _SEARCH_CACHE.clear()
    return n


def _quick_query(db: str, sql: str, args: tuple = (), timeout: int = 30, role: str = "read"):
    """一次性短连接查询（用完即关）。返回首行或 None。

    这里刻意不用 _rcached：检索跑在每次搜索新建的 worker 线程里，线程本地
    连接池会随线程数累积（19 个仓 × 每个搜索线程），短连接更省连接数。
    """
    conn = _open_conn(role, db, timeout)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _has_ctime_index(cur, db: str, role: str) -> bool:
    """该仓 raw_comments 是否已有 idx_ctime（进程内缓存）。

    缓存键必须带 role：`bilibili_data` 这个名字在 D(写缓冲) 与 E(主库) 上
    都存在，是两个不同的物理库，索引情况可能不一样。
    """
    key = (role, db)
    hit = _IDX_CACHE.get(key)
    if hit is not None:
        return hit
    ok = False
    try:
        cur.execute(
            "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
            "AND TABLE_NAME='raw_comments' AND INDEX_NAME=%s LIMIT 1", (db, _CTIME_IDX))
        ok = cur.fetchone() is not None
    except Exception:
        ok = False
    _IDX_CACHE[key] = ok
    return ok


def _table_rows(db: str, role: str) -> int:
    """该仓 raw_comments 估算行数（information_schema，缓存 5 分钟）。"""
    key = (role, db)
    now = time.monotonic()
    hit = _ROWS_CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    n = 0
    try:
        row = _quick_query(db, "SELECT TABLE_ROWS AS n FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='raw_comments'", (db,), 60, role)
        n = int((row.get("n") if isinstance(row, dict) else row[0]) or 0)
    except Exception:
        n = 0
    _ROWS_CACHE[key] = (now, n)
    return n


def _probe_hits(cur, db: str, groups: list[list[str]], cond: str, params: list,
                excl: str, excl_params: list, min_like: int) -> int:
    """用 idx_ctime 反扫采样 _PROBE_ROWS 行，数出 content 命中的行数。"""
    smart_x, smart_p = _like_cond_smart("x", groups)
    like_x, like_p = "", []
    if min_like and min_like > 0:
        like_x = " AND x.like_num >= %s"
        like_p = [int(min_like)]
    sql = (f"SELECT COUNT(*) AS n FROM ("
           f"SELECT c.content, c.like_num FROM `{db}`.raw_comments c "
           f"FORCE INDEX({_CTIME_IDX}) WHERE 1=1{cond}{excl} "
           f"ORDER BY c.ctime DESC LIMIT %s) x WHERE 1=1{smart_x}{like_x}")
    cur.execute(sql, [*params, *excl_params, _PROBE_ROWS, *smart_p, *like_p])
    row = cur.fetchone()
    return int((row.get("n") if isinstance(row, dict) else row[0]) or 0)


def _plan_hint(cur, db: str, role: str, groups: list[list[str]], cond: str, params: list,
               excl: str, excl_params: list, limit: int, min_like: int) -> str:
    """密度自适应选计划：返回可插入 FROM 子句的索引提示（可能为空串）。"""
    if not limit or limit <= 0:
        return ""
    if not _has_ctime_index(cur, db, role):
        return ""
    total = _table_rows(db, role)
    if total <= 0:
        return ""
    try:
        hits = _probe_hits(cur, db, groups, cond, params, excl, excl_params, min_like)
    except Exception as exc:
        logger.debug("密度采样失败(%s)，按全表扫: %s", db, exc)
        return ""
    if hits <= 0:
        # 采样区间一条不中：索引反扫会一路走到表尾（实测慢 3.9 倍），全表扫更划算
        return f" IGNORE INDEX({_CTIME_IDX})"
    density = hits / float(min(_PROBE_ROWS, total))
    if limit / density <= total * _INDEX_WALK_BUDGET:
        return f" FORCE INDEX({_CTIME_IDX})"
    return f" IGNORE INDEX({_CTIME_IDX})"


def _shard_max_ctime(db: str) -> tuple[bool, str | None]:
    """该仓最新评论时间（idx_ctime 反扫 1 条，O(1)）。返回 (是否可信, 值)。

    没有索引时直接判不可信 —— 不退化成全表扫 MAX(ctime)（那是十几秒）。
    """
    now = time.monotonic()
    hit = _MAXCTIME_CACHE.get(db)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    out: tuple = (False, None)
    try:
        row = _quick_query(db, f"SELECT ctime FROM raw_comments FORCE INDEX({_CTIME_IDX}) "
                               f"ORDER BY ctime DESC LIMIT 1")
        v = (row.get("ctime") if isinstance(row, dict) else row[0]) if row else None
        out = (True, str(v) if v is not None else None)
    except Exception:
        # 没索引（报错 1176）或仓不可达：判不可信，不退化成全表扫 MAX(ctime)
        out = (False, None)
    _MAXCTIME_CACHE[db] = (now, out)
    return out


def _order_shards_fresh_first(dbs: list) -> list:
    """分仓按「最新评论时间」倒序；时间未知的排最后（保持原相对顺序）。"""
    known: list = []
    unknown: list = []
    for i, db in enumerate(dbs):
        ok, mx = _shard_max_ctime(db)
        if ok and mx:
            known.append((mx, i, db))
        else:
            unknown.append((i, db))
    known.sort(key=lambda t: t[0], reverse=True)
    return [db for _, _, db in known] + [db for _, db in unknown]


def _can_stop_early(rows: list, limit: int, remaining: list, is_unlimited: bool) -> bool:
    """已凑满 limit 条、且剩余所有仓的最新评论都早于当前第 limit 名 → 可停。

    正确性：剩余仓里不可能有任何一条挤进全局前 limit 名。任一仓时间未知
    就保守返回 False（宁可多扫，不可漏结果）。
    """
    if is_unlimited or not remaining or limit <= 0 or len(rows) < limit:
        return False
    thr = sorted((str(r.get("ctime") or "") for r in rows), reverse=True)[limit - 1]
    for db in remaining:
        ok, mx = _shard_max_ctime(db)
        if not ok:
            return False
        if mx is not None and mx >= thr:
            return False
    return True


def _parse_keywords(raw: str) -> list[str]:
    """把逗号/空格分隔的关键词字符串解析为去重列表（保持顺序）。"""
    import re as _re
    parts = _re.split(r"[,，\s]+", (raw or "").strip())
    seen: set = set()
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def parse_smart_query(raw: str) -> list[list[str]]:
    """智能查询解析：空格/逗号=同时包含(AND)，OR/|=任一包含，引号=精确短语。

    例：`烂片,剧情差` -> [[烂片],[剧情差]] 的 AND（单组多词）；
        `a OR b` / `a|b` -> [[a],[b]]（多组 OR）；
        `"白厄 开箱"` -> [["白厄 开箱"]]（引号内空格不拆分）。
    返回 OR-组列表，每组为 AND 词列表；空查询返回 []。
    """
    import re as _re
    text = (raw or "").strip()
    if not text:
        return []
    # 先按 OR 分组（大小写不敏感的 OR 单词，或 | / ／ 分隔符）
    groups = _re.split(r"\s*(?:\bOR\b|\||／)\s*", text, flags=_re.IGNORECASE)
    out: list[list[str]] = []
    for g in groups:
        # 引号短语整体保留，其余按逗号/中文逗号/空白拆分
        toks = _re.findall(r'"([^"]+)"|“([^”]+)”|([^,，\s]+)', g)
        terms: list[str] = []
        for a, b, c in toks:
            t = (a or b or c).strip()
            if t and t not in terms:
                terms.append(t)
        if terms:
            out.append(terms)
    return out


def flatten_smart_query(groups: list[list[str]]) -> list[str]:
    """OR-组压平成去重词表（用于高亮/展示）。"""
    seen: set = set()
    out: list[str] = []
    for g in groups:
        for t in g:
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _like_cond_smart(alias: str, groups: list[list[str]]) -> tuple[str, list]:
    """智能查询的 LIKE 条件：组内 AND、组间 OR。"""
    parts, params = [], []
    for terms in groups:
        conds = []
        for kw in terms:
            conds.append(f"{alias}.content LIKE %s")
            params.append("%" + _like_esc(kw) + "%")
        parts.append("(" + " AND ".join(conds) + ")")
    return " AND (" + " OR ".join(parts) + ")", params


def _ft_term(keyword: str) -> str:
    """把关键词包装成 ngram 全文索引的布尔短语（含转义）。

    ngram_token_size=2：短语"残影里"会被分词为 残影/影里 两个二元组，
    与正文的二元组分词逐段比对，等效"包含子串"匹配；对于 2 字符词
    （如"残影"）即是精确包含匹配。返回 "kw" 形式（IN BOOLEAN MODE）。
    """
    kw = keyword.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{kw}"'


def _ft_usable(keyword: str) -> bool:
    """是否是 ngram 全文索引可处理的词（≥2 字符；单字走原 LIKE 兜底）。"""
    return len(keyword.strip()) >= 2


def _multi_ft_usable(keywords: list[str]) -> bool:
    """多关键词场景：全部 ≥2 字符才走 FULLTEXT，否则回退 LIKE。"""
    return bool(keywords) and all(_ft_usable(k) for k in keywords)


def _multi_like_cond(alias: str, keywords: list[str]) -> tuple[str, list]:
    """多关键词 AND LIKE 条件：每个关键词都要出现在 content 中。"""
    conds, params = [], []
    for kw in keywords:
        conds.append(f" AND {alias}.content LIKE %s")
        params.append("%" + _like_esc(kw) + "%")
    return "".join(conds), params


def _multi_ft_where(alias: str, keywords: list[str]) -> tuple[str, list]:
    """多关键词 FULLTEXT AND 条件：用 +\"kw1\" +\"kw2\" ... 布尔模式。

    ngram 全文索引下 +\"kw\" 表示该短语必须出现，多个 + 之间是 AND。
    """
    terms = " ".join(f'+{_ft_term(k)}' for k in keywords)
    return f" AND MATCH({alias}.content) AGAINST(%s IN BOOLEAN MODE)", [terms]


def _dt_cond(alias: str, start: str, end: str) -> tuple[str, tuple]:
    """时间范围条件（按每行日期字符串 ts >= start AND ts <= end 过滤）。"""
    cond, params = "", []
    if start:
        cond += f" AND {alias} >= %s"
        params.append(start)
    if end:
        cond += f" AND {alias} <= %s"
        params.append(end)
    return cond, params


def _exclude_likes(exclude: str) -> tuple[str, list]:
    """把“排除词(逗号分隔)”转成 NOT LIKE 条件。

    解决子串匹配误伤：如搜“国男”会命中“中国男人/外国男人”，
    填排除词“中国男,外国男”即可过滤。
    """
    words = [w.strip() for w in (exclude or "").split(",") if w.strip()]
    conds = []
    params: list = []
    for w in words:
        conds.append(" AND c.content NOT LIKE %s")
        params.append("%" + _like_esc(w) + "%")
    return "".join(conds), params


def _search_one_db(db: str, role: str, keyword: str, start: str, end: str, exclude: str, limit: int,
                   keywords: list[str] | None = None, groups: list[list[str]] | None = None,
                   min_like: int = 0):
    if groups is None:
        groups = [keywords] if keywords else parse_smart_query(keyword)
    groups = [g for g in groups if g]
    if not groups:
        return []
    kw_list = flatten_smart_query(groups)
    if not limit or limit <= 0:
        limit = 500000
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role=="read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"], password=base["password"], database=db, charset=base["charset"], cursorclass=pymysql.cursors.DictCursor, connect_timeout=5,
                           # 稀疏词走全表扫时，千万行仓在 HDD 上可能要几分钟；
                           # 120s 会把大仓静默判超时跳过（结果不全），对齐 db.py 的 E 读超时
                           read_timeout=600)
    try:
        with conn.cursor() as cur:
            cond, params = _dt_cond("c.ctime", start, end)
            excl, excl_params = _exclude_likes(exclude)
            like_cond = ""
            like_params: list = []
            if min_like and min_like > 0:
                like_cond = " AND c.like_num >= %s"
                like_params = [int(min_like)]
            cols = ("t.rpid, t.uid, u.uname, t.content, t.like_num, t.ctime, t.ip_location, t.video_aid, v.bvid, v.title, v.owner_uid, v.owner_name")
            # 密度自适应：给下面的 LIKE 路径挑「反扫索引」还是「全表扫」
            # （FULLTEXT 路径不给提示词，否则 MATCH 找不到全文索引）
            hint = _plan_hint(cur, db, role, groups, cond, params, excl, excl_params, limit, min_like)
            if len(groups) > 1:
                # OR 查询走 LIKE（布尔全文索引表达不了 OR-of-AND，直接 LIKE 最准）
                smart_where, smart_params = _like_cond_smart("c", groups)
                cols_c = cols.replace("t.", "c.")
                where = "1=1" + smart_where + cond + excl + like_cond
                sql = f"SELECT {cols_c} FROM {db}.raw_comments c{hint} LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                cur.execute(sql, [*smart_params, *params, *excl_params, *like_params, limit])
            elif len(kw_list) == 1 and _ft_usable(kw_list[0]):
                # 单关键词 FULLTEXT 路径（原有逻辑）
                term = _ft_term(kw_list[0])
                sub_lim = limit * 3 + 3
                inner_cols = "c.rpid, c.uid, c.content, c.like_num, c.ctime, c.ip_location, c.video_aid"
                inner = f"SELECT {inner_cols} FROM {db}.raw_comments c WHERE MATCH(c.content) AGAINST(%s IN BOOLEAN MODE)" + cond + excl + like_cond + " LIMIT %s"
                sql = f"SELECT {cols} FROM ({inner}) t LEFT JOIN {db}.meta_video v ON v.aid=t.video_aid LEFT JOIN {db}.dim_user u ON u.uid=t.uid ORDER BY t.ctime DESC LIMIT %s"
                try:
                    cur.execute(sql, [term, *params, *excl_params, *like_params, sub_lim, limit])
                except Exception:
                    pat = "%" + _like_esc(kw_list[0]) + "%"
                    where = "c.content LIKE %s" + cond + excl + like_cond
                    cols_c = cols.replace("t.", "c.")
                    sql2 = f"SELECT {cols_c} FROM {db}.raw_comments c{hint} LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                    cur.execute(sql2, [pat, *params, *excl_params, *like_params, limit])
            elif len(kw_list) >= 2 and _multi_ft_usable(kw_list):
                # 多关键词 FULLTEXT AND 路径
                ft_where, ft_params = _multi_ft_where("c", kw_list)
                inner_cols = "c.rpid, c.uid, c.content, c.like_num, c.ctime, c.ip_location, c.video_aid"
                inner = f"SELECT {inner_cols} FROM {db}.raw_comments c WHERE 1=1" + ft_where + cond + excl + like_cond + f" LIMIT {limit * 3 + 3}"
                sql = f"SELECT {cols} FROM ({inner}) t LEFT JOIN {db}.meta_video v ON v.aid=t.video_aid LEFT JOIN {db}.dim_user u ON u.uid=t.uid ORDER BY t.ctime DESC LIMIT %s"
                try:
                    cur.execute(sql, [*ft_params, *params, *excl_params, *like_params, limit])
                except Exception:
                    # FULLTEXT 失败回退 LIKE
                    fb_where, fb_params = _multi_like_cond("c", kw_list)
                    cols_c = cols.replace("t.", "c.")
                    where = "1=1" + fb_where + cond + excl + like_cond
                    sql2 = f"SELECT {cols_c} FROM {db}.raw_comments c{hint} LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                    cur.execute(sql2, [*fb_params, *params, *excl_params, *like_params, limit])
            else:
                # LIKE 路径（单关键词单字 / 多关键词回退）
                fb_where, fb_params = _multi_like_cond("c", kw_list)
                cols_c = cols.replace("t.", "c.")
                where = "1=1" + fb_where + cond + excl + like_cond
                sql = f"SELECT {cols_c} FROM {db}.raw_comments c{hint} LEFT JOIN {db}.meta_video v ON v.aid=c.video_aid LEFT JOIN {db}.dim_user u ON u.uid=c.uid WHERE {where} ORDER BY c.ctime DESC LIMIT %s"
                cur.execute(sql, [*fb_params, *params, *excl_params, *like_params, limit])
            return list(cur.fetchall())
    finally:
        conn.close()


def search_comments(keyword: str, *, start: str = "", end: str = "", exclude: str = "", limit: int = 100,
                    progress_cb=None, min_like: int = 0, stop_event=None) -> dict:
    """跨库搜评论。D 写缓冲最先查（全文索引秒出首屏），E 各仓随后逐仓回调。

    progress_cb(db_label, rows): 每完成一仓立即回调，GUI 流式展示。
    min_like: 只要点赞数 >= 该值的评论（找高赞神评）。
    stop_event: 可选 threading.Event；置位后在仓与仓之间尽早停止（不可中断
    正在执行的单仓查询，但能避免继续遍历后续仓），返回已得结果。
    查询语法：空格/逗号=同时包含，OR/|=任一包含，"引号"=精确短语。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"rows": [], "total": 0}
    groups = parse_smart_query(keyword)
    if not groups:
        return {"rows": [], "total": 0}
    is_unlimited = not limit or limit <= 0
    qlimit = 500000 if is_unlimited else limit
    # 进程内缓存：同一关键词的重复检索直接命中（稀疏词全表扫极贵，值得缓存）
    ckey = _search_cache_key("cmt", keyword, start, end, exclude, qlimit, min_like)
    cached = _search_cache_get(ckey)
    if cached is not None and not (stop_event is not None and stop_event.is_set()):
        if progress_cb:
            try: progress_cb("缓存", cached.get("rows") or [])
            except Exception: pass
        return dict(cached)
    dbs = _shard_dbs()
    # 分仓按「最新评论时间」倒序：新数据先出首屏（结果本来就是按时间倒序），
    # 且排序后一旦凑满就能整段跳过更旧的仓。无索引的仓时间未知，排最后。
    if len(dbs) > 1:
        try:
            dbs = _order_shards_fresh_first(dbs)
        except Exception as exc:
            logger.debug("分仓排序失败，按原顺序: %s", exc)
    all_rows = []
    seen: set = set()  # rpid 去重（重采视频会导致 D/E/小仓出现同一条）
    failed: list = []
    def _add(rows: list) -> None:
        for r in rows:
            k = r.get("rpid")
            if k in seen:
                continue
            seen.add(k)
            all_rows.append(r)
    # D 写缓冲优先（全文索引，最快，先画首屏；以前只在 E 零命中才查 D）
    try:
        rows = _search_one_db("bilibili_data", "write", keyword, start, end, exclude, qlimit, groups=groups, min_like=min_like)
        _add(rows)
        if progress_cb:
            try: progress_cb("D缓冲", rows)
            except Exception: pass
    except Exception as exc:
        failed.append(f"D缓冲:{exc}")
        logger.warning("搜评论 D缓冲 跳过: %s", exc)
    same_dsn = _uid_same_dsn()
    e_targets = [d for d in dbs if not (same_dsn and d == "bilibili_data")]
    # D 缓冲装的是最新数据：它一个库就凑满 limit 且所有 E 仓都更旧时，
    # 全局前 limit 名全在 D 里，连一个分仓都不用查。
    stopped = _can_stop_early(all_rows, limit, e_targets, is_unlimited)
    if stopped:
        logger.info("搜评论提前结束：D 缓冲已凑满 %d 条，全部 E 仓更旧", len(all_rows))
    for pos, db in enumerate(dbs):
        if stopped:
            break
        if stop_event is not None and stop_event.is_set():
            logger.info("搜评论被用户取消，已停于 %s 仓之前", db)
            break
        if same_dsn and db == "bilibili_data":
            continue  # 单库模式：D 与 E 主库同一物理库，避免重复
        try:
            rows = _search_one_db(db, "read", keyword, start, end, exclude, qlimit, groups=groups, min_like=min_like)
            _add(rows)
            if progress_cb:
                try: progress_cb(db, rows)
                except Exception: pass
        except Exception as exc:
            failed.append(f"{db}:{exc}")
            logger.warning("搜评论 %s 跳过: %s", db, exc)
        # 提前结束：已凑满 limit 条，且剩余仓的最新评论都更旧 → 不可能再进榜
        remaining = [d for d in dbs[pos + 1:] if not (same_dsn and d == "bilibili_data")]
        if _can_stop_early(all_rows, limit, remaining, is_unlimited):
            logger.info("搜评论提前结束：已得 %d 条，剩余 %d 个仓都更旧", len(all_rows), len(remaining))
            stopped = True
            break
    all_rows.sort(key=lambda r: r.get("ctime") or "", reverse=True)
    out = {"rows": all_rows, "total": len(all_rows)} if is_unlimited else \
        {"rows": all_rows[:limit], "total": None if len(all_rows) > limit else len(all_rows)}
    if stopped:
        # 主动截断：后面还有多少条未知，必须报「≥」而不是具体数
        out["total"] = None
    if failed:
        out["failed"] = failed
    if stop_event is not None and stop_event.is_set():
        out["cancelled"] = True
    # 仅缓存完整、无失败、非取消的评论检索结果
    if not stopped and not failed and not out.get("cancelled"):
        _search_cache_put(ckey, out)
    return out


def _exclude_likes_danmaku(exclude: str) -> tuple[str, list]:
    """同 _exclude_likes，但针对弹幕表别名 d。"""
    words = [w.strip() for w in (exclude or "").split(",") if w.strip()]
    conds, params = [], []
    for w in words:
        conds.append(" AND d.content NOT LIKE %s")
        params.append("%" + _like_esc(w) + "%")
    return "".join(conds), params


def _search_danmaku_one_db(db: str, role: str, groups: list[list[str]], start: str, end: str,
                           exclude: str, limit: int) -> list[dict]:
    """单个物理库内搜弹幕（表名带库前缀，同库内 JOIN）。"""
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=600)
    try:
        with conn.cursor() as cur:
            kw_list = flatten_smart_query(groups)
            cond, params = _dt_cond("d.ctime", start, end)
            excl, excl_params = _exclude_likes_danmaku(exclude)
            cols = ("t.id, t.uid, t.mid_hash, u.uname, t.content, t.progress, "
                    "t.ctime, t.video_aid, v.bvid, v.title, v.owner_name")
            if len(groups) > 1:
                # OR 查询：内容 OR 条件 + 发送者标识兜底（取首词）
                smart_where, smart_params = _like_cond_smart("d", groups)
                cols_d = cols.replace("t.", "d.")
                uid_pat = "%" + _like_esc(groups[0][0]) + "%"
                where = ("1=1" + smart_where + cond + excl)
                uid_where = ("(d.mid_hash LIKE %s OR d.uid LIKE %s)"
                             + cond + excl)
                sql = (
                    f"SELECT {cols_d} FROM `{db}`.raw_danmaku d "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                    f"WHERE {where} "
                    "UNION "
                    "SELECT d.id, d.uid, d.mid_hash, u.uname, d.content, "
                    "d.progress, d.ctime, d.video_aid, "
                    "v.bvid, v.title, v.owner_name "
                    f"FROM `{db}`.raw_danmaku d "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                    f"WHERE {uid_where} "
                    "ORDER BY ctime DESC LIMIT %s"
                )
                cur.execute(sql, [*smart_params, *params, *excl_params,
                                   uid_pat, uid_pat, *params, *excl_params,
                                   limit])
            elif len(kw_list) == 1 and _ft_usable(kw_list[0]):
                # 单关键词 FULLTEXT（原有逻辑）
                term = _ft_term(kw_list[0])
                sub_lim = limit * 5 + 5
                inner_cols = ("d.id, d.uid, d.mid_hash, d.content, d.progress, "
                              "d.ctime, d.video_aid")
                inner = (
                    f"SELECT {inner_cols} FROM `{db}`.raw_danmaku d "
                    "WHERE MATCH(d.content) AGAINST(%s IN BOOLEAN MODE)"
                    + cond + excl + " "
                    "LIMIT %s"
                )
                sql = (
                    "(SELECT " + cols + " FROM (" + inner + ") t "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = t.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = t.uid) "
                    "UNION "
                    "(SELECT d.id, d.uid, d.mid_hash, u.uname, d.content, "
                    "d.progress, d.ctime, d.video_aid, "
                    "v.bvid, v.title, v.owner_name "
                    f"FROM `{db}`.raw_danmaku d "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                    "WHERE (d.mid_hash LIKE %s OR d.uid LIKE %s)"
                    + cond + excl + " "
                    "ORDER BY d.ctime DESC LIMIT %s) "
                    "ORDER BY ctime DESC LIMIT %s"
                )
                pat = "%" + _like_esc(kw_list[0]) + "%"
                try:
                    cur.execute(sql, [term, *params, *excl_params, sub_lim,
                                      pat, pat, *params, *excl_params, sub_lim,
                                      limit + 1])
                except Exception:
                    # 无全文索引（已按空间考量 DROP）时回退 LIKE，
                    # 与评论单关键词路径保持一致，不让检索崩掉。
                    cols_d = cols.replace("t.", "d.")
                    sql2 = (
                        f"SELECT {cols_d} FROM `{db}`.raw_danmaku d "
                        f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                        f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                        f"WHERE d.content LIKE %s{cond}{excl} "
                        "ORDER BY d.ctime DESC LIMIT %s"
                    )
                    cur.execute(sql2, [pat, *params, *excl_params, limit + 1])
            elif len(kw_list) >= 2:
                # 多关键词：内容 ALL LIKE + mid_hash/uid 兜底
                like_where, like_params = _multi_like_cond("d", kw_list)
                cols_d = cols.replace("t.", "d.")
                # mid_hash/uid 搜第一个关键词（通常用户搜的是同一个人）
                uid_pat = "%" + _like_esc(kw_list[0]) + "%"
                where = ("1=1" + like_where + cond + excl)
                uid_where = ("(d.mid_hash LIKE %s OR d.uid LIKE %s)"
                             + cond + excl)
                sql = (
                    f"SELECT {cols_d} FROM `{db}`.raw_danmaku d "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                    f"WHERE {where} "
                    "UNION "
                    "SELECT d.id, d.uid, d.mid_hash, u.uname, d.content, "
                    "d.progress, d.ctime, d.video_aid, "
                    "v.bvid, v.title, v.owner_name "
                    f"FROM `{db}`.raw_danmaku d "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                    f"WHERE {uid_where} "
                    "ORDER BY ctime DESC LIMIT %s"
                )
                cur.execute(sql, [*like_params, *params, *excl_params,
                                  uid_pat, uid_pat, *params, *excl_params,
                                  limit + 1])
            else:
                # 单字关键词 LIKE 路径
                pat = "%" + _like_esc(kw_list[0]) + "%"
                where = ("(d.content LIKE %s OR d.mid_hash LIKE %s "
                         "OR d.uid LIKE %s)" + cond + excl)
                cols_d = cols.replace("t.", "d.")
                sql = (
                    f"SELECT {cols_d} FROM `{db}`.raw_danmaku d "
                    f"LEFT JOIN `{db}`.meta_video v ON v.aid = d.video_aid "
                    f"LEFT JOIN `{db}`.dim_user u ON u.uid = d.uid "
                    f"WHERE {where} "
                    "ORDER BY d.ctime DESC LIMIT %s"
                )
                cur.execute(sql, [pat, pat, pat, *params, *excl_params,
                                  limit + 1])
            rows = list(cur.fetchall())
            # LIMIT+1 探测截断：多 1 条说明还有更多
            return rows
    finally:
        try:
            conn.close()
        except Exception:
            pass


def search_danmaku(keyword: str, *, start: str = "", end: str = "",
                   exclude: str = "", limit: int = 100, stop_event=None) -> dict:
    """跨库搜弹幕（ngram 全文索引）或发送者标识（mid_hash/uid LIKE）。

    D 缓冲 + E 各仓逐库查，Python 按时间归并、按 id 去重。
    查询语法同 search_comments：空格/逗号=同时包含，OR/|=任一包含，"引号"=精确短语。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"rows": [], "total": 0}
    groups = parse_smart_query(keyword)
    if not groups:
        return {"rows": [], "total": 0}
    is_unlimited = not limit or limit <= 0
    qlimit = 500000 if is_unlimited else limit
    # 每库多取 1 条做截断探测（与旧单库语义一致）
    per_db = qlimit + 1
    ckey = _search_cache_key("dmk", keyword, start, end, exclude, qlimit, 0)
    cached = _search_cache_get(ckey)
    if cached is not None and not (stop_event is not None and stop_event.is_set()):
        return dict(cached)
    all_rows: list[dict] = []
    seen: set = set()
    failed: list = []
    def _add(rows: list) -> None:
        for r in rows:
            k = r.get("id")
            if k in seen:
                continue
            seen.add(k)
            all_rows.append(r)
    try:
        _add(_search_danmaku_one_db("bilibili_data", "write", groups, start, end, exclude, per_db))
    except Exception as exc:
        failed.append(f"D缓冲:{exc}")
        logger.warning("搜弹幕 D缓冲 跳过: %s", exc)
    for db in (_shard_dbs() or ["bilibili_data"]):
        if stop_event is not None and stop_event.is_set():
            logger.info("搜弹幕被用户取消，已停于 %s 仓之前", db)
            break
        if _uid_same_dsn() and db == "bilibili_data":
            continue
        try:
            _add(_search_danmaku_one_db(db, "read", groups, start, end, exclude, per_db))
        except Exception as exc:
            failed.append(f"{db}:{exc}")
            logger.warning("搜弹幕 %s 跳过: %s", db, exc)
    all_rows.sort(key=lambda r: r.get("ctime") or "", reverse=True)
    out = {"rows": all_rows, "total": len(all_rows)} if is_unlimited else \
        {"rows": all_rows[:limit], "total": None if len(all_rows) > limit else len(all_rows)}
    if failed:
        out["failed"] = failed
    if stop_event is not None and stop_event.is_set():
        out["cancelled"] = True
    if not failed and not out.get("cancelled"):
        _search_cache_put(_search_cache_key("dmk", keyword, start, end, exclude, qlimit, 0), out)
    return out


def _iter_search_one_db(db: str, role: str, groups: list[list[str]], *, table: str, id_col: str,
                        alias: str, cols: str, start: str = "", end: str = "",
                        exclude: str = "", page: int = 5000, min_like: int = 0) -> Iterable[dict]:
    """单个物理库内流式导出搜索命中全部行（keyset 分页，无深偏移）。

    按 (ctime, 主键) 倒序翻页，ctime 撞车用主键兜底保证不重不漏。
    """
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=300)
    try:
        with conn.cursor() as cur:
            cond, params = _dt_cond(f"{alias}.ctime", start, end)
            if alias == "d":
                excl, excl_params = _exclude_likes_danmaku(exclude)
            else:
                excl, excl_params = _exclude_likes(exclude)
            smart_where, smart_params = _like_cond_smart(alias, groups)
            like_cond = ""
            like_params: list = []
            if alias == "c" and min_like and min_like > 0:
                like_cond = f" AND {alias}.like_num >= %s"
                like_params = [int(min_like)]
            last_ctime = None
            last_id = None
            while True:
                params_batch = list(smart_params) + list(params) + list(excl_params) + list(like_params)
                if last_ctime is not None:
                    sql = (
                        f"SELECT {cols} FROM `{db}`.{table} {alias} "
                        f"LEFT JOIN `{db}`.meta_video v ON v.aid = " + alias + ".video_aid "
                        f"LEFT JOIN `{db}`.dim_user u ON u.uid = " + alias + ".uid "
                        f"WHERE 1=1 {smart_where}"
                        + cond + excl + like_cond +
                        f" AND ({alias}.ctime, {alias}.{id_col}) < (%s, %s) "
                        f"ORDER BY {alias}.ctime DESC, {alias}.{id_col} DESC LIMIT %s"
                    )
                    args = [*params_batch, last_ctime, last_id, page]
                else:
                    sql = (
                        f"SELECT {cols} FROM `{db}`.{table} {alias} "
                        f"LEFT JOIN `{db}`.meta_video v ON v.aid = " + alias + ".video_aid "
                        f"LEFT JOIN `{db}`.dim_user u ON u.uid = " + alias + ".uid "
                        f"WHERE 1=1 {smart_where}"
                        + cond + excl + like_cond +
                        f" ORDER BY {alias}.ctime DESC, {alias}.{id_col} DESC LIMIT %s"
                    )
                    args = [*params_batch, page]
                cur.execute(sql, args)
                rows = list(cur.fetchall())
                if not rows:
                    break
                yield from rows
                if len(rows) < page:
                    break
                last = rows[-1]
                last_ctime = last["ctime"]
                last_id = last[id_col]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _iter_search_all_dbs(groups: list[list[str]], *, table: str, id_col: str,
                         alias: str, cols: str, start: str = "", end: str = "",
                         exclude: str = "", page: int = 5000, min_like: int = 0) -> Iterable[dict]:
    """跨库流式导出：D 缓冲 + E 各仓逐库 keyset 翻页，按主键去重。"""
    seen: set = set()
    targets: list[tuple[str, str]] = [("bilibili_data", "write")]
    try:
        from config import get_config
        cfg = get_config()
        d_db = getattr(cfg, "mysql_write", cfg.mysql)["database"]
        targets = [(d_db, "write")]
        for db in (_shard_dbs() or ["bilibili_data"]):
            if _uid_same_dsn() and db == d_db:
                continue
            targets.append((db, "read"))
    except Exception:
        pass
    for db, role in targets:
        try:
            for r in _iter_search_one_db(db, role, groups, table=table, id_col=id_col,
                                         alias=alias, cols=cols, start=start, end=end,
                                         exclude=exclude, page=page, min_like=min_like):
                k = r.get(id_col)
                if k in seen:
                    continue
                seen.add(k)
                yield r
        except Exception as exc:
            logger.warning("导出流 %s(%s) 跳过: %s", db, role, exc)


def iter_search_comments(keyword: str, *, start: str = "", end: str = "",
                         exclude: str = "", page: int = 5000, min_like: int = 0) -> Iterable[dict]:
    """流式导出全部命中评论（跨库 keyset 分页），字段同 search_comments 内容分支。"""
    cols = ("c.rpid, c.uid, u.uname, c.content, c.like_num, c.ctime, "
            "c.ip_location, c.video_aid, v.bvid, v.title, "
            "v.owner_uid, v.owner_name")
    groups = parse_smart_query(keyword)
    if not groups:
        return
        yield  # 保持生成器语义
    yield from _iter_search_all_dbs(groups, table="raw_comments", id_col="rpid", alias="c",
                                    cols=cols, start=start, end=end,
                                    exclude=exclude, page=page, min_like=min_like)


def iter_search_danmaku(keyword: str, *, start: str = "", end: str = "",
                        exclude: str = "", page: int = 5000) -> Iterable[dict]:
    """流式导出全部命中弹幕（跨库 keyset 分页）。"""
    cols = ("d.id, d.uid, d.mid_hash, u.uname, d.content, d.progress, "
            "d.ctime, d.video_aid, v.bvid, v.title, v.owner_name")
    groups = parse_smart_query(keyword)
    if not groups:
        return
        yield  # 保持生成器语义
    yield from _iter_search_all_dbs(groups, table="raw_danmaku", id_col="id", alias="d",
                                    cols=cols, start=start, end=end,
                                    exclude=exclude, page=page)


def _video_match_cond(groups: list[list[str]]) -> tuple[str, list]:
    """视频匹配条件：组内词 AND、组间 OR；每词命中标题/BV/UP 任一字段。"""
    parts, params = [], []
    for terms in groups:
        conds = []
        for kw in terms:
            pat = "%" + _like_esc(kw) + "%"
            conds.append("(v.title LIKE %s OR v.bvid LIKE %s "
                         "OR v.owner_name LIKE %s OR v.owner_uid LIKE %s)")
            params.extend([pat] * 4)
        parts.append("(" + " AND ".join(conds) + ")")
    return "(" + " OR ".join(parts) + ")", params


def _search_videos_one_db(db: str, role: str, groups: list[list[str]], start: str, end: str,
                          limit: int) -> list[dict]:
    """单个物理库内搜视频（含库内评论/弹幕计数）。"""
    import pymysql

    from config import get_config
    cfg = get_config()
    base = getattr(cfg, "mysql_archive" if role == "read" else "mysql_write", cfg.mysql)
    conn = pymysql.connect(host=base["host"], port=base["port"], user=base["user"],
                           password=base["password"], database=db, charset=base["charset"],
                           cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=5, read_timeout=600)
    try:
        with conn.cursor() as cur:
            where_content, all_params = _video_match_cond(groups)
            conditions = [where_content]
            params: list = list(all_params)
            if start:
                conditions.append("FROM_UNIXTIME(v.pubdate) >= %s")
                params.append(start)
            if end:
                conditions.append("FROM_UNIXTIME(v.pubdate) <= %s")
                params.append(end)
            where = " AND ".join(conditions)
            cur.execute(
                f"SELECT v.aid, v.bvid, v.title, v.owner_uid, v.owner_name, v.pubdate, "
                f"       v.part "
                f"FROM `{db}`.meta_video v "
                f"WHERE {where} "
                "ORDER BY v.pubdate DESC LIMIT %s",
                [*params, limit + 1])
            videos = list(cur.fetchall())
            aids = [int(v["aid"]) for v in videos if v.get("aid")]
            cc: dict = {}
            dc: dict = {}
            if aids:
                fmt = ",".join(["%s"] * len(aids))
                cur.execute(
                    f"SELECT video_aid AS aid, COUNT(*) AS n FROM `{db}`.raw_comments "
                    f"WHERE video_aid IN ({fmt}) GROUP BY video_aid", tuple(aids))
                cc = {int(r["aid"]): int(r["n"]) for r in cur.fetchall()}
                cur.execute(
                    f"SELECT video_aid AS aid, COUNT(*) AS n FROM `{db}`.raw_danmaku "
                    f"WHERE video_aid IN ({fmt}) GROUP BY video_aid", tuple(aids))
                dc = {int(r["aid"]): int(r["n"]) for r in cur.fetchall()}
            for v in videos:
                v["comment_count"] = cc.get(int(v.get("aid") or 0), 0)
                v["danmaku_count"] = dc.get(int(v.get("aid") or 0), 0)
            return videos
    finally:
        try:
            conn.close()
        except Exception:
            pass


def search_videos(keyword: str, *, start: str = "", end: str = "",
                  limit: int = 50) -> dict:
    """跨库搜视频（标题/BV/UP昵称/UP UID 匹配），D 缓冲 + E 各仓合并去重。

    查询语法：空格/逗号=同时包含，OR/|=任一包含，"引号"=精确短语。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"rows": [], "total": 0}
    groups = parse_smart_query(keyword)
    if not groups:
        return {"rows": [], "total": 0}
    is_unlimited = not limit or limit <= 0
    qlimit = 500000 if is_unlimited else limit
    all_videos: list[dict] = []
    seen: set = set()
    failed: list = []
    def _add(videos: list) -> None:
        for v in videos:
            k = (v.get("aid"), v.get("part", 1))
            if k in seen:
                continue
            seen.add(k)
            all_videos.append(v)
    try:
        _add(_search_videos_one_db("bilibili_data", "write", groups, start, end, qlimit))
    except Exception as exc:
        failed.append(f"D缓冲:{exc}")
        logger.warning("搜视频 D缓冲 跳过: %s", exc)
    for db in (_shard_dbs() or ["bilibili_data"]):
        if _uid_same_dsn() and db == "bilibili_data":
            continue
        try:
            _add(_search_videos_one_db(db, "read", groups, start, end, qlimit))
        except Exception as exc:
            failed.append(f"{db}:{exc}")
            logger.warning("搜视频 %s 跳过: %s", db, exc)
    all_videos.sort(key=lambda v: int(v.get("pubdate") or 0), reverse=True)
    if is_unlimited:
        out = {"rows": all_videos, "total": len(all_videos)}
    else:
        out = {"rows": all_videos[:limit],
               "total": None if len(all_videos) > limit else len(all_videos)}
    if failed:
        out["failed"] = failed
    return out

