"""增量轮询面板：对已采集视频定时自动补采新弹幕/评论。

界面从上到下：
    控制行（间隔/开始停止轮询/停止当前任务/批量刷新）
    当前状态栏（状态 + 进度 [i/n] + 下次轮询）
    实时抓取内容（增量抓到的评论逐条展示）
    更新日志（任务级提示）
    已采集视频表
"""
from __future__ import annotations

import datetime
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from crawler.tasks.increment import incremental_update
from gui.theme import C, F, S
from gui.util import load_videos
from gui.widgets import SmartTable

# 每个 200ms tick 最多渲染的增量评论条数；超出的仅计数不渲染(数据已写库)
_ITEM_RENDER_BUDGET = 20
# 实时流行数上限：超出截掉头部，防 Tk Text 长行数 O(n) 卡死
_MAX_STREAM_LINES = 2000


class UpdatePanel(ttk.Frame):
    """周期轮询：每个周期对所有已采集视频跑增量更新。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.msg_queue: queue.Queue = queue.Queue()
        self._running = False
        self._timer_id = None
        self._stop_flag: threading.Event | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=2)
        self.rowconfigure(3, weight=2)
        self.rowconfigure(4, weight=3)

        top = ttk.Frame(self, padding=(6, 6))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="周期轮询间隔(分钟):").pack(side="left")
        self.var_interval = tk.StringVar(value="30")
        ttk.Spinbox(top, from_=1, to=1440, textvariable=self.var_interval, width=5).pack(side="left", padx=4)
        ttk.Label(top, text="UP最大抓页数:").pack(side="left")
        self.var_max_pages = tk.StringVar(value="100")
        ttk.Spinbox(top, from_=1, to=500, textvariable=self.var_max_pages, width=5).pack(side="left", padx=4)
        self.btn_toggle = ttk.Button(top, text="开始轮询", command=self._toggle)
        self.btn_toggle.pack(side="left", padx=10)
        ttk.Button(top, text="立即补采一次", command=self._run_once).pack(side="left")
        ttk.Button(top, text="批量更新历史博主",
                   command=self._run_ups_batch).pack(side="left", padx=10)
        ttk.Button(top, text="刷新视频列表", command=self._refresh_videos).pack(side="left", padx=10)
        self.btn_stop = ttk.Button(top, text="■ 停止任务", command=self._stop_task,
                                   state="disabled")
        self.btn_stop.pack(side="right", padx=6)

        # 当前状态栏
        status = ttk.LabelFrame(self, text="当前状态", padding=(4, 2))
        status.grid(row=1, column=0, sticky="ew", padx=6)
        self.lbl_state = ttk.Label(status, text="空闲，等待开始", style="Secondary.TLabel")
        self.lbl_state.pack(side="left")
        self.lbl_progress = ttk.Label(status, text="进度: --", foreground=C.ACCENT)
        self.lbl_progress.pack(side="left", padx=4)
        self.lbl_next = ttk.Label(status, text="")
        self.lbl_next.pack(side="right", padx=4)

        # 实时内容流
        stream = ttk.LabelFrame(self, text="实时抓取内容（增量评论）", padding=(4, 2))
        stream.grid(row=2, column=0, sticky="nsew", padx=6)
        stream.columnconfigure(0, weight=1)
        stream.rowconfigure(0, weight=1)
        self.txt_stream = tk.Text(stream, height=8, wrap="word", state="disabled",
                                  font=F.MONO_SM)
        sb0 = ttk.Scrollbar(stream, command=self.txt_stream.yview)
        self.txt_stream.configure(yscrollcommand=sb0.set)
        self.txt_stream.grid(row=0, column=0, sticky="nsew")
        sb0.grid(row=0, column=1, sticky="ns")

        # 更新日志
        log = ttk.LabelFrame(self, text="更新日志", padding=(4, 2))
        log.grid(row=3, column=0, sticky="nsew", padx=6, pady=(6, 0))
        log.columnconfigure(0, weight=1)
        log.rowconfigure(0, weight=1)
        self.txt = tk.Text(log, height=6, wrap="word", state="disabled", font=F.MONO_SM)
        sb = ttk.Scrollbar(log, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # 已采集视频表
        grid = ttk.LabelFrame(self, text="已采集视频", padding=(4, 2))
        grid.grid(row=4, column=0, sticky="nsew", padx=6, pady=(6, 0))
        grid.columnconfigure(0, weight=1)
        grid.rowconfigure(0, weight=1)
        self.tree = SmartTable(grid, show="headings")
        self.tree.set_columns(
            [("bvid", "BV", 16), ("title", "标题", 52),
             ("cmt", "评论数", 8), ("dmk", "弹幕数", 8),
             ("flag", "状态", 10)]
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb2 = ttk.Scrollbar(grid, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb2.set)
        sb2.grid(row=0, column=1, sticky="ns")
        self._load_videos_async()

    # ---------------- 会话状态（重启恢复） ----------------
    def snapshot_state(self) -> dict:
        return {"var_interval": self.var_interval.get(),
                "var_max_pages": self.var_max_pages.get()}

    def restore_state(self, d: dict) -> None:
        for name in ("var_interval", "var_max_pages"):
            val = d.get(name)
            if isinstance(val, str):
                try:
                    getattr(self, name).set(val)
                except (AttributeError, tk.TclError):
                    pass

    # ---------------- 操作 ----------------
    def _append_log(self, line: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        self.txt.configure(state="disabled")
        try:
            self.txt.see("end")
        except tk.TclError:
            pass

    def _append_stream(self, line: str) -> None:
        self.txt_stream.configure(state="normal")
        self.txt_stream.insert("end", line + "\n")
        # 行数封顶：超过上限截掉头部一半，防 Tk Text 行数增长导致 O(n) 卡死
        try:
            first = int(self.txt_stream.index("end-1c").split(".")[0])
            if first > _MAX_STREAM_LINES:
                self.txt_stream.delete("1.0", f"{first - _MAX_STREAM_LINES // 2}.0")
        except tk.TclError:
            pass
        self.txt_stream.configure(state="disabled")
        try:
            self.txt_stream.see("end")
        except tk.TclError:
            pass

    def _refresh_videos(self) -> None:
        # 后台线程重载，完成后回填表格（不阻塞 UI）
        self._load_videos_async()

    def _load_videos_async(self) -> None:
        """异步加载已采集视频列表（启动/刷新时调用）。

        generation 标记：并发多次调用时，旧轮询循环与旧 worker 自动失效。
        """
        self._vload_gen = getattr(self, "_vload_gen", 0) + 1
        gen = self._vload_gen
        self._vload_q = queue.Queue()
        threading.Thread(target=self._vload_worker, args=(gen,), daemon=True).start()
        self.after(150, lambda: self._vload_poll(gen))

    def _vload_worker(self, gen: int) -> None:
        try:
            videos = load_videos()
        except Exception:
            videos = None
        if gen == getattr(self, "_vload_gen", 0):
            self._vload_q.put(videos)

    def _vload_poll(self, gen: int) -> None:
        if gen != getattr(self, "_vload_gen", 0):
            return  # 已有更新的加载在跑，本循环退役
        try:
            videos = self._vload_q.get_nowait()
        except queue.Empty:
            self.after(150, lambda: self._vload_poll(gen))
            return
        self._render_videos(videos)

    def _render_videos(self, videos: list) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for v in videos or []:
            c = v.get("comment_count", 0)
            d = v.get("danmaku_count", 0)
            c_done = v.get("comment_done")
            d_done = v.get("danmaku_done")
            flag = ""
            if c_done or d_done:
                flag = "完成" if (c_done or not c) and (d_done or not d) else "未完"
            self.tree.insert("", "end", values=(v["bvid"], v["title"][:60], c, d, flag))

    def _toggle(self) -> None:
        if self._running:
            self._running = False
            if self._timer_id is not None:
                self.after_cancel(self._timer_id)
                self._timer_id = None
            self.btn_toggle.configure(text="开始轮询")
            self._append_log("=== 轮询已停止 ===")
            self._set_state("空闲，等待开始", progress="")
            return
        try:
            minutes = float(self.var_interval.get())
        except ValueError:
            messagebox.showerror("错误", "轮询间隔需为数字（分钟）")
            return
        if minutes < 1:
            minutes = 1
        self._running = True
        self.btn_toggle.configure(text="停止轮询")
        self._append_log(f"=== 开始轮询，间隔 {minutes:.0f} 分钟 ===")
        self._run_once()
        self._schedule(minutes)

    def _schedule(self, minutes: float) -> None:
        if not self._running:
            return
        self._timer_id = self.after(int(minutes * 60 * 1000), self._on_cycle)
        self._set_state(
            f"轮询运行中（间隔 {minutes:.0f} 分钟）",
            next_=f"下次: {self._minutes_time(minutes)}",
        )

    def _on_cycle(self) -> None:
        if not self._running:
            return
        self._run_once()
        try:
            minutes = float(self.var_interval.get())
            if minutes < 1:
                minutes = 1
        except ValueError:
            minutes = 30
        self._schedule(minutes)

    @staticmethod
    def _minutes_time(minutes: float) -> str:
        import datetime
        return (datetime.datetime.now() + datetime.timedelta(minutes=minutes)).strftime("%H:%M")

    def _set_state(self, text: str, *, progress: str = "", next_: str = "") -> None:
        try:
            self.lbl_state.configure(text=text)
            self.lbl_progress.configure(text=f"进度: {progress}" if progress else "进度: --")
            self.lbl_next.configure(text=next_)
        except tk.TclError:
            pass

    def _run_once(self) -> None:
        self._spawn_task(self._worker_run, "=== 开始一轮增量补采 ===")

    def _run_ups_batch(self) -> None:
        self._spawn_task(self._worker_ups_batch,
                         "=== 历史博主批量增量更新启动 ===")

    def _spawn_task(self, target, intro: str) -> None:
        if self._worker_alive():
            self._append_log("[跳过] 已有后台任务在运行")
            return
        self._append_log(intro)
        self._stop_flag = threading.Event()
        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()
        try:
            self.btn_stop.configure(state="normal")
        except tk.TclError:
            pass
        self._set_state("任务执行中…")
        self.after(150, self._poll_worker)

    def _worker_alive(self) -> bool:
        return getattr(self, "_worker", None) is not None and self._worker.is_alive()

    def _on_comment(self, kind: str, raw: dict) -> None:
        """评论 item 回调：格式化一条到实时内容流。

        兼容两种输入：原始 API 结构（content 为 dict{message} 或 str）
        与 normalize 后的结构（content 为 str）。
        """
        try:
            c = raw.get("content")
            content = c.get("message", "") if isinstance(c, dict) else str(c or "")
            member = raw.get("member")
            member_name = (member.get("uname", "") if isinstance(member, dict)
                           else str(member or "")) or raw.get("mid", "")
            ts = raw.get("ctime", 0)
            tstr = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""
            self._post_stream(f"[评分] {tstr} {member_name} | 赞{raw.get('like', 0)} | {content}")
        except Exception as exc:
            logger = __import__("logging").getLogger(__name__)
            logger.exception("评论流回调失败: %s", exc)

    def _worker_run(self) -> None:
        try:
            from crawler.api.client import ApiClient
            from storage import dao

            client = ApiClient()
            aids = dao.list_all_video_aids()
            if not aids:
                self._post_log("库中暂无已采集视频，跳过")
                return
            incremental_update(client, aids, progress_cb=self._post_log,
                               item_cb=self._on_comment,
                               stop_flag=self._stop_flag)
            self._post_log(f"增量补采完成：处理 {len(aids)} 个视频（明细见日志文件）")
        except Exception as exc:
            self._post_log(f"[错误] 增量补采失败: {exc}")

    def _worker_ups_batch(self) -> None:
        try:
            from crawler.api.client import ApiClient
            from crawler.tasks.increment import update_ups_batch
            from storage import dao

            rows = dao.query_up_stats()
            uids = [r["mid"] for r in rows]
            if not uids:
                self._post_log("库中暂无已记录的博主，跳过")
                return
            try:
                max_pages = max(1, int(self.var_max_pages.get() or 100))
            except ValueError:
                max_pages = 100
            self._post_log(f"共 {len(uids)} 位博主待增量更新，每个UP最多抓 {max_pages} 页…")
            client = ApiClient()
            result = update_ups_batch(
                client, uids, max_pages=max_pages,
                progress_cb=self._post_log,
                item_cb=self._on_comment,
                stop_flag=self._stop_flag,
            )
            self._post_log(
                f"[完成] 博主批量增量更新：处理 {result['ups']} / 成功 "
                f"{result['ups_ok']} / 评论 {result['comments']} 条"
            )
            if result.get("errors"):
                self._post_log(f"[提示] {result['errors'][-1]}")
        except Exception as exc:
            self._post_log(f"[错误] 批量博主更新失败: {exc}")

    def _post_log(self, text: str) -> None:
        try:
            self.msg_queue.put(("log", text))
        except Exception:
            pass

    def _post_stream(self, text: str) -> None:
        try:
            self.msg_queue.put(("item", text))
        except Exception:
            pass

    def _stop_task(self) -> None:
        """请求停止当前正在运行的后台任务（下一检查点生效）。"""
        if self._stop_flag is None:
            return
        self._stop_flag.set()
        try:
            self.btn_stop.configure(state="disabled")
        except tk.TclError:
            pass
        self._append_log("已请求停止任务，将在当前视频处理完后退出…")
        self._set_state("正在停止当前任务…")

    def _poll_worker(self) -> None:
        import re
        rendered = 0
        try:
            while True:
                tag, text = self.msg_queue.get_nowait()
                if tag == "item":
                    # 渲染预算：高吞吐时跳过渲染只计数(数据已写库)
                    if rendered >= _ITEM_RENDER_BUDGET:
                        continue
                    rendered += 1
                    self._append_stream(text)
                    continue
                self._append_log(text)
                m = re.search(r"\[(\d+)/(\d+)\]", text)
                if m:
                    self._set_state("任务执行中…",
                                    progress=f"{m.group(1)}/{m.group(2)}")
                elif "完成" in text or "失败" in text or "停止" in text:
                    try:
                        from gui.util import ping_main as _ping
                        _ping(self, "增量轮询")
                    except Exception:
                        pass
                    self._set_state(
                        "空闲" if not self._running else
                        f"轮询运行中（间隔 {self.var_interval.get() or 30} 分钟）",
                        progress="", next_="" if not self._running
                        else f"下次: {self._minutes_time(float(self.var_interval.get()) or 30)}")
        except (queue.Empty, ValueError):
            pass
        if self._worker_alive():
            self.after(200, self._poll_worker)
        else:
            self._worker = None
            try:
                self.btn_stop.configure(state="disabled")
            except tk.TclError:
                pass
            self._refresh_videos()