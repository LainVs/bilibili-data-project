"""用户查询面板：Claude 质感用户画像与发言反查。"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui.theme import C, F, S
from gui.util import fmt_dt
from gui.widgets import ClaudeBadge, ClaudeCard, SmartTable
from storage import dao

logger = logging.getLogger(__name__)


class UserQueryPanel(ttk.Frame):
    """按 UID 检索库内该用户的所有历史发言与维度信息。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._rows: dict[str, dict] = {}
        self._qgen = 0
        self._last_uid = 0
        self._build_ui()

    def snapshot_state(self) -> dict:
        return {
            "uid": self.var_uid.get(),
            "limit": self.var_limit.get(),
        }

    def restore_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        for name, val in (("var_uid", state.get("uid")), ("var_limit", state.get("limit"))):
            if isinstance(val, str):
                try:
                    getattr(self, name).set(val)
                except (AttributeError, tk.TclError):
                    pass

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # ── 顶栏检索卡片 ──
        top_card = ClaudeCard(
            self,
            title="用户画像与发言反查",
            subtitle="输入 B站真实数字 UID 检索该用户历史发言（弹幕与一/二级评论）",
            padding=14,
        )
        top_card.grid(row=0, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(S.PAD_PAGE, 10))

        bar = tk.Frame(top_card.body, bg=C.BG_CARD)
        bar.pack(fill="x")

        ttk.Label(bar, text="用户 UID:", style="Secondary.TLabel").pack(side="left")
        self.var_uid = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var_uid, width=22)
        entry.pack(side="left", padx=(6, 10))
        entry.bind("<Return>", lambda _e: self._query())

        self.btn_query = ttk.Button(bar, text="检索发言", style="Accent.TButton", command=self._query)
        self.btn_query.pack(side="left")

        self.btn_export = ttk.Button(bar, text="导出当前CSV", command=self._export, state="disabled")
        self.btn_export.pack(side="left", padx=(8, 4))
        self.btn_export_all = ttk.Button(bar, text="导出全部CSV", command=self._export_all, state="disabled")
        self.btn_export_all.pack(side="left", padx=4)

        self.var_limit = tk.StringVar(value="2000")
        ttk.Label(bar, text="条数上限:", style="Secondary.TLabel").pack(side="left", padx=(16, 4))
        ttk.Spinbox(bar, from_=0, to=100000, textvariable=self.var_limit, width=7).pack(side="left")

        # ── 中部用户信息卡片 ──
        self.info_card = ClaudeCard(
            self,
            title="用户身份档案",
            subtitle="输入 UID 点击检索后将在此展示库内用户画像与发言统计",
            padding=12,
        )
        self.info_card.grid(row=1, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(0, 10))

        self.lbl_user = ttk.Label(self.info_card.body, text="尚未检索", style="Secondary.TLabel")
        self.lbl_user.pack(anchor="w")

        # ── 底部发言结果卡片 ──
        results_card = ClaudeCard(
            self,
            title="历史发言列表",
            subtitle="双击任意数据行可弹出完整长文阅读窗口",
            padding=12,
        )
        results_card.grid(row=2, column=0, sticky="nsew", padx=S.PAD_PAGE, pady=(0, S.PAD_PAGE))
        results_card.body.columnconfigure(0, weight=1)
        results_card.body.rowconfigure(0, weight=1)

        # 结果头部的统计徽章
        self.badge_count = ClaudeBadge(results_card.header_right, text="0 条记录", status="muted")
        self.badge_count.pack(side="right")
        self.lbl_count = ttk.Label(self, text="共 0 条")  # 兼容引用

        cols = [
            ("kind", "类型", 8),
            ("time", "发送时间", 14),
            ("content", "发言正文", 110),
            ("loc", "IP 属地", 10),
            ("meta", "关联视频与互动", 30),
        ]
        self.tree = SmartTable(results_card.body, show="headings")
        self.tree.set_columns(cols)
        self.tree.grid(row=0, column=0, sticky="nsew", pady=(4, 0))

        sb = ttk.Scrollbar(results_card.body, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns", pady=(4, 0))

        self.tree.bind("<Double-1>", self._on_double_click)

    def _query(self) -> None:
        try:
            uid = int(self.var_uid.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("格式错误", "请输入有效的数字 UID")
            return
        try:
            limit = int(self.var_limit.get())
        except ValueError:
            limit = 0
        if limit < 0:
            limit = 0

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows.clear()
        self.lbl_user.configure(text="正在检索库内发言...")
        self.badge_count.set_status("检索中...", status="info")
        self.btn_export.configure(state="disabled")
        self.btn_export_all.configure(state="disabled")

        self._qgen += 1
        gen = self._qgen

        def _worker() -> None:
            user = dao.query_user(uid)
            comments = dao.query_comments_by_uid(uid, limit=limit)
            danmaku = dao.query_danmaku_by_uid(uid, limit=limit)
            self.after(0, lambda: self._apply_user(gen, uid, user, comments, danmaku))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_user(
        self, gen: int, uid: int, user: dict | None, comments: list, danmaku: list
    ) -> None:
        if gen != self._qgen:
            return
        try:
            from gui.util import ping_main as _ping

            _ping(self, "用户查询")
        except Exception:
            pass
        if user:
            uname = user.get("uname") or "(未命名)"
            level = user.get("level", 0)
            vip = "大会员" if user.get("is_vip") else "普通用户"
            sign = (user.get("signature") or "")[:40]
            self.info_card.update_header(
                title=f"{uname} (UID: {user['uid']})",
                subtitle=f"Lv.{level} · {vip} · 性别: {user.get('sex') or '-'} · 签名: {sign}",
            )
            self.lbl_user.configure(text=f"共入库相关记录: 评论 {len(comments)} 条，弹幕 {len(danmaku)} 条")
        else:
            self.info_card.update_header(
                title=f"用户 UID: {uid}",
                subtitle="未入库该用户的维度信息（仅展示发言历史）",
            )
            self.lbl_user.configure(text=f"共入库相关记录: 评论 {len(comments)} 条，弹幕 {len(danmaku)} 条")

        for c in comments:
            title = (c.get("title") or "")[:40]
            iid = self.tree.insert(
                "",
                "end",
                values=(
                    "评论",
                    fmt_dt(c.get("ctime")),
                    str(c.get("content", ""))[:200],
                    c.get("ip_location") or "-",
                    f"{c.get('bvid', '')} 赞 {c.get('like_num', 0)} {title}",
                ),
            )
            self._rows[iid] = {"kind": "评论", **c}

        for d in danmaku:
            title = (d.get("title") or "")[:40]
            prog_s = f"{float(d.get('progress', 0) or 0):.1f}s"
            iid = self.tree.insert(
                "",
                "end",
                values=(
                    "弹幕",
                    fmt_dt(d.get("ctime")),
                    str(d.get("content", ""))[:200],
                    "-",
                    f"{d.get('bvid', '')} {prog_s} {title}",
                ),
            )
            self._rows[iid] = {"kind": "弹幕", **d}

        total = len(comments) + len(danmaku)
        self.badge_count.set_status(
            f"共 {total:,} 条 (评 {len(comments)} / 弹 {len(danmaku)})",
            status="success" if total > 0 else "muted",
        )
        self.lbl_count.configure(text=f"共 {total} 条")

        has_data = bool(self._rows)
        self.btn_export.configure(state="normal" if has_data else "disabled")
        self.btn_export_all.configure(state="normal" if has_data else "disabled")

    def _export(self) -> None:
        if not self._rows:
            messagebox.showinfo("提示", "尚未查询，无数据可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出查询结果",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"user_{self.var_uid.get()}_export.csv",
        )
        if not path:
            return
        import csv

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["类型", "时间", "内容", "属地", "点赞数", "BV号", "视频标题", "弹幕时间(秒)"])
                for row in self._rows.values():
                    kind = row.get("kind", "")
                    writer.writerow([
                        kind,
                        fmt_dt(row.get("ctime")),
                        row.get("content", ""),
                        row.get("ip_location") or "",
                        row.get("like_num", 0) if kind == "评论" else "",
                        row.get("bvid", ""),
                        row.get("title", ""),
                        row.get("progress", "") if kind == "弹幕" else "",
                    ])
            messagebox.showinfo("导出完成", f"已成功导出 {len(self._rows)} 条发言至:\n{path}")
        except Exception as exc:
            messagebox.showerror("导出失败", f"写入失败: {exc}")

    def _export_all(self) -> None:
        try:
            uid = int(self.var_uid.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("错误", "请输入有效的数字 UID")
            return
        path = filedialog.asksaveasfilename(
            title="导出该用户全部发言",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"user_{uid}_all.csv",
        )
        if not path:
            return
        import csv

        box = tk.Toplevel(self)
        box.title("导出全部发言")
        box.geometry("420x110")
        box.configure(bg=C.BG_WINDOW)
        lbl = ttk.Label(box, text="正在读取库内全部发言并生成 CSV...", style="Hint.TLabel")
        lbl.pack(padx=16, pady=(16, 8), anchor="w")
        prog = ttk.Progressbar(box, mode="indeterminate")
        prog.pack(fill="x", padx=16)
        prog.start(10)

        def _worker():
            try:
                comments = dao.query_comments_by_uid(uid, limit=0)
                danmaku = dao.query_danmaku_by_uid(uid, limit=0)
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["类型", "时间", "内容", "属地", "点赞数", "BV号", "视频标题", "弹幕时间(秒)"])
                    for c in comments:
                        writer.writerow([
                            "评论",
                            fmt_dt(c.get("ctime")),
                            c.get("content", ""),
                            c.get("ip_location") or "",
                            c.get("like_num", 0),
                            c.get("bvid", ""),
                            c.get("title", ""),
                            "",
                        ])
                    for d in danmaku:
                        writer.writerow([
                            "弹幕",
                            fmt_dt(d.get("ctime")),
                            d.get("content", ""),
                            "",
                            "",
                            d.get("bvid", ""),
                            d.get("title", ""),
                            d.get("progress", ""),
                        ])
                n = len(comments) + len(danmaku)
                self.after(0, lambda: [box.destroy(), messagebox.showinfo("导出完成", f"已导出该用户全部 {n:,} 条发言至:\n{path}")])
            except Exception as exc:
                self.after(0, lambda: [box.destroy(), messagebox.showerror("导出失败", str(exc))])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_double_click(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        row = self._rows.get(sel[0])
        if not row:
            return
        win = tk.Toplevel(self)
        win.title(f"{row.get('kind', '发言')}详情")
        win.geometry("620x380")
        win.configure(bg=C.BG_WINDOW)

        card = ClaudeCard(
            win,
            title=f"{row.get('kind', '发言')} 详情",
            subtitle=f"时间: {fmt_dt(row.get('ctime'))} · 视频: {row.get('bvid', '')} {row.get('title', '')}",
            padding=12,
        )
        card.pack(fill="both", expand=True, padx=12, pady=12)

        txt = tk.Text(card.body, wrap="word", font=F.BODY)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", row.get("content", ""))
        txt.configure(state="disabled")
