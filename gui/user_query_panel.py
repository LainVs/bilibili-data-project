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
        self._traj: dict | None = None
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

        self.btn_export = ttk.Button(bar, text="导出当前", command=self._export, state="disabled")
        self.btn_export.pack(side="left", padx=(8, 4))
        self.btn_export_all = ttk.Button(bar, text="导出全部", command=self._export_all, state="disabled")
        self.btn_export_all.pack(side="left", padx=4)
        self.btn_traj = ttk.Button(bar, text="属地轨迹", command=self._show_ip_trajectory, state="disabled")
        self.btn_traj.pack(side="left", padx=4)

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

        # 分析 IP 属地流转轨迹
        from analysis.ip_trajectory import analyze_user_ip_trajectory
        self._traj = analyze_user_ip_trajectory(comments)
        has_traj = bool(self._traj and self._traj.get("valid_records", 0) > 0)
        self.btn_traj.configure(state="normal" if has_traj else "disabled")

        if has_traj:
            t = self._traj
            extra = f" ｜ 属地: {' ➔ '.join(t['distinct_ips'][:3])}"
            if len(t["distinct_ips"]) > 3:
                extra += f" 等{len(t['distinct_ips'])}省"
            if t["teleport_count"] > 0:
                extra += f" ｜ ⚠ 异常瞬移 {t['teleport_count']} 次"
            elif t["jump_count"] > 0:
                extra += f" (流转 {t['jump_count']} 次)"
            self.lbl_user.configure(text=self.lbl_user.cget("text") + extra)

        has_data = bool(self._rows)
        self.btn_export.configure(state="normal" if has_data else "disabled")
        self.btn_export_all.configure(state="normal" if has_data else "disabled")

    def _export(self) -> None:
        if not self._rows:
            messagebox.showinfo("提示", "尚未查询，无数据可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出查询结果",
            defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 文件", "*.csv")],
            initialfile=f"user_{self.var_uid.get()}_export.xlsx",
        )
        if not path:
            return

        cols = [
            ("kind", "类型"),
            ("time_fmt", "时间"),
            ("content", "内容"),
            ("ip_location", "属地"),
            ("like_num", "点赞数"),
            ("bvid", "BV号"),
            ("title", "视频标题"),
            ("progress", "弹幕时间(秒)"),
        ]
        export_rows = []
        for row in self._rows.values():
            r = dict(row)
            r["time_fmt"] = fmt_dt(r.get("ctime"))
            export_rows.append(r)

        try:
            if path.endswith(".xlsx"):
                import etl
                etl.export_to_styled_xlsx({"用户发言": (cols, export_rows)}, path)
            else:
                import csv
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    writer.writerow([c[1] for c in cols])
                    for r in export_rows:
                        writer.writerow([r.get(c[0], "") for c in cols])
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
            defaultextension=".xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 文件", "*.csv")],
            initialfile=f"user_{uid}_all.xlsx",
        )
        if not path:
            return

        box = tk.Toplevel(self)
        box.title("导出全部发言")
        box.geometry("420x110")
        box.configure(bg=C.BG_WINDOW)
        lbl = ttk.Label(box, text="正在读取库内全部发言并生成文件...", style="Hint.TLabel")
        lbl.pack(padx=16, pady=(16, 8), anchor="w")
        prog = ttk.Progressbar(box, mode="indeterminate")
        prog.pack(fill="x", padx=16)
        prog.start(10)

        def _worker():
            try:
                comments = dao.query_comments_by_uid(uid, limit=0)
                danmaku = dao.query_danmaku_by_uid(uid, limit=0)
                cols = [
                    ("kind", "类型"),
                    ("time_fmt", "时间"),
                    ("content", "内容"),
                    ("ip_location", "属地"),
                    ("like_num", "点赞数"),
                    ("bvid", "BV号"),
                    ("title", "视频标题"),
                    ("progress", "弹幕时间(秒)"),
                ]
                all_rows = []
                for c in comments:
                    all_rows.append({
                        "kind": "评论",
                        "time_fmt": fmt_dt(c.get("ctime")),
                        "content": c.get("content", ""),
                        "ip_location": c.get("ip_location") or "",
                        "like_num": c.get("like_num", 0),
                        "bvid": c.get("bvid", ""),
                        "title": c.get("title", ""),
                        "progress": "",
                    })
                for d in danmaku:
                    all_rows.append({
                        "kind": "弹幕",
                        "time_fmt": fmt_dt(d.get("ctime")),
                        "content": d.get("content", ""),
                        "ip_location": "",
                        "like_num": "",
                        "bvid": d.get("bvid", ""),
                        "title": d.get("title", ""),
                        "progress": d.get("progress", ""),
                    })

                if path.endswith(".xlsx"):
                    import etl
                    etl.export_to_styled_xlsx({"全部发言": (cols, all_rows)}, path)
                else:
                    import csv
                    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                        writer = csv.writer(fh)
                        writer.writerow([c[1] for c in cols])
                        for r in all_rows:
                            writer.writerow([r.get(c[0], "") for c in cols])

                n = len(all_rows)
                self.after(0, lambda: [box.destroy(), messagebox.showinfo("导出完成", f"已导出该用户全部 {n:,} 条发言至:\n{path}")])
            except Exception as exc:
                # lambda 默认参数绑定，避免 except 结束后 exc 被解绑导致 NameError
                self.after(0, lambda e=str(exc): [box.destroy(), messagebox.showerror("导出失败", e)])

        threading.Thread(target=_worker, daemon=True).start()

    def _show_ip_trajectory(self) -> None:
        if not self._traj or not self._traj.get("valid_records"):
            messagebox.showinfo("提示", "该用户暂无包含有效 IP 属地的发言记录")
            return

        traj = self._traj
        uid = traj.get("uid") or self._last_uid
        win = tk.Toplevel(self)
        win.title(f"UID: {uid} 属地迁移轨迹与异常跳跃检测")
        win.geometry("820x540")
        win.configure(bg=C.BG_WINDOW)

        # 顶栏概要卡片
        summary_card = ClaudeCard(
            win,
            title=f"UID: {uid} IP 属地演化档案",
            subtitle=f"风险评级: [{traj['risk_level']}] · 涉及属地: {'、'.join(traj['distinct_ips']) or '无'}",
            padding=12,
        )
        summary_card.pack(fill="x", padx=12, pady=(12, 6))

        lbl_desc = ttk.Label(summary_card.body, text=traj.get("summary", ""), style="Secondary.TLabel", wraplength=780)
        lbl_desc.pack(anchor="w", pady=(0, 6))

        metrics_frame = tk.Frame(summary_card.body, bg=C.BG_CARD)
        metrics_frame.pack(fill="x")

        def _add_metric(parent, label, val, is_alert=False):
            box = tk.Frame(parent, bg=C.BG_CARD)
            box.pack(side="left", padx=(0, 20))
            color = C.ACCENT if is_alert else C.FG_PRIMARY
            ttk.Label(box, text=label, style="Hint.TLabel").pack(anchor="w")
            tk.Label(box, text=val, font=F.TITLE, bg=C.BG_CARD, fg=color).pack(anchor="w")

        _add_metric(metrics_frame, "有效属地记录", f"{traj['valid_records']} 条")
        _add_metric(metrics_frame, "跨域流转", f"{traj['jump_count']} 次")
        _add_metric(metrics_frame, "异常瞬移", f"{traj['teleport_count']} 次", is_alert=(traj['teleport_count'] > 0))
        _add_metric(metrics_frame, "最高隐含时速", f"{traj['max_speed_kmh']:,.0f} km/h", is_alert=(traj['max_speed_kmh'] > 1000))

        # 中部表格卡片
        table_card = ClaudeCard(
            win,
            title="属地跃迁时序明细",
            subtitle="记录每次属地变更的起止时间、空间距离与隐含时速（超速瞬移多为代理池或账号租借）",
            padding=10,
        )
        table_card.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        table_card.body.columnconfigure(0, weight=1)
        table_card.body.rowconfigure(0, weight=1)

        cols = [
            ("idx", "序号", 6),
            ("from_ip", "迁出地", 10),
            ("to_ip", "迁往地", 10),
            ("from_time", "离开时间", 18),
            ("to_time", "到达时间", 18),
            ("interval", "间隔耗时", 12),
            ("dist", "大地距离", 12),
            ("speed", "隐含时速", 14),
            ("flag", "判定结论", 18),
        ]
        table = SmartTable(table_card.body, show="headings", height=10)
        table.set_columns(cols)
        table.tag_configure("teleport", foreground=C.ACCENT)
        table.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(table_card.body, command=table.yview)
        table.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")

        for i, tr in enumerate(traj.get("transitions") or [], start=1):
            tag = ("teleport",) if tr.get("is_teleport") else ()
            table.insert("", "end", values=(
                i,
                tr["from_ip"],
                tr["to_ip"],
                fmt_dt(tr["from_time"]),
                fmt_dt(tr["to_time"]),
                f"{tr['interval_hours']:.1f} 小时",
                f"{tr['distance_km']:.0f} km",
                f"{tr['speed_kmh']:,.0f} km/h",
                tr["flag"],
            ), tags=tag)

        # 底栏按钮
        btn_bar = tk.Frame(win, bg=C.BG_WINDOW)
        btn_bar.pack(fill="x", padx=12, pady=(0, 12))

        def _export_traj():
            transitions = traj.get("transitions") or []
            if not transitions:
                messagebox.showinfo("提示", "该用户无跨属地跳转记录，无需导出")
                return
            path = filedialog.asksaveasfilename(
                title="导出属地迁移轨迹",
                defaultextension=".xlsx",
                filetypes=[("Excel 工作簿", "*.xlsx"), ("CSV 文件", "*.csv")],
                initialfile=f"uid_{uid}_ip_trajectory.xlsx",
            )
            if not path:
                return
            t_cols = [
                ("from_ip", "迁出地"), ("to_ip", "迁往地"),
                ("from_time", "离开时间"), ("to_time", "到达时间"),
                ("interval_hours", "间隔耗时(小时)"), ("distance_km", "大地距离(km)"),
                ("speed_kmh", "隐含时速(km/h)"), ("flag", "判定结论"),
            ]
            try:
                if path.endswith(".xlsx"):
                    import etl
                    etl.export_to_styled_xlsx({"属地迁移轨迹": (t_cols, transitions)}, path)
                else:
                    import csv
                    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                        w = csv.writer(fh)
                        w.writerow([h for _, h in t_cols])
                        for tr in transitions:
                            w.writerow([tr.get(k, "") for k, _ in t_cols])
                messagebox.showinfo("导出成功", f"已成功导出属地迁移轨迹至:\n{path}")
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc))

        ttk.Button(btn_bar, text="导出轨迹表", command=_export_traj).pack(side="left")
        ttk.Button(btn_bar, text="关闭", command=win.destroy).pack(side="right")

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
