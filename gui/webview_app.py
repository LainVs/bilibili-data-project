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
import threading
import uuid

logger = logging.getLogger(__name__)

WEBUI_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "webui"


class _TaskRegistry:
    """长任务注册表：前端拿 task_id 轮询进度，避免单次调用超时阻塞。

    任务体在独立线程执行，进度写入内存字典；完成后保留 10 分钟供前端
    取走结果（防止前端稍晚一轮轮询就取不到）。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, label: str) -> str:
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[tid] = {"id": tid, "label": label, "state": "running",
                                "progress": "", "result": None, "error": None}
        return tid

    def update(self, tid: str, progress: str) -> None:
        with self._lock:
            t = self._tasks.get(tid)
            if t is not None:
                t["progress"] = progress

    def finish(self, tid: str, result=None, error: str | None = None) -> None:
        with self._lock:
            t = self._tasks.get(tid)
            if t is None:
                return
            t["result"] = result
            t["error"] = error
            t["state"] = "failed" if error else "done"

    def get(self, tid: str) -> dict:
        with self._lock:
            t = self._tasks.get(tid)
            return dict(t) if t else {"id": tid, "state": "unknown"}

    def purge(self) -> None:
        with self._lock:
            for tid in [k for k, v in self._tasks.items() if v["state"] != "running"]:
                self._tasks.pop(tid, None)


_TASKS = _TaskRegistry()


class _CollectRunner:
    """采集任务运行器：把 worker 的消息队列批量推送给前端。

    WebView2 相对 Tk 的核心优势在这里体现：前端用 DocumentFragment
    批量上屏 + 上限截断，几万条弹幕也能流畅滚动；Tk 版受限于 Text
    控件每帧全量重排，只能丢弃预览帧。

    推送策略：120ms 一批，每批最多 60 条（与 Tk 版预算对齐），
    超出部分只更新计数不渲染，数据仍完整写入数据库。
    """

    BATCH_MS = 120
    BATCH_MAX = 60

    def __init__(self, window, msg_q) -> None:
        self.win = window
        self.q = msg_q
        self.running = False
        self.stats = {"comments": 0, "danmaku": 0}
        self.worker = None
        self._job = None

    def start(self, worker) -> None:
        self.worker = worker
        self.running = True
        worker.start()
        self._tick()

    def stop(self) -> None:
        self.running = False
        if self.worker is not None and hasattr(self.worker, "stop"):
            try:
                self.worker.stop()
            except Exception:
                pass

    def _push(self, payload: dict) -> None:
        """把一批消息推给前端（JSON 序列化由 pywebview 处理）。"""
        try:
            import json

            self.win.evaluate_js(
                f"window.onCollectBatch({json.dumps(payload, ensure_ascii=False)})")
        except Exception:
            pass

    def _tick(self) -> None:
        if not self.running:
            return
        import queue as _q

        items: list[dict] = []
        events: list[dict] = []
        dropped = 0
        for _ in range(800):
            try:
                msg = self.q.get_nowait()
            except _q.Empty:
                break
            mtype = msg.get("type")
            if mtype == "item":
                kind = msg.get("kind", "comment")
                self.stats[kind if kind in self.stats else "comments"] += 1
                if len(items) < self.BATCH_MAX:
                    items.append({"kind": kind, "data": msg.get("data") or {}})
                else:
                    dropped += 1
            else:
                events.append(msg)

        if items or events or dropped:
            self._push({"items": items, "events": events,
                        "dropped": dropped, "stats": dict(self.stats)})

        alive = self.worker is not None and self.worker.is_alive()
        if not alive:
            # 收尾：再清一次队列，确保最后的消息不丢
            tail_items, tail_events = [], []
            for _ in range(2000):
                try:
                    msg = self.q.get_nowait()
                except _q.Empty:
                    break
                if msg.get("type") == "item":
                    tail_items.append({"kind": msg.get("kind", "comment"),
                                       "data": msg.get("data") or {}})
                else:
                    tail_events.append(msg)
            if tail_items or tail_events:
                self._push({"items": tail_items[:200], "events": tail_events,
                            "dropped": max(0, len(tail_items) - 200),
                            "stats": dict(self.stats)})
            self.running = False
            self.worker = None
            self._push({"items": [], "events": [{"type": "runner_done"}],
                        "dropped": 0, "stats": dict(self.stats)})
            return

        import threading

        self._job = threading.Timer(self.BATCH_MS / 1000.0, self._tick)
        self._job.daemon = True
        self._job.start()


_RUNNERS: dict[str, _CollectRunner] = {}


class JsApi:
    """暴露给前端 JS 的后端桥。方法名即 window.pywebview.api.<name>。

    约定：所有方法返回可 JSON 序列化的 dict；绝不抛异常给前端
    （错误用 {"ok": False, "error": "..."} 表达），前端永远拿得到响应。
    长任务走 task_id + 轮询，避免单次调用阻塞过久。
    """

    def __init__(self) -> None:
        self._win = None  # 由 run() 注入，供实时推送使用

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

    # ── 研报页：长任务异步化 ──
    def start_report(self, bvid: str, out_name: str = "", limit: int = 0,
                     sections: dict | None = None, auto_sync: bool = True) -> dict:
        """启动研报生成，立即返回 task_id；前端用 task_status 轮询。"""
        from gui.web_bridge import generate_report_task

        tid = _TASKS.create(f"研报 {bvid}")

        def _run() -> None:
            try:
                res = generate_report_task(
                    bvid, out_name, limit, sections, auto_sync,
                    progress_cb=lambda m: _TASKS.update(tid, m))
                _TASKS.finish(tid, result=res)
            except Exception as exc:
                logger.exception("研报生成失败")
                _TASKS.finish(tid, error=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "task_id": tid}

    def start_export(self, bvid: str, path: str, fmt: str = "sqlite") -> dict:
        """启动数据打包导出（SQLite/XLSX），返回 task_id。"""
        from gui.web_bridge import export_bundle_task

        tid = _TASKS.create(f"导出 {fmt.upper()}")

        def _run() -> None:
            try:
                _TASKS.update(tid, f"正在打包 {bvid} 全部数据到 {fmt.upper()} ...")
                res = export_bundle_task(bvid, path, fmt)
                _TASKS.finish(tid, result=res)
            except Exception as exc:
                logger.exception("打包导出失败")
                _TASKS.finish(tid, error=str(exc))

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "task_id": tid}

    def task_status(self, task_id: str) -> dict:
        """轮询长任务状态：running / done / failed / unknown。"""
        return _TASKS.get(task_id)

    # ── 采集页 ──
    def start_collect(self, target: str, mode: str = "video",
                      comments: bool = True, danmaku: bool = False) -> dict:
        """启动采集任务，消息通过 window.onCollectBatch 实时推送。"""
        import queue as _q

        try:
            from gui.crawl_worker import CrawlWorker, PostCrawlWorker
            from gui.web_bridge import extract_target_bvid

            target = (target or "").strip()
            if not target:
                return {"ok": False, "error": "请输入目标"}
            if not comments and not danmaku and mode != "post":
                return {"ok": False, "error": "请至少勾选评论或弹幕"}
            if self._win is None:
                return {"ok": False, "error": "窗口未就绪"}

            existing = _RUNNERS.get("current")
            if existing is not None and existing.running:
                return {"ok": False, "error": "已有采集任务在运行，请先停止"}

            if mode == "post":
                q = _q.Queue()
                worker = PostCrawlWorker(q, target=target,
                                         comments=comments, incremental=True)
            else:
                parsed = extract_target_bvid(target)
                if not parsed.get("ok"):
                    return {"ok": False, "error": parsed.get("error", "无法识别 BV 号")}
                if parsed.get("kind") == "shortlink":
                    return {"ok": False, "need_resolve": True,
                            "link": parsed["link"],
                            "error": "需要先解析短链"}
                bvid = parsed["bvid"]
                q = _q.Queue()
                worker = CrawlWorker(q, bvid=bvid,
                                     comments=comments, danmaku=danmaku)

            runner = _CollectRunner(self._win, q)
            _RUNNERS["current"] = runner
            runner.start(worker)
            return {"ok": True, "runner": "current",
                    "target": target, "mode": mode}
        except Exception as exc:
            logger.exception("start_collect failed")
            return {"ok": False, "error": str(exc)}

    def stop_collect(self) -> dict:
        """停止当前采集任务（软停止：已采数据仍会落库）。"""
        runner = _RUNNERS.get("current")
        if runner is None or not runner.running:
            return {"ok": False, "error": "当前没有运行中的任务"}
        runner.stop()
        return {"ok": True}

    def resolve_link(self, link: str) -> dict:
        """解析 b23.tv 短链（联网，前端 await）。"""
        from gui.web_bridge import resolve_shortlink

        try:
            return resolve_shortlink(link)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_video_options(self) -> dict:
        """已采集视频下拉列表。"""
        from gui.web_bridge import load_video_options

        try:
            return load_video_options()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "items": []}

    def get_up_stats(self, uid_filter: str = "") -> dict:
        """UP 主采集统计表。"""
        from gui.web_bridge import load_up_stats

        try:
            return load_up_stats(uid_filter)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "rows": []}

    def get_stored_content(self, bvid: str, limit: int = 200) -> dict:
        """查看库内已存的评论/弹幕。"""
        from gui.web_bridge import load_stored_content

        try:
            return load_stored_content(bvid, limit)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
    window = webview.create_window(
        "B站弹幕/评论采集系统",
        str(index),
        js_api=api,
        width=1180,
        height=760,
        min_size=(960, 640),
        background_color="#faf9f5",
    )
    api._win = window  # 实时推送需要窗口句柄
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
