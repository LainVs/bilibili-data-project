"""全局搜索面板：评论 / 弹幕 / 视频 三区，右侧边栏切换。

大数据量下 LIKE 全扫可能耗时数秒，搜索统一在后台线程执行，
完成后回填界面，保证主线程（UI）不卡顿。

展示设计：
  * 右侧边栏“评论 / 弹幕 / 视频”，点哪个只展开哪个分区（全模块区域）。
  * 每个分区可在“列表”与“全文”两种视图间切换：
      列表 = 每行一条的紧凑表格；全文 = 逐条分块、完整正文 + 详细信息。
  * 全文视图支持 A-/A+ 调整字号。
  * 双击列表行仍可弹窗查看并高亮关键词。
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from gui.util import fmt_dt
from gui.widgets import SmartTable
from storage import dao

logger = logging.getLogger(__name__)


class SearchPanel(ttk.Frame):
    """跨表全局搜索：关键词 + 来源 + 时间范围 + 侧边栏分区切换。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._search_q: queue.Queue = queue.Queue()
        self._active_sec = "cmt"          # 当前展示分区 cmt/dmk/video
        self._view_mode = "全文"          # 列表 / 全文
        self._font_size = 10
        self._last_results: dict = {}
        self._last_kw = ""
        self._sec_rows: dict[str, list[dict]] = {"cmt": [], "dmk": [], "video": []}
        self._list_iids: dict[str, dict] = {}  # iid -> 完整行（用于双击详情）
        self._build_ui()
        self.start_polling()

    def start_polling(self) -> None:
        """主线程周期排污搜索结果队列（每 150ms），线程安全回填 UI。"""
        self.after(150, self.start_polling)
        try:
            while True:
                item = self._search_q.get_nowait()
                if item is None:
                    self.btn_search.configure(state="normal", text="搜索")
                    self.lbl_hint.configure(text="搜索出错，详见日志")
                elif isinstance(item, tuple):
                    if item and item[0] == "cmt_partial":
                        self._apply_cmt_partial(*item[1:])
                    else:
                        self._apply(*item)
        except queue.Empty:
            pass

    # ---------------- 会话状态（重启恢复） ----------------
    def snapshot_state(self) -> dict:
        return {
            "var_kw": self.var_kw.get(),
            "var_in_c": self.var_in_c.get(),
            "var_in_d": self.var_in_d.get(),
            "var_in_v": self.var_in_v.get(),
            "var_start": self.var_start.get(),
            "var_end": self.var_end.get(),
            "var_limit": self.var_limit.get(),
            "var_exclude": self.var_exclude.get(),
        }

    def restore_state(self, d: dict) -> None:
        for name in ("var_kw", "var_start", "var_end", "var_limit", "var_exclude"):
            val = d.get(name)
            if isinstance(val, str):
                try:
                    getattr(self, name).set(val)
                except (AttributeError, tk.TclError):
                    pass
        for name in ("var_in_c", "var_in_d", "var_in_v"):
            if name in d:
                try:
                    getattr(self, name).set(bool(d.get(name)))
                except (AttributeError, tk.TclError):
                    pass

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # 查询条件行
        top = ttk.Frame(self, padding=(6, 6))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="关键词:").pack(side="left")
        self.var_kw = tk.StringVar()
        kw = ttk.Entry(top, textvariable=self.var_kw, width=28)
        kw.pack(side="left", padx=(2, 8))
        kw.bind("<Return>", lambda _e: self._search())

        self.var_in_c = tk.BooleanVar(value=True)
        self.var_in_d = tk.BooleanVar(value=True)
        self.var_in_v = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="评论", variable=self.var_in_c).pack(side="left")
        ttk.Checkbutton(top, text="弹幕", variable=self.var_in_d).pack(side="left")
        ttk.Checkbutton(top, text="视频", variable=self.var_in_v).pack(side="left")

        ttk.Label(top, text="  从:").pack(side="left", padx=(10, 0))
        self.var_start = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_start, width=11).pack(side="left", padx=2)
        ttk.Label(top, text="至:").pack(side="left")
        self.var_end = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_end, width=11).pack(side="left", padx=2)

        self.var_limit = tk.StringVar(value="0")
        ttk.Label(top, text="每分区条数(0=无限):").pack(side="left", padx=(10, 0))
        ttk.Spinbox(top, from_=0, to=100000, textvariable=self.var_limit, width=7).pack(side="left")
        self.btn_search = ttk.Button(top, text="搜索", command=self._search)
        self.btn_search.pack(side="left", padx=(10, 0))
        self.btn_export = ttk.Button(top, text="导出命中CSV", command=self._export,
                                     state="disabled")
        self.btn_export.pack(side="left", padx=(6, 0))

        tip = ttk.Frame(self)
        tip.grid(row=1, column=0, sticky="ew", padx=8)
        ttk.Label(tip, text="排除词(逗号分隔):", foreground="#888").pack(side="left")
        self.var_exclude = tk.StringVar()
        ttk.Entry(tip, textvariable=self.var_exclude, width=30).pack(side="left", padx=(2, 8))
        ttk.Label(tip, text="排除含这些词的评论/弹幕（如搜“国男”填“中国男,外国男”去掉子串误伤）",
                  foreground="#888").pack(side="left")
        ttk.Label(tip, text="时间格式如 2024-01-01 或 2024-01-01 00:00 (可留空)",
                  foreground="#888").pack(side="right", padx=(6, 0))

        # 主体：左侧结果区 + 右侧边栏
        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="nsew", padx=6, pady=(2, 4))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- 左侧结果区 ----
        main = ttk.Frame(body)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(main, padding=(0, 0, 0, 3))
        toolbar.grid(row=0, column=0, sticky="ew")
        self.lbl_sec_title = ttk.Label(toolbar, text="评论", font=("", 10, "bold"))
        self.lbl_sec_title.pack(side="left")
        ttk.Button(toolbar, text="列表", width=5,
                   command=lambda: self._set_view("列表")).pack(side="left", padx=(12, 2))
        ttk.Button(toolbar, text="全文", width=5,
                   command=lambda: self._set_view("全文")).pack(side="left")
        ttk.Label(toolbar, text=" 字号:").pack(side="left", padx=(14, 0))
        ttk.Button(toolbar, text="A-", width=3, command=lambda: self._zoom(-1)).pack(side="left", padx=(0, 2))
        self.lbl_font = ttk.Label(toolbar, text="10", width=3)
        self.lbl_font.pack(side="left")
        ttk.Button(toolbar, text="A+", width=3, command=lambda: self._zoom(1)).pack(side="left", padx=(2, 0))
        ttk.Button(toolbar, text="全部展开", width=8,
                   command=self._expand_all_flag).pack(side="right", padx=(4, 0))
        self.lbl_sec_cnt = ttk.Label(toolbar, text="")
        self.lbl_sec_cnt.pack(side="right", padx=(0, 8))

        # 列表视图 + 全文视图，共用下方区域（互斥显示）
        viewbox = ttk.Frame(main)
        viewbox.grid(row=1, column=0, sticky="nsew")
        viewbox.columnconfigure(0, weight=1)
        viewbox.rowconfigure(0, weight=1)

        self._list_box = ttk.Frame(viewbox)
        self._list_box.grid(row=0, column=0, sticky="nsew")
        self._list_box.columnconfigure(0, weight=1)
        self._list_box.rowconfigure(0, weight=1)
        self.tree = SmartTable(self._list_box, show="headings")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self._list_box, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._on_double_click)

        self._text_box = ttk.Frame(viewbox)
        self._text_box.grid(row=0, column=0, sticky="nsew")
        self._text_box.columnconfigure(0, weight=1)
        self._text_box.rowconfigure(0, weight=1)
        self.txt = tk.Text(self._text_box, wrap="word", state="disabled",
                           font=("Microsoft YaHei UI", self._font_size),
                           padx=8, pady=4, background="#fcfcfc")
        sb2 = ttk.Scrollbar(self._text_box, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb2.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb2.grid(row=0, column=1, sticky="ns")
        # 全文内点击可自定义：双击复制等，暂不绑定

        # ---- 分页条（列表/全文共用，内存翻页不重查库） ----
        self._page = 0
        self._page_size = 100
        pager = ttk.Frame(main)
        pager.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 2))
        self.btn_page_prev = ttk.Button(pager, text="◀ 上一页", width=10,
                                        command=self._page_prev)
        self.btn_page_prev.pack(side="left")
        self.lbl_page = ttk.Label(pager, text="第 1/1 页")
        self.lbl_page.pack(side="left", padx=10)
        self.btn_page_next = ttk.Button(pager, text="下一页 ▶", width=10,
                                        command=self._page_next)
        self.btn_page_next.pack(side="left")
        ttk.Label(pager, text=f"每页 {self._page_size} 条 · 完整结果可用“导出命中CSV”",
                  foreground="#888").pack(side="right")

        # ---- 右侧边栏 ----
        sidebar = ttk.Frame(body, relief="groove", borderwidth=1)
        sidebar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        ttk.Label(sidebar, text="分区", font=("", 9, "bold")).pack(pady=(6, 4))
        self.btn_sec_cmt = ttk.Button(sidebar, text="评论", width=12,
                                      command=lambda: self._set_sec("cmt"))
        self.btn_sec_cmt.pack(pady=2)
        self.btn_sec_dmk = ttk.Button(sidebar, text="弹幕", width=12,
                                      command=lambda: self._set_sec("dmk"))
        self.btn_sec_dmk.pack(pady=2)
        self.btn_sec_video = ttk.Button(sidebar, text="视频", width=12,
                                        command=lambda: self._set_sec("video"))
        self.btn_sec_video.pack(pady=2)
        self._sec_btns = {"cmt": self.btn_sec_cmt, "dmk": self.btn_sec_dmk,
                          "video": self.btn_sec_video}
        mdl_label = ttk.Label(
            sidebar,
            text="点分区按钮切换展示；\n列表=紧凑表格\n全文=完整正文+详情\n双击列表行可看高亮详情",
            foreground="#888", justify="left", wraplength=110)
        mdl_label.pack(pady=(14, 0), padx=4)

        self.lbl_hint = ttk.Label(self, text="输入关键词后按回车或点搜索。多个关键词用逗号分隔（如：烂片,剧情差）表示全部都要包含。",
                                  foreground="#b36")
        self.lbl_hint.grid(row=3, column=0, sticky="w", padx=8)
        self._fresh_sec_view()
        self._render_section_title()

    # ---------------- 视图状态 ----------------
    def _set_sec(self, sec: str) -> None:
        """右侧边栏：切到某分区，并重建该分区当前视图。"""
        self._active_sec = sec
        self._page = 0
        self._fresh_sec_view()
        self._render_sec()

    def _set_view(self, mode: str) -> None:
        self._view_mode = mode
        self._fresh_sec_view()
        self._render_sec()

    def _zoom(self, delta: int) -> None:
        self._font_size = max(8, min(24, self._font_size + delta))
        try:
            self.txt.configure(font=("Microsoft YaHei UI", self._font_size))
        except tk.TclError:
            pass
        try:
            self.lbl_font.configure(text=str(self._font_size))
        except tk.TclError:
            pass

    def _expand_all_flag(self) -> None:
        """“全部展开”仅对全文视图有意义：切换为文本视图即完整展示。"""
        self._set_view("全文")

    def _fresh_sec_view(self) -> None:
        """依据当前视图开关，显示列表或全文控件。"""
        try:
            if self._view_mode == "列表":
                self._text_box.grid_forget()
                self._list_box.grid(row=0, column=0, sticky="nsew")
            else:
                self._list_box.grid_forget()
                self._text_box.grid(row=0, column=0, sticky="nsew")
        except tk.TclError:
            pass

    # ---------------- 搜索 ----------------
    def _search(self) -> None:
        kw = self.var_kw.get().strip()
        if not kw:
            messagebox.showwarning("提示", "请输入关键词")
            return
        try:
            limit = int(self.var_limit.get())
        except ValueError:
            limit = 3000
        start = self.var_start.get().strip()
        end = self.var_end.get().strip()
        if start and end and start > end:
            messagebox.showerror("错误", "时间范围无效：开始晚于结束")
            return
        exclude = self.var_exclude.get().strip()
        sources = {"cmt": self.var_in_c.get(), "dmk": self.var_in_d.get(),
                   "video": self.var_in_v.get()}

        hint = "搜索中…"
        if len(kw) < 2:
            hint = "单字关键词走全表扫描，需要几十秒，请耐心等待…"
        self.lbl_hint.configure(text=hint)
        self.btn_search.configure(state="disabled", text="搜索中…")
        # 搜索代际：旧搜索的残留回调直接丢弃，防止覆盖新结果
        self._search_gen = getattr(self, "_search_gen", 0) + 1
        my_gen = self._search_gen
        # 流式前先清空评论区，避免新旧混排
        self._sec_rows["cmt"] = []
        self._stream_total = 0

        def _worker() -> None:
            def _on_shard(db_label: str, rows: list) -> None:
                self._search_q.put(("cmt_partial", my_gen, db_label, rows))
            try:
                results = self._query(sources, kw, start, end, limit, exclude,
                                      cmt_cb=_on_shard)
            except Exception as exc:
                logger.exception("搜索失败")
                results = None
            self._search_q.put((results, kw, limit, my_gen))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_cmt_partial(self, gen: int, db_label: str, rows: list) -> None:
        """流式回填：某仓搜完立即追加显示（旧代际直接丢弃）。"""
        if gen != getattr(self, "_search_gen", 0):
            return
        if rows:
            self._sec_rows["cmt"].extend(rows)
        n = len(self._sec_rows["cmt"])
        self._stream_total = n
        self.lbl_hint.configure(text=f"采集中…评论已出 {n} 条（{db_label} 完成）")
        btn = self._sec_btns["cmt"]
        try:
            btn.configure(text=f"评论 {n}")
        except tk.TclError:
            pass
        self.btn_export.configure(state="normal" if n else "disabled")
        if self._active_sec == "cmt":
            self._render_sec()

    def _apply(self, results: dict | None, kw: str, limit: int, gen: int = 0) -> None:
        # 旧搜索的终局结果直接丢弃
        if gen and gen != getattr(self, "_search_gen", 0):
            return
        self.btn_search.configure(state="normal", text="搜索")
        if results is None:
            self.lbl_hint.configure(text="搜索出错，详见日志")
            return
        try:
            from gui.util import ping_main as _ping
            _ping(self, "全局搜索")
        except Exception:
            pass
        for sec in ("cmt", "dmk", "video"):
            data = results.get(sec) or {}
            self._sec_rows[sec] = data.get("rows", [])
        self._last_results = results
        self._last_kw = kw
        self._page = 0

        # 汇总与截断提示
        shown = {"cmt": len(self._sec_rows["cmt"]),
                 "dmk": len(self._sec_rows["dmk"]),
                 "video": len(self._sec_rows["video"])}
        totals = {"cmt": (results.get("cmt") or {}).get("total", shown["cmt"]),
                  "dmk": (results.get("dmk") or {}).get("total", shown["dmk"]),
                  "video": (results.get("video") or {}).get("total", shown["video"])}
        trunc = [{"cmt": "评论", "dmk": "弹幕", "video": "视频"}[k]
                 for k in ("cmt", "dmk", "video")
                 if totals[k] is None or totals[k] > shown[k]]
        shown_txt = {k: (str(v) if v is not None else f"≥{shown[k]}")
                     for k, v in totals.items()}
        from storage.dao import _parse_keywords
        kw_display = " + ".join(_parse_keywords(kw)) if _parse_keywords(kw) else kw
        msg = (f"词「{kw_display}」评论 {shown['cmt']}/{shown_txt['cmt']} · "
               f"弹幕 {shown['dmk']}/{shown_txt['dmk']} · "
               f"视频 {shown['video']}/{shown_txt['video']} 条")
        if trunc:
            msg += "　（分页预览，完整结果可用“导出命中CSV”取全部）"
        self.lbl_hint.configure(text=msg)

        # 侧边栏按钮显示命中数
        for sec in ("cmt", "dmk", "video"):
            btn = self._sec_btns[sec]
            n = shown[sec]
            t = totals[sec]
            label = {"cmt": "评论", "dmk": "弹幕", "video": "视频"}[sec]
            btn.configure(text=f"{label} {n}" + ("" if t == n else f"/{t or '≥' + str(n)}"))
        self.btn_export.configure(state="normal" if any(shown.values()) else "disabled")
        self._render_sec()

    def _query(self, sources: dict, kw: str, start: str, end: str, limit: int,
               exclude: str = "", cmt_cb=None) -> dict:
        out = {}
        if sources.get("cmt"):
            out["cmt"] = dao.search_comments(kw, start=start, end=end,
                                             exclude=exclude, limit=limit,
                                             progress_cb=cmt_cb)
        if sources.get("dmk"):
            out["dmk"] = dao.search_danmaku(kw, start=start, end=end,
                                            exclude=exclude, limit=limit)
        if sources.get("video"):
            out["video"] = dao.search_videos(kw, start=start, end=end, limit=limit)
        return out

    # ---------------- 渲染 ----------------
    def _render_sec(self) -> None:
        sec = self._active_sec
        rows = self._sec_rows.get(sec, [])
        total = len(rows)
        pages = max(1, -(-total // self._page_size))  # ceil 除法
        self._page = max(0, min(self._page, pages - 1))
        start = self._page * self._page_size
        page_rows = rows[start:start + self._page_size]
        self._render_section_title()
        if self._view_mode == "列表":
            self._render_list(sec, page_rows)
        else:
            self._render_text(sec, page_rows)
        # 分页条状态
        self.lbl_page.configure(text=f"第 {self._page + 1}/{pages} 页 · 共 {total} 条")
        try:
            self.btn_page_prev.configure(state="disabled" if self._page == 0 else "normal")
            self.btn_page_next.configure(state="disabled" if self._page >= pages - 1 else "normal")
        except tk.TclError:
            pass

    def _page_prev(self) -> None:
        self._page = max(0, self._page - 1)
        self._render_sec()

    def _page_next(self) -> None:
        self._page += 1
        self._render_sec()

    def _render_section_title(self) -> None:
        label = {"cmt": "评论", "dmk": "弹幕", "video": "视频"}[self._active_sec]
        try:
            self.lbl_sec_title.configure(text=label)
        except tk.TclError:
            pass

    def _render_list(self, sec: str, rows: list[dict]) -> None:
        tree = self.tree
        children = tree.get_children()
        if children:
            tree.delete(*children)
        self._list_iids.clear()
        if sec == "cmt":
            tree.set_columns(
                [("time", "时间", 14), ("sender", "发送者(UID)", 18),
                 ("loc", "属地", 10), ("like", "赞", 6), ("video", "视频", 40),
                 ("content", "内容", 90)])
            for r in rows:
                sender = (r.get("uname") or str(r.get("uid") or ""))
                if r.get("uid"):
                    sender = f"{r.get('uname') or ''}({r.get('uid')})" if r.get("uname") else str(r.get("uid"))
                iid = tree.insert("", "end", values=(
                    fmt_dt(r.get("ctime")), sender, r.get("ip_location") or "",
                    r.get("like_num", 0),
                    f"{r.get('bvid', '')} {str(r.get('title', ''))[:26]}",
                    str(r.get("content", ""))[:120]))
                self._list_iids[iid] = r
        elif sec == "dmk":
            tree.set_columns(
                [("time", "时间", 14), ("sender", "发送者(UID)", 18),
                 ("pos", "进度", 8), ("video", "视频", 40), ("content", "内容", 90)])
            for r in rows:
                sender = r.get("uname") or ""
                uid = r.get("uid")
                if uid or r.get("mid_hash"):
                    hid = f"#{r.get('mid_hash')}" if r.get("mid_hash") else ""
                    code = f"({uid or hid})" if (uid or hid) else ""
                    sender = f"{sender}{code}"
                iid = tree.insert("", "end", values=(
                    fmt_dt(r.get("ctime")), sender,
                    f"{float(r.get('progress', 0) or 0):.1f}s",
                    f"{r.get('bvid', '')} {str(r.get('title', ''))[:26]}",
                    str(r.get("content", ""))[:120]))
                self._list_iids[iid] = r
        else:
            tree.set_columns(
                [("pub", "发布时间", 14), ("title", "标题", 52),
                 ("bvid", "BV", 16), ("up", "UP", 20),
                 ("cmt", "评论", 8), ("dmk", "弹幕", 8)])
            for r in rows:
                up = r.get("owner_name") or r.get("owner_uid") or ""
                if r.get("owner_uid") and r.get("owner_name"):
                    up = f"{up}({r.get('owner_uid')})"
                iid = tree.insert("", "end", values=(
                    fmt_dt(r.get("pubdate")), str(r.get("title", ""))[:50],
                    r.get("bvid", ""), up, r.get("comment_count", 0),
                    r.get("danmaku_count", 0)))
                self._list_iids[iid] = r

    def _render_text(self, sec: str, rows: list[dict]) -> None:
        txt = self.txt
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.tag_configure("hit", background="#ffe28a", foreground="#a00")
        txt.tag_configure("hd", foreground="#333", font=("Microsoft YaHei UI", self._font_size, "bold"))
        txt.tag_configure("sub", foreground="#777")
        txt.tag_configure("sep", foreground="#bbb")
        kw = self._last_kw
        for i, r in enumerate(rows, start=1):
            if sec == "cmt":
                sender = r.get("uname") or str(r.get("uid") or "未知")
                if r.get("uid") and r.get("uname"):
                    sender = f"{sender}({r.get('uid')})"
                txt.insert("end", f"[评论] {fmt_dt(r.get('ctime'))}\t{sender}", "hd")
                txt.insert("end", f"\n属地 {r.get('ip_location') or '未知'} · 赞 {r.get('like_num', 0)}", "sub")
                txt.insert("end", f"\n视频 {r.get('bvid', '')} · {r.get('title', '') or ''} · UP {r.get('owner_name', '') or ''}", "sub")
                txt.insert("end", f"\n{str(r.get('content', ''))}")
            elif sec == "dmk":
                sender = r.get("uname") or "未登记昵称"
                uid = r.get("uid")
                if uid or r.get("mid_hash"):
                    hid = f"#{r.get('mid_hash')}" if r.get("mid_hash") else ""
                    code = f"({uid or hid})" if (uid or hid) else ""
                    sender = f"{sender}{code}"
                txt.insert("end", f"[弹幕] {fmt_dt(r.get('ctime'))}\t{sender}", "hd")
                txt.insert("end", f"\n进度 {float(r.get('progress', 0) or 0):.1f}s · 视频 {r.get('bvid', '')} · {r.get('title', '') or ''}", "sub")
                txt.insert("end", f"\n{str(r.get('content', ''))}")
            else:
                txt.insert("end", f"[视频] {fmt_dt(r.get('pubdate'))}\t{r.get('bvid', '')}", "hd")
                txt.insert("end", f"\n{r.get('title', '') or ''}", "sub")
                txt.insert("end", (f"\nUP {r.get('owner_name', '') or ''}({r.get('owner_uid', '')}) · "
                                   f"评论 {r.get('comment_count', 0)} / 弹幕 {r.get('danmaku_count', 0)}"), "sub")
                txt.insert("end", "")
            txt.insert("end", "\n" + "─" * 60 + "\n", "sep")
        if kw:
            self._highlight(txt, kw)
        txt.configure(state="disabled")
        try:
            txt.see("1.0")
        except tk.TclError:
            pass

    @staticmethod
    def _highlight(txt: "tk.Text", keyword: str) -> None:
        """高亮所有关键词（逗号分隔），每个词单独高亮。"""
        from storage.dao import _parse_keywords
        txt.tag_configure("hit", background="#ffe28a", foreground="#a00")
        for kw in _parse_keywords(keyword):
            start = "1.0"
            while True:
                pos = txt.search(kw, start, stopindex="end", nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(kw)}c"
                txt.tag_add("hit", pos, end)
                start = end

    # ---------------- 双击详情 ----------------
    def _on_double_click(self, event) -> None:
        iid = event.widget.identify_row(event.y)
        if not iid:
            return
        row = self._list_iids.get(iid)
        if row is None:
            return
        sec = self._active_sec
        if sec == "video":
            self._view_video(row)
        else:
            self._show_plain(row)

    def _view_video(self, row: dict) -> None:
        bvid = row.get("bvid") or ""
        def worker():
            try:
                meta = dao.find_video_by_bvid(bvid)
                if not meta:
                    return
                aid = meta["aid"]
                comments = dao.query_comments_by_video(aid, limit=800)
                danmaku = dao.query_danmaku_by_video(aid, limit=800)
                self.after(0, lambda: self._show_video_win(bvid, row, comments, danmaku))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("查询失败", str(exc)))
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _show_video_win(self, bvid: str, row: dict, comments: list, danmaku: list) -> None:
        win = tk.Toplevel(self)
        win.title(f"视频库内内容 | {bvid} | {str(row.get('title', ''))[:44]}")
        win.geometry("720x560")
        txt = tk.Text(win, wrap="word", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        txt.configure(state="normal")
        txt.insert("1.0", f"==== {bvid} | {str(row.get('title', ''))[:44]} ====\n")
        txt.insert("end", f"-- 评论 {len(comments)} 条 --\n")
        for c in comments:
            loc = ("[" + str(c.get("ip_location", "")) + "]") if c.get("ip_location") else ""
            txt.insert("end", f"[评论] {fmt_dt(c.get('ctime'))} likes={c.get('like_num', 0)} uid={c.get('uid', 0)} {loc} | {str(c.get('content', ''))}\n")
        txt.insert("end", f"-- 弹幕 {len(danmaku)} 条 --\n")
        for d in danmaku:
            txt.insert("end", f"[弹幕] {fmt_dt(d.get('ctime'))} {float(d.get('progress', 0) or 0):.1f}s | {d.get('mid_hash', '') or d.get('uid', '')} | {str(d.get('content', ''))}\n")
        txt.configure(state="disabled")
        _ = values

    def _show_plain(self, row: dict) -> None:
        win = tk.Toplevel(self)
        win.title("完整内容（关键词已高亮）")
        win.geometry("660x440")
        txt = tk.Text(win, wrap="word", font=("Microsoft YaHei UI", 10),
                      padx=10, pady=10, state="disabled")
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        txt.configure(state="normal")
        sec = self._active_sec
        if sec == "cmt":
            txt.insert("1.0", (f"时间 {fmt_dt(row.get('ctime'))} · 发送者 {row.get('uname') or row.get('uid') or '未知'}"
                               f"(UID {row.get('uid', '')}) · 属地 {row.get('ip_location') or '未知'} · 赞 {row.get('like_num', 0)}\n"
                               f"视频 {row.get('bvid', '')} · {row.get('title', '') or ''} · UP {row.get('owner_name', '') or ''}\n\n"))
            txt.insert("end", str(row.get("content", "")))
        elif sec == "dmk":
            txt.insert("1.0", (f"时间 {fmt_dt(row.get('ctime'))} · 发送者 {row.get('uname') or '未登记'} "
                               f"(UID {row.get('uid', '')} #{row.get('mid_hash', '')}) · 进度 {float(row.get('progress', 0) or 0):.1f}s\n"
                               f"视频 {row.get('bvid', '')} · {row.get('title', '') or ''}\n\n"))
            txt.insert("end", str(row.get("content", "")))
        if self._last_kw:
            self._highlight(txt, self._last_kw)
        txt.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 8))

    # ---------------- 导出命中 CSV（全量，后台流式） ----------------
    def _export(self) -> None:
        import re as _re
        kw = self._last_kw
        if not kw:
            messagebox.showinfo("提示", "尚未搜索或上次搜索无结果")
            return
        from tkinter import filedialog

        safe_kw = _re.sub(r'[\\/:*?"<>|]+', '_', kw).strip('_') or kw
        path = filedialog.asksaveasfilename(
            title="导出全部命中结果",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"搜索_{safe_kw}.csv",
        )
        if not path:
            return
        start = self.var_start.get().strip()
        end = self.var_end.get().strip()
        exclude = self.var_exclude.get().strip()
        want = {"cmt": self.var_in_c.get(), "dmk": self.var_in_d.get(),
                "video": False}  # 视频命中量小且 LIKE 可接受，暂不并入全量导出

        self.btn_export.configure(state="disabled", text="导出中…")
        self.lbl_hint.configure(text="正在导出全部命中，请稍候…")

        def _worker() -> None:
            import csv

            n_cmt = n_dmk = 0
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["类型", "时间", "发送者", "发送者UID", "内容",
                                "属地", "点赞", "播放进度(秒)", "视频BV",
                                "视频标题", "UP主"])
                    if want["cmt"]:
                        for r in dao.iter_search_comments(
                                kw, start=start, end=end, exclude=exclude):
                            n_cmt += 1
                            w.writerow(self._row_comment(r))
                            if n_cmt % 5000 == 0:
                                self._push_export_hint(f"导出中…评论已写 {n_cmt} 条")
                    if want["dmk"]:
                        for r in dao.iter_search_danmaku(
                                kw, start=start, end=end, exclude=exclude):
                            n_dmk += 1
                            w.writerow(self._row_danmaku(r))
                            if n_dmk % 5000 == 0:
                                self._push_export_hint(f"导出中…弹幕已写 {n_dmk} 条")
                self._push_export_done(path, n_cmt, n_dmk)
            except Exception as exc:
                logger.exception("导出命中失败")
                self._push_export_hint(f"导出失败：{exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _push_export_hint(self, text: str) -> None:
        def _cb() -> None:
            try:
                self.lbl_hint.configure(text=text)
            except tk.TclError:
                pass
        try:
            self.after(0, _cb)
        except tk.TclError:
            pass

    def _push_export_done(self, path: str, n_cmt: int, n_dmk: int) -> None:
        def _cb() -> None:
            try:
                self.btn_export.configure(state="normal", text="导出命中CSV")
                self.lbl_hint.configure(
                    text=f"导出完成：评论 {n_cmt} 条 / 弹幕 {n_dmk} 条 → {path}")
                messagebox.showinfo("导出完成", f"已导出全部命中\n评论 {n_cmt} 条 / "
                                               f"弹幕 {n_dmk} 条\n{path}")
            except tk.TclError:
                pass
        try:
            self.after(0, _cb)
        except tk.TclError:
            pass

    def _row_comment(self, r) -> list:
        return ["评论", fmt_dt(r.get("ctime")), r.get("uname", "") or "",
                r.get("uid", ""), str(r.get("content", "")),
                r.get("ip_location", "") or "", r.get("like_num", ""), "",
                r.get("bvid", ""), r.get("title", ""), r.get("owner_name", "") or ""]

    def _row_danmaku(self, r) -> list:
        return ["弹幕", fmt_dt(r.get("ctime")), r.get("uname", "") or "",
                r.get("uid", ""), str(r.get("content", "")), "",
                "", float(r.get("progress", 0) or 0), r.get("bvid", ""),
                r.get("title", ""), r.get("owner_name", "") or ""]