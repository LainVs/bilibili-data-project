"""用户查询面板：按 UID 检索库内该用户的所有评论与弹幕。"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui.theme import C, F, S
from gui.util import fmt_dt
from gui.widgets import SmartTable
from storage import dao


class UserQueryPanel(ttk.Frame):
    """按 UID 查询：该用户的历史评论与弹幕。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._rows: dict[str, dict] = {}  # iid -> 完整原始行
        self._qgen = 0  # 查询代际：旧查询的慢结果直接丢弃
        self._last_uid = 0
        self._build_ui()

    # ---------------- 会话状态（重启恢复） ----------------
    def snapshot_state(self) -> dict:
        return {"var_uid": self.var_uid.get(), "var_limit": self.var_limit.get()}

    def restore_state(self, d: dict) -> None:
        for name in ("var_uid", "var_limit"):
            val = d.get(name)
            if isinstance(val, str):
                try:
                    getattr(self, name).set(val)
                except (AttributeError, tk.TclError):
                    pass

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=(6, 6))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="用户 UID:").pack(side="left")
        self.var_uid = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.var_uid, width=24)
        entry.pack(side="left", padx=4)
        entry.bind("<Return>", lambda _e: self._query())
        ttk.Button(top, text="查询", command=self._query).pack(side="left", padx=(8, 0))
        self.btn_export = ttk.Button(top, text="导出CSV", command=self._export, state="disabled")
        self.btn_export.pack(side="left", padx=8)
        self.btn_export_all = ttk.Button(top, text="导出全部", command=self._export_all, state="disabled")
        self.btn_export_all.pack(side="left", padx=2)
        self.var_limit = tk.StringVar(value="2000")
        ttk.Label(top, text="条数(0=无限，默认2000):").pack(side="left", padx=(14, 0))
        ttk.Spinbox(top, from_=0, to=100000, textvariable=self.var_limit, width=7).pack(side="left")

        # 用户信息
        info = ttk.LabelFrame(self, text="用户信息", padding=(4, 2))
        info.grid(row=1, column=0, sticky="ew", padx=6)
        self.lbl_user = ttk.Label(info, text="未查询")
        self.lbl_user.pack(side="left")

        # 结果
        results = ttk.LabelFrame(self, text="查询结果", padding=(4, 2))
        results.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        cols = ("kind", "time", "content", "loc", "meta")
        self.tree = SmartTable(results, show="headings")
        self.tree.set_columns(
            [("kind", "类型", 6), ("time", "时间", 13),
             ("content", "内容", 120), ("loc", "属地", 11), ("meta", "来源/播放进度", 26)]
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(results, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._on_double_click)
        hint = ttk.Label(results, text="双击任意一行可查看完整内容", style="Hint.TLabel")
        hint.grid(row=1, column=0, sticky="w", padx=2)

        self.lbl_count = ttk.Label(self, text="共 0 条")
        self.lbl_count.grid(row=3, column=0, sticky="w", padx=8)

    def _query(self) -> None:
        try:
            uid = int(self.var_uid.get().strip())
        except (ValueError, TypeError):
            messagebox.showerror("错误", "请输入有效的 UID（纯数字）")
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
        self.lbl_user.configure(text="查询中...")
        self._qgen += 1
        my_gen = self._qgen
        self._last_uid = uid

        def worker():
            try:
                user = dao.query_user(uid)
                comments = dao.query_comments_by_uid(uid, limit)
                danmaku = dao.query_danmaku_by_uid(uid, limit)
                self.after(0, lambda: self._apply_user(my_gen, uid, user, comments, danmaku))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("查询失败", str(exc)))
        import threading
        threading.Thread(target=worker, daemon=True).start()
        return
        # 旧同步路径已移至后台
        user = dao.query_user(uid)
        if user:
            self.lbl_user.configure(text=f"UID={user['uid']} 昵称={user['uname'] or '(无)'}")
        else:
            self.lbl_user.configure(text=f"UID={uid}（库中无用户维度信息，仅展示其发言）")
        comments = dao.query_comments_by_uid(uid, limit)
        danmaku = dao.query_danmaku_by_uid(uid, limit)

        for c in comments:
            title = (c.get("title") or "")[:40]
            iid = self.tree.insert("", "end", values=(
                "评论",
                fmt_dt(c.get("ctime")),
                str(c.get("content", ""))[:200],
                c.get("ip_location") or "",
                f"{c.get('bvid', '')} 赞{c.get('like_num', 0)} {title}",
            ))
            self._rows[iid] = {"kind": "评论", **c}
        for d in danmaku:
            title = (d.get("title") or "")[:40]
            iid = self.tree.insert("", "end", values=(
                "弹幕",
                fmt_dt(d.get("ctime")),
                str(d.get("content", ""))[:200],
                "",
                f"{d.get('bvid', '')} {float(d.get('progress', 0) or 0):.1f}s {title}",
            ))
            self._rows[iid] = {"kind": "弹幕", **d}
        self.lbl_count.configure(text=f"共 {len(comments) + len(danmaku)} 条（评论 {len(comments)} / 弹幕 {len(danmaku)}）")
        has_data = bool(self._rows)
        self.btn_export.configure(state="normal" if has_data else "disabled")
        self.btn_export_all.configure(state="normal" if has_data else "disabled")

    def _apply_user(self, gen: int, uid: int, user: dict | None, comments: list, danmaku: list) -> None:
        if gen != self._qgen:
            return  # 旧查询结果丢弃
        try:
            from gui.util import ping_main as _ping
            _ping(self, "用户查询")
        except Exception:
            pass
        if user:
            self.lbl_user.configure(text=f"UID={user['uid']} 昵称={user['uname'] or '(无)'} 等级={user['level']} 性别={user['sex'] or '-'} VIP={user['is_vip']} 签名={user['signature'][:40]}")
        else:
            self.lbl_user.configure(text=f"UID={uid}（库中无用户维度信息，仅展示其发言）")
        for c in comments:
            title = (c.get("title") or "")[:40]
            iid = self.tree.insert("", "end", values=("评论", fmt_dt(c.get("ctime")), str(c.get("content",""))[:200], c.get("ip_location") or "", f"{c.get('bvid','')} 赞{c.get('like_num',0)} {title}"))
            self._rows[iid] = {"kind": "评论", **c}
        for d in danmaku:
            title = (d.get("title") or "")[:40]
            iid = self.tree.insert("", "end", values=("弹幕", fmt_dt(d.get("ctime")), str(d.get("content",""))[:200], "", f"{d.get('bvid','')} {float(d.get('progress',0) or 0):.1f}s {title}"))
            self._rows[iid] = {"kind": "弹幕", **d}
        self.lbl_count.configure(text=f"共 {len(comments) + len(danmaku)} 条（评论 {len(comments)} / 弹幕 {len(danmaku)}）")
        has_data = bool(self._rows)
        self.btn_export.configure(state="normal" if has_data else "disabled")
        self.btn_export_all.configure(state="normal" if has_data else "disabled")

    def _export(self) -> None:
        """导出当前查询结果（全文，不受列表截断）为 CSV。"""
        if not self._rows:
            messagebox.showinfo("提示", "尚未查询，无数据可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出查询结果",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        import csv

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["类型", "时间", "视频BV", "所属UP", "视频标题", "内容",
                            "属地", "点赞", "播放进度(秒)", "完整内容"])
                for row in self._rows.values():
                    kind = row.get("kind")
                    is_cmt = kind == "评论"
                    w.writerow([
                        "评论" if is_cmt else "弹幕",
                        fmt_dt(row.get("ctime")),
                        row.get("bvid", ""),
                        row.get("owner_name", "") or "",
                        row.get("title", "") or "",
                        str(row.get("content", ""))[:200],
                        row.get("ip_location", "") or "" if is_cmt else "",
                        row.get("like_num", ""),
                        float(row.get("progress", 0) or 0) if not is_cmt else "",
                        row.get("content", ""),
                    ])
            messagebox.showinfo("导出完成", f"已导出 {len(self._rows)} 条记录\n{path}")
        except OSError as exc:
            messagebox.showerror("导出失败", f"写入文件时出错：{exc}")

    def _export_all(self) -> None:
        """后台导出：重新查全量（无 limit）并写入 CSV。"""
        try:
            uid = self._last_uid
        except (ValueError, TypeError):
            messagebox.showerror("错误", "请先查询一个用户")
            return
        path = filedialog.asksaveasfilename(
            title="导出该用户全部记录", defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("文本文件", "*.txt")],
        )
        if not path:
            return
        self.btn_export_all.configure(state="disabled", text="导出中...")
        gen = self._qgen

        def worker():
            try:
                comments = dao.query_comments_by_uid(uid, 0)
                danmaku = dao.query_danmaku_by_uid(uid, 0)
                import csv
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.writer(f)
                    w.writerow(["类型", "时间", "视频BV", "所属UP", "视频标题", "内容", "属地", "点赞", "播放进度(秒)", "完整内容"])
                    for c in comments:
                        w.writerow(["评论", fmt_dt(c.get("ctime")), c.get("bvid", ""), c.get("owner_name", "") or "", c.get("title", "") or "", str(c.get("content", ""))[:200], c.get("ip_location", "") or "", c.get("like_num", ""), "", c.get("content", "")])
                    for d in danmaku:
                        w.writerow(["弹幕", fmt_dt(d.get("ctime")), d.get("bvid", ""), d.get("owner_name", "") or "", d.get("title", "") or "", str(d.get("content", ""))[:200], "", "", float(d.get("progress", 0) or 0), d.get("content", "")])
                self.after(0, lambda: messagebox.showinfo("导出完成", f"已导出 {len(comments) + len(danmaku)} 条\n{path}") if gen == self._qgen else None)
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("导出失败", str(exc)) if gen == self._qgen else None)
            finally:
                self.after(0, lambda: self.btn_export_all.configure(state="normal", text="导出全部"))
        import threading
        threading.Thread(target=worker, daemon=True).start()

    # ---------------- 完整内容弹窗 ----------------
    def _on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        row = self._rows.get(iid)
        if row:
            self._show_detail(row)

    def _show_detail(self, row: dict) -> None:
        """弹出完整内容查看窗口（含全文，不受截断限制）。"""
        win = tk.Toplevel(self)
        win.title("完整内容")
        win.geometry("620x400")
        win.transient(self.winfo_toplevel())
        win.grab_set()

        txt = tk.Text(win, wrap="word", font=F.BODY)
        sb = ttk.Scrollbar(win, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb.pack(side="right", fill="y", padx=(0, 6), pady=6)

        kind = row.get("kind")
        lines = []
        bvid = row.get("bvid")
        title = str((row.get("title") or "")[:60])
        if kind == "评论":
            lines += [
                "【评论】",
                f"  时间: {fmt_dt(row.get('ctime'))}",
                f"  视频: {bvid or row.get('video_aid', '')} {title if bvid else ''}".rstrip(),
                f"  点赞: {row.get('like_num', 0)}    属地: {row.get('ip_location') or '未知'}",
                "",
                row.get("content", ""),
            ]
        else:
            lines += [
                "【弹幕】",
                f"  时间: {fmt_dt(row.get('ctime'))}",
                f"  视频: {bvid or row.get('video_aid', '')} {row.get('title', '')[:30] if bvid else ''}".rstrip(),
                f"  播放位置: {float(row.get('progress', 0) or 0):.1f}s    发送者: {row.get('mid_hash', '') or row.get('uid', '')}",
                "",
                row.get("content", ""),
            ]
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")

        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 8))