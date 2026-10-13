"""广泛采集面板：以种子 UID 起 BFS 滚动扩展，自动反推新 UP 采集评论。"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from gui.crawl_worker import (
    MSG_DONE,
    MSG_ERROR,
    MSG_ITEM,
    MSG_PROGRESS,
    BroadCrawlWorker,
    PopularCrawlWorker,
)
from gui.theme import C
from gui.util import format_comment_line
from gui.widgets import SmartTable

logger = logging.getLogger(__name__)

# 实时流行数上限：超出截掉头部，防 Tk Text 长行数 O(n) 卡死
_MAX_STREAM_LINES = 1500
# 每个 120ms tick 最多渲染的评论条数；超出的丢弃渲染(数据仍写库)
_ITEM_RENDER_BUDGET = 30

FIELDS = [
    ("最大深度", "max_depth", "9999", 0),
    ("UP上限", "max_ups", "999999", 0),
    ("视频列表页数", "max_pages", "500", 0),
    ("每UP反推上限", "top_n", "100", 0),
    ("候选最少评论数", "min_comments", "1", 0),
    ("存储上限MB", "max_storage_mb", "25000", 0),
]

HOT_FIELD = ("热搜视频数", "hot_count", "100", 0)


class BroadPanel(ttk.Frame):
    """广泛采集：种子 → 自动扩展 → 逐 UP 采评论，附存储上限护栏。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.worker: BroadCrawlWorker | None = None
        self.msg_q: queue.Queue = queue.Queue()
        self._stick_bottom = True  # 是否跟随实时流底部（用户上翻后自动置 False）
        self._drag = False  # 是否正在拖动滚动条
        self._t0: float = 0.0  # 本次任务开始时刻（运行时长）
        self.last_status: dict = {}  # 任务完成后保留最后快照
        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        top = ttk.Frame(self, padding=(6, 6))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="种子 UID（逗号/空格/换行分隔，BFS起点）:").pack(anchor="w")
        self.ent_seeds = tk.Text(top, height=2)
        self.ent_seeds.pack(side="top", fill="x", pady=(2, 4))

        params = ttk.Frame(top)
        params.pack(fill="x")
        self._vars: dict[str, tk.StringVar] = {}
        for label, key, default, _lo in FIELDS:
            cell = ttk.Frame(params)
            cell.pack(side="left", padx=6, anchor="n")
            ttk.Label(cell, text=label).pack(anchor="w")
            var = tk.StringVar(value=default)
            self._vars[key] = var
            ttk.Spinbox(cell, from_=0, to=100000, width=7,
                        textvariable=var).pack(anchor="w")

        btns = ttk.Frame(top)
        btns.pack(fill="x", pady=(6, 0))
        self.btn_start = ttk.Button(btns, text="开始广泛采集", style="Accent.TButton", command=self._toggle)
        self.btn_start.pack(side="left")
        self.btn_lib_seed = ttk.Button(btns, text="从库内UP起采（10个）",
                                       command=self._seed_from_library)
        self.btn_lib_seed.pack(side="left", padx=6)
        self.btn_hot_seed = ttk.Button(btns, text="从热搜UP起采",
                                       command=self._seed_from_hot)
        self.btn_hot_seed.pack(side="left", padx=6)
        ttk.Label(btns, text="个数:").pack(side="left", padx=(2, 0))
        self.var_hot_seed = tk.StringVar(value="10")
        self._vars["hot_seed_count"] = self.var_hot_seed
        ttk.Spinbox(btns, from_=1, to=100, width=4,
                    textvariable=self.var_hot_seed).pack(side="left")
        self.var_perpetual = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="永续采集", variable=self.var_perpetual).pack(side="left", padx=(12, 0))
        self.btn_pause = ttk.Button(btns, text="暂停", command=self._toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        self.lbl_stat = ttk.Label(btns, text="当前库占用：查询中…")
        self.lbl_stat.pack(side="left", padx=12)

        hot = ttk.Frame(top)
        hot.pack(fill="x", pady=(6, 0))
        self.btn_hot = ttk.Button(hot, text="一键采集热搜视频评论",
                                  command=self._toggle_hot)
        self.btn_hot.pack(side="left")
        ttk.Label(hot, text="数量:").pack(side="left", padx=(10, 2))
        self.var_hot = tk.StringVar(value=HOT_FIELD[2])
        self._vars[HOT_FIELD[1]] = self.var_hot
        ttk.Spinbox(hot, from_=1, to=100, width=5,
                    textvariable=self.var_hot).pack(side="left")

        # 进度行：单UP视频进度 + 总UP进度
        prog = ttk.Frame(self, padding=(6, 0))
        prog.grid(row=1, column=0, sticky="ew")
        self.prog = ttk.Progressbar(prog, orient="horizontal", length=180,
                                    mode="determinate", maximum=100)
        self.prog.pack(side="left", padx=(0, 8))
        self.lbl_vpos = ttk.Label(prog, text="", foreground=C.ACCENT)
        self.lbl_vpos.pack(side="left", padx=(0, 8))
        self.lbl_status = ttk.Label(prog, text="就绪。粘贴种子 UID 开始。")
        self.lbl_status.pack(side="left")
        prog2 = ttk.Frame(self, padding=(6, 0))
        prog2.grid(row=1, column=0, sticky="e", padx=6)
        ttk.Label(prog2, text="总UP:").pack(side="left")
        self.prog_total = ttk.Progressbar(prog2, orient="horizontal", length=140,
                                          mode="determinate", maximum=100, value=0)
        self.prog_total.pack(side="left", padx=(4, 0))
        self.lbl_total = ttk.Label(prog2, text="0/0", style="Secondary.TLabel")
        self.lbl_total.pack(side="left", padx=(4, 0))

        # 已处理 UP 表
        grid = ttk.LabelFrame(self, text="已采集 UP", padding=(4, 2))
        grid.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        self.tree = SmartTable(grid, show="headings")
        self.tree.set_columns(
            [("uid", "UID", 12), ("total", "视频总数", 9),
             ("done", "已采/总", 12), ("cmt", "评论数", 9)]
        )
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(grid, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        # 实时流
        stream = ttk.LabelFrame(self, text="实时内容流", padding=(4, 2))
        stream.grid(row=3, column=0, sticky="nsew", padx=6)
        stream.columnconfigure(0, weight=1)
        stream.rowconfigure(0, weight=1)
        self.txt = tk.Text(stream, height=10, wrap="word", state="disabled",
                           font=("Consolas", 10))
        sb2 = ttk.Scrollbar(stream, command=self._sb_yview_wrapper)
        self.txt.configure(yscrollcommand=sb2.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb2.grid(row=0, column=1, sticky="ns")
        # 用户手动上翻（滚轮/触控板/拖动滚动条）→ 解除"跟随底部"
        self.txt.bind("<MouseWheel>", self._on_wheel)
        self.txt.bind("<Button-4>", lambda _e: self._user_scroll_up())
        self.txt.bind("<Button-5>", lambda _e: self._user_scroll_down())
        # 滚动条拖动：按下即锁定不跟随，松开时按位置恢复
        sb2.bind("<ButtonPress-1>", lambda _e: setattr(self, "_drag", True))
        sb2.bind("<ButtonRelease-1>", self._on_sb_release)

        # 当前信息（运行中逐秒刷新）
        info = ttk.LabelFrame(self, text="当前信息", padding=(6, 2))
        info.grid(row=4, column=0, sticky="ew", padx=6, pady=(2, 2))
        info.columnconfigure(0, weight=1)
        self.lbl_info_now = ttk.Label(info, text="空闲", foreground="#335")
        self.lbl_info_now.grid(row=0, column=0, sticky="w")
        self.lbl_info_tot = ttk.Label(info, text="", foreground="#555")
        self.lbl_info_tot.grid(row=1, column=0, sticky="w")

        self._pause_stream = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="暂停实时流", variable=self._pause_stream).grid(row=5, column=0, sticky="w", padx=6, pady=4)
        ttk.Button(self, text="清空流/表", command=lambda: self._clear(confirm=True)).grid(
            row=5, column=0, sticky="e", padx=6, pady=4)

        self._refresh_db_size()

    # ---------------- 工具 ----------------
    def _append(self, line: str, *, see: bool = False) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        # 行数封顶：超过上限截掉头部一半，防 Tk Text 长行数 O(n) 卡死
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

    def _clear(self, *, confirm: bool = False) -> None:
        # confirm=True 仅用于用户点「清空流/表」按钮；内部开新采集时的自动
        # 清理不提示（否则每次开始采集都弹窗）。表格是采集候选结果、
        # Text 是实时流记录，属有价值数据，故按钮路径非空时二次确认。
        if confirm:
            has_rows = bool(self.tree.get_children())
            has_text = bool(self.txt.get("1.0", "end-1c").strip())
            if has_rows or has_text:
                what = []
                if has_rows:
                    what.append(f"候选表 {len(self.tree.get_children())} 行")
                if has_text:
                    what.append("实时流记录")
                if not messagebox.askyesno(
                        "确认清空", "将清空：" + "、".join(what) + "。\n此操作不可撤销，仍要继续吗？"):
                    return
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", "（已清空 — 运行采集后，实时流与候选将显示在此）")
        self.txt.configure(state="disabled")
        self._stick_bottom = True  # 新一批内容恢复跟随底部
        for i in self.tree.get_children():
            self.tree.delete(i)

    def _refresh_info(self) -> None:
        st = dict(self.last_status)
        is_running = self.worker is not None and self.worker.is_alive()
        if self.worker is not None:
            ws = dict(self.worker.status) if isinstance(self.worker.status, dict) else {}
            if getattr(self.worker, "stats", None):
                ws.setdefault("videos", self.worker.stats.get("videos", 0))
                ws.setdefault("comments", self.worker.stats.get("comments", 0))
                ws.setdefault("candidates", self.worker.stats.get("candidates", 0))
                # stats["errors"] 在运行中可能是 int（_on_status 写入的计数），结束时是 list
                _errs = self.worker.stats.get("errors", 0)
                ws.setdefault("errors", len(_errs) if isinstance(_errs, list) else int(_errs or 0))
            # worker 刚启动时 ws 可能仍空，用 last_status 兜底，但运行中不显示空闲
            if ws:
                st = ws
            elif is_running and not st:
                try:
                    _mu = self._vars.get("max_ups").get() if hasattr(self, "_vars") and "max_ups" in self._vars else "?"
                except Exception:
                    _mu = "?"
                st = {"mode": "广泛", "phase": "collecting", "action": "正在启动…",
                      "queue": 0, "processed": 0, "max_ups": _mu}
        self.last_status = st
        try:
            mode = st.get("mode", "")
            phase = st.get("phase", "")
            queued = st.get("queue", 0)
            now_txt = "空闲"
            if is_running and not phase:
                now_txt = "[广泛] 正在启动…"
            elif phase == "done":
                if queued:
                    now_txt = f"[{mode}] 上个UP完成 · 队列剩余 {queued}"
                else:
                    now_txt = f"[{mode}] 任务完成"
            elif phase == "collecting":
                parts = [f"[{mode}]"]
                uid = st.get("uid", 0)
                who = f"UP {uid}" if uid else "准备中"
                uname = st.get("uname", "") or ""
                if uname:
                    who += f"（{uname}）"
                parts.append(f"正在采集 {who}")
                vtitle = st.get("vtitle", "") or ""
                if vtitle:
                    parts.append(f"视频《{vtitle}》")
                action = st.get("action", "") or ""
                if action:
                    parts.append(action)
                if mode == "广泛":
                    parts.append(f"队列剩余 {queued}")
                now_txt = " · ".join(parts)
            elif phase == "paused":
                now_txt = f"[{mode}] 已暂停 · 队列剩余 {queued}"
            if self._t0:
                elapsed = int(time.time() - self._t0)
                if elapsed < 60:
                    now_txt += f" · 已运行 {elapsed} 秒"
                else:
                    now_txt += f" · 已运行 {elapsed//60} 分 {elapsed%60} 秒"
            if mode == "热搜":
                suffix = f" · 视频 {st.get('fetched', 0)}/{st.get('videos', 0)}"
            else:
                suffix = f" · UP {st.get('processed', 0)}/{st.get('max_ups', '?')}"
            self.lbl_info_now.configure(text=f"进度: {now_txt}{suffix}")
            # 累计：若 videos 仍 0 但正在采，用当前UP的进度兜底
            v_tot = st.get('videos', 0)
            if not v_tot and st.get('phase') == 'collecting':
                try:
                    # 从进度条取当前视频cur
                    cur_txt = self.lbl_vpos.cget("text")  # "视频 446/4242"
                    import re as _re
                    m = _re.search(r"(\d+)\s*/", cur_txt)
                    if m: v_tot = int(m.group(1))
                except Exception: pass
            self.lbl_info_tot.configure(
                text=(f"累计: 视频 {v_tot} 个 · "
                      f"评论 {st.get('comments', 0)} 条 · "
                      f"反推候选 {st.get('candidates', 0)} 个 · "
                      f"错误 {st.get('errors', 0)} 个"))
            # 总UP进度条
            try:
                proc = int(st.get('processed', 0) or 0)
                max_u = st.get('max_ups', 0)
                try: max_u = int(max_u)
                except Exception: max_u = 0
                if max_u and max_u < 999999:
                    self.prog_total.configure(maximum=max_u, value=proc)
                    self.lbl_total.configure(text=f"{proc}/{max_u}")
                else:
                    # 永续或大上限时用已处理数作进度
                    self.prog_total.configure(maximum=max(100, proc+10), value=proc)
                    self.lbl_total.configure(text=f"{proc} 已采")
            except Exception: pass
        except tk.TclError:
            pass

    # ---- 用户滚动意图跟踪 ----
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
        """滚轮/触控板：向上 → 解除跟随；向下滚到底 → 恢复跟随。"""
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

    def _is_at_bottom(self) -> bool:
        """当前视口是否贴底（用户已向上翻看时返回 False，避免强制跳动）。"""
        try:
            if self.txt.compare("end-1c", "==", "1.0"):  # 空文本
                return True
            return self.txt.yview()[1] >= 0.999
        except tk.TclError:
            return True

    def _see_end(self) -> None:
        try:
            self.txt.see("end")
        except tk.TclError:
            pass

    def _refresh_db_size(self) -> None:
        def worker():
            try:
                from storage import dao
                mb = dao.get_db_size() / (1024 * 1024)
                self.after(0, lambda: self.lbl_stat.configure(text=f"当前库占用：{mb:.1f} MB"))
            except Exception:
                self.after(0, lambda: self.lbl_stat.configure(text="当前库占用：未知（DB不可达）"))
        threading.Thread(target=worker, daemon=True).start()

    def _seed_from_library(self) -> None:
        """从库内 UP 起采：跨全库按评论数 Top 随机抽（后台，不卡UI）。"""
        self.btn_lib_seed.configure(state="disabled", text="正在查询…")
        self.lbl_status.configure(text="正在从数据库精选活跃 UP 主作为种子...")

        def worker() -> None:
            try:
                from storage import dao
                # 库种子：跳过僵尸，Top随机抽 10 个
                uids = dao.query_top_up_uids(10)
            except Exception as exc:
                logger.warning("读取库内 UP 失败: %s", exc)
                msg = f"读取库内 UP 失败：{exc}"
                self.after(0, lambda m=msg: self._seed_done(None, m, src="lib"))
                return
            self.after(0, lambda u=uids: self._seed_done(u, None, src="lib"))

        threading.Thread(target=worker, daemon=True).start()

    def _seed_from_hot(self) -> None:
        """从热搜起采：取热门榜视频的 UP 去重后填种子（个数 1-100 自选）。"""
        try:
            want = max(1, min(100, int(self.var_hot_seed.get())))
        except ValueError:
            messagebox.showerror("错误", "热搜UP个数需为整数")
            return
        self.btn_hot_seed.configure(state="disabled", text="正在获取…")
        self.lbl_status.configure(text=f"正在请求 B站热搜榜单并解析前 {want} 个活跃 UP 主...")
        def worker(want_n: int = want) -> None:
            try:
                from crawler.api.client import ApiClient
                from crawler.api.popular import PopularFetcher
                client = ApiClient()
                try:
                    fetcher = PopularFetcher(client)
                    items = fetcher.collect(count=max(20, want_n * 2))
                finally:
                    client.close()
                uids = []
                seen = set()
                for v in items:
                    uid = int(v.get("owner_uid") or 0)
                    if uid and uid not in seen:
                        seen.add(uid); uids.append(uid)
                    if len(uids) >= want_n:
                        break
                if not uids:
                    raise RuntimeError("热搜未取到 UP")
                self.after(0, lambda u=uids: self._seed_done(u, None, src="hot"))
                return
            except Exception as exc:
                logger.warning("热搜种子失败: %s", exc)
                msg = f"热搜种子失败：{exc}"
                self.after(0, lambda m=msg: self._seed_done(None, m, src="hot"))
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _seed_done(self, uids: list[int] | None, err: str | None, src: str = "lib") -> None:
        src_name = "库内活跃UP" if src == "lib" else "热搜榜单UP"
        btn = self.btn_hot_seed if src == "hot" else self.btn_lib_seed
        orig_text = "从热搜UP起采" if src == "hot" else "从库内UP起采（10个）"

        if err or not uids:
            btn.configure(state="normal", text=orig_text)
            self.lbl_status.configure(text=f"载入失败：{err or '未获取到有效种子'}")
            messagebox.showwarning("提示", err or f"{src_name}暂无可用种子 UP")
            return

        self.ent_seeds.delete("1.0", "end")
        self.ent_seeds.insert("1.0", "\n".join(str(u) for u in uids))
        self._refresh_db_size()

        # 1. 按钮微交互反馈：变绿/提示成功，2.5秒后自动恢复
        btn.configure(state="normal", text=f"✓ 已载入 {len(uids)} 个")
        self.after(2500, lambda: btn.configure(text=orig_text))

        # 2. 状态栏显式提示
        preview = ", ".join(str(u) for u in uids[:4]) + ("..." if len(uids) > 4 else "")
        msg = f"✓ 成功载入 {len(uids)} 个{src_name}种子 ({preview})，已填入上方文本框，点击「开始广泛采集」即可启动"
        self.lbl_status.configure(text=msg)

        # 3. 实时流打印
        self._append(f"[种子载入] 成功从{src_name}载入 {len(uids)} 个种子 UID: {preview}")

    @staticmethod
    def _parse_seeds(text: str) -> list[int]:
        out = []
        for tok in re.split(r"[,\s\n；;]+", text):
            tok = tok.strip()
            if tok.isdigit():
                out.append(int(tok))
        return out

    # ---------------- 会话状态（重启恢复） ----------------
    def _app(self):
        """顶层 MainWindow（任务断点通知用；非 MainWindow 时返回 None）。"""
        top = self.winfo_toplevel()
        return top if hasattr(top, "_on_task_started") else None

    def snapshot_state(self) -> dict:
        try:
            seeds = self.ent_seeds.get("1.0", "end")
        except tk.TclError:
            seeds = ""
        state = {"seeds": seeds}
        for key, var in self._vars.items():
            try:
                state[key] = var.get()
            except tk.TclError:
                pass
        return state

    def restore_state(self, d: dict) -> None:
        seeds = d.get("seeds")
        if isinstance(seeds, str):
            try:
                self.ent_seeds.delete("1.0", "end")
                self.ent_seeds.insert("1.0", seeds)
            except tk.TclError:
                pass
        for key, var in self._vars.items():
            if key in d:
                try:
                    var.set(str(d[key]))
                except tk.TclError:
                    pass

    def start_task(self) -> None:
        """断点续采：按已恢复的表单直接启动广泛采集。"""
        self._toggle()

    # ---------------- 任务控制 ----------------
    def _toggle(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.stop()
            self.btn_start.configure(text="正在停止…")
            return
        seeds = self._parse_seeds(self.ent_seeds.get("1.0", "end"))
        if not seeds:
            messagebox.showwarning("提示", "请先输入至少一个种子 UID")
            return
        kwargs = {}
        for label, key, _default, _scale in FIELDS:
            try:
                kwargs[key] = int(self._vars[key].get())
            except ValueError:
                messagebox.showerror("错误", f"{label} 需为整数")
                return
        perpetual = bool(self.var_perpetual.get())
        self._start_mode("广泛采集", lambda: BroadCrawlWorker(
            self.msg_q, seeds=seeds,
            max_depth=kwargs["max_depth"], max_ups=kwargs["max_ups"],
            max_pages=kwargs["max_pages"], top_n=kwargs["top_n"],
            min_comments=kwargs["min_comments"],
            max_storage_mb=kwargs["max_storage_mb"],
            perpetual=perpetual,
        ))
        for u in seeds:
            self._append(f"  种子 UID={u}")
        app = self._app()
        if app is not None:
            app._on_task_started(
                f"广泛采集：种子{len(seeds)}个 深度{kwargs['max_depth']} "
                f"UP上限{kwargs['max_ups']} 页数{kwargs['max_pages']}")

    def _toggle_hot(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.stop()
            self.btn_hot.configure(text="正在停止…")
            return
        try:
            count = max(1, min(100, int(self.var_hot.get())))
        except ValueError:
            messagebox.showerror("错误", "热搜视频数需为整数")
            return
        self._start_mode("热搜视频", lambda: PopularCrawlWorker(
            self.msg_q, count=count))
        app = self._app()
        if app is not None:
            app._on_task_started(f"一键采集热搜视频评论：{count} 个")

    def _toggle_pause(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            return
        try:
            self.worker.pause()
            is_p = self.worker.is_paused()
            self.btn_pause.configure(text="继续" if is_p else "暂停")
            self.lbl_status.configure(text="已暂停" if is_p else "继续采集中…")
        except Exception:
            pass

    def _start_mode(self, prefix: str, factory) -> None:
        """启动任意采集 worker 的公共流程（清流、起进度条、开轮询）。"""

        self._clear()
        self.lbl_vpos.configure(text="")
        self._append(f"=== 开始{prefix} ===")
        self.worker = factory()
        self._t0 = time.time()
        self.last_status = {}
        self.worker.start()
        self.btn_start.configure(text="停止采集", style="Danger.TButton")
        self.btn_hot.configure(text="停止采集")
        self.btn_pause.configure(state="normal", text="暂停")
        try:
            self.prog.stop()
            self.prog.configure(mode="indeterminate", maximum=100)
            self.prog.start(15)
            self.prog_total.configure(value=0)
            self.lbl_total.configure(text="0/0")
        except tk.TclError:
            pass
        self.after(120, self._poll)

    def _poll(self) -> None:
        try:
            self._drain()
        except Exception as exc:
            logger.warning("broad _drain 异常: %s", exc)
        try:
            self._refresh_info()
        except Exception as exc:
            logger.warning("broad _refresh_info 异常: %s", exc)
        # information_schema 查询在写库高峰期会变慢（阻塞主线程），节流到 5s 一次
        try:
            now = time.time()
            if now - getattr(self, "_db_refresh_at", 0.0) > 5.0:
                self._db_refresh_at = now
                self._refresh_db_size()
        except Exception:
            pass
        try:
            if self.worker is not None:
                if self.worker.is_alive():
                    try:
                        if getattr(self.worker, "is_paused", lambda: False)():
                            self.lbl_status.configure(text="已暂停")
                    except Exception: pass
                    self.after(120, self._poll)
                    return
                self.worker = None
                self.btn_start.configure(text="开始广泛采集", style="Accent.TButton")
                self.btn_hot.configure(text="一键采集热搜视频评论")
                self.btn_pause.configure(state="disabled", text="暂停")
                try:
                    self.prog.stop()
                    self.prog.configure(mode="determinate", value=100)
                except tk.TclError:
                    pass
                try:
                    self._refresh_info()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("broad _poll 收尾异常: %s", exc)
            # 兜底：只要 worker 还活着就继续轮询，避免 UI 假死
            try:
                if self.worker is not None and self.worker.is_alive():
                    self.after(120, self._poll)
            except Exception:
                pass

    def _drain(self) -> None:
        stick = self._stick_bottom
        rendered = 0
        paused = bool(self._pause_stream.get())
        batch_lines: list[str] = []

        for _ in range(500):
            try:
                msg = self.msg_q.get_nowait()
            except queue.Empty:
                break
            mtype = msg.get("type")
            if mtype == MSG_ITEM:
                if not paused and rendered < _ITEM_RENDER_BUDGET:
                    rendered += 1
                    batch_lines.append(format_comment_line(msg.get("data") or {}))
                continue
            try:
                self._handle(msg, batch_lines=batch_lines, paused=paused)
            except Exception as exc:
                logger.warning("broad _handle 异常: %s", exc)

        if batch_lines and not paused:
            self._append_batch(batch_lines)
            if stick:
                self._see_end()  # 仅在解锁跟随时回底；已上翻则保持阅读位置

    def _fill_tree(self) -> None:
        def worker():
            try:
                from storage import dao
                rows = dao.query_up_stats()
                self.after(0, lambda: self._fill_tree_apply(rows))
            except Exception:
                pass
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _fill_tree_apply(self, rows: list) -> None:
        for r in rows:
            try:
                mid = r.get("mid", "")
                uid_cell = f"{mid}({r.get('uname') or ''})" if r.get("uname") else str(mid)
                self.tree.insert("", "end", values=(uid_cell, r.get("video_total",0), f"{r.get('video_fetched',0)}/{r.get('video_total',0)}", r.get("comment_total",0)))
            except Exception:
                pass

    def _handle(self, msg: dict, batch_lines: list[str] | None = None, paused: bool = False) -> None:
        mtype = msg.get("type")
        if mtype == MSG_PROGRESS:
            text = msg.get("text", "")
            self.lbl_status.configure(text=text)
            if not paused:
                if batch_lines is not None:
                    batch_lines.append(f"→ {text}")
                else:
                    self._append(f"→ {text}")
            m2 = re.search(r"\[(\d+)/(\d+)\]", text)
            if m2:
                cur, total = int(m2.group(1)), int(m2.group(2))
                try:
                    self.prog.stop()
                    self.prog.configure(mode="determinate",
                                        maximum=max(total, 1), value=cur)
                    self.lbl_vpos.configure(text=f"视频 {cur}/{total}")
                except tk.TclError:
                    pass
        elif mtype == MSG_ITEM:
            line = format_comment_line(msg.get("data") or {})
            if not paused:
                if batch_lines is not None:
                    batch_lines.append(line)
                else:
                    self._append(line)
        elif mtype == MSG_ERROR:
            err_txt = f"[错误] {msg.get('text', '')}"
            if not paused:
                if batch_lines is not None:
                    batch_lines.append(err_txt)
                else:
                    self._append(err_txt)
            self.lbl_status.configure(text=f"错误：{msg.get('text', '')}")
        elif mtype == MSG_DONE:
            stats = msg.get("stats") or {}
            if stats.get("kind") == "hot":
                hot = stats
                text = (f"热搜视频评论采集完成：视频 {hot.get('fetched', 0)}/{hot.get('videos', 0)} "
                        f"评论 {hot.get('comments', 0)} 条")
                end_line = "==== 热搜视频评论采集完成 ===="
            else:
                text = (f"广泛采集完成：处理 UP {stats.get('ups', 0)} 个 / "
                        f"评论 {stats.get('comments', 0)} 条 / "
                        f"反推候选 {stats.get('candidates', 0)} 个")
                end_line = "==== 广泛采集完成 ===="
            errs = stats.get("errors") or []
            if isinstance(errs, list) and errs:
                text += f"（{errs[-1]}）"
            elif isinstance(errs, int) and errs:
                text += f"（错误 {errs} 个）"
            self.lbl_status.configure(text=text)
            self._append(end_line)
            self._refresh_db_size()
            if stats.get("kind") != "hot":
                self._fill_tree()
            app = self._app()
            if app is not None:
                app._on_task_done(self)
