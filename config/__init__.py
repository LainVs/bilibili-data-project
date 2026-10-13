"""项目配置加载模块

从 config/settings.ini 读取配置。
支持通过命令行参数或环境变量覆盖敏感字段（如 cookie）。
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path

# 项目根目录（本文件上一级）
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT_DIR / "config" / "settings.ini"

# 默认 UA 列表，settings.ini 未配置或为空时使用
DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


class Config:
    """集中管理项目配置。"""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        # RawConfigParser：禁用 % 插值，原样保留 Cookie 中的特殊字符
        parser = configparser.RawConfigParser()
        parser.read(path, encoding="utf-8")
        self._parser = parser

        self.mysql = {
            "host": self._get("mysql", "host", "127.0.0.1"),
            "port": self._get_int("mysql", "port", 3306),
            "user": self._get("mysql", "user", "root"),
            "password": self._get("mysql", "password", ""),
            "database": self._get("mysql", "database", "bilibili_data"),
            "charset": self._get("mysql", "charset", "utf8mb4"),
        }
        # 双库：写库D（采集落库）/ 归档库E（查询统计）
        # 兼容：未配置则回退到 [mysql]
        def _mysql_block(section: str) -> dict:
            return {
                "host": self._get(section, "host", self.mysql["host"]),
                "port": self._get_int(section, "port", self.mysql["port"]),
                "user": self._get(section, "user", self.mysql["user"]),
                "password": self._get(section, "password", self.mysql["password"]),
                "database": self._get(section, "database", self.mysql["database"]),
                "charset": self._get(section, "charset", self.mysql["charset"]),
            }
        self.mysql_write = _mysql_block("mysql_write") if self._parser.has_section("mysql_write") else dict(self.mysql)
        # 归档库别名：mysql_archive / mysql_read（兼容旧名，优先 archive）
        has_archive = self._parser.has_section("mysql_archive")
        has_read = self._parser.has_section("mysql_read")
        if has_archive and has_read:
            # 双存时以 archive 为准，read 视为过期
            arch_section = "mysql_archive"
        elif has_archive:
            arch_section = "mysql_archive"
        elif has_read:
            arch_section = "mysql_read"
        else:
            arch_section = None
        if arch_section:
            self.mysql_archive = dict(_mysql_block(arch_section))
            self.mysql_read = dict(self.mysql_archive)
        else:
            self.mysql_archive = dict(self.mysql)
            self.mysql_read = dict(self.mysql_archive)

        # cookie 优先取环境变量，其次专用文件(避免 ini 解析转义问题)，最后 ini
        self.cookie = (
            os.environ.get("BILI_COOKIE", "")
            or _read_cookie_file()
            or self._get("bilibili", "cookie", "")
        )
        uas = self._get("bilibili", "user_agents", "")
        self.user_agents = [ua.strip() for ua in uas.split(";") if ua.strip()] or DEFAULT_USER_AGENTS
        # 请求间隔：随机抖动区间（避免固定节拍风控）+ 全局限速
        self.request_delay = self._get_float("bilibili", "request_delay", 0.65)
        self.request_delay_min = self._get_float("bilibili", "request_delay_min", 0.4)
        self.request_delay_max = self._get_float("bilibili", "request_delay_max", 0.9)
        self.global_qps = self._get_float("bilibili", "global_qps", 6.5)
        self.up_workers = self._get_int("bilibili", "up_workers", 3)
        self.danmaku_day_workers = self._get_int("bilibili", "danmaku_day_workers", 4)
        self.comment_sub_workers = self._get_int("bilibili", "comment_sub_workers", 2)
        self.list_page_workers = self._get_int("bilibili", "list_page_workers", 4)
        # 各列表接口服务端墙不同(压测实测)，按类型独立取值；缺省回退 list_page_workers
        self.space_list_workers = self._get_int("bilibili", "space_list_workers", 0) or None
        self.fav_list_workers = self._get_int("bilibili", "fav_list_workers", 0) or None
        self.relation_list_workers = self._get_int("bilibili", "relation_list_workers", 0) or None
        self.max_retries = self._get_int("bilibili", "max_retries", 5)
        # 风控/频控码退避时长(秒)；探针设置为 0 可快速暴露风控错误
        self.risk_backoff_seconds = self._get_float("bilibili", "risk_backoff_seconds", 30)
        self.increment_interval_minutes = self._get_int("bilibili", "increment_interval_minutes", 60)

    def _get(self, section: str, key: str, default: str) -> str:
        try:
            v = self._parser.get(section, key)
            if v is None or str(v).strip() == "":
                return default
            return v
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default

    def _get_int(self, section: str, key: str, default: int) -> int:
        try:
            return int(str(self._get(section, key, str(default))).strip())
        except (ValueError, TypeError):
            return default

    def _get_float(self, section: str, key: str, default: float) -> float:
        try:
            return float(str(self._get(section, key, str(default))).strip())
        except (ValueError, TypeError):
            return default


def _read_cookie_file() -> str:
    """从 config/bili_cookie.txt 读取 Cookie 原文（跳过注释行）。"""
    path = ROOT_DIR / "config" / "bili_cookie.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    cleaned = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return ";".join(cleaned)


_config: Config | None = None


def get_config() -> Config:
    """获取全局唯一配置实例。"""
    global _config
    if _config is None:
        _config = Config()
    return _config


def _update_section(section: str, **kv) -> None:
    import tempfile
    path = CONFIG_FILE
    parser = configparser.RawConfigParser()
    if path.exists():
        parser.read(path, encoding="utf-8")
    if not parser.has_section(section):
        parser.add_section(section)
    for k, v in kv.items():
        if v is not None:
            parser.set(section, k, str(v))
    # 原子写入：先写临时文件再替换
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), text=True)
    try:
        with open(tmp_fd, "w", encoding="utf-8") as fh:
            parser.write(fh)
            fh.flush()
            try:
                import os
                os.fsync(fh.fileno())
            except Exception:
                pass
        import os
        os.replace(tmp_path, path)
    finally:
        try:
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
    global _config
    _config = None


def update_mysql_config(*, host=None, port=None, user=None,
                        password=None, database=None, charset=None) -> None:
    """更新 [mysql] 与 [mysql_write]（写库），重置缓存。"""
    _update_section("mysql", host=host, port=port, user=user, password=password, database=database, charset=charset)
    _update_section("mysql_write", host=host, port=port, user=user, password=password, database=database, charset=charset)


def update_mysql_archive_config(*, host=None, port=None, user=None, password=None, database=None, charset=None) -> None:
    _update_section("mysql_archive", host=host, port=port, user=user, password=password, database=database, charset=charset)
