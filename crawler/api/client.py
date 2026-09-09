"""带反爬合规的 HTTP 请求封装。

特性：
- 随机 UA 轮换
- 实例级智能限速：随机抖动间隔（避免固定节拍被识别为爬虫）
- 全局 QPS 护栏：所有实例合计仍受控，防并发过载（风控与并发并行两件事）
- 指数退避重试（429/5xx/网络错误）
- 402~ 数字错误码解析（B站接口返回的 JSON code）
"""
from __future__ import annotations

import random
import threading
import time

import requests

from config import get_config

# 全局 QPS 护栏（跨实例共享，防止多视频并发时合计突破安全速率）
_global_lock = threading.Lock()
_global_last_at = 0.0


def _global_gate(cfg) -> None:
    """所有请求实例共享的全局速率闸门。

    并发视频各自有独立节流，但合计仍不能超过 global_qps，
    否则多个视频同时翻页会对外表现为"稳定高频"，易触发风控。
    """
    global _global_last_at
    delay = 1.0 / cfg.global_qps
    while True:
        with _global_lock:
            now = time.monotonic()
            wait = delay - (now - _global_last_at)
            if wait <= 0:
                _global_last_at = now
                return
        time.sleep(max(0.0, wait * 0.9))


class ApiError(Exception):
    """B站接口返回业务错误。code 为接口 JSON 的 code 字段。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class ApiClient:
    """带合规反爬策略的 API 客户端。

    - 每个实例有独立节流状态（不同 worker 各用各的，互不排队）；
    - 随机抖动间隔：request_delay 为中心，在 [min, max] 均匀随机，
      避免稳定节拍；-412 等风控仍退避 30s。
    - stop_event：可选 threading.Event，退避睡眠可被立刻打断。
    """

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        cfg = get_config()
        self.cookie = cfg.cookie
        self.user_agents = cfg.user_agents
        self.max_retries = cfg.max_retries
        # 风控/频控码的退避时长（秒）；探针可置 0 以快速暴露错误
        self.risk_backoff = float(getattr(cfg, "risk_backoff_seconds", 30.0))
        # 实例级节流
        self._delay_min = cfg.request_delay_min
        self._delay_max = cfg.request_delay_max
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0
        self.stop_event = stop_event
        self._session = requests.Session()

    def close(self) -> None:
        """显式关闭底层 HTTP session（释放 keep-alive 连接）。"""
        try:
            self._session.close()
        except Exception:
            pass

    def _interruptible_sleep(self, seconds: float) -> None:
        """可中断睡眠：stop_event 置位时尽快返回，避免停止被退避拖住。"""
        if self.stop_event is not None:
            self.stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _jitter_delay(self, purpose: str = "") -> float:
        """随机抖动间隔（均匀分布在区间内）。"""
        return random.uniform(self._delay_min, self._delay_max)

    def _throttle(self) -> None:
        """实例内限速：与上一次请求至少间隔 jitter；再叠全局闸门。"""
        cfg = get_config()
        delay = self._jitter_delay()
        wait = 0.0
        with self._rate_lock:
            now = time.monotonic()
            wait = delay - (now - self._last_request_at)
            if wait > 0:
                self._last_request_at = now + wait
            else:
                self._last_request_at = now
        if wait > 0:
            self._interruptible_sleep(wait)
        _global_gate(cfg)

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(self.user_agents),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            **( {"Cookie": self.cookie} if self.cookie else {}),
        }

    def get_json(self, url: str, params: dict | None = None) -> dict:
        """GET 请求并解析 JSON，带重试与限速。

        B站接口 JSON 形如 {code, message, data}，code==0 视为成功。
        """
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._session.get(url, params=params, headers=self._headers(), timeout=15)
                resp.raise_for_status()
                payload = resp.json()
            except requests.RequestException as exc:
                last_err = exc
            except ValueError as exc:
                last_err = exc
            else:
                code = payload.get("code", -1)
                if code == 0:
                    return payload
                if code in (-412, -403, -799):  # 风控/频控错误，必须退避
                    self._interruptible_sleep(self.risk_backoff)
                last_err = ApiError(code, str(payload.get("message", "unknown")))
            self._interruptible_sleep(_short_backoff(attempt))
        raise ApiError(-1, f"请求失败: {last_err}") from last_err

    def get_bytes(self, url: str, params: dict | None = None) -> bytes:
        """GET 请求原始字节（弹幕 protobuf 用）。

        B站部分接口在限流时返回 JSON 错误（如 {code:-702}），
        这里探测 Content-Type / 首字节，若为 JSON 错误则抛 ApiError。
        """
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._session.get(url, params=params, headers=self._headers(), timeout=15)
                resp.raise_for_status()
                content = resp.content
                ctype = resp.headers.get("Content-Type", "")
                # JSON 错误探测：内容以 { 开头且能被解析为 dict
                if content and content[:1] == b"{":
                    try:
                        payload = resp.json()
                    except ValueError:
                        pass
                    else:
                        code = payload.get("code", 0)
                        if code < 0:  # 业务错误码（含风控 -412、seg.so 频控 -702 等）
                            if code in (-412, -403, -702, -799):  # 标记为风控/频控码
                                self._interruptible_sleep(self.risk_backoff)
                            raise ApiError(code, str(payload.get("message", "unknown")))
                return content
            except ApiError as exc:  # 业务错误码，无需重试，直接上抛
                raise exc
            except requests.RequestException as exc:
                last_err = exc
            except ValueError as exc:
                last_err = exc
            self._interruptible_sleep(_short_backoff(attempt))
        raise last_err from last_err


def _short_backoff(attempt: int) -> float:
    return min(0.5 * (2 ** attempt), 8)


def fetch_pages_parallel(fetch_one, pages: list[int], *, workers: int = 4,
                         stop_flag=None, client_factory=None,
                         use_client_fn="") -> list[tuple[int, list]]:
    """页码式分页接口的多页并行拉取（Vector 化翻页）。

    fetch_one(client, pn) -> 该页 items(list[dict])；空列表表示页空/异常。
    pages：待并行拉取的页码列表。
    每 worker 用独立 ApiClient（独立实例节流），全局 QPS 护栏兜底总速率；
    返回 [(pn, items)] 按页码升序，便于调用方按原顺序拼接数据。

    适用场景：space 视频列表、关系列表、收藏夹等"pn 页码分页"接口，
    首页已知 total 后可把剩余页全部并发拉取，翻页墙钟时间 ÷ workers。
    """
    from concurrent.futures import ThreadPoolExecutor

    cfg = get_config()
    n_w = max(1, min(workers, len(pages)))
    if not pages:
        return []
    maker = client_factory or getattr(__import__("crawler.api.client", fromlist=["ApiClient"]), "ApiClient")
    out: dict[int, list] = {}

    def _run(pn: int) -> None:
        if stop_flag is not None and stop_flag.is_set():
            return
        client = maker(stop_event=stop_flag)
        try:
            items = fetch_one(client, pn) or []
        except Exception as exc:
            __import__("logging").getLogger(__name__).warning(
                "并行翻页第 %s 页失败: %s", pn, exc)
            return
        finally:
            close = getattr(client, "close", None)
            if close:
                close()
        out[pn] = items

    with ThreadPoolExecutor(max_workers=n_w, thread_name_prefix="list-paginate") as pool:
        list(pool.map(_run, pages))
    return [(pn, out[pn]) for pn in sorted(out)]