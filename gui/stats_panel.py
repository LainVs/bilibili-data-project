"""统计面板：库内采集汇总 + 排行榜。

排行榜（发言最多的人 / 重复文案 / 高赞评论 / 被评视频 / 被评UP）
支持时间范围与自定义 Top N，数据量大时统一在后台线程执行，
完成后通过队列线程安全回填，避免主线程卡死。

启动时只读取 stats_cache 缓存（单行 JSON，秒级），不触发全表聚合；
仅点击「刷新」才重新查询并落盘缓存——彻底消除启动即慢的问题。
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui.theme import C, F, S
from gui.util import fmt_dt
from gui.widgets import ClaudeBadge, ClaudeCard, SmartTable
from storage import dao

logger = logging.getLogger(__name__)


class StatsPanel(ttk.Frame):
    """库内采集数据总览 + 排行榜。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._stats_q: queue.Queue = queue.Queue()
        self._db_q: queue.Queue = queue.Queue()
        self._build_ui()
        self.start_polling()
        self._load_cached()
        self._refresh_db_info()

    def _refresh_db_info(self) -> None:
        """后台线程查询数据库位置/占用/数据目录（秒级，不阻塞UI）。"""

        def _worker() -> None:
            try:
                info = dao.query_db_usage()
                # 追加数据目录路径
                from storage.db import get_conn
                conn = get_conn("read")
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT @@datadir AS d")
                        row = cur.fetchone()
                        if row:
                            info["datadir"] = row["d"]
                finally:
                    conn.close()
                self._db_q.put(info)
            except Exception as exc:
                logger.warning("数据库信息查询失败: %s", exc)
                self._db_q.put(None)

        threading.Thread(target=_worker, daemon=True).start()

    def _reset_refresh_btns(self) -> None:
        try:
            self.btn_refresh.configure(state="normal", text="刷新")
            self.btn_stats_only.configure(state="normal", text="仅刷新总览")
            self.prog.configure(value=0)
            self._overlay.place_forget()
        except tk.TclError:
            pass

    def _check_cancel_join(self) -> None:
        """取消后轮询等后台线程真正退出，再恢复按钮（避免重入假死）。"""
        try:
            th = getattr(self, "_stats_thread", None)
            if th is not None and th.is_alive():
                self.after(500, self._check_cancel_join)
                return
            self._reset_refresh_btns()
        except tk.TclError:
            pass

    def _load_cached(self) -> None:
        """启动只读上次缓存（后台线程，不阻塞 UI），完成后队列回填。

        原实现主线程同步读库：库繁忙时（连接排队/建表等待）会卡住整个窗口，
        故改为与「刷新」一致的后台执行模式。
        """

        def _worker() -> None:
            try:
                cached = dao.load_stats_cache()
            except Exception as exc:
                logger.warning("统计缓存读取失败: %s", exc)
                cached = None
            if cached and cached.get("stats") is not None:
                # 有 stats 就秒显，无 ranks 也不自动刷（让用户手动点刷新）
                self._stats_q.put((cached["stats"], cached.get("ranks"),
                                   cached.get("time", "")))
            else:
                self._stats_q.put(None)  # 无缓存 → 显示占位，不自动刷

        threading.Thread(target=_worker, daemon=True).start()

    def start_polling(self) -> None:
        """主线程周期排污结果队列（每 200ms），实现线程安全回填。"""
        self.after(200, self.start_polling)
        if getattr(self, "_auto_stats", False):
            self._auto_stats = False
            self.refresh(mode="stats_only")  # 坏缓存/无缓存：自动补真实统计
        try:
            while True:
                payload = self._stats_q.get_nowait()
                if isinstance(payload, tuple) and len(payload) > 3 and payload[3] == "rank_partial":
                    self._apply_rank_partial(payload)
                elif isinstance(payload, tuple) and payload and payload[0] == "stat_partial":
                    self._apply_stat_partial(*payload[1:])
                elif isinstance(payload, tuple) and payload and payload[0] == "insight":
                    _, ins, charts, summary = (payload + (None, {}, ""))[:4]
                    self._apply_insights(ins, charts or {}, summary or "")
                elif isinstance(payload, tuple) and payload and payload[0] == "detect":
                    _, results, meta, total = (payload + (None, {}, 0))[:4]
                    self._apply_detect(results or [], meta or {}, total or 0)
                elif isinstance(payload, tuple) and payload and payload[0] == "progress":
                    _, d, t, txt = payload
                    if txt:
                        try: self._overlay.configure(text=txt)
                        except tk.TclError: pass
                    if d is not None and t is not None:
                        try: self.prog.configure(value=d, maximum=t)
                        except tk.TclError: pass
                else:
                    self._apply(payload)
        except queue.Empty:
            pass
        try:
            while True:
                info = self._db_q.get_nowait()
                self._apply_db_info(info)
        except queue.Empty:
            pass

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)  # 排行榜 notebook

        wrap = ttk.Frame(self, padding=10)
        wrap.grid(row=0, column=0, sticky="ew")
        wrap.columnconfigure(0, weight=1)

        # 筛选行
        top = ttk.Frame(wrap)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top, text="库内采集统计", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text=" 时间从:").pack(side="left", padx=(16, 0))
        self.var_start = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_start, width=11).pack(side="left", padx=2)
        ttk.Label(top, text="至:").pack(side="left")
        self.var_end = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_end, width=11).pack(side="left", padx=2)
        ttk.Label(top, text="Top N:").pack(side="left", padx=(12, 0))
        self.var_top = tk.StringVar(value="100")
        ttk.Spinbox(top, from_=10, to=500, textvariable=self.var_top, width=6).pack(side="left")
        ttk.Label(top, text="文案长度下限:").pack(side="left", padx=(12, 0))
        self.var_minlen = tk.StringVar(value="0")
        ttk.Spinbox(top, from_=0, to=200, textvariable=self.var_minlen, width=5).pack(side="left")
        self.btn_refresh = ttk.Button(top, text="刷新", style="Accent.TButton", command=self.refresh)
        self.btn_refresh.pack(side="right")
        self.btn_stats_only = ttk.Button(top, text="仅刷新总览",
                                         command=lambda: self.refresh(mode="stats_only"))
        self.btn_stats_only.pack(side="right", padx=6)
        self.lbl_updated = ttk.Label(top, text="", style="Hint.TLabel")
        self.lbl_updated.pack(side="left", padx=8)
        ttk.Label(top, text="时间/长度只作用于排行榜（总览为全量）", style="Hint.TLabel").pack(side="right", padx=8)

        # 排行榜勾选行
        rank_row = ttk.Frame(wrap)
        rank_row.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(rank_row, text="刷新排行榜:", style="Secondary.TLabel").pack(side="left")
        self._rank_vars: dict[str, tk.BooleanVar] = {}
        for key, label in [("top_users", "发言人"), ("top_texts", "重复文案"),
                           ("top_liked", "最高赞"), ("top_videos", "热门视频"),
                           ("top_ups", "热门UP")]:
            var = tk.BooleanVar(value=True)
            self._rank_vars[key] = var
            ttk.Checkbutton(rank_row, text=label, variable=var).pack(side="left", padx=(6, 0))

        # 数据总览卡片
        fm = ClaudeCard(wrap, title="核心数据指标（全库总览）", subtitle="涵盖已采视频、评论正文、弹幕流及真实 UID 逆向统计", padding=14)
        fm.grid(row=2, column=0, sticky="new", pady=(0, 10))
        fm.body.columnconfigure(0, weight=1)
        fm.body.columnconfigure(1, weight=1)

        labels = [
            ("采集视频数", "videos"),
            ("有评论的视频数", "video_with_comments"),
            ("已采评论条数", "comments"),
            ("已采弹幕条数", "danmaku"),
            ("用户维度(UP/评论者)人数", "users"),
            ("去重评论UID数", "comment_uids"),
            ("已解析UID弹幕条数", "danmaku_parsed"),
            ("未解析弹幕条数", "danmaku_unparsed"),
            ("弹幕UID反查命中hash数", "crc_hits"),
            ("反查缓存总条数", "crc_total"),
        ]
        entries: list[tuple[ttk.Label, str]] = []
        for i, (text, key) in enumerate(labels):
            r, c = divmod(i, 2)
            lbl = ttk.Label(fm.body, text=f"{text}: --", style="Card.TLabel", anchor="w")
            lbl.grid(row=r, column=c, sticky="ew", padx=(0, 24), pady=3)
            entries.append((lbl, key))
        self._cells = entries
        # 真进度条（总览 10 步）
        self.prog = ttk.Progressbar(wrap, mode="determinate", maximum=10, value=0)
        self.prog.grid(row=3, column=0, sticky="ew", pady=(4, 0))

        # 数据库位置与占用卡片
        db = ClaudeCard(wrap, title="底层 MySQL 存储引擎", subtitle="磁盘物理空间占用、内存占用与数据目录", padding=14)
        db.grid(row=4, column=0, sticky="new", pady=(0, 10))
        db.body.columnconfigure(0, weight=1)
        self.lbl_dbloc = ttk.Label(db.body, text="位置: --", style="Card.TLabel", anchor="w")
        self.lbl_dbloc.grid(row=0, column=0, sticky="ew")
        self.lbl_dbsize = ttk.Label(db.body, text="磁盘占用: --", style="Card.TLabel", anchor="w")
        self.lbl_dbsize.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.lbl_dbmem = ttk.Label(db.body, text="内存: --", style="Card.TLabel", anchor="w")
        self.lbl_dbmem.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self.lbl_dbtables = ttk.Label(db.body, text="表占用: --", style="CardHint.TLabel", anchor="w")
        self.lbl_dbtables.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        self.lbl_datadir = ttk.Label(db.body, text="数据目录: --", style="CardHint.TLabel", anchor="w")
        self.lbl_datadir.grid(row=4, column=0, sticky="ew", pady=(2, 0))

        # 排行榜（底部 Notebook）
        nb = ttk.Notebook(self)
        nb.grid(row=4, column=0, sticky="nsew", padx=6, pady=(0, 4))
        self._rank_trees: dict[str, SmartTable] = {}
        self._rank_cnts: dict[str, ttk.Label] = {}
        spec = [
            ("top_users", "发言最多的人", [
                ("uid", "UID", 10), ("uname", "昵称", 22), ("cnt", "评论数", 10)]),
            ("top_texts", "重复最多的文案", [
                ("content", "内容(完全相同归组)", 80), ("cnt", "次数", 9),
                ("senders", "人数", 9), ("tlen", "字数", 7)]),
            ("top_liked", "最高赞评论", [
                ("like", "赞", 8), ("uname", "发送者", 16), ("content", "内容", 70),
                ("time", "时间", 14), ("video", "视频", 40)]),
            ("top_videos", "被评论最多的视频", [
                ("bvid", "BV", 16), ("title", "标题", 50), ("up", "UP", 18),
                ("cnt", "评论数", 10)]),
            ("top_ups", "被评论最多的UP", [
                ("uid", "UID", 10), ("uname", "昵称", 24), ("cnt", "评论数", 10)]),
        ]
        for key, title, cols in spec:
            tab = ttk.Frame(nb, padding=(4, 2))
            nb.add(tab, text=title)
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(1, weight=1)
            top_bar = ttk.Frame(tab)
            top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 2))
            cnt = ttk.Label(top_bar, text="0 条", foreground="#888")
            cnt.pack(side="left")
            btn_exp = ttk.Button(top_bar, text="导出本榜CSV",
                                 command=lambda k=key, t=title, c=cols: self._export_rank_csv(k, t, c))
            btn_exp.pack(side="right")
            tree = SmartTable(tab, show="headings")
            tree.set_columns(cols)
            tree.grid(row=1, column=0, sticky="nsew")
            sb = ttk.Scrollbar(tab, command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            sb.grid(row=1, column=1, sticky="ns")
            self._rank_trees[key] = tree
            self._rank_cnts[key] = cnt
        self._build_insight_tab(nb)
        self._build_detect_tab(nb)
        self._nb = nb
        self._overlay = ttk.Label(nb, text="统计中，点击「取消统计」可取消...", background=C.BG_TABLE_SEL, foreground=C.ACCENT,
                                  font=F.SUBTITLE, anchor="center")

    def _export_rank_csv(self, key: str, title: str, cols: list[tuple]) -> None:
        tree = self._rank_trees.get(key)
        if not tree or not tree.get_children():
            messagebox.showinfo("提示", "当前榜单暂无数据，请先刷新排行榜")
            return
        path = filedialog.asksaveasfilename(
            title=f"导出{title}",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"{title}.csv",
        )
        if not path:
            return
        import csv
        headers = [c[1] for c in cols]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for iid in tree.get_children():
                    w.writerow(tree.item(iid, "values"))
            messagebox.showinfo("导出完成", f"已成功导出 {len(tree.get_children())} 条记录至:\n{path}")
        except Exception as exc:
            messagebox.showerror("导出失败", f"写入文件失败: {exc}")

    # ---------------- 趋势洞察（统计 V2 新增页） ----------------
    def _build_insight_tab(self, nb: ttk.Notebook) -> None:
        """第 6 个页签：关键词抽样 → 内存聚合 → 高频词/情感/时段/趋势图。"""
        tab = ttk.Frame(nb, padding=(6, 4))
        nb.add(tab, text="趋势洞察")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        self._insight_imgs: list = []  # 持有 PhotoImage 引用防 GC
        self._insight_data: dict = {}
        self._insight_charts: dict = {}

        ctl = ttk.Frame(tab)
        ctl.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(ctl, text="关键词抽样:").pack(side="left")
        self.var_insight_kw = tk.StringVar()
        ttk.Entry(ctl, textvariable=self.var_insight_kw, width=22).pack(side="left", padx=2)
        ttk.Label(ctl, text="近N天:").pack(side="left", padx=(8, 0))
        self.var_insight_days = tk.StringVar(value="30")
        ttk.Spinbox(ctl, from_=7, to=365, textvariable=self.var_insight_days,
                    width=5).pack(side="left")
        ttk.Label(ctl, text="图表:").pack(side="left", padx=(8, 0))
        self.var_insight_chart = tk.StringVar(value="每日趋势")
        _cb = ttk.Combobox(ctl, textvariable=self.var_insight_chart, width=10,
                           values=["每日趋势", "24小时分布", "高频词"],
                           state="readonly")
        _cb.pack(side="left", padx=2)
        _cb.bind("<<ComboboxSelected>>", self._show_insight_chart)
        self.btn_insight = ttk.Button(ctl, text="生成洞察",
                                      command=self.refresh_insights)
        self.btn_insight.pack(side="left", padx=8)
        ttk.Button(ctl, text="导出CSV",
                   command=self.export_insights_csv).pack(side="left")
        ttk.Button(ctl, text="打开图表目录",
                   command=self.open_charts_dir).pack(side="left", padx=4)
        self.lbl_insight_status = ttk.Label(ctl, text="", style="Hint.TLabel")
        self.lbl_insight_status.pack(side="left", padx=8)

        self.lbl_insight_summary = ttk.Label(tab, text="输入关键词限定范围（留空=最新采样），点「生成洞察」。",
                                             style="Secondary.TLabel", wraplength=860,
                                             justify="left")
        self.lbl_insight_summary.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        body = ttk.Frame(tab)
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Label(left, text="高频词 Top20", style="Secondary.TLabel").pack(anchor="w")
        self.insight_terms = SmartTable(left, show="headings")
        self.insight_terms.set_columns([("term", "词", 22), ("cnt", "次数", 10)])
        self.insight_terms.pack(fill="both", expand=True)
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        ttk.Label(right, text="图表", style="Secondary.TLabel").grid(row=0, column=0, sticky="w")
        self.lbl_insight_img = ttk.Label(right, text="（生成洞察后显示图表）",
                                         anchor="center", background=C.BG_CARD)
        self.lbl_insight_img.grid(row=1, column=0, sticky="nsew")
        # 缓存秒显：启动时若有上次 insights，直接显示文字部分
        self.after(600, self._show_cached_insights)

    def _show_cached_insights(self) -> None:
        try:
            from analysis import stats_service as _svc
            ins, when = _svc.load_cached_insights()
            if ins:
                self._apply_insights(ins, {}, f"上次洞察: {when}（点生成洞察刷新）")
        except Exception:
            pass

    def refresh_insights(self) -> None:
        """后台生成洞察（抽样→聚合→画图→缓存），主线程不阻塞。"""
        if getattr(self, "_insight_thread", None) and self._insight_thread.is_alive():
            return
        try:
            days = max(7, min(365, int(self.var_insight_days.get() or 30)))
        except ValueError:
            days = 30
        kw = self.var_insight_kw.get().strip()
        self.btn_insight.configure(state="disabled", text="生成中…")
        self.lbl_insight_status.configure(text="抽样聚合中…")

        def _worker() -> None:
            try:
                from analysis import stats_service as _svc
                from visualization import charts as _charts
                rows = _svc.sample_texts_for_insight(limit=5000, keyword=kw)
                ins = _svc.compute_insights(rows, top=20, days=days)
                # 全库趋势覆盖抽样（更准；失败则保留抽样值）
                try:
                    full_daily = _svc.get_daily_comments(days=days)
                    if full_daily and sum(d.get("cnt", 0) for d in full_daily) > 0:
                        ins["daily"] = full_daily
                except Exception:
                    pass
                try:
                    full_hours = _svc.get_hourly_comments()
                    if full_hours and sum(h.get("cnt", 0) for h in full_hours) > 0:
                        ins["hours"] = full_hours
                except Exception:
                    pass
                out_dir = os.path.join("logs", "stats_charts")
                charts = _charts.render_all(ins, out_dir)
                try:
                    _svc.save_cached_insights(ins)
                except Exception:
                    pass
                summary = _svc.insights_summary(ins)
                self._stats_q.put(("insight", ins, charts, summary))
            except Exception as exc:
                logger.warning("洞察生成失败: %s", exc)
                self._stats_q.put(("insight", None, {}, f"洞察生成失败：{exc}"))

        self._insight_thread = threading.Thread(target=_worker, daemon=True)
        self._insight_thread.start()

    def _apply_insights(self, ins: dict | None, charts: dict,
                        summary: str) -> None:
        try:
            self.btn_insight.configure(state="normal", text="生成洞察")
        except tk.TclError:
            return
        if not isinstance(ins, dict) or not ins.get("total"):
            self.lbl_insight_status.configure(text="")
            self.lbl_insight_summary.configure(text=summary or "暂无数据")
            return
        self._insight_data = ins
        self._insight_charts = charts or {}
        self.lbl_insight_status.configure(text=f"样本 {ins.get('total', 0)} 条")
        try:
            self.lbl_insight_summary.configure(text=summary)
        except tk.TclError:
            pass
        # 高频词表
        try:
            for iid in self.insight_terms.get_children():
                self.insight_terms.delete(iid)
            for t in (ins.get("terms") or [])[:20]:
                self.insight_terms.insert("", "end",
                                          values=(t.get("term"), t.get("cnt", 0)))
        except tk.TclError:
            pass
        self._show_insight_chart()

    def _show_insight_chart(self, *_args) -> None:
        """按下拉选择显示一张图（PhotoImage 展示 PNG 文件）。"""
        key_map = {"每日趋势": "daily", "24小时分布": "hours", "高频词": "terms"}
        key = key_map.get(self.var_insight_chart.get(), "daily")
        path = (self._insight_charts or {}).get(key)
        if not path or not os.path.exists(path):
            # 无 matplotlib 或无数据时给文字降级
            extra = ""
            try:
                from visualization import charts as _c
                if not _c.available():
                    extra = "（未安装 matplotlib：pip install matplotlib 后重生成）"
            except Exception:
                pass
            self.lbl_insight_img.configure(text=f"暂无图表{extra}", image="")
            return
        try:
            img = tk.PhotoImage(file=path)
            # 过宽时等比缩小到页签宽度内
            try:
                w = max(1, self.lbl_insight_img.winfo_width() or 800)
                if img.width() > w and w > 100:
                    step = max(1, img.width() // w)
                    if step > 1:
                        img = img.subsample(step, step)
            except Exception:
                pass
            self._insight_imgs.append(img)
            self.lbl_insight_img.configure(text="", image=img)
        except tk.TclError as exc:
            self.lbl_insight_img.configure(text=f"图表显示失败：{exc}", image="")

    def export_insights_csv(self) -> None:
        if not self._insight_data:
            messagebox.showinfo("导出", "暂无洞察数据，先点「生成洞察」。")
            return
        path = filedialog.asksaveasfilename(title="导出洞察CSV",
                                            defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile="insights.csv")
        if not path:
            return
        try:
            from analysis import stats_service as _svc
            _svc.export_insights_csv(self._insight_data, path)
            messagebox.showinfo("导出", f"已导出：{path}")
        except Exception as exc:
            messagebox.showerror("导出", f"导出失败：{exc}")

    def open_charts_dir(self) -> None:
        d = os.path.abspath(os.path.join("logs", "stats_charts"))
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("图表目录", d)

    # ---------------- 水军检测（第 7 个页签） ----------------
    def _build_detect_tab(self, nb: ttk.Notebook) -> None:
        """水军/刷屏/控评检测：按 BV 取数 → 后台打分 → 列表+导出。"""
        tab = ttk.Frame(nb, padding=(6, 4))
        nb.add(tab, text="水军检测")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self._detect_results: list[dict] = []

        ctl = ttk.Frame(tab)
        ctl.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(ctl, text="视频 BV:").pack(side="left")
        self.var_detect_bvid = tk.StringVar()
        ent = ttk.Entry(ctl, textvariable=self.var_detect_bvid, width=22)
        ent.pack(side="left", padx=2)
        ent.bind("<Return>", lambda _e: self.refresh_detect())
        ttk.Label(ctl, text="上限:").pack(side="left", padx=(8, 0))
        self.var_detect_limit = tk.StringVar(value="5000")
        ttk.Spinbox(ctl, from_=500, to=20000,
                    textvariable=self.var_detect_limit, width=6).pack(side="left")
        self.btn_detect = ttk.Button(ctl, text="开始检测",
                                     command=self.refresh_detect)
        self.btn_detect.pack(side="left", padx=8)
        ttk.Button(ctl, text="导出CSV",
                   command=self.export_detect_csv).pack(side="left")
        ttk.Button(ctl, text="看该用户发言",
                   command=self._detect_goto_user).pack(side="left", padx=4)
        self.lbl_detect_status = ttk.Label(ctl, text="", style="Hint.TLabel")
        self.lbl_detect_status.pack(side="left", padx=8)

        self.lbl_detect_summary = ttk.Label(
            tab, text="输入 BV（留空=最新采样兜底），点「开始检测」。结果仅供参考，请结合原文人工复核。",
            style="Secondary.TLabel", wraplength=860, justify="left")
        self.lbl_detect_summary.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        body = ttk.Frame(tab)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.tree_detect = SmartTable(body, show="headings")
        self.tree_detect.set_columns([
            ("uid", "UID", 12), ("uname", "昵称", 16),
            ("total", "发言数", 8), ("repeat", "复读率", 8),
            ("burst", "爆发峰值", 9), ("ip", "Top属地", 10),
            ("score", "评分", 7), ("level", "定级", 8),
            ("hits", "命中信号", 16),
        ])
        self.tree_detect.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(body, command=self.tree_detect.yview)
        self.tree_detect.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

    def refresh_detect(self) -> None:
        """后台线程执行检测，主线程通过队列回填（可重复点击=取消）。"""
        th = getattr(self, "_detect_thread", None)
        if th is not None and th.is_alive():
            try:
                self._detect_stop.set()
            except Exception:
                pass
            self.btn_detect.configure(state="disabled", text="取消中…")
            self.after(500, self._check_detect_join)
            return
        bvid = self.var_detect_bvid.get().strip()
        try:
            limit = max(500, min(20000, int(self.var_detect_limit.get() or 5000)))
        except ValueError:
            limit = 5000
        self._detect_stop = threading.Event()
        self.btn_detect.configure(state="normal", text="取消检测")
        self.lbl_detect_status.configure(text="检测中…")
        try:
            for iid in self.tree_detect.get_children():
                self.tree_detect.delete(iid)
        except tk.TclError:
            pass

        def _worker() -> None:
            try:
                from analysis import stats_service as _svc
                res = _svc.detect_for_video(bvid=bvid, limit=limit)
                if self._detect_stop.is_set():
                    return
                self._stats_q.put(("detect", res.get("results") or [],
                                   res.get("meta") or {}, res.get("total") or 0))
            except Exception as exc:
                logger.warning("水军检测失败: %s", exc)
                try:
                    self._stats_q.put(("detect", [], {}, 0))
                except Exception:
                    pass

        self._detect_thread = threading.Thread(target=_worker, daemon=True)
        self._detect_thread.start()

    def _check_detect_join(self) -> None:
        try:
            th = getattr(self, "_detect_thread", None)
            if th is not None and th.is_alive():
                self.after(500, self._check_detect_join)
                return
            self.btn_detect.configure(state="normal", text="开始检测")
            self.lbl_detect_status.configure(text="已取消")
        except tk.TclError:
            pass

    def _apply_detect(self, results: list[dict], meta: dict, total: int) -> None:
        try:
            self.btn_detect.configure(state="normal", text="开始检测")
        except tk.TclError:
            return
        self._detect_results = list(results or [])
        try:
            for iid in self.tree_detect.get_children():
                self.tree_detect.delete(iid)
            for r in self._detect_results:
                level_txt = {"high": "高危", "suspect": "存疑"}.get(
                    r.get("level"), "正常")
                self.tree_detect.insert("", "end", values=(
                    r.get("uid"), (r.get("uname") or "")[:16],
                    r.get("total"), r.get("repeat_rate"),
                    r.get("burst_max"),
                    f"{r.get('ip_top') or '-'} {r.get('ip_ratio') or 0}",
                    r.get("score"), level_txt,
                    "+".join(r.get("hits") or []) or "-",
                ))
        except tk.TclError:
            pass
        try:
            from analysis import stats_service as _svc
            summary = _svc.detect_summary(
                {"results": self._detect_results, "total": total})
            title = ""
            if meta and meta.get("bvid"):
                title = f"{meta.get('bvid')} {(meta.get('title') or '')[:30]}｜"
            self.lbl_detect_summary.configure(text=title + summary)
            self.lbl_detect_status.configure(
                text=f"样本 {total} 条 / 嫌疑 {len(self._detect_results)} 人")
        except tk.TclError:
            pass

    def export_detect_csv(self) -> None:
        if not getattr(self, "_detect_results", None):
            messagebox.showinfo("导出", "暂无检测结果，先点「开始检测」。")
            return
        path = filedialog.asksaveasfilename(title="导出水军检测CSV",
                                            defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile="water_army.csv")
        if not path:
            return
        try:
            from analysis import stats_service as _svc
            _svc.export_detect_csv(self._detect_results, path)
            messagebox.showinfo("导出", f"已导出：{path}")
        except Exception as exc:
            messagebox.showerror("导出", f"导出失败：{exc}")

    def _detect_goto_user(self) -> None:
        sel = self.tree_detect.selection()
        if not sel:
            messagebox.showinfo("提示", "先在结果表中选中一行。")
            return
        try:
            vals = self.tree_detect.item(sel[0], "values")
            uid = str(vals[0]).strip()
        except (tk.TclError, IndexError):
            return
        if not uid or not uid.isdigit():
            return
        top = self.winfo_toplevel()
        switch = getattr(top, "_switch", None)
        if not callable(switch):
            messagebox.showinfo("提示", f"UID={uid}")
            return
        switch("用户查询")
        try:
            from gui.user_query_panel import UserQueryPanel
            for panel in getattr(top, "_pages", {}).values():
                if isinstance(panel, UserQueryPanel):
                    panel.var_uid.set(uid)
                    panel._query()
                    break
        except Exception as exc:
            logger.warning("跳转用户查询失败: %s", exc)

    def snapshot_state(self) -> dict:
        """会话快照：统计筛选 + 洞察输入（供重启恢复）。"""
        try:
            return {"start": self.var_start.get(), "end": self.var_end.get(),
                    "top": self.var_top.get(), "minlen": self.var_minlen.get(),
                    "kw": self.var_insight_kw.get(),
                    "days": self.var_insight_days.get(),
                    "chart": self.var_insight_chart.get(),
                    "detect_bvid": self.var_detect_bvid.get(),
                    "detect_limit": self.var_detect_limit.get()}
        except Exception:
            return {}

    def restore_state(self, d: dict) -> None:
        try:
            if not isinstance(d, dict):
                return
            for var, key in ((getattr(self, "var_start", None), "start"),
                             (getattr(self, "var_end", None), "end"),
                             (getattr(self, "var_top", None), "top"),
                             (getattr(self, "var_minlen", None), "minlen"),
                             (getattr(self, "var_insight_kw", None), "kw"),
                             (getattr(self, "var_insight_days", None), "days"),
                             (getattr(self, "var_insight_chart", None), "chart"),
                             (getattr(self, "var_detect_bvid", None), "detect_bvid"),
                             (getattr(self, "var_detect_limit", None), "detect_limit")):
                if var is not None and d.get(key) is not None:
                    var.set(str(d.get(key)))
        except Exception:
            pass

    def refresh(self, mode: str = "full") -> None:
        """后台线程执行统计（与排行榜）查询，完成后回填，主线程不阻塞。

        mode="full"        ：总览 + 全部排行榜 + 落盘缓存（较慢）
        mode="stats_only"  ：只刷新"数据总览"条目（秒级），排行榜保持上次结果
        点击时若正在统计，则取消本次统计。
        """
        if getattr(self, "_stats_thread", None) and self._stats_thread.is_alive():
            # 协作式取消：发信号后按钮置“取消中…”禁用，等线程真正退出再恢复
            # （dao 层查完当前仓即停；在途单条 SQL 最多等到 read 超时）
            self._stats_stop.set()
            self.btn_refresh.configure(state="disabled", text="取消中…")
            self.btn_stats_only.configure(state="disabled", text="取消中…")
            try:
                self._overlay.configure(text="正在取消…查完当前仓即停")
            except tk.TclError:
                pass
            self.after(500, self._check_cancel_join)
            return
        try:
            top_n = max(1, int(self.var_top.get() or 100))
        except ValueError:
            top_n = 100
        try:
            min_len = max(0, int(self.var_minlen.get() or 0))
        except ValueError:
            min_len = 0
        start = self.var_start.get().strip()
        end = self.var_end.get().strip()
        if mode == "full" and start and end and start > end:
            from tkinter import messagebox

            messagebox.showerror("错误", "时间范围无效：开始晚于结束")
            return
        # 在主线程安全读取 Tkinter 变量（禁止在子线程中调用 .get()）
        which = [k for k, v in self._rank_vars.items() if v.get()] if mode == "full" else []
        rank_count = len(which)
        max_prog_steps = 10 if mode == "stats_only" else (10 + rank_count)

        self._stats_stop = threading.Event()
        # 允许点击取消，所以保持可点
        self.btn_refresh.configure(state="normal", text="取消统计" if mode=="full" else "刷新")
        self.btn_stats_only.configure(state="normal" if mode!="full" else "disabled")
        if mode == "full":
            self.btn_refresh.configure(text="取消统计")
        else:
            self.btn_stats_only.configure(text="总览统计中…")
        try:
            self.prog.configure(value=0, maximum=max_prog_steps)
        except Exception: pass
        try:
            self._overlay.place(relx=0, rely=1.0, anchor="sw", relwidth=1, relheight=0.18)
            self._overlay.lift()
        except tk.TclError:
            pass
        self._refresh_db_info()

        def _worker() -> None:
            stop = self._stats_stop
            stats = ranks = None
            STAT_NAMES = ["采集视频数","有评论的视频数","已采评论条数","已采弹幕条数","用户维度",
                          "去重评论UID数","已解析弹幕","未解析弹幕","弹幕hash","缓存总条数"]
            RANK_NAMES = {"top_users": "发言最多的人", "top_texts": "重复文案",
                          "top_liked": "最高赞评论", "top_videos": "被评论最多的视频",
                          "top_ups": "被评论最多的UP"}
            def _set_overlay(txt):
                self._stats_q.put(("progress", None, None, txt))

            def _prog(done, total):
                self._stats_q.put(("progress", done, total, None))

            rank_done_idx = [0]
            def _stat_done(key, value, mark=None):
                try:
                    self._stats_q.put(("stat_partial", key, value, mark))
                except Exception:
                    pass
            try:
                def _prog10(d, t):
                    name = STAT_NAMES[d-1] if 1 <= d <= len(STAT_NAMES) else f"总览 {d}/{t}"
                    txt = f"统计中… 总览 {name} ({d}/{t})  点击「取消统计」可取消"
                    self._stats_q.put(("progress", d, max_prog_steps, txt))
                _set_overlay("统计中… 正在统计总览 (0/10)")
                stats = dao.get_collection_stats(progress_cb=_prog10, stop_event=stop,
                                                 stat_done_cb=_stat_done)
                self._stats_q.put(("progress", 10, max_prog_steps, "总览就绪"))
            except dao.StatsCancelled:
                pass
            except Exception as exc:
                logger.warning("统计查询失败: %s", exc)
            if stop.is_set():
                self.after(0, lambda: self._reset_refresh_btns())
                return
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            if mode == "stats_only":
                self._stats_q.put((stats, None, now, "stats_only"))
                if stats is not None and not stop.is_set():
                    try:
                        from gui.util import ping_main as _ping2
                        self.after(0, lambda: _ping2(self, "统计洞察"))
                    except Exception:
                        pass
                return
            # 总览先行落缓存（不等几小时的排行榜）：全量无缺失才存，保留旧排行榜
            try:
                if (isinstance(stats, dict) and not stats.get("_failed_shards")
                        and not stats.get("_partial")):
                    _prev = dao.load_stats_cache() or {}
                    dao.save_stats_cache({"stats": stats, "ranks": _prev.get("ranks"),
                                          "time": now})
            except Exception:
                pass
            # 排行榜：逐个完成逐个回调 + 逐个落缓存
            ranks = {}
            partial_ranks = {}
            def _rank_done(key, rows):
                ranks[key] = rows
                partial_ranks[key] = rows
                rank_done_idx[0] += 1
                name = RANK_NAMES.get(key, key)
                _set_overlay(f"排行榜 {name} 完成 ({rank_done_idx[0]}/{rank_count})")
                try:
                    self.after(0, lambda d=rank_done_idx[0]: self.prog.configure(value=10+d, maximum=10+rank_count))
                except Exception: pass
                # 逐个落缓存（已完成的榜立即存盘，中断不丢；stats 为空不存防毒缓存）
                if stats is not None:
                    try:
                        dao.save_stats_cache({"stats": stats, "ranks": dict(partial_ranks), "time": now})
                    except Exception:
                        pass
                # 逐个推给 GUI 显示
                try:
                    self._stats_q.put((None, {key: rows}, now, "rank_partial"))
                except Exception:
                    pass
            try:
                def _prog_rank(d, t):
                    # d 从 1 开始，对应 which 列表的索引
                    key = which[d-1] if d <= len(which) else "?"
                    name = RANK_NAMES.get(key, key)
                    _set_overlay(f"统计中… 排行榜 {name} ({d}/{rank_count})  点击「取消统计」可取消")
                    try:
                        self.after(0, lambda dd=d: self.prog.configure(value=10+dd, maximum=10+rank_count))
                    except Exception: pass
                ranks = dao.get_rankings(start=start, end=end, top=top_n,
                                         min_text_len=min_len, progress_cb=_prog_rank,
                                         stop_event=stop, which=which,
                                         rank_done_cb=_rank_done)
                self.after(0, lambda: self.prog.configure(value=10+rank_count, maximum=10+rank_count))
                _set_overlay("统计完成")
            except dao.StatsCancelled:
                pass
            except Exception as exc:
                logger.warning("排行榜查询失败: %s", exc)
            if stop.is_set():
                self.after(0, lambda: self._reset_refresh_btns())
                return
            _failed = (stats.get("_failed_shards") or {}) if isinstance(stats, dict) else {}
            if stats is not None and ranks is not None and not _failed:
                dao.save_stats_cache({"stats": stats, "ranks": ranks, "time": now})
                try:
                    from gui.util import ping_main as _ping
                    self.after(0, lambda: _ping(self, "统计"))
                except Exception:
                    pass
            elif _failed:
                # 部分分片失败：不存残数，明确告诉用户哪几项不全
                _miss = ", ".join(f"{k}({','.join(v)})" for k, v in _failed.items())
                logger.warning("统计部分分片失败，未存缓存: %s", _miss)
                now = f"部分失败（{_miss}），显示为部分数据，未存缓存"
            else:
                try:
                    cached = dao.load_stats_cache()
                except Exception:
                    cached = None
                if cached and cached.get("stats") is not None and cached.get("ranks") is not None:
                    stats = cached["stats"]
                    ranks = cached["ranks"]
                    now = "上次成功数据（本次刷新失败，可能数据库繁忙）"
                    logger.warning("统计刷新失败，回退缓存展示")
                elif stats is None:
                    stats = None
            self._stats_q.put((stats, ranks, now))
        self._stats_thread = threading.Thread(target=_worker, daemon=True)
        self._stats_thread.start()


    def _set_updated(self, text: str) -> None:
        try:
            self.lbl_updated.configure(text=text)
        except Exception:
            pass

    @staticmethod
    def _fmt_rows(n) -> str:
        """行数缩写：3_000_000 → 3.0M。"""
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            return "?"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.0f}K"
        return str(n)

    def _apply_db_info(self, info) -> None:
        if not isinstance(info, dict):
            for lbl in (self.lbl_dbloc, self.lbl_dbsize,
                        self.lbl_dbmem, self.lbl_dbtables, self.lbl_datadir):
                lbl.configure(text=f"{lbl.cget('text').split(':')[0]}: 查询失败")
            return
        ver = f"{info.get('version', '')}".split("-")[0]
        self.lbl_dbloc.configure(
            text=f"位置: {info.get('host', '')}:{info.get('port', '')} "
                 f"/ {info.get('database', '')} ({info.get('charset', '')})"
                 + (f"   版本: {ver}" if ver else ""))
        size = info.get("size_total_mb") or 0
        size_txt = (f"{size / 1024:.2f} GB" if size >= 1024 else f"{size:.0f} MB")
        self.lbl_dbsize.configure(
            text=f"磁盘占用: {size_txt}（数据+索引，共 {info.get('table_count', 0)} 张表）")
        bp = info.get("bp_mb")
        cached = info.get("cached_mb")
        mem = info.get("mem_mb")
        parts = [f"缓冲池 {bp:g} MB" if bp else "缓冲池: --"]
        if cached is not None:
            parts.append(f"已缓存数据 {cached:g} MB")
        if mem is not None:
            parts.append(f"进程分配 ≈{mem:g} MB")
        self.lbl_dbmem.configure(text="内存: " + " / ".join(parts))
        top = [t for t in (info.get("tables") or []) if t.get("name")]
        big = top[:4]
        if big:
            cells = "   ".join(
                f"{t['name']} {self._fmt_rows(t.get('rows'))}行 {t.get('size_mb', 0):g}MB"
                for t in big)
            self.lbl_dbtables.configure(text=f"占用前{len(big)}: {cells}")
        else:
            self.lbl_dbtables.configure(text="表占用: 无业务表")
        datadir = info.get("datadir", "")
        if datadir:
            self.lbl_datadir.configure(text=f"数据目录: {datadir}")

    def _apply(self, payload) -> None:
        if payload is None:  # 无缓存：显示占位，不写失败
            for lbl, _key in self._cells:
                lbl.configure(text=f"{lbl.cget('text').split(':')[0]}: --")
            for _key, tree in self._rank_trees.items():
                for iid in tree.get_children():
                    tree.delete(iid)
            self._set_updated("暂无统计——点击「刷新」生成（首次生成较慢）")
            return
        if not isinstance(payload, tuple):
            return
        stats, ranks, updated = (payload + ("", ""))[:3]
        mode = payload[3] if len(payload) > 3 else "full"
        self.btn_refresh.configure(state="normal")
        self.btn_refresh.configure(text="刷新")
        self.btn_stats_only.configure(state="normal")
        self.btn_stats_only.configure(text="仅刷新总览")
        if mode == "stats_only":
            # 仅总览：只更新条目与时间标签，排行榜与缓存不动
            self._set_updated(
                f"总览更新: {updated}" if stats is not None
                else "仅总览刷新失败（排行榜保持上次结果）")
            if stats is None:
                for lbl, _key in self._cells:
                    lbl.configure(text="-- 读取失败 --")
            else:
                self._fill_cells(stats)
            self.prog.configure(value=10, maximum=10)
            self.after(300, lambda: [
                self.prog.configure(value=0),
                self._overlay.place_forget()
            ])
            return
        self._set_updated(f"上次更新: {updated}" if updated else "")
        # 总览
        if stats is None:
            for lbl, _key in self._cells:
                lbl.configure(text="-- 读取失败 --")
        else:
            self._fill_cells(stats)
            partial = stats.get("_partial") or {}
            if partial:
                self._set_updated(
                    f"上次更新: {updated}　⚠ {len(partial)}项非全量（E离线/部分库缺失），连E后刷新")
        # 排行榜
        if not isinstance(ranks, dict):
            for key, tree in self._rank_trees.items():
                for iid in tree.get_children():
                    tree.delete(iid)
                self._rank_cnts[key].configure(text="-- 读取失败 --")
            self.after(400, lambda: [
                self.prog.configure(value=0),
                self._overlay.place_forget()
            ])
            return
        for key, tree in self._rank_trees.items():
            for iid in tree.get_children():
                tree.delete(iid)
            rows = ranks.get(key) or []
            for r in rows:
                tree.insert("", "end", values=self._fmt_row(key, r))
            self._rank_cnts[key].configure(text=f"{len(rows)} 条")
        self.after(600, lambda: [
            self.prog.configure(value=0),
            self._overlay.place_forget()
        ])

    def _fill_cells(self, stats: dict) -> None:
        """填充总览格子；_partial 标注的项追加警告后缀。"""
        partial = (stats.get("_partial") or {}) if isinstance(stats, dict) else {}
        for lbl, key in self._cells:
            base = f"{lbl.cget('text').split(':')[0]}: {stats.get(key, 0):,}"
            mark = partial.get(key)
            lbl.configure(text=f"{base}（{mark}）" if mark else base)

    def _fill_one(self, key: str, value, mark=None) -> None:
        """单个格子实时回填（总览流式）。"""
        try:
            for lbl, k in self._cells:
                if k == key:
                    base = f"{lbl.cget('text').split(':')[0]}: {int(value or 0):,}"
                    lbl.configure(text=f"{base}（{mark}）" if mark else base)
                    break
        except (tk.TclError, TypeError, ValueError):
            pass

    def _apply_stat_partial(self, key: str, value, mark=None) -> None:
        self._fill_one(key, value, mark)

    def _apply_rank_partial(self, payload) -> None:
        """逐榜完成时的增量回填：只更新完成的那个榜的 Treeview。"""
        if not isinstance(payload, tuple) or len(payload) < 2:
            return
        _, partial_ranks, _, _ = (payload + ("", "", ""))[:4]
        if not isinstance(partial_ranks, dict):
            return
        for key, rows in partial_ranks.items():
            tree = self._rank_trees.get(key)
            if tree is None:
                continue
            for iid in tree.get_children():
                tree.delete(iid)
            for r in (rows or []):
                tree.insert("", "end", values=self._fmt_row(key, r))
            cnt_lbl = self._rank_cnts.get(key)
            if cnt_lbl:
                cnt_lbl.configure(text=f"{len(rows)} 条")

    @staticmethod
    def _fmt_row(key: str, r: dict) -> tuple:
        """按榜单类型把 DB 行格式化为表格 cells。"""
        if key == "top_users":
            return (r.get("uid", ""), r.get("uname", "") or "", r.get("cnt", 0))
        if key == "top_texts":
            content = str(r.get("content", ""))
            return (content[:200], r.get("cnt", 0), r.get("senders", 0),
                    r.get("tlen", len(content)))
        if key == "top_liked":
            return (r.get("like_num", 0), r.get("uname", "") or r.get("uid", ""),
                    str(r.get("content", ""))[:180], fmt_dt(r.get("ctime")),
                    f"{r.get('bvid', '')} {str(r.get('title', '') or '')[:26]}")
        if key == "top_videos":
            up = r.get("owner_name") or ""
            return (r.get("bvid", ""), str(r.get("title", "") or "")[:60], up,
                    r.get("cnt", 0))
        if key == "top_ups":
            return (r.get("uid", ""), r.get("uname", "") or "", r.get("cnt", 0))
        return tuple(str(v) for v in r.values())[:6]