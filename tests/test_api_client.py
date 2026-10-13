"""crawler/api/client.py 单元测试（纯离线，不发起真实网络请求）。

覆盖反爬合规策略中易出错的逻辑：
- get_json 成功 / 业务错误码 / 风控退避 / 重试耗尽
- get_bytes 的 JSON 错误探测（限流时接口返回 JSON 而非 protobuf）
- 重试次数与 ApiError 语义
"""
from __future__ import annotations

import pytest
import requests

from crawler.api import client as client_mod
from crawler.api.client import ApiClient, ApiError


class FakeResponse:
    """最小响应替身：只提供被测代码用到的属性/方法。

    json() 模拟 requests：优先用显式 json_data，否则尝试解析 content。
    """

    def __init__(self, *, json_data=None, content=b"", raise_exc=None):
        self._json = json_data
        self.content = content
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        if self._json is not None:
            return self._json
        import json as _json

        return _json.loads(self.content.decode("utf-8"))


class FakeSession:
    """按预设序列返回响应；记录每次请求的 URL。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls: list[str] = []
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    def close(self):
        self.closed = True


@pytest.fixture
def fast_client(monkeypatch):
    """构造 ApiClient 并把所有 sleep/限速打成 no-op，保证测试秒级且确定。"""
    # 冻结配置读取，避免依赖本机 settings.ini
    class _Cfg:
        cookie = ""
        user_agents = ["UA"]
        max_retries = 3
        risk_backoff_seconds = 0.0
        request_delay_min = 0.0
        request_delay_max = 0.0

    monkeypatch.setattr(client_mod, "get_config", lambda: _Cfg())

    def _new(responses):
        c = ApiClient()
        c._session = FakeSession(responses)
        monkeypatch.setattr(c, "_throttle", lambda: None)
        monkeypatch.setattr(c, "_interruptible_sleep", lambda s: None)
        monkeypatch.setattr(client_mod, "_global_gate", lambda cfg: None)
        return c

    return _new


# ---------- get_json ----------

def test_get_json_success(fast_client):
    c = fast_client([FakeResponse(json_data={"code": 0, "data": {"x": 1}})])
    out = c.get_json("https://api/x")
    assert out["code"] == 0
    assert out["data"] == {"x": 1}


def test_get_json_business_error_raises_apierror(fast_client):
    c = fast_client([FakeResponse(json_data={"code": -404, "message": "not found"})] * 5)
    with pytest.raises(ApiError) as ei:
        c.get_json("https://api/x")
    # 重试耗尽后抛 -1 包装错误，根因保留
    assert ei.value.code == -1
    assert "请求失败" in ei.value.message


def test_get_json_retries_on_network_error_then_succeeds(fast_client):
    c = fast_client([
        requests.ConnectionError("boom"),
        FakeResponse(json_data={"code": 0, "data": {}}),
    ])
    out = c.get_json("https://api/x")
    assert out["code"] == 0
    assert len(c._session.urls) == 2, "网络错误后应重试一次"


def test_get_json_risk_control_triggers_backoff(fast_client):
    """-412 风控码应走退避分支（此处验证不抛 ValueError 且最终抛 ApiError）。"""
    backoffs = []
    c = fast_client([FakeResponse(json_data={"code": -412, "message": "risk"})] * 5)
    c._interruptible_sleep = lambda s: backoffs.append(s)  # 覆盖 fixture 的 no-op 以观测
    with pytest.raises(ApiError):
        c.get_json("https://api/x")
    # max_retries=3 -> 3 次 _throttle 外，-412 各触发一次退避（值为 risk_backoff=0.0）
    assert backoffs, "风控码应触发退避调用"


def test_get_json_exhausts_retries(fast_client):
    c = fast_client([requests.Timeout("t")] * 10)
    with pytest.raises(ApiError):
        c.get_json("https://api/x")
    assert len(c._session.urls) == 3, "max_retries=3 应只尝试 3 次"


# ---------- get_bytes ----------

def test_get_bytes_returns_raw_when_protobuf(fast_client):
    c = fast_client([FakeResponse(content=b"\x08\x01\x12\x03abc")])
    out = c.get_bytes("https://api/dm")
    assert out == b"\x08\x01\x12\x03abc"


def test_get_bytes_detects_json_rate_limit_error(fast_client):
    """限流时弹幕接口返回 JSON 错误体（首字节 '{'），应抛 ApiError 而非当 protobuf。"""
    body = b'{"code": -702, "message": "freq limit"}'
    c = fast_client([FakeResponse(content=body)])
    with pytest.raises(ApiError) as ei:
        c.get_bytes("https://api/dm")
    assert ei.value.code == -702


def test_get_bytes_retries_on_transport_error_then_ok(fast_client):
    c = fast_client([
        requests.ConnectionError("boom"),
        FakeResponse(content=b"\x00\x01"),
    ])
    assert c.get_bytes("https://api/dm") == b"\x00\x01"


# ---------- 其他 ----------

def test_close_closes_session(fast_client):
    c = fast_client([])
    c.close()
    assert c._session.closed is True


def test_short_backoff_is_bounded():
    # 指数退避封顶 8s
    assert client_mod._short_backoff(0) == pytest.approx(0.5)
    assert client_mod._short_backoff(20) == pytest.approx(8.0)
