"""采集面板：输入目标(视频/帖子)，选择弹幕/评论，实时预览抓取内容。"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from gui.crawl_worker import (
    MSG_DONE,
    MSG_ERROR,
    MSG_ITEM,
    MSG_META,
    MSG_PROGRESS,
    CrawlWorker,
    PostCrawlWorker,
    UpCrawlWorker,
)
from gui.theme import C, F
from gui.util import format_comment_line, format_danmaku_line, load_videos, video_summary
from gui.widgets import PillTabs, SmartTable

logger = logging.getLogger(__name__)

# 实时流行数上限：超出截掉头部，防 Tk Text 长行数 O(n) 卡死
_MAX_STREAM_LINES = 1500
# 每个 120ms tick 最多渲染的评论/弹幕条数；超出的仅计数不渲染(数据仍写库)
_ITEM_RENDER_BUDGET = 30


class CollectPanel(ttk.Frame):
    """定向采集：输入 BV/视频链接，实时查看内容流+进度。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.worker: CrawlWorker | None = None
        self.msg_q: queue.Queue = queue.Queue()
        self._videos: list[dict] = []
        self._up_rows: dict[int, str] = {}
        self._stick_bottom = True  # 是否跟随实时流底部（用户上翻后自动置 False）
        self._drag = False  # 是否正在拖动滚动条
        self._doing_kind = ""  # 当前视频正在采的类型 comment/danmaku
        self._doing_cnt = 0    # 当前视频该类型已采条数
        self._bread_rel = ""   # 面包屑：当前关系用户（关系采集时）
        self._bread_folder = ""  # 面包屑：当前收藏夹
        self._bread_video = ""   # 面包屑：当前视频标题（视频级进度消息解析）
        self._pause_stream = tk.BooleanVar(value=True)
        self._build_ui()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)  # 中间区域弹性
        self.rowconfigure(3, weight=0)

        # 目标输入行 + 采集模式
        top = ttk.Frame(self, padding=(6, 6))
        top.grid(row=0, column=0, sticky="ew")
        self.var_mode = tk.StringVar(value="video")
        self.var_target = tk.StringVar()
        self.cbo_videos: ttk.Combobox | None = None

        def _on_mode(_e=None) -> None:
            mode = self.var_mode.get()
            try:
                if hasattr(self, "pill_modes") and self.pill_modes.get() != mode:
                    self.pill_modes.set(mode)
            except Exception:
                pass
            entry_label.configure(
                text={"video": "视频 BV / 链接:",
                      "up": "UP主 UID:",
                      "fav": "收藏夹用户 UID:",
                      "rel": "关系用户 UID:",
                      "batch": "批量UP:",
                      "post": "帖子链接 / ID:"}[mode])
            # 仅“关注/粉丝”模式显示方向与人数上限控件
            for w in (lbl_dir, cbo_dir, lbl_max, spn_max):
                if mode == "rel":
                    w.pack(side="left", padx=(6 if w is lbl_dir else 0, 0))
                else:
                    try:
                        w.pack_forget()
                    except tk.TclError:
                        pass
            # 仅“批量UP”模式显示多行UID输入区
            if mode == "batch":
                self.batch_frame.grid(row=1, column=0, sticky="ew", padx=6)
            else:
                try:
                    self.batch_frame.grid_forget()
                except (AttributeError, tk.TclError):
                    pass

        self._on_mode = _on_mode  # 会话恢复/断点续采需要：切换模式界面同步

        tabs = [
            ("video", "视频"),
            ("up", "UP主"),
            ("fav", "收藏夹"),
            ("rel", "关注/粉丝"),
            ("batch", "批量UP"),
            ("post", "帖子/专栏"),
        ]
        self.pill_modes = PillTabs(
            top,
            tabs=tabs,
            initial_key=self.var_mode.get(),
            on_change=lambda k: (self.var_mode.set(k), _on_mode()),
        )
        self.pill_modes.pack(side="left", padx=(0, 10))

        entry_label = ttk.Label(top, text="视频 BV / 链接:")
        entry_label.pack(side="left", padx=(2, 0))
        entry = ttk.Entry(top, textvariable=self.var_target, width=22)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self._toggle())

        self.var_dir = tk.StringVar(value="both")
        lbl_dir = ttk.Label(top, text="方向:")
        cbo_dir = ttk.Combobox(top, textvariable=self.var_dir, state="readonly",
                               values=("both", "following", "follower"),
                               width=9)
        lbl_max = ttk.Label(top, text="人数上限(0=不限):")
        self.var_max_users = tk.StringVar(value="0")
        spn_max = ttk.Spinbox(top, from_=0, to=100000, width=6,
                              textvariable=self.var_max_users)

        self.var_comments = tk.BooleanVar(value=True)
        self.var_danmaku = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="评论", variable=self.var_comments).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(top, text="弹幕", variable=self.var_danmaku).pack(side="left", padx=4)
        self.var_incremental = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="增量(去重)", variable=self.var_incremental).pack(side="left", padx=6)

        self.btn_start = ttk.Button(top, text="开始采集", style="Accent.TButton", command=self._toggle)
        self.btn_start.pack(side="left", padx=(10, 0))

        # 批量采集：贴 UID + 勾选要采集的类型（运行中可追加）区（grid 在 row=1，由模式切换显示）
        self.batch_frame = ttk.LabelFrame(
            self, text="批量采集（粘贴UID，每行一个，可逗号/空格。运行中可继续粘贴点“追加”）",
            padding=(4, 2))
        bf = ttk.Frame(self.batch_frame)
        bf.pack(fill="x")
        self.txt_batch_uids = tk.Text(bf, height=3, width=42, font=F.MONO_SM)
        self.txt_batch_uids.pack(side="left", fill="both", expand=True, padx=(0, 6))
        ctrls = ttk.Frame(bf)
        ctrls.pack(side="right", anchor="n")

        ckrow = ttk.Frame(ctrls)
        ckrow.pack(fill="x")
        ttk.Label(ckrow, text="对这些人采集:").pack(side="left")
        self.var_ck_up = tk.BooleanVar(value=True)
        self.var_ck_fav = tk.BooleanVar(value=False)
        self.var_ck_rel = tk.BooleanVar(value=False)
        ttk.Checkbutton(ckrow, text="UP视频", variable=self.var_ck_up).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(ckrow, text="收藏夹", variable=self.var_ck_fav).pack(side="left", padx=2)
        ttk.Checkbutton(ckrow, text="关注/粉丝", variable=self.var_ck_rel,
                        command=self._sync_rel_dir).pack(side="left", padx=2)

        relrow = ttk.Frame(ctrls)
        relrow.pack(fill="x", pady=(2, 0))
        ttk.Label(relrow, text="关系方向:").pack(side="left")
        self.var_rel_dir = tk.StringVar(value="both")
        self.var_rel_dir_dd = ttk.Combobox(relrow, textvariable=self.var_rel_dir, state="readonly",
                                           values=("both", "following", "follower"), width=9)
        self.var_rel_dir_dd.pack(side="left")

        self.btn_batch_start = ttk.Button(ctrls, text="◎ 开始批量采集", command=self._batch_toggle)
        self.btn_batch_start.pack(fill="x", pady=(4, 0))
        self.btn_batch_add = ttk.Button(ctrls, text="⊕ 追加(接续)", command=self._batch_append,
                                        state="disabled")
        self.btn_batch_add.pack(fill="x", pady=(2, 0))
        self.lbl_batch_state = ttk.Label(ctrls, text="空闲：粘贴UID开始批量")
        self.lbl_batch_state.pack(fill="x")
        self._sync_rel_dir()

        # 可拖拽布局：上=采集统计，下=实时内容流（row1 为批量UP输入区，仅批量模式占用）
        pane = ttk.Panedwindow(self, orient="vertical")
        pane.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 4))
        self.rowconfigure(2, weight=1)

        # ---- 上：采集统计区 ----
        sel = ttk.LabelFrame(pane, text="已采集统计", padding=(4, 2))
        pane.add(sel, weight=2)
        sel.columnconfigure(0, weight=1)

        head = ttk.Frame(sel)
        head.grid(row=0, column=0, sticky="ew")
        self.lbl_total = ttk.Label(head, text="已保存视频：加载中…")
        self.lbl_total.pack(side="left")
        ttk.Label(head, text="  UID筛选:").pack(side="left", padx=(10, 0))
        self.var_filter_uid = tk.StringVar()
        self.ent_filter_uid = ttk.Entry(head, textvariable=self.var_filter_uid, width=12)
        self.ent_filter_uid.pack(side="left", padx=4)
        self.ent_filter_uid.bind("<Return>", lambda _e: self._refresh_up_stats())
        ttk.Button(head, text="筛选", command=self._refresh_up_stats).pack(side="left")
        ttk.Button(head, text="清除", command=self._clear_uid_filter).pack(side="left", padx=2)

        self.up_tree = SmartTable(sel, show="headings")
        self.up_tree.set_columns(
            [("uid", "UP (UID)", 22), ("total", "视频总数", 9),
             ("fetched", "已采集视频", 11), ("cmt", "评论", 9),
             ("dmk", "弹幕", 9)]
        )
        self.up_tree.grid(row=1, column=0, sticky="ew")
        self.up_tree.bind("<<TreeviewSelect>>", self._filter_by_up)

        row = ttk.Frame(sel)
        row.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.cbo_videos = ttk.Combobox(row, state="readonly", width=78, height=24)
        self.cbo_videos.pack(side="left", fill="x", expand=True)
        self.cbo_videos.bind("<<ComboboxSelected>>", self._pick_video)
        self.cbo_videos.bind("<Button-1>", lambda _e: self._refresh_video_list())
        # 启动错峰：等窗口渲染完再后台加载视频列表，避免与 DB 探测并发抢连接
        self.after(1200, self._load_videos_async)
        ttk.Button(row, text="导出BV到输入框",
                   command=lambda: self._pick_video(_fill_only=True)).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="查看库内内容", command=self._view_stored).pack(side="left", padx=4)

        # ---- 下：实时内容流 ----
        stream = ttk.LabelFrame(pane, text="实时抓取内容", padding=(4, 2))
        pane.add(stream, weight=3)
        stream.columnconfigure(0, weight=1)
        stream.rowconfigure(0, weight=1)

        self.txt = tk.Text(stream, height=16, wrap="none", state="disabled",
                           font=F.MONO, background=C.BG_CARD)
        sb_y = ttk.Scrollbar(stream, orient="vertical",
                             command=self._sb_yview_wrapper)
        sb_x = ttk.Scrollbar(stream, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        # 用户手动上翻（滚轮/触控板/拖动滚动条）→ 解除"跟随底部"
        self.txt.bind("<MouseWheel>", self._on_wheel)
        self.txt.bind("<Button-4>", lambda _e: self._user_scroll_up())
        self.txt.bind("<Button-5>", lambda _e: self._user_scroll_down())
        self.txt.bind("<KeyPress-Up>", lambda _e: self._mark_no_follow())
        self.txt.bind("<KeyPress-Down>", lambda _e: self._mark_no_follow())
        # 滚动条拖动：按下即锁定不跟随，松开时按位置恢复
        sb_y.bind("<ButtonPress-1>", lambda _e: setattr(self, "_drag", True))
        sb_y.bind("<ButtonRelease-1>", self._on_sb_release)

        # 状态行
        status = ttk.Frame(self, padding=(6, 2))
        status.grid(row=3, column=0, sticky="ew")
        self.prog = ttk.Progressbar(status, orient="horizontal",
                                    length=180, mode="determinate", maximum=100)
        self.prog.pack(side="left", padx=(0, 6))
        self.lbl_vpos = ttk.Label(status, text="", foreground=C.ACCENT)
        self.lbl_vpos.pack(side="left", padx=(0, 8))
        self.lbl_status = ttk.Label(status, text="就绪。输入 BV 或视频链接开始。")
        self.lbl_status.pack(side="left")
        ttk.Checkbutton(status, text="暂停实时流", variable=self._pause_stream).pack(side="right", padx=(6, 0))
        ttk.Button(status, text="清空流", command=self._clear_stream).pack(side="right")

        # 最底部：实时“正在干什么”指示条
        action = ttk.Frame(self, padding=(6, 2))
        action.grid(row=4, column=0, sticky="ew")
        ttk.Label(action, text="正在:", foreground=C.FG_HINT).pack(side="left")
        self.lbl_doing = ttk.Label(action, text="空闲", foreground=C.ACCENT,
                                   font=F.SUBTITLE)
        self.lbl_doing.pack(side="left", padx=(4, 12))
        self.lbl_doing_cnt = ttk.Label(action, text="", foreground=C.FG_SECONDARY)
        self.lbl_doing_cnt.pack(side="left")

    # ---------------- 会话状态（重启恢复） ----------------
    def _app(self):
        """顶层 MainWindow（任务断点通知用；非 MainWindow 时返回 None）。"""
        top = self.winfo_toplevel()
        return top if hasattr(top, "_on_task_started") else None

    def _task_desc(self) -> str:
        """当前任务的人类可读描述（写入会话，供续采提示）。"""
        mode = self.var_mode.get()
        kinds = [k for k, on in (("评论", self.var_comments.get()),
                                 ("弹幕", self.var_danmaku.get())) if on]
        extra = f" ({'+'.join(kinds)})" if kinds else ""
        if mode == "batch":
            n = len(self._parse_uids(self._batch_text()))
            ck = [k for k, on in (("UP", self.var_ck_up.get()),
                                  ("收藏夹", self.var_ck_fav.get()),
                                  ("关注/粉丝", self.var_ck_rel.get())) if on]
            base = f"批量 {n} 个UID ({'+'.join(ck) or '无'})"
        else:
            target = self.var_target.get().strip()
            base = {
                "video": f"视频 {target}",
                "up": f"UP主 UID={target}",
                "fav": f"收藏夹 UID={target}",
                "rel": f"关注/粉丝 UID={target}",
                "post": f"帖子 {target}",
            }.get(mode, mode)
        return base + extra + ("，增量" if self.var_incremental.get() else "，全量")

    def snapshot_state(self) -> dict:
        return {
            "var_mode": self.var_mode.get(),
            "var_target": self.var_target.get(),
            "var_dir": self.var_dir.get(),
            "var_max_users": self.var_max_users.get(),
            "var_comments": self.var_comments.get(),
            "var_danmaku": self.var_danmaku.get(),
            "var_incremental": self.var_incremental.get(),
            "batch_uids": self._batch_text(),
            "var_ck_up": self.var_ck_up.get(),
            "var_ck_fav": self.var_ck_fav.get(),
            "var_ck_rel": self.var_ck_rel.get(),
            "var_rel_dir": self.var_rel_dir.get(),
        }

    def restore_state(self, d: dict) -> None:
        """把上次会话的表单值回填（控件均已构建，仅 set 不重建界面）。"""
        for name in ("var_mode", "var_target", "var_dir", "var_max_users"):
            val = d.get(name)
            if isinstance(val, str):
                try:
                    getattr(self, name).set(val)
                except (AttributeError, tk.TclError):
                    pass
        for name in ("var_comments", "var_danmaku", "var_incremental",
                     "var_ck_up", "var_ck_fav", "var_ck_rel"):
            if name in d:
                try:
                    getattr(self, name).set(bool(d.get(name)))
                except (AttributeError, tk.TclError):
                    pass
        batch = d.get("batch_uids")
        if isinstance(batch, str):
            try:
                self.txt_batch_uids.delete("1.0", "end")
                self.txt_batch_uids.insert("1.0", batch)
            except (AttributeError, tk.TclError):
                pass
        rel_dir = d.get("var_rel_dir")
        if isinstance(rel_dir, str):
            try:
                self.var_rel_dir.set(rel_dir)
            except (AttributeError, tk.TclError):
                pass
        try:
            self._on_mode()
            self._sync_rel_dir()
        except (AttributeError, tk.TclError):
            pass

    def start_task(self) -> None:
        """断点续采：按已恢复的表单直接启动任务（批量模式自动转发）。"""
        self._toggle()

    # ---------------- 批量UP ----------------
    @staticmethod
    def _to_int(s) -> int:
        try:
            return int(s)
        except (TypeError, ValueError):
            return 0

    # ---------------- 交互 ----------------
    def _refresh_up_stats(self) -> None:
        """刷新 UP 汇总表（后台线程，不卡UI）。"""
        def worker():
            try:
                from storage import dao
                total_videos = dao.count_saved_videos()
            except Exception as exc:
                logger.warning("视频总数查询失败: %s", exc)
                total_videos = 0
            try:
                stats = dao.query_up_stats()
            except Exception as exc:
                logger.warning("UP 汇总查询失败: %s", exc)
                stats = []
            self.after(0, lambda: self._apply_up_stats(total_videos, stats))
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _apply_up_stats(self, total_videos: int, stats: list) -> None:
        """主线程应用统计结果。"""
        # UID 筛选：仅显示匹配的 UP 行
        fuid = self.var_filter_uid.get().strip() if hasattr(self, "var_filter_uid") else ""
        if fuid:
            try:
                fuid_n = int(fuid)
                stats = [s for s in stats if s.get("mid") == fuid_n]
            except (TypeError, ValueError):
                stats = [s for s in stats if fuid in str(s.get("mid", ""))]
        try:
            self.lbl_total.configure(text=f"已保存视频：{total_videos} 个")
        except Exception:
            pass
        prev = self._selected_up_mid()
        for item in self.up_tree.get_children():
            self.up_tree.delete(item)
        self._up_rows = {}  # mid(int) -> iid
        for s in stats:
            mid = s.get("mid")
            total = s.get("video_total") or 0
            got = s.get("video_fetched") or 0
            pct = f" {got}/{total}" if total else ""
            uid_cell = f"{mid}({s.get('uname') or ''})" if s.get("uname") else str(mid)
            iid = self.up_tree.insert("", "end", values=(
                uid_cell,
                total,
                f"{got}{pct}",
                s.get("comment_total") or 0,
                s.get("danmaku_total") or 0,
            ))
            self._up_rows[mid] = iid
        self._filter_by_up()
        if prev is not None:
            iid = self._up_rows.get(prev)
            if iid:
                try:
                    self.up_tree.selection_set(iid)
                except tk.TclError:
                    pass

    def _selected_up_mid(self):
        """当前选中 UP 行对应的 mid（未选中返回 None）。"""
        for item in self.up_tree.selection():
            for mid, iid in self._up_rows.items():
                if iid == item:
                    return mid
        return None

    def _up(self, mid) -> str:
        return self._up_rows.get(mid, "")

    def _filter_by_up(self, _e=None) -> None:
        """选中某 UP → 下方视频下拉只显示该 UP 的视频（纯 UI 过滤，不重查库）。"""
        self._apply_video_options()

    def _apply_video_options(self) -> None:
        """按当前 UP 过滤 self._videos 并填充下拉。"""
        mid = self._selected_up_mid()
        videos = ([v for v in self._videos if v.get("owner_uid") == mid]
                  if mid is not None else self._videos)
        self.cbo_videos["values"] = [video_summary(v) for v in videos]
        self.cbo_videos.set("")

    def _clear_uid_filter(self) -> None:
        self.var_filter_uid.set("")
        self._refresh_up_stats()

    def _refresh_video_list(self) -> None:
        # 后台线程重载全库列表，完成后回填下拉（不阻塞 UI）
        self._load_videos_async()

    def _load_videos_async(self) -> None:
        """异步加载已采集视频列表（启动/刷新时调用，避免 UI 卡死）。

        generation 标记：并发多次调用时，旧轮询循环与旧 worker 自动失效，
        不留下永久自旋的 after 循环。
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
        if videos:
            self._videos = videos
            self._apply_video_options()

    def _pick_video(self, _e=None, *, _fill_only: bool = False) -> None:
        idx = self.cbo_videos.current()
        if 0 <= idx < len(self._videos):
            self.var_target.set(self._videos[idx]["bvid"])

    def _view_stored(self) -> None:
        """查看已采集视频在库内的评论/弹幕（后台线程）。"""
        idx = self.cbo_videos.current()
        if not (0 <= idx < len(self._videos)):
            messagebox.showwarning("提示", "请先在“已采集视频”下拉框选择一个视频")
            return
        v = self._videos[idx]
        aid = v.get("aid")
        self.lbl_status.configure(text="查询中...")
        def worker():
            try:
                from storage import dao
                comments = dao.query_comments_by_video(aid, limit=800)
                danmaku = dao.query_danmaku_by_video(aid, limit=800)
                self.after(0, lambda: self._show_stored(v, comments, danmaku))
            except Exception as exc:
                # lambda 默认参数绑定，避免 except 结束后 exc 被解绑导致 NameError
                self.after(0, lambda e=str(exc): messagebox.showerror("查询失败", e))
                self.after(0, lambda: self.lbl_status.configure(text="查询失败"))
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _show_stored(self, v: dict, comments: list, danmaku: list) -> None:
        self._clear_stream()
        self.txt.configure(wrap="word")
        self._append_line(f"==== 库内内容 | {v.get('bvid', '')} | {v.get('title', '')[:60]} ====")
        self._append_line(f"-- 评论 {len(comments)} 条（含回复）--")
        for c in comments:
            self._append_line(format_comment_line(c))
        self._append_line(f"-- 弹幕 {len(danmaku)} 条 --")
        for d in danmaku:
            self._append_line(format_danmaku_line(d))
        self.lbl_status.configure(text=f"已加载库内内容：评论 {len(comments)} 条 / 弹幕 {len(danmaku)} 条（滚动查看）")
        self._stick_bottom = False
        self._see_top()

    def _see_top(self) -> None:
        try:
            self.txt.see("1.0")
        except tk.TclError:
            pass

    def _clear_stream(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self._stick_bottom = True  # 新一批内容恢复跟随底部

    def _append_line(self, line: str, *, see: bool = False) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        # 行数封顶：超过上限截掉头部一半，防止 Tk Text 行数增长导致 O(n) 卡死
        try:
            first = int(self.txt.index("end-1c").split(".")[0])
            if first > _MAX_STREAM_LINES:
                self.txt.delete("1.0", f"{first - _MAX_STREAM_LINES // 2}.0")
        except tk.TclError:
            pass
        self.txt.configure(state="disabled")
        if see:
            self._see_end()

    def _append_batch(self, lines: list[str], *, see: bool = False) -> None:
        """单次事务批量写入多行，大幅降低 Tk Text 组件重排开销。"""
        if not lines:
            return
        try:
            self.txt.configure(state="normal")
            self.txt.insert("end", "\n".join(lines) + "\n")
            first = int(self.txt.index("end-1c").split(".")[0])
            if first > _MAX_STREAM_LINES:
                self.txt.delete("1.0", f"{first - _MAX_STREAM_LINES // 2}.0")
            self.txt.configure(state="disabled")
            if see:
                self._see_end()
        except tk.TclError:
            pass

    def _see_end(self) -> None:
        try:
            self.txt.see("end")
        except tk.TclError:
            pass

    def _is_at_bottom(self) -> bool:
        """当前视口是否贴底（用户已向上翻看时返回 False，避免强制跳动）。"""
        try:
            if self.txt.compare("end-1c", "==", "1.0"):  # 空文本
                return True
            return self.txt.yview()[1] >= 0.999
        except tk.TclError:
            return True

    # ---- 用户滚动意图跟踪（事件级，不受内容量影响）----
    def _sb_yview_wrapper(self, *args) -> None:
        """滚动条命令转发：拖动/点按时同步"是否跟随底部"。"""
        if self.txt is not None:
            self.txt.yview(*args)
        if not self._drag:
            self._stick_bottom = self._is_at_bottom()

    def _on_sb_release(self, _e) -> None:
        self._drag = False
        self._stick_bottom = self._is_at_bottom()

    def _on_wheel(self, event) -> None:
        """滚轮/触控板：向上 → 解除跟随；向下滚到底 → 恢复跟随。

        注意不返回 "break"，以免阻止 Text 默认滚动。
        """
        delta = getattr(event, "delta", 0)
        if delta > 0:  # Windows 正值=向上滚（看更早内容）
            self._stick_bottom = False
        elif delta < 0 and self._is_at_bottom():
            self._stick_bottom = True

    def _user_scroll_up(self) -> None:
        self._stick_bottom = False

    def _user_scroll_down(self) -> None:
        if self._is_at_bottom():
            self._stick_bottom = True

    def _mark_no_follow(self) -> None:
        self._stick_bottom = False

    def _toggle(self) -> None:
        if self.var_mode.get() == "batch":
            self._batch_toggle()
            return
        if self.worker is not None and self.worker.is_alive():
            self.worker.stop()
            self.btn_start.configure(text="正在停止…", state="disabled")
            return
        target = self.var_target.get().strip()
        if not target:
            messagebox.showwarning("提示", "请输入目标（BV/UID/收藏夹UID/帖子链接）")
            return
        if not self.var_comments.get() and not self.var_danmaku.get():
            messagebox.showwarning("提示", "请至少勾选评论或弹幕")
            return
        self._clear_stream()
        self.txt.configure(wrap="none")  # 实时流恢复横向滚动
        mode = self.var_mode.get()

        from gui.crawl_worker import FavCrawlWorker, RelationCrawlWorker

        if mode == "up":
            try:
                uid = int(target)
            except (ValueError, TypeError):
                messagebox.showerror("错误", "UP主 UID 需为纯数字，例如 946974")
                return
            worker = UpCrawlWorker(self.msg_q, uid=uid,
                                   comments=self.var_comments.get(),
                                   danmaku=self.var_danmaku.get(),
                                   incremental=self.var_incremental.get())
            self.lbl_status.configure(text=f"UP主 {uid}：正在拉取视频列表…")
        elif mode == "fav":
            try:
                uid = int(target)
            except (ValueError, TypeError):
                messagebox.showerror("错误", "收藏夹用户 UID 需为纯数字")
                return
            worker = FavCrawlWorker(self.msg_q, uid=uid,
                                    comments=self.var_comments.get(),
                                    danmaku=self.var_danmaku.get(),
                                    incremental=self.var_incremental.get())
            self.lbl_status.configure(text=f"收藏夹用户 {uid}：正在拉取收藏夹列表…")
        elif mode == "rel":
            try:
                uid = int(target)
            except (ValueError, TypeError):
                messagebox.showerror("错误", "关系用户 UID 需为纯数字")
                return
            try:
                max_users = int(self.var_max_users.get())
                if max_users < 0:
                    max_users = 0
            except ValueError:
                max_users = 0
            worker = RelationCrawlWorker(
                self.msg_q, uid=uid, direction=self.var_dir.get(),
                max_users=max_users,
                comments=self.var_comments.get(),
                danmaku=self.var_danmaku.get(),
                incremental=self.var_incremental.get())
            dir_text = {"both": "关注+粉丝", "following": "仅关注",
                        "follower": "仅粉丝"}.get(self.var_dir.get(), "both")
            limit_txt = "不限" if max_users == 0 else str(max_users)
            self.lbl_status.configure(
                text=f"关系用户 {uid}：拉取{dir_text}（上限{limit_txt}人）…")
        elif mode == "post":
            worker = PostCrawlWorker(self.msg_q, target=target,
                                     comments=self.var_comments.get(),
                                     incremental=self.var_incremental.get())
            self.lbl_status.configure(text="帖子：准备采集评论…")
        else:
            bvid = self._extract_bvid(target)
            if not bvid:
                messagebox.showerror("错误", "无法识别 BV 号（示例：BV1xx4y1z7Ab）")
                return
            from gui.crawl_worker import CrawlWorker

            worker = CrawlWorker(self.msg_q, bvid=bvid,
                                 comments=self.var_comments.get(),
                                 danmaku=self.var_danmaku.get())
            self.lbl_status.configure(text=f"采集中：{bvid} …")

        self.worker = worker
        self.worker.start()
        self.btn_start.configure(text="停止采集", style="Danger.TButton", state="normal")
        self._reset_progress()
        self._doing_kind, self._doing_cnt = "", 0
        self._bread_video = ""
        self._set_doing("任务启动…")
        app = self._app()
        if app is not None:
            app._on_task_started(self._task_desc())
        self.after(120, self._poll)

    @staticmethod
    def _extract_bvid(text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        import re
        # 1. 优先正则提取形如 BV1xx4y1z7Ab（不受中文标点、前后粘连汉字影响）
        m = re.search(r"(?<![0-9a-zA-Z])(BV1[0-9A-Za-z]{9})(?![0-9a-zA-Z])", text, re.IGNORECASE)
        if m:
            bvid = m.group(1)
            return "BV" + bvid[2:]
        # 2. b23.tv 短链解析 (跟随重定向提取真实 BV)
        if "b23.tv" in text or "bili2233.cn" in text:
            url_m = re.search(r"https?://(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]+", text)
            if url_m:
                try:
                    import requests
                    resp = requests.head(url_m.group(0), allow_redirects=True, timeout=5,
                                         headers={"User-Agent": "Mozilla/5.0"})
                    m_b23 = re.search(r"(?<![0-9a-zA-Z])(BV1[0-9A-Za-z]{9})(?![0-9a-zA-Z])", resp.url, re.IGNORECASE)
                    if m_b23:
                        return "BV" + m_b23.group(1)[2:]
                except Exception:
                    pass
        # 3. 兜底路径匹配
        if "video/" in text:
            part = text.split("video/", 1)[1]
            candidate = part.split("/")[0].split("?")[0].strip()
            if candidate.lower().startswith("bv"):
                return "BV" + candidate[2:]
        for tok in text.split():
            if tok.lower().startswith("bv"):
                return "BV" + tok.split(",")[0].strip()[2:]
        return ""

    # ---------------- 批量UP ----------------
    @staticmethod
    def _parse_uids(text: str) -> list[int]:
        out: list[int] = []
        import re

        for tok in re.split(r"[,\s\n；;]+", text):
            tok = tok.strip()
            if tok.isdigit():
                out.append(int(tok))
        # 去重保序
        seen: set[int] = set()
        return [u for u in out if not (u in seen or seen.add(u))]

    def _sync_rel_dir(self) -> None:
        """勾选"关注/粉丝"时启用方向下拉，否则禁用。"""
        try:
            if self.var_ck_rel.get():
                self.var_rel_dir.set(self.var_rel_dir.get() or "both")
                self.var_rel_dir_dd.state(["!disabled"])
            else:
                self.var_rel_dir_dd.state(["disabled"])
        except (AttributeError, tk.TclError):
            pass

    def _build_batch_jobs(self) -> list[dict]:
        """把 UID 列表展开为任务队列（按勾选的类型）。

        每个 UID 都生成被勾选类型的任务；去重保序。
        """
        uids = self._parse_uids(self._batch_text())
        if not uids:
            return []
        do_up = self.var_ck_up.get() if hasattr(self, "var_ck_up") else True
        do_fav = self.var_ck_fav.get() if hasattr(self, "var_ck_fav") else False
        do_rel = self.var_ck_rel.get() if hasattr(self, "var_ck_rel") else False
        direction = self.var_rel_dir.get() if hasattr(self, "var_rel_dir") else "both"
        jobs: list[dict] = []
        seen: set[tuple] = set()
        for uid in uids:
            for kind in ("up", "fav", "rel"):
                if kind == "up" and not do_up:
                    continue
                if kind == "fav" and not do_fav:
                    continue
                if kind == "rel" and not do_rel:
                    continue
                key = (kind, uid, direction if kind == "rel" else None)
                if key in seen:
                    continue
                seen.add(key)
                jobs.append({"kind": kind, "uid": uid,
                             "direction": direction if kind == "rel" else None})
        return jobs

    def _batch_text(self) -> str:
        try:
            return self.txt_batch_uids.get("1.0", "end")
        except tk.TclError:
            return ""

    def _batch_worker_alive(self) -> bool:
        return (getattr(self, "worker", None) is not None
                and self.worker.is_alive()
                and getattr(self.worker, "_job_q", None) is not None)

    def _batch_toggle(self) -> None:
        """开始/停止批量采集（复用 self.worker 以统一 _poll 消息泵）。"""
        if self._batch_worker_alive():
            self.worker.stop()
            self.btn_batch_start.configure(text="正在停止…")
            return
        from gui.crawl_worker import BatchUpWorker

        jobs = self._build_batch_jobs()
        if not jobs:
            messagebox.showwarning("提示", "请先在批量区粘贴至少一个 UID")
            return
        if not self.var_comments.get() and not self.var_danmaku.get():
            messagebox.showwarning("提示", "请至少勾选评论或弹幕")
            return
        kinds = {j["kind"] for j in jobs}
        kind_names = "、".join({"up": "UP", "fav": "收藏夹",
                               "rel": "关注/粉丝"}.get(k, k) for k in sorted(kinds))
        self._clear_stream()
        self.txt.configure(wrap="none")
        self._append_line(f"=== 开始批量采集：{len(jobs)} 个任务（{kind_names}）===")
        worker = BatchUpWorker(
            self.msg_q,
            comments=self.var_comments.get(),
            danmaku=self.var_danmaku.get(),
            incremental=self.var_incremental.get())
        worker.add_jobs(jobs)
        self.worker = worker
        self.worker.start()
        self.btn_batch_start.configure(text="■ 停止批量")
        self.btn_batch_add.configure(state="normal")
        self.lbl_batch_state.configure(text=f"批量运行中：{len(jobs)} 个任务已入队")
        self.btn_start.configure(text="批量采集中")
        self._reset_progress()
        self._doing_kind, self._doing_cnt = "", 0
        self._bread_video = ""
        self._set_doing(f"批量任务：{len(jobs)} 个待处理")
        app = self._app()
        if app is not None:
            app._on_task_started(self._task_desc())
        self.after(120, self._poll)

    def _batch_append(self) -> None:
        """运行中追加一批任务（接续采集，不影响队列中已有任务）。"""
        jobs = self._build_batch_jobs()
        if not jobs:
            messagebox.showwarning("提示", "请先粘贴要追加的 UID")
            return
        if self._batch_worker_alive():
            self.worker.add_jobs(jobs)
            self._append_line(f"→ 追加 {len(jobs)} 个任务，队列剩余 {self.worker.remaining} 个")
            self.lbl_batch_state.configure(
                text=f"已追加，队列 {self.worker.remaining} 剩余")
            self.lbl_status.configure(
                text=f"批量运行中：追加 {len(jobs)} 个任务")
        else:
            # 队列已结束后追加 → 视为重新开始批量
            self._batch_toggle()
            return
        self.after(120, self._poll)

    def _on_batch_done(self) -> None:
        try:
            self.btn_batch_start.configure(text="◎ 开始批量采集")
            self.btn_batch_add.configure(state="disabled")
            self.lbl_batch_state.configure(text="空闲：粘贴UID开始批量")
            self._doing_kind, self._doing_cnt = "", 0
            self._set_doing("批量完成")
            self.lbl_doing_cnt.configure(text="")
        except tk.TclError:
            pass

    # ---------------- 消息泵 ----------------
    def _poll(self) -> None:
        self._drain_queue()
        if self.worker is not None:
            if self.worker.is_alive():
                self.after(120, self._poll)
            else:
                was_batch = getattr(self.worker, "_job_q", None) is not None
                self.worker = None
                if was_batch:
                    self._on_batch_done()
                self.btn_start.configure(text="开始采集", style="Accent.TButton", state="normal")
                self._finish_progress()
                was_batch_label = "批量采集已结束" if was_batch else "就绪。输入 BV 或视频链接开始。"
                if self.var_mode.get() == "batch":
                    self.lbl_status.configure(text=was_batch_label)
                else:
                    self.lbl_status.configure(text="就绪。输入 BV 或视频链接开始。")

    def _drain_queue(self) -> None:
        seen_any = False
        stick = self._stick_bottom  # 事件级跟踪：用户上翻即 False，不再被阈值误判
        rendered = 0
        batch_lines: list[str] = []
        new_cnt = {"comment": 0, "danmaku": 0}
        paused = bool(self._pause_stream.get())

        for _ in range(500):
            try:
                msg = self.msg_q.get_nowait()
            except queue.Empty:
                break
            mtype = msg.get("type")
            if mtype == MSG_ITEM:
                k = msg.get("kind", "comment")
                new_cnt[k] = new_cnt.get(k, 0) + 1
                if not paused and rendered < _ITEM_RENDER_BUDGET:
                    rendered += 1
                    data = msg.get("data") or {}
                    if k == "danmaku":
                        batch_lines.append(format_danmaku_line(data))
                    else:
                        batch_lines.append(format_comment_line(data))
                continue

            self._handle_msg(msg, batch_lines=batch_lines, paused=paused)
            seen_any = True

        # 1. 批量追加文本到 Text 控件（单次事务锁屏+写入，大幅消除 CPU 尖刺）
        if batch_lines and not paused:
            self._append_batch(batch_lines)
            seen_any = True

        # 2. 批量更新计数标签（每 120ms tick 仅更新一次，绝不触发高频 DOM 重排）
        tot_new = new_cnt["comment"] + new_cnt["danmaku"]
        if tot_new > 0:
            if new_cnt["danmaku"] > new_cnt["comment"]:
                self._doing_kind = "弹幕"
            else:
                self._doing_kind = "评论"
            self._doing_cnt += tot_new
            try:
                self.lbl_doing_cnt.configure(text=f"{self._doing_kind} 已采 {self._doing_cnt} 条")
                self._set_doing(f"{self._doing_root()}{self._doing_sub()}采集{self._doing_kind}")
            except tk.TclError:
                pass

        if seen_any and stick and not paused:
            self._see_end()  # 仅在解锁跟随时回底；已上翻则保持阅读位置

    def _handle_msg(self, msg: dict, batch_lines: list[str] | None = None, paused: bool = False) -> None:
        mtype = msg.get("type")
        if mtype == MSG_META:
            if msg.get("post_id"):
                self.lbl_status.configure(
                    text=f"帖子：kind={msg.get('kind')} id={msg.get('post_id')} {msg.get('title', '')[:40]}"
                )
            else:
                self.lbl_status.configure(
                    text=f"元信息：{msg.get('bvid')} aid={msg.get('aid')} {msg.get('title', '')[:40]}"
                )
                self._refresh_video_list()
        elif mtype == MSG_PROGRESS:
            text = msg.get("text", "")
            self.lbl_status.configure(text=text)
            if not paused:
                if batch_lines is not None:
                    batch_lines.append(f"→ {text}")
                else:
                    self._append_line(f"→ {text}")
            self._on_up_progress(text)
            self._on_doing_progress(text)
        elif mtype == MSG_ITEM:
            data = msg.get("data") or {}
            line = format_danmaku_line(data) if msg.get("kind") == "danmaku" else format_comment_line(data)
            if not paused:
                if batch_lines is not None:
                    batch_lines.append(line)
                else:
                    self._append_line(line)
        elif mtype == MSG_ERROR:
            err_txt = f"[错误] {msg.get('text', '')}"
            if not paused:
                if batch_lines is not None:
                    batch_lines.append(err_txt)
                else:
                    self._append_line(err_txt)
            self.lbl_status.configure(text=f"错误：{msg.get('text', '')}")
            self._set_doing(f"出错：{msg.get('text', '')[:40]}")
        elif mtype == MSG_DONE:
            stats = msg.get("stats") or {}
            fetched = stats.get("fetched", 0)
            if stats.get("jobs") is not None:
                text = (f"批量完成：{stats.get('jobs', 0)} 个任务 / "
                        f"UP{stats.get('ups', 0)}·收藏夹{stats.get('favs', 0)}·"
                        f"关系{stats.get('rels', 0)} / 视频{stats.get('videos', 0)}个 / "
                        f"新采{stats.get('fetched', 0)}个 / "
                        f"评论{stats.get('comments', 0)}条 / 弹幕{stats.get('danmaku', 0)}条"
                        + (f"（{stats.get('errors', [''])[-1]}）" if stats.get("errors") else ""))
            elif stats.get("ups") is not None and stats.get("related") is not None:
                text = (f"关系采集完成：关系人{stats.get('related', 0)}个 / "
                        f"UP{stats.get('ups_ok', 0)}个 / 视频{stats.get('videos', 0)}个 / "
                        f"新采{stats.get('fetched', 0)}个 / "
                        f"评论{stats.get('comments', 0)}条 / 弹幕{stats.get('danmaku', 0)}条"
                        + (f"（{stats.get('errors', [''])[-1]}）" if stats.get("errors") else ""))
            elif stats.get("folders") is not None and self.worker is not None and \
                    getattr(self.worker, "uid", None) is not None and \
                    hasattr(self.worker, "max_pages"):
                text = (f"收藏夹完成：夹{stats.get('folders', 0)}个 / "
                        f"视频{stats.get('videos', 0)}个 / 新采{fetched}个 / "
                        f"评论{stats.get('comments', 0)}条 / 弹幕{stats.get('danmaku', 0)}条")
            elif fetched:
                text = (f"UP完成：视频{stats.get('videos', 0)}个 / 新采{fetched}个 / "
                        f"评论{stats.get('comments', 0)}条 / 弹幕{stats.get('danmaku', 0)}条")
            elif stats.get("post"):
                text = f"帖子完成：评论 {stats.get('comments', 0)} 条"
            else:
                text = f"完成：评论 {stats.get('comments', 0)} 条 / 弹幕 {stats.get('danmaku', 0)} 条"
            self.lbl_status.configure(text=text)
            self._append_line("==== 本次采集完成 ====")
            self._doing_kind, self._doing_cnt = "", 0
            self._set_doing("已完成")
            self.lbl_doing_cnt.configure(text="")
            self._finish_progress()
            self._refresh_up_stats()
            self._refresh_video_list()
            app = self._app()
            if app is not None:
                app._on_task_done(self)

    def _on_doing_progress(self, text: str) -> None:
        """从进度文本解析“正在干什么”面包屑（层次：任务→列表→用户→视频）。"""
        import re

        root = self._doing_root()
        vm = re.match(r"\[(\d+)/(\d+)\]", text)  # 当前视频进度
        if vm:
            self._doing_kind, self._doing_cnt = "", 0
            self.lbl_doing_cnt.configure(text=f"视频 {vm.group(1)}/{vm.group(2)}")
            title = text.split("]", 1)[1].strip() if "]" in text else ""
            if title:  # 进度文本带标题 → 记入面包屑，状态栏显示"…→ 视频「标题」→ …"
                self._bread_video = title
            self._set_doing(f"{root}{self._doing_sub()}采集视频")
            return
        rel = re.match(r"\[关系 (\d+)/(\d+)\] 开始采集 (.+)\((\d+)\)", text)
        if rel:
            self._bread_rel = f"关系用户 {rel.group(4)}「{rel.group(3)}」"
            self._doing_kind, self._doing_cnt = "", 0
            self.lbl_doing_cnt.configure(text=f"关系人 {rel.group(1)}/{rel.group(2)}")
            self._set_doing(f"{root}{self._doing_sub()}准备采集")
            return
        if text.strip().startswith("—— 收藏夹："):
            self._bread_folder = text.split("：", 1)[1][:40]
            self._bread_video = ""  # 新收藏夹的视频标题待下一条视频级消息刷新
            self._doing_kind, self._doing_cnt = "", 0
            self.lbl_doing_cnt.configure(text="")
            self._set_doing(f"{root}正在拉取收藏夹「{self._bread_folder}」的视频…")
            return
        if text.strip().startswith("UP主共") or text.strip().startswith("用户共") \
                or text.strip().startswith("去重后共"):
            self._set_doing(f"{root}已拿到列表，准备逐条采集…")
            return
        low = text.strip()
        if low.startswith("采集") or low.startswith("采评论") or low.startswith("采弹幕"):
            self._doing_kind, self._doing_cnt = "", 0
            self.lbl_doing_cnt.configure(text="")
            self._set_doing(f"{root}{self._doing_sub()}{low[:34]}")
        elif "拉取" in low or "读取" in low or "探测" in low:
            self._set_doing(f"{root}{self._doing_sub()}{low[:40]}")
        else:
            self._set_doing(f"{root}{self._doing_sub()}{low[:40]}")

    def _doing_root(self) -> str:
        """最外层上下文：任务类型 + 目标 UID（批量时用当前 job）。"""
        w = getattr(self, "worker", None)
        if w is None:
            return ""
        job = getattr(w, "current_job", None)
        if job is not None:  # BatchUpWorker 正在执行的任务
            kind = job.get("kind", "up")
            uid = job.get("uid", "")
            label = {"up": "UP", "fav": "收藏夹用户", "rel": "关系用户"}.get(kind, kind)
            return f"[批量·{label} {uid}] "
        uid = getattr(w, "uid", None)
        if uid is None:
            return ""
        if hasattr(w, "direction"):  # RelationCrawlWorker
            dir_text = {"following": "关注", "follower": "粉丝"}.get(
                w.direction, "关注+粉丝")
            return f"用户 {uid} 的{dir_text}列表 → "
        return f"UP {uid} → "

    def _doing_sub(self) -> str:
        """第二层上下文：当前关系用户 / 当前收藏夹 / 当前视频标题。"""
        parts = []
        if getattr(self, "_bread_rel", ""):
            parts.append(self._bread_rel + " → ")
        elif getattr(self, "_bread_folder", ""):
            parts.append(f"收藏夹「{self._bread_folder}」 → ")
        if getattr(self, "_bread_video", ""):
            parts.append(f"视频「{self._bread_video}」 → ")
        return "".join(parts)

    def _on_doing_item(self, kind: str) -> None:
        """每条 item 到达：累计计数并刷新“正在采 X 第 n 条”。"""
        if kind == "danmaku":
            self._doing_kind = "弹幕"
        else:
            self._doing_kind = "评论"
        self._doing_cnt += 1
        self.lbl_doing_cnt.configure(text=f"{self._doing_kind} 已采 {self._doing_cnt} 条")
        self._set_doing(f"{self._doing_root()}{self._doing_sub()}采集{self._doing_kind}")

    def _set_doing(self, text: str) -> None:
        try:
            self.lbl_doing.configure(text=text)
        except tk.TclError:
            pass

    def _on_up_progress(self, text: str) -> None:
        """解析 UP 采集进度，实时刷新汇总表已采集视频数。"""
        import re

        m = re.match(r"\[(\d+)/(\d+)\]", text)
        if not m:
            return
        cur, total = int(m.group(1)), int(m.group(2))
        if not hasattr(self, "up_tree"):
            return
        # 找到当前采集的 UP 行（进度来自正在跑的 UP worker）
        uid = getattr(self.worker, "uid", None) if self.worker else None
        iid = self._up(uid) if uid else ""
        if iid:
            values = list(self.up_tree.item(iid, "values"))
            if values:
                values[2] = f"{cur}/{total}"
                self.up_tree.item(iid, values=values)
        self._set_progress_determinate(cur, total)
        # 常驻显示视频已采集比例（即使正在采集当前视频评论，也保持显示）
        try:
            self.lbl_vpos.configure(text=f"视频 {cur}/{total}")
        except tk.TclError:
            pass

    # ---- 进度条 ----
    def _reset_progress(self) -> None:
        """任务开始：设零并进入脉冲动画（未知总量）。"""
        try:
            self.prog.stop()
            self.prog.configure(mode="indeterminate", maximum=100)
            self.prog.start(15)
            self.lbl_vpos.configure(text="")
        except tk.TclError:
            pass

    def _set_progress_determinate(self, cur: int, total: int) -> None:
        """已知总量（如 UP 批量 [i/n]）：切为确定型进度条。"""
        try:
            if total <= 0:
                return
            self.prog.stop()
            self.prog.configure(mode="determinate", maximum=total, value=cur)
        except tk.TclError:
            pass

    def _finish_progress(self) -> None:
        """任务结束：进度条填满并复位。"""
        try:
            self.prog.stop()
            self.prog.configure(mode="determinate", maximum=100, value=100)
            self.lbl_vpos.configure(text="")
        except tk.TclError:
            pass
