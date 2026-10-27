"""WebView2 (GPU) 图形界面入口 —— pywebview 壳 + Claude 设计语言前端。

架构（对齐 Agent2API 桌面端的成功经验）：
- 壳进程：pywebview / WebView2，GPU 合成渲染，系统级惯性滚动；
- 前端：webui/ 下手写 HTML/CSS/JS，页面 DOM 常驻、display:none 切页；
- 桥：JsApi 暴露给前端 window.pywebview.api，直接复用现有 Python
  后端（storage.dao / config / archive_sync），业务逻辑零重写。

Tk 版仍完整保留：python main.py gui --engine tk 可随时回退。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEBUI_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "webui"


class JsApi:
    """暴露给前端 JS 的后端桥。方法名即 window.pywebview.api.<name>。

    约定：所有方法返回可 JSON 序列化的 dict；绝不抛异常给前端
    （错误用 {"ok": False, "error": "..."} 表达），前端永远拿得到响应。
    """

    # ── 连接状态页 ──
    def get_db_status(self) -> dict:
        """双库连通性检测（后台线程里由前端 await 调用）。"""
        try:
            from gui.web_bridge import check_databases

            return check_databases()
        except Exception as exc:
            logger.exception("get_db_status failed")
            return {"ok": False, "error": str(exc)}

    def get_overview(self) -> dict:
        """总览数字（轻量缓存读，秒回）。"""
        try:
            from gui.web_bridge import read_overview

            return read_overview()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def check_archive_resume(self) -> dict:
        """归档断点检测。"""
        try:
            from gui.web_bridge import check_archive_resume

            return check_archive_resume()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── 全局搜索页 ──
    def search(self, keyword: str, kind: str = "comments", limit: int = 100,
               start: str = "", end: str = "", exclude: str = "",
               min_like: int = 0) -> dict:
        """跨仓检索（前端 await 调用，注意此调用可能耗时数秒）。"""
        try:
            from gui.web_bridge import run_search

            return run_search(keyword, kind=kind, limit=limit, start=start,
                              end=end, exclude=exclude, min_like=min_like)
        except Exception as exc:
            logger.exception("search failed")
            return {"ok": False, "error": str(exc), "rows": [], "total": 0}

    # ── 窗口控制 ──
    def minimize(self) -> None:
        try:
            import webview

            webview.windows[0].minimize()
        except Exception:
            pass

    def toggle_maximize(self) -> None:
        try:
            import webview

            win = webview.windows[0]
            win.toggle_fullscreen() if win.event_fullscreen.is_set() else win.maximize()
        except Exception:
            pass


def run() -> int:
    """启动 WebView2 引擎 GUI。返回进程退出码。"""
    try:
        import webview
    except ImportError as exc:
        logger.error("pywebview 未安装：pip install pywebview")
        raise SystemExit(
            "WebView 引擎需要 pywebview：pip install pywebview\n"
            "或使用旧版界面：python main.py gui --engine tk"
        ) from exc

    index = WEBUI_DIR / "index.html"
    if not index.exists():
        raise SystemExit(f"前端资源缺失: {index}")

    api = JsApi()
    webview.create_window(
        "B站弹幕/评论采集系统",
        str(index),
        js_api=api,
        width=1180,
        height=760,
        min_size=(960, 640),
        background_color="#faf9f5",
    )
    # WebView2 (Edge/Chromium) 后端：GPU 合成 + 惯性滚动
    webview.start(gui="edgechromium" if _has_edge() else None, debug=False)
    return 0


def _has_edge() -> bool:
    """检测 WebView2 运行时是否可用（Windows 上 EdgeWebView 目录存在即用）。"""
    try:
        import os

        pf = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        return os.path.isdir(os.path.join(pf, "Microsoft", "EdgeWebView", "Application"))
    except Exception:
        return False
