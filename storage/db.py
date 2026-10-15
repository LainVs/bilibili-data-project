"""MySQL 连接管理。

使用 pymysql 直连，提供数据库初始化、建表与连接获取。
"""
from __future__ import annotations

import logging
import threading

import pymysql

from config import get_config

logger = logging.getLogger(__name__)


def _cfg_for(role: str) -> dict:
    cfg = get_config()
    if role in ("read", "archive", "query"):
        return getattr(cfg, "mysql_archive", cfg.mysql)
    if role in ("write", "buffer", "d"):
        return getattr(cfg, "mysql_write", cfg.mysql)
    return cfg.mysql


def create_database(role: str = "write") -> None:
    """创建数据库（若不存在）。"""
    cfg = _cfg_for(role)
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        charset=cfg["charset"],
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{cfg['database']}` DEFAULT CHARACTER SET utf8mb4")
        conn.commit()
    finally:
        conn.close()


def get_conn(role: str = "write") -> pymysql.connections.Connection:
    """获取一个数据库连接。

    role: write=缓冲库D（采集落库）, read/archive=归档库E（查询统计）
    带连接/读写超时：MySQL 假死或网络黑洞时避免线程无限挂起。
    """
    cfg = _cfg_for(role)
    # 归档库E是HDD，14M聚合/全文检索需更长读超时
    rt = 600 if role in ("read", "archive", "query") else 300
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset=cfg["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=rt,
        write_timeout=120,
    )


def init_db(role: str = "write") -> None:
    """初始化数据库与全部表。"""
    create_database(role)
    conn = get_conn(role)
    try:
        with conn.cursor() as cur:
            for ddl in _ALL_DDL:
                cur.execute(ddl)
            _migrate(cur)
        conn.commit()
    finally:
        conn.close()


def ensure_conn(conn: pymysql.connections.Connection | None, role: str = "write") -> pymysql.connections.Connection:
    """若 conn 为 None 则新建连接；调用方负责关闭。"""
    return conn or get_conn(role)


# 进程内 once 标记：ensure_* 都是 IF NOT EXISTS 幂等 DDL，
# 同一进程只执行一次，避免每次统计/查询重复建连查 information_schema。
# 失败不标记（E 离线时下次重试），成功才标记。
_ensured: set[str] = set()
_ensured_lock = threading.Lock()


# 手动断开标记：用户在“连”面板点“断开D/E”后置位，守护线程不再自动
# `net start` 拉起，直到用户点重连（或检测到连接恢复）才清除。
# key 归一化为 "write"(D)/"read"(E)，与 _cfg_for 的 role 语义一致。
_manual_offline: set[str] = set()
_manual_offline_lock = threading.Lock()


def _offline_key(role: str) -> str:
    if role in ("read", "archive", "query"):
        return "read"
    if role in ("write", "buffer", "d"):
        return "write"
    return "write"


def set_manual_offline(role: str = "write") -> None:
    """标记某库为用户手动断开（守护线程跳过自启）。"""
    with _manual_offline_lock:
        _manual_offline.add(_offline_key(role))


def clear_manual_offline(role: str = "write") -> None:
    """清除手动断开标记（重连/恢复后调用）。"""
    with _manual_offline_lock:
        _manual_offline.discard(_offline_key(role))


def is_manual_offline(role: str = "write") -> bool:
    """某库是否处于用户手动断开状态。"""
    with _manual_offline_lock:
        return _offline_key(role) in _manual_offline


def _once_claim(key: str) -> bool:
    """返回 True 表示本次需要执行；False 表示本进程已成功做过，跳过。"""
    with _ensured_lock:
        return key not in _ensured


def _once_done(key: str, ok: bool) -> None:
    if ok:
        with _ensured_lock:
            _ensured.add(key)


def ensure_up_stat_table(role: str = "write") -> None:
    """确保 up_crawl_stat 表存在（老库升级：首次查询前自动补建）。"""
    key = f"up_stat:{role}"
    if not _once_claim(key):
        return
    conn = get_conn(role)
    try:
        with conn.cursor() as cur:
            cur.execute(_UP_STAT_DDL)
        conn.commit()
        _once_done(key, True)
    finally:
        conn.close()


def ensure_stats_cache_table(role: str = "write") -> None:
    """确保 stats_cache 表存在（老库升级：启动时惰性补建）。"""
    key = f"stats_cache:{role}"
    if not _once_claim(key):
        return
    conn = get_conn(role)
    try:
        with conn.cursor() as cur:
            cur.execute(_STATS_CACHE_DDL)
        conn.commit()
        _once_done(key, True)
    finally:
        conn.close()


def ensure_stat_counters(role: str = "write") -> None:
    """确保统计去重计数表存在（老库升级：惰性补建）。"""
    key = f"stat_counters:{role}"
    if not _once_claim(key):
        return
    conn = get_conn(role)
    try:
        with conn.cursor() as cur:
            for ddl in (_STAT_UID_COUNTER_DDL, _STAT_AID_COUNTER_DDL):
                cur.execute(ddl)
        conn.commit()
        _once_done(key, True)
    finally:
        conn.close()


def ensure_global_uid_tables(role: str = "read") -> None:
    """确保全库去重并集表存在。只允许建在 E 主库，D/小仓库不建。"""
    key = f"global_uid:{role}"
    if not _once_claim(key):
        return
    conn = get_conn(role)
    try:
        with conn.cursor() as cur:
            for ddl in (_GLOBAL_UIDS_DDL, _GLOBAL_COMMENT_UIDS_DDL, _GLOBAL_META_DDL):
                cur.execute(ddl)
        conn.commit()
        _once_done(key, True)
    finally:
        conn.close()


def _migrate(cur) -> None:
    """对已存在的旧表执行轻量迁移。"""
    _add_column(cur, "raw_comments", "ref_type",
                "ALTER TABLE raw_comments "
                "ADD COLUMN ref_type TINYINT NOT NULL DEFAULT 1 "
                "COMMENT '内容类型:1=视频 12=专栏 17=动态', "
                "ADD KEY idx_aid_type (video_aid, ref_type)")
    _add_column(cur, "meta_video", "owner_name",
                "ALTER TABLE meta_video "
                "ADD COLUMN owner_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT 'UP主昵称'")
    _add_column(cur, "dim_user", "fans",
                "ALTER TABLE dim_user ADD COLUMN fans INT NOT NULL DEFAULT 0 COMMENT '粉丝数', ADD KEY idx_fans (fans)")
    # owner_uid 索引：种子查询与反推都需 GROUP BY owner_uid，无索引时 E 上 24万行全扫超时
    try:
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='meta_video' AND index_name='idx_owner'")
        if cur.fetchone()["n"] == 0:
            cur.execute("ALTER TABLE meta_video ADD KEY idx_owner (owner_uid)")
    except Exception as exc:
        # 建索引失败会导致相关查询退化为全表扫描（E 库上百秒），必须可见
        logger.warning("idx_owner 索引检查/创建失败，后续按 owner_uid 查询可能全表扫描: %s", exc)
    # (uid, ctime) 覆盖索引：按用户查评论/弹幕 ORDER BY ctime，无它时优化器倒扫全表回表，HDD 上百秒
    for _tbl in ("raw_comments", "raw_danmaku"):
        try:
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name=%s AND index_name='idx_uid_ctime'", (_tbl,))
            if cur.fetchone()["n"] == 0:
                cur.execute(f"ALTER TABLE {_tbl} ADD KEY idx_uid_ctime (uid, ctime)")
        except Exception as exc:
            logger.warning("%s 的 idx_uid_ctime 索引检查/创建失败，按用户+时间查询可能全表扫描: %s", _tbl, exc)


def _add_column(cur, table: str, column: str, alter_sql: str) -> None:
    """若列不存在则执行 ALTER（老库升级用）。"""
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    if cur.fetchone()["n"] == 0:
        try:
            cur.execute(alter_sql)
        except Exception as exc:
            # 缺列会让依赖该列的写入/查询报错，静默吞掉极难排查
            logger.warning("为 %s 补列 %s 失败，请手动执行迁移: %s", table, column, exc)


# ---- DDL 集合 ----
_VIDEO_DDL = """
CREATE TABLE IF NOT EXISTS meta_video (
    aid        BIGINT       NOT NULL COMMENT '视频aid',
    bvid       VARCHAR(32)  NOT NULL COMMENT '视频bvid',
    cid        BIGINT       NOT NULL COMMENT '视频分P的cid',
    title      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '视频标题',
    owner_uid  BIGINT       NOT NULL DEFAULT 0 COMMENT 'UP主UID',
    owner_name VARCHAR(128) NOT NULL DEFAULT '' COMMENT 'UP主昵称',
    pubdate    INT          NOT NULL DEFAULT 0 COMMENT '发布时间(unix)',
    part       INT          NOT NULL DEFAULT 1 COMMENT '分P序号',
    create_time DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (aid, part),
    UNIQUE KEY uk_bvid_part (bvid, part),
    KEY idx_owner (owner_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='视频元信息'
"""

_DANMAKU_DDL = """
CREATE TABLE IF NOT EXISTS raw_danmaku (
    id         BIGINT       NOT NULL COMMENT '弹幕dmid',
    video_aid  BIGINT       NOT NULL COMMENT '所属视频aid',
    uid        BIGINT       NOT NULL DEFAULT 0 COMMENT '发送者真实UID(0=未解析)',
    mid_hash   VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '发送者mid_hash',
    content    TEXT         NOT NULL COMMENT '弹幕内容',
    progress   FLOAT        NOT NULL DEFAULT 0 COMMENT '视频内出现时间(秒)',
    mode       INT          NOT NULL DEFAULT 1 COMMENT '弹幕类型',
    color      INT          NOT NULL DEFAULT 16777215 COMMENT '弹幕颜色',
    ctime      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发送时间',
    is_parsed  TINYINT      NOT NULL DEFAULT 0 COMMENT '是否已反查UID',
    PRIMARY KEY (id),
    KEY idx_aid (video_aid),
    KEY idx_uid (uid),
    KEY idx_uid_ctime (uid, ctime),
    KEY idx_mid_hash (mid_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='弹幕原始数据'
"""

# 评论检索关键索引：`WHERE content LIKE '%词%' ORDER BY ctime DESC LIMIT n` 靠它
# 反扫 + 凑够即停；没有它就只能全表扫 + filesort（跨 19 仓串行累加成分钟级）。
# 归档分仓建表时**剔除**此行、改为装载完成后单独建（InnoDB 排序批量构建远快于
# 边 LOAD 边维护索引），见 storage/archive_sync.py 的「2.55 建检索索引」。
_COMMENT_CTIME_KEY = "    KEY idx_ctime (ctime),\n"

_COMMENT_DDL = f"""
CREATE TABLE IF NOT EXISTS raw_comments (
    rpid       BIGINT       NOT NULL COMMENT '评论ID',
    video_aid  BIGINT       NOT NULL COMMENT '所属对象ID(视频aid/动态id/专栏cvid)',
    ref_type   TINYINT      NOT NULL DEFAULT 1 COMMENT '对象类型:1视频 12专栏 17动态',
    parent     BIGINT       NOT NULL DEFAULT 0 COMMENT '上级评论ID(0=一级)',
    uid        BIGINT       NOT NULL COMMENT '用户UID',
    content    TEXT         NOT NULL COMMENT '评论内容',
    like_num   INT          NOT NULL DEFAULT 0 COMMENT '点赞数',
    ctime      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '评论时间',
    ip_location VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'IP属地',
    PRIMARY KEY (rpid),
{_COMMENT_CTIME_KEY}    KEY idx_aid (video_aid, ref_type),
    KEY idx_uid (uid),
    KEY idx_uid_ctime (uid, ctime),
    KEY idx_parent (parent)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='评论原始数据'
"""

_USER_DDL = """
CREATE TABLE IF NOT EXISTS dim_user (
    uid          BIGINT      NOT NULL COMMENT '用户UID',
    uname        VARCHAR(128) NOT NULL DEFAULT '' COMMENT '昵称',
    level        INT         NOT NULL DEFAULT 0 COMMENT '用户等级',
    sex          VARCHAR(8)  NOT NULL DEFAULT '' COMMENT '性别',
    is_vip       TINYINT     NOT NULL DEFAULT 0 COMMENT '是否大会员',
    signature    VARCHAR(512) NOT NULL DEFAULT '' COMMENT '个性签名',
    fans         INT         NOT NULL DEFAULT 0 COMMENT '粉丝数',
    update_time  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (uid),
    KEY idx_fans (fans)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户维度表'
"""

_CRACK_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS crckey_cache (
    mid_hash   VARCHAR(16)  NOT NULL COMMENT '弹幕mid_hash',
    uid        BIGINT       NOT NULL COMMENT '反查得到的UID',
    resolved   TINYINT      NOT NULL DEFAULT 1 COMMENT '是否成功(0=未找到)',
    create_time DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (mid_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='CRC32反查结果缓存'
"""

_PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS crawl_progress (
    video_aid      BIGINT      NOT NULL COMMENT '视频aid',
    danmaku_done   TINYINT     NOT NULL DEFAULT 0 COMMENT '弹幕是否采集完整',
    comment_done   TINYINT     NOT NULL DEFAULT 0 COMMENT '评论是否采集完整',
    last_error     VARCHAR(512) NOT NULL DEFAULT '' COMMENT '最近错误',
    update_time    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (video_aid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='采集进度/断点续传'
"""

_POST_DDL = """
CREATE TABLE IF NOT EXISTS meta_post (
    id         BIGINT       NOT NULL COMMENT '帖子/专栏ID',
    post_type  TINYINT      NOT NULL COMMENT '类型:1视频 12专栏 17动态',
    title      VARCHAR(512) NOT NULL DEFAULT '' COMMENT '标题/动态摘要',
    owner_uid  BIGINT       NOT NULL DEFAULT 0 COMMENT '作者UID',
    pubdate    INT          NOT NULL DEFAULT 0 COMMENT '发布时间(unix)',
    create_time DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, post_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='帖子/专栏元信息'
"""

_UP_STAT_DDL = """
CREATE TABLE IF NOT EXISTS up_crawl_stat (
    mid          BIGINT       NOT NULL COMMENT 'UP主UID',
    video_total  INT          NOT NULL DEFAULT 0 COMMENT '该UP视频总数(拉取时记录)',
    video_fetched INT         NOT NULL DEFAULT 0 COMMENT '已采集视频数',
    comment_total INT         NOT NULL DEFAULT 0 COMMENT '已采评论数',
    danmaku_total INT         NOT NULL DEFAULT 0 COMMENT '已采弹幕数',
    update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (mid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='UP主采集统计'
"""

_STATS_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS stats_cache (
    id         TINYINT      NOT NULL COMMENT '单行缓存ID(固定1)',
    payload    MEDIUMTEXT   NOT NULL COMMENT 'JSON: {stats,ranks,time}',
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='统计面板缓存(避免启动全表聚合)'
"""

_STAT_UID_COUNTER_DDL = """
CREATE TABLE IF NOT EXISTS stat_comment_uids (
    uid BIGINT NOT NULL COMMENT '出现过的评论者UID(去重)',
    PRIMARY KEY (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='去重评论UID计数(统计物化)'
"""

_STAT_AID_COUNTER_DDL = """
CREATE TABLE IF NOT EXISTS stat_comment_aids (
    video_aid BIGINT NOT NULL COMMENT '出现过评论的对象ID(去重)',
    PRIMARY KEY (video_aid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='有评论对象计数(统计物化)'
"""

# 全库去重并集表：只建在 E 主库 bilibili_data，不进 _ALL_DDL（不随小仓库复制）。
# 由归档 hook + 回填 + 写线程直播喂数；统计 users/comment_uids 改查这两张表。
_GLOBAL_UIDS_DDL = """
CREATE TABLE IF NOT EXISTS global_uids (
    uid BIGINT NOT NULL COMMENT '全库出现过的用户UID(去重并集)',
    uname VARCHAR(128) NOT NULL DEFAULT '' COMMENT '最后一次见到的昵称',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全库用户去重并集'
"""

_GLOBAL_COMMENT_UIDS_DDL = """
CREATE TABLE IF NOT EXISTS global_comment_uids (
    uid BIGINT NOT NULL COMMENT '全库发过评论的UID(去重并集)',
    PRIMARY KEY (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全库评论者去重并集'
"""

_GLOBAL_META_DDL = """
CREATE TABLE IF NOT EXISTS global_meta (
    k VARCHAR(32) NOT NULL COMMENT '键：backfill_shards/backfill_time',
    v VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '值：已覆盖的小仓库csv/回填时间',
    PRIMARY KEY (k)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全局去重表覆盖标记'
"""

_ALL_DDL = [
    _VIDEO_DDL,
    _DANMAKU_DDL,
    _COMMENT_DDL,
    _USER_DDL,
    _CRACK_CACHE_DDL,
    _PROGRESS_DDL,
    _POST_DDL,
    _UP_STAT_DDL,
    _STATS_CACHE_DDL,
    _STAT_UID_COUNTER_DDL,
    _STAT_AID_COUNTER_DDL,
]
