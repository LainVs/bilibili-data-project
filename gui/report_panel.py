"""研报生成面板：选择性生成排版精美的单文件离线 HTML 分析研报。"""
from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from gui.theme import C, F, S
from gui.widgets import ClaudeBadge, ClaudeCard
from reporting import ReportSections, generate_report

logger = logging.getLogger(__name__)


class ReportPanel(ttk.Frame):
    """一键生成纯静态、零依赖单文件 HTML 网页研报。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._last_report_path: str = ""
        self._is_generating = False
        self._build_ui()

    def snapshot_state(self) -> dict:
        return {
            "bvid": self.var_bvid.get(),
            "limit": self.var_limit.get(),
            "auto_sync": self.var_auto_sync.get(),
            "sec_overview": self.var_sec_overview.get(),
            "sec_trends": self.var_sec_trends.get(),
            "sec_sentiment": self.var_sec_sentiment.get(),
            "sec_terms": self.var_sec_terms.get(),
            "sec_water_army": self.var_sec_water_army.get(),
            "sec_top_comments": self.var_sec_top_comments.get(),
        }

    def restore_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        if "bvid" in state:
            self.var_bvid.set(str(state["bvid"]))
        if "limit" in state:
            self.var_limit.set(str(state["limit"]))
        if "auto_sync" in state:
            self.var_auto_sync.set(bool(state["auto_sync"]))
        for var_name in (
            "sec_overview", "sec_trends", "sec_sentiment",
            "sec_terms", "sec_water_army", "sec_top_comments"
        ):
            if var_name in state and hasattr(self, f"var_{var_name}"):
                getattr(self, f"var_{var_name}").set(bool(state[var_name]))

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)

        # ── 顶栏标题 ──
        header = tk.Frame(self, bg=C.BG_WINDOW)
        header.grid(row=0, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(S.PAD_PAGE, 10))
        ttk.Label(header, text="可视化网页研报引擎", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="生成零外部依赖、自包含 SVG 矢量的单文件 HTML 独立分析研报，手机与电脑浏览器双击即开，支持按需选择板块",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # ── 1. 目标与基础设置卡片 ──
        target_card = ClaudeCard(
            self,
            title="研报对象与数据范围",
            subtitle="输入要生成研报的视频 BV 号，并配置样本提取上限与导出路径",
            padding=14,
        )
        target_card.grid(row=1, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(0, 12))

        t_body = target_card.body

        r1 = tk.Frame(t_body, bg=C.BG_CARD)
        r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="视频 BV 号:", style="Secondary.TLabel").pack(side="left")
        self.var_bvid = tk.StringVar(value="")
        ent_bvid = ttk.Entry(r1, textvariable=self.var_bvid, width=20)
        ent_bvid.pack(side="left", padx=(6, 12))

        ttk.Label(r1, text="采样上限:", style="Secondary.TLabel").pack(side="left")
        self.var_limit = tk.StringVar(value="5000")
        ttk.Spinbox(r1, from_=500, to=50000, textvariable=self.var_limit, width=7).pack(side="left", padx=(4, 12))

        ttk.Label(r1, text="输出文件名:", style="Secondary.TLabel").pack(side="left")
        self.var_out = tk.StringVar(value="bilibili_report.html")
        ttk.Entry(r1, textvariable=self.var_out, width=24).pack(side="left", padx=(4, 6))
        ttk.Button(r1, text="浏览...", command=self._browse_output).pack(side="left")

        r2 = tk.Frame(t_body, bg=C.BG_CARD)
        r2.pack(fill="x", pady=(6, 2))
        self.var_auto_sync = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            r2,
            text="生成前自动联网增量补采最新数据 (若网络连通，自动拉取最新发布评论/弹幕与官方播放点赞指标)",
            variable=self.var_auto_sync,
        ).pack(side="left")

        # ── 2. 选择性实现版块勾选卡片 ──
        sec_card = ClaudeCard(
            self,
            title="选择性研报内容板块 (支持按需组合)",
            subtitle="自由勾选本次研报需要包含的深度分析维度，不勾选的版块将完全不占用篇幅与算力",
            padding=14,
        )
        sec_card.grid(row=2, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(0, 12))

        s_body = sec_card.body

        sec_grid = tk.Frame(s_body, bg=C.BG_CARD)
        sec_grid.pack(fill="x", pady=(4, 8))
        sec_grid.columnconfigure(0, weight=1)
        sec_grid.columnconfigure(1, weight=1)

        self.var_sec_overview = tk.BooleanVar(value=True)
        self.var_sec_trends = tk.BooleanVar(value=True)
        self.var_sec_sentiment = tk.BooleanVar(value=True)
        self.var_sec_terms = tk.BooleanVar(value=True)
        self.var_sec_water_army = tk.BooleanVar(value=True)
        self.var_sec_top_comments = tk.BooleanVar(value=True)

        ttk.Checkbutton(sec_grid, text="01 / 核心资产与指标总览 (评论/弹幕/独立用户/总赞)", variable=self.var_sec_overview).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Checkbutton(sec_grid, text="02 / 互动传播与时间节律 (每日趋势折线 + 24小时分布柱状)", variable=self.var_sec_trends).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Checkbutton(sec_grid, text="03 / 舆论倾向与情感基调 (正面/中立/负面极性环形图)", variable=self.var_sec_sentiment).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Checkbutton(sec_grid, text="04 / 讨论热词与观众地域 (Top24 词频标签墙 + IP 属地矩阵)", variable=self.var_sec_terms).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Checkbutton(sec_grid, text="05 / 水军刷屏与控评名单 (复读/爆发/同属地聚集证据链)", variable=self.var_sec_water_army).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Checkbutton(sec_grid, text="06 / 精选高赞热评矩阵 (Top10 标志性代表发言)", variable=self.var_sec_top_comments).grid(row=2, column=1, sticky="w", pady=4)

        btn_bar = tk.Frame(s_body, bg=C.BG_CARD)
        btn_bar.pack(fill="x", pady=(6, 0))
        ttk.Button(btn_bar, text="全部勾选", command=self._select_all_sections).pack(side="left")
        ttk.Button(btn_bar, text="全部取消", command=self._deselect_all_sections).pack(side="left", padx=6)

        # ── 3. 动作操作与生成状态卡片 ──
        act_card = ClaudeCard(
            self,
            title="生成与交付控制",
            subtitle="生成完毕后可一键在默认浏览器打开查看，或直接发送 HTML 单文件给他人",
            padding=14,
        )
        act_card.grid(row=3, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(0, S.PAD_PAGE))

        a_body = act_card.body

        ctrl_row = tk.Frame(a_body, bg=C.BG_CARD)
        ctrl_row.pack(fill="x")

        self.btn_gen = ttk.Button(ctrl_row, text="一键生成独立研报", style="Accent.TButton", command=self._start_generate)
        self.btn_gen.pack(side="left")

        self.btn_open = ttk.Button(ctrl_row, text="在浏览器中查看", command=self._open_in_browser, state="disabled")
        self.btn_open.pack(side="left", padx=8)

        self.btn_folder = ttk.Button(ctrl_row, text="打开所在文件夹", command=self._open_folder, state="disabled")
        self.btn_folder.pack(side="left")

        self.badge_status = ClaudeBadge(act_card.header_right, text="待命", status="muted")
        self.badge_status.pack(side="right")

        self.prog = ttk.Progressbar(a_body, mode="indeterminate")
        self.prog.pack(fill="x", pady=(14, 8))

        self.lbl_info = ttk.Label(
            a_body,
            text="提示：输入 BV 号并选好板块后，点击「一键生成独立研报」即可开始装配。",
            style="Secondary.TLabel",
        )
        self.lbl_info.pack(anchor="w")

    def _browse_output(self) -> None:
        bvid = self.var_bvid.get().strip() or "video"
        default_name = f"report_{bvid}.html"
        path = filedialog.asksaveasfilename(
            title="保存研报 HTML 文件",
            defaultextension=".html",
            filetypes=[("HTML 网页文件", "*.html")],
            initialfile=default_name,
        )
        if path:
            self.var_out.set(path)

    def _select_all_sections(self) -> None:
        for var in (
            self.var_sec_overview, self.var_sec_trends, self.var_sec_sentiment,
            self.var_sec_terms, self.var_sec_water_army, self.var_sec_top_comments
        ):
            var.set(True)

    def _deselect_all_sections(self) -> None:
        for var in (
            self.var_sec_overview, self.var_sec_trends, self.var_sec_sentiment,
            self.var_sec_terms, self.var_sec_water_army, self.var_sec_top_comments
        ):
            var.set(False)

    def _start_generate(self) -> None:
        if self._is_generating:
            return
        bvid = self.var_bvid.get().strip()
        if not bvid:
            messagebox.showwarning("提示", "请输入要生成研报的视频 BV 号（如 BV1tJ4m1T7QL）")
            return

        try:
            limit = max(100, int(self.var_limit.get() or 5000))
        except ValueError:
            limit = 5000

        out_name = self.var_out.get().strip() or f"report_{bvid}.html"
        if not out_name.endswith(".html"):
            out_name += ".html"
        if not os.path.isabs(out_name):
            out_name = os.path.abspath(os.path.join("logs", out_name))

        sections = ReportSections(
            overview=self.var_sec_overview.get(),
            trends=self.var_sec_trends.get(),
            sentiment=self.var_sec_sentiment.get(),
            terms=self.var_sec_terms.get(),
            water_army=self.var_sec_water_army.get(),
            top_comments=self.var_sec_top_comments.get(),
        )

        auto_sync = self.var_auto_sync.get()
        sync_hint = "（已开启生成前自动增量补采最新数据与官方指标）" if auto_sync else "（纯离线本地数据模式）"

        self._is_generating = True
        self.btn_gen.configure(state="disabled", text="正在装配生成...")
        self.btn_open.configure(state="disabled")
        self.btn_folder.configure(state="disabled")
        self.badge_status.set_status("生成中", status="info")
        self.lbl_info.configure(text=f"正在处理 {bvid} {sync_hint}...")
        self.prog.start(10)

        def worker():
            try:
                out_path = generate_report(bvid, out_name, limit=limit, sections=sections, auto_sync=auto_sync)
                self.after(0, lambda: self._on_generate_success(out_path))
            except Exception as exc:
                logger.exception("生成研报异常: %s", exc)
                self.after(0, lambda e=str(exc): self._on_generate_failed(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_generate_success(self, path: str) -> None:
        self._is_generating = False
        self._last_report_path = path
        self.prog.stop()
        self.btn_gen.configure(state="normal", text="一键生成独立研报")
        self.btn_open.configure(state="normal")
        self.btn_folder.configure(state="normal")
        self.badge_status.set_status("已生成", status="success")

        size_kb = os.path.getsize(path) / 1024
        self.lbl_info.configure(
            text=f"✓ 研报生成成功！单文件大小: {size_kb:.1f} KB | 路径: {path}\n点击「在浏览器中查看」即可直接查阅或另存为 PDF。"
        )

    def _on_generate_failed(self, error: str) -> None:
        self._is_generating = False
        self.prog.stop()
        self.btn_gen.configure(state="normal", text="一键生成独立研报")
        self.badge_status.set_status("生成失败", status="error")
        self.lbl_info.configure(text=f"生成失败：{error}")
        messagebox.showerror("生成失败", f"生成研报遇到错误：\n{error}")

    def _open_in_browser(self) -> None:
        if not self._last_report_path or not os.path.exists(self._last_report_path):
            messagebox.showinfo("提示", "研报文件不存在，请先生成。")
            return
        try:
            webbrowser.open(f"file:///{os.path.abspath(self._last_report_path)}")
        except Exception:
            try:
                os.startfile(self._last_report_path)  # type: ignore[attr-defined]
            except Exception as exc:
                messagebox.showerror("打开失败", f"无法打开浏览器：{exc}")

    def _open_folder(self) -> None:
        if not self._last_report_path:
            return
        folder = os.path.dirname(os.path.abspath(self._last_report_path))
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("路径", folder)
