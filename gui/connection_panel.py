"""连接状态面板：Claude 质感双库监控与归档管理。"""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import ttk

import pymysql

from config import get_config
from gui.theme import C, F, S
from gui.widgets import ClaudeBadge, ClaudeCard, ModernConsole


def _test_conn(cfg: dict, timeout: int = 3) -> tuple[bool, str]:
    try:
        c = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset=cfg.get("charset", "utf8mb4"),
            connect_timeout=timeout,
            read_timeout=5,
        )
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        c.close()
        return True, "正常在线 (端口监听正常)"
    except Exception as exc:
        err = str(exc).splitlines()[0]
        if "10061" in err or "积极拒绝" in err:
            return False, "服务未就绪 (10061 端口未监听，请点击下方「管理员重连」)"
        if "1045" in err or "Access denied" in err:
            return False, "认证失败 (账号或密码错误，请检查设置)"
        if "1049" in err or "Unknown database" in err:
            return False, "数据库未初始化 (保存配置将自动建库)"
        if "timed out" in err.lower():
            return False, "连接超时 (磁盘忙碌或休眠中)"
        return False, err[:80]


def _service_status(name: str) -> str:
    try:
        r = subprocess.run(
            ["sc", "query", name],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if "RUNNING" in r.stdout:
            return "Running"
        if "STOPPED" in r.stdout:
            return "Stopped"
        if "START_PENDING" in r.stdout:
            return "Starting"
        return "Unknown"
    except Exception:
        return "Unknown"


class ConnectionPanel(ttk.Frame):
    """现代化双 MySQL 存储与归档状态面板。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._archive_thread: threading.Thread | None = None
        self._archive_stop = threading.Event()
        self._build()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # ── 顶栏标题 ──
        header = tk.Frame(self, bg=C.BG_WINDOW)
        header.grid(row=0, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(S.PAD_PAGE, 12))
        ttk.Label(header, text="存储与数据库状态", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="双 MySQL 架构：缓冲库 D (高吞吐写入) + 归档库 E (移动硬盘长效冷存与统计)",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        # ── 第一行：D / E 双卡片并排 ──
        cards_row = tk.Frame(self, bg=C.BG_WINDOW)
        cards_row.grid(row=1, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(0, 12))
        cards_row.columnconfigure(0, weight=1)
        cards_row.columnconfigure(1, weight=1)

        # 缓冲库 D
        card_d = ClaudeCard(
            cards_row,
            title="缓冲库 D (写入 3306)",
            subtitle="服务: MySQL80 · 主存储表",
            padding=14,
        )
        card_d.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.badge_d = ClaudeBadge(card_d.header_right, text="检测中...", status="info")
        self.badge_d.pack(side="right")

        body_d = tk.Frame(card_d, bg=C.BG_CARD)
        body_d.pack(fill="x", pady=(4, 8))
        ttk.Label(body_d, text="实例地址:", style="Secondary.TLabel").grid(row=0, column=0, sticky="w")
        self.lbl_d_addr = ttk.Label(body_d, text="--", style="Card.TLabel")
        self.lbl_d_addr.grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(body_d, text="系统服务:", style="Secondary.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.lbl_d_svc = ttk.Label(body_d, text="--", style="Card.TLabel")
        self.lbl_d_svc.grid(row=1, column=1, sticky="w", padx=8, pady=3)

        ttk.Label(body_d, text="连通状态:", style="Secondary.TLabel").grid(row=2, column=0, sticky="w")
        self.lbl_d_conn = ttk.Label(body_d, text="--", style="Card.TLabel")
        self.lbl_d_conn.grid(row=2, column=1, sticky="w", padx=8)

        act_d = tk.Frame(card_d, bg=C.BG_CARD)
        act_d.pack(fill="x", pady=(6, 0))
        ttk.Button(act_d, text="管理员重连 D", command=self._admin_start_d).pack(side="left")
        ttk.Button(act_d, text="断开 D", command=self._admin_stop_d).pack(side="left", padx=6)

        # 归档库 E
        card_e = ClaudeCard(
            cards_row,
            title="归档库 E (查询 3307)",
            subtitle="服务: MySQL80-E · 移动硬盘存储",
            padding=14,
        )
        card_e.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.badge_e = ClaudeBadge(card_e.header_right, text="检测中...", status="info")
        self.badge_e.pack(side="right")

        body_e = tk.Frame(card_e, bg=C.BG_CARD)
        body_e.pack(fill="x", pady=(4, 8))
        ttk.Label(body_e, text="实例地址:", style="Secondary.TLabel").grid(row=0, column=0, sticky="w")
        self.lbl_e_addr = ttk.Label(body_e, text="--", style="Card.TLabel")
        self.lbl_e_addr.grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(body_e, text="系统服务:", style="Secondary.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.lbl_e_svc = ttk.Label(body_e, text="--", style="Card.TLabel")
        self.lbl_e_svc.grid(row=1, column=1, sticky="w", padx=8, pady=3)

        ttk.Label(body_e, text="连通状态:", style="Secondary.TLabel").grid(row=2, column=0, sticky="w")
        self.lbl_e_conn = ttk.Label(body_e, text="--", style="Card.TLabel")
        self.lbl_e_conn.grid(row=2, column=1, sticky="w", padx=8)

        act_e = tk.Frame(card_e, bg=C.BG_CARD)
        act_e.pack(fill="x", pady=(6, 0))
        ttk.Button(act_e, text="管理员重连 E", command=self._admin_start_e).pack(side="left")
        ttk.Button(act_e, text="断开 E", command=self._admin_stop_e).pack(side="left", padx=6)

        # ── 第二行：操作与归档控制卡片 ──
        ctrl_card = ClaudeCard(
            self,
            title="数据归档与运维控制",
            subtitle="手动触发分仓归档同步、进程清理与参数设置",
            padding=14,
        )
        ctrl_card.grid(row=2, column=0, sticky="ew", padx=S.PAD_PAGE, pady=(0, 12))

        b_row = tk.Frame(ctrl_card, bg=C.BG_CARD)
        b_row.pack(fill="x")
        ttk.Button(b_row, text="刷新检测", style="Accent.TButton", command=self.refresh).pack(side="left")
        self.btn_archive = ttk.Button(b_row, text="手动归档 D -> E", command=self._manual_archive)
        self.btn_archive.pack(side="left", padx=6)
        self.btn_archive_cancel = ttk.Button(
            b_row, text="取消归档", command=self._cancel_archive, state="disabled"
        )
        self.btn_archive_cancel.pack(side="left", padx=6)
        ttk.Button(b_row, text="清理卡死查询", style="Danger.TButton", command=self._kill_stuck).pack(
            side="left", padx=6
        )
        ttk.Button(b_row, text="数据库连接配置", command=self._open_db).pack(side="left", padx=6)

        self.lbl_tip = ttk.Label(b_row, text="双库就绪，系统运行正常", style="Hint.TLabel")
        self.lbl_tip.pack(side="left", padx=12)

        # 进度指示
        p_row = tk.Frame(ctrl_card, bg=C.BG_CARD)
        p_row.pack(fill="x", pady=(10, 0))
        self.prog = ttk.Progressbar(p_row, mode="determinate", maximum=100)
        self.prog.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(p_row, text="待归档", style="Secondary.TLabel")
        self.lbl_prog.pack(side="left", padx=8)

        # ── 第三行：黑曜石终端日志卡片 ──
        log_card = ClaudeCard(
            self,
            title="执行与同步日志",
            subtitle="实时输出底层跨库拷贝、校验与迁移信息",
            padding=14,
        )
        log_card.grid(row=3, column=0, sticky="nsew", padx=S.PAD_PAGE, pady=(0, S.PAD_PAGE))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)

        self.console = ModernConsole(log_card, height=8)
        self.console.pack(fill="both", expand=True, pady=(4, 0))
        self.txt_log = self.console.txt  # 保持向下兼容

        self.after(800, self._check_resume)
        self.after(500, self.refresh)

    def _append_log(self, line: str, tag: str = "info") -> None:
        self.console.append(line, tag=tag)

    def _check_resume(self) -> None:
        try:
            from storage.archive_sync import _load_progress

            prog = _load_progress()
            if prog and prog.get("shard"):
                shard = prog["shard"]
                offs = prog.get("offsets", {})
                total = sum(offs.values()) if offs else 0
                self.lbl_tip.configure(text=f"检测到断点 {shard} 未完成（已拷 {total} 行），点「手动归档」继续")
                self.lbl_prog.configure(text=f"断点 {shard} 可续")
                self._append_log(f"检测到断点 {shard}，可续拷", tag="warn")
        except Exception:
            pass

    def _admin_start_d(self) -> None:
        import ctypes

        try:
            from storage.db import clear_manual_offline

            clear_manual_offline("write")
        except Exception:
            pass
        self.lbl_tip.configure(text="正在申请管理员权限启动 MySQL80 服务，请在系统 UAC 弹窗中点击「是」...")
        self.badge_d.set_status("正在启动", status="info")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                '-NoProfile -Command "net start MySQL80; Start-Sleep 2"',
                None,
                0,
            )
            if ret <= 32:
                from tkinter import messagebox

                messagebox.showwarning("提权提示", f"管理员提权未确认或被取消 (代码 {ret})")
                self.lbl_tip.configure(text="提权未确认，请重新点击「管理员重连 D」")
            else:
                self.lbl_tip.configure(text="已发起启动指令，正在等待 MySQL80 端口就绪...")
        except Exception as exc:
            from tkinter import messagebox

            messagebox.showerror("错误", str(exc))
        self.after(3500, self.refresh)

    def _admin_stop_d(self) -> None:
        from tkinter import messagebox

        if not messagebox.askokcancel(
            "断开 D",
            "将停止 MySQL80 (缓冲库 D，3306)。\n\n"
            "· 采集/写入任务将暂停；\n"
            "· 守护线程不会自动拉起，直到点「管理员重连 D」。\n\n确定断开吗？",
        ):
            return
        import ctypes

        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                '-NoProfile -Command "net stop MySQL80"',
                None,
                0,
            )
            if ret <= 32:
                messagebox.showwarning("提示", f"提权失败({ret})")
                return
            try:
                from storage.db import set_manual_offline

                set_manual_offline("write")
            except Exception:
                pass
            try:
                from storage.dao import _rclose_all

                _rclose_all()
            except Exception:
                pass
            self.lbl_tip.configure(text="已请求断开 D，5 秒后自动刷新...")
        except Exception as exc:
            messagebox.showerror("错误", str(exc))
        self.after(5000, self.refresh)

    def _admin_stop_e(self) -> None:
        from tkinter import messagebox

        if not messagebox.askokcancel(
            "断开 E",
            "将停止 MySQL80-E (归档库 E，3307)。\n\n"
            "· 查询将自动安全回退到 D；\n"
            "· 拔出移动硬盘或关机前请先断开 E；\n\n确定断开吗？",
        ):
            return
        import ctypes

        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                '-NoProfile -Command "net stop MySQL80-E"',
                None,
                0,
            )
            if ret <= 32:
                messagebox.showwarning("提示", f"提权失败({ret})")
                return
            try:
                from storage.db import set_manual_offline

                set_manual_offline("read")
            except Exception:
                pass
            try:
                from storage.dao import _rclose_all

                _rclose_all()
            except Exception:
                pass
            self.lbl_tip.configure(text="已请求断开 E，5 秒后自动刷新...")
        except Exception as exc:
            messagebox.showerror("错误", str(exc))
        self.after(5000, self.refresh)

    def _admin_start_e(self) -> None:
        import ctypes, tempfile

        try:
            from storage.db import clear_manual_offline

            clear_manual_offline("read")
        except Exception:
            pass
        try:
            r = subprocess.run(
                ["sc", "query", "MySQL80-E"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if "RUNNING" in r.stdout:
                self.lbl_tip.configure(text="E 已在运行，直接刷新")
                self.after(500, self.refresh)
                return
        except Exception:
            pass

        self.lbl_tip.configure(text="正在申请管理员权限启动 MySQL80-E 归档服务，请在 UAC 弹窗中允许...")
        self.badge_e.set_status("正在启动", status="info")
        ps = r"""
$ErrorActionPreference="SilentlyContinue"
net stop MySQL80-E 2>&1 | Out-Host
Start-Sleep 2
if (Test-Path "E:\") {
    icacls "E:\MySQLData\data" /grant "NETWORK SERVICE:(OI)(CI)F" /T | Out-Null
}
net start MySQL80-E 2>&1 | Out-Host
"""
        try:
            tf = tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False, encoding="utf-8")
            tf.write(ps)
            tf.close()
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                f'-NoProfile -ExecutionPolicy Bypass -File "{tf.name}"',
                None,
                1,
            )
            if ret <= 32:
                from tkinter import messagebox

                messagebox.showwarning("提权提示", f"管理员提权未确认或被取消 (代码 {ret})")
                self.lbl_tip.configure(text="提权未确认，请重新点击「管理员重连 E」")
            else:
                self.lbl_tip.configure(text="已发起提权修复与拉起，正在等待 MySQL80-E 端口就绪...")
        except Exception as exc:
            from tkinter import messagebox

            messagebox.showerror("错误", str(exc))
        self.after(15000, self.refresh)

    def _cancel_archive(self) -> None:
        self._archive_stop.set()
        self.after(0, lambda: self.lbl_tip.configure(text="已请求取消归档..."))

    def _manual_archive(self) -> None:
        import re
        from tkinter import messagebox

        try:
            from config import get_config as _gc
            import pymysql as _pm

            _cfg = _gc().mysql_archive
            _c = _pm.connect(
                host=_cfg["host"],
                port=_cfg["port"],
                user=_cfg["user"],
                password=_cfg["password"],
                database=_cfg["database"],
                charset=_cfg.get("charset", "utf8mb4"),
                connect_timeout=3,
                read_timeout=5,
            )
            _c.close()
        except Exception as _e:
            messagebox.showwarning("归档", f"E 库未连通，无法归档：{_e}\n请先点「管理员重连 E」")
            return

        _th = getattr(self, "_archive_thread", None)
        if _th is not None and _th.is_alive():
            messagebox.showwarning("归档", "已有归档任务正在运行，请稍候。")
            return

        self._archive_stop.clear()
        self.btn_archive.configure(state="disabled", text="归档中...")
        self.btn_archive_cancel.configure(state="normal")
        self.prog.configure(mode="determinate", maximum=1, value=0)
        self.console.clear()
        self.lbl_tip.configure(text="正在计算分仓行数与门槛...")
        self.lbl_prog.configure(text="预估计算中...")

        def worker():
            try:
                from storage.archive_sync import _MIN_ROWS_FOR_NEW_SHARD as _TH
            except Exception:
                _TH = 2000000
            counts: dict = {}
            count_err = ""
            try:
                from storage.db import get_conn as _gc2

                _c2 = _gc2("write")
                try:
                    with _c2.cursor() as _cur:
                        for _t in ("raw_comments", "raw_danmaku", "meta_video", "dim_user"):
                            _cur.execute(f"SELECT COUNT(*) AS n FROM {_t}")
                            _row = _cur.fetchone()
                            counts[_t] = int((_row or {}).get("n") or 0)
                finally:
                    try:
                        _c2.close()
                    except Exception:
                        pass
            except Exception as exc:
                count_err = str(exc).splitlines()[0][:200]

            _total_est = counts.get("raw_comments", 0)
            self.after(0, lambda v=_total_est: self.prog.configure(maximum=max(1, v), value=0))
            if count_err:
                self.after(
                    0, lambda e=count_err: self._append_log(f"预检：D 表统计失败: {e}", tag="error")
                )
                self.after(0, lambda: self.lbl_prog.configure(text="预估未知"))
            else:
                self.after(0, lambda v=_total_est: self.lbl_prog.configure(text=f"预估 {v} 行 0%"))
                self.after(
                    0,
                    lambda c=dict(counts): self._append_log(
                        f"预检 D: 评论 {c.get('raw_comments', 0):,} / 弹幕 {c.get('raw_danmaku', 0):,} / 视频 {c.get('meta_video', 0):,} / 用户 {c.get('dim_user', 0):,}",
                        tag="accent",
                    ),
                )
                if _total_est < _TH:
                    self.after(
                        0,
                        lambda v=_total_est, t=_TH: self._append_log(
                            f"预检提示: 当前 D 库评论 {v:,} 行 < 门槛 {t:,} 行，若无断点将被拒绝（请继续采集攒数）",
                            tag="warn",
                        ),
                    )

            total_est = _total_est

            def on_prog(msg: str):
                self.after(0, lambda: self.lbl_tip.configure(text=msg))
                self.after(0, lambda m=msg: self._append_log(m, tag="info"))
                m = re.search(r"拷\s*(\d+)-(\d+)", msg)
                if m:
                    try:
                        cur = int(m.group(2))
                        self.after(
                            0,
                            lambda c=cur, t=total_est: self.prog.configure(value=min(c, t)),
                        )
                        self.after(
                            0,
                            lambda c=cur, t=total_est: self.lbl_prog.configure(
                                text=f"预估 {t:,} 行 {min(100, int(c * 100 / max(1, t)))}%"
                            ),
                        )
                    except Exception:
                        pass
                elif "拷完" in msg:
                    m2 = re.search(r"(\d+)\s*行", msg)
                    if m2:
                        try:
                            cur = int(m2.group(1))
                            self.after(
                                0,
                                lambda c=cur, t=total_est: self.prog.configure(value=min(c, t)),
                            )
                        except Exception:
                            pass

            try:
                from storage.archive_sync import shard_archive

                res = shard_archive(progress_cb=on_prog, stop_flag=self._archive_stop)
                self.after(
                    0,
                    lambda: self.prog.configure(
                        mode="determinate", maximum=max(1, total_est), value=total_est
                    ),
                )
                self.after(0, lambda: self.lbl_prog.configure(text=f"预估 {total_est:,} 行 100%"))
                if res.get("ok"):
                    self.after(0, lambda: self.lbl_tip.configure(text=res["msg"]))
                    self.after(0, lambda m=res["msg"]: self._append_log(m, tag="success"))
                else:
                    self.after(0, lambda: messagebox.showwarning("归档提示", res["msg"]))
                    self.after(0, lambda: self.lbl_tip.configure(text=res["msg"]))
                    self.after(0, lambda m=res["msg"]: self._append_log(m, tag="warn"))
                self.after(0, self.refresh)
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("归档失败", str(exc)))
                self.after(0, lambda: self.lbl_tip.configure(text="归档失败"))
                self.after(0, lambda e=str(exc): self._append_log(f"归档异常: {e}", tag="error"))
            finally:
                self.after(0, lambda: self.btn_archive_cancel.configure(state="disabled"))
                self.after(0, lambda: self.btn_archive.configure(state="normal", text="手动归档 D -> E"))

        self._archive_thread = threading.Thread(target=worker, daemon=True)
        self._archive_thread.start()

    def _kill_stuck(self) -> None:
        from tkinter import messagebox

        if not messagebox.askokcancel(
            "清理卡死查询",
            "将中止执行超过 300 秒的长慢查询（Sleep/系统元数据除外）。\n确定继续吗？",
        ):
            return
        self.lbl_tip.configure(text="正在排查卡死查询...")

        def worker():
            try:
                from storage.db import get_conn

                c = get_conn("read")
                with c.cursor() as cur:
                    cur.execute("SHOW FULL PROCESSLIST")
                    rows = cur.fetchall()
                    me = cur.connection.thread_id()
                    killed = 0
                    for r in rows:
                        rid = r.get("Id")
                        if rid == me or (r.get("Command") or "") == "Sleep":
                            continue
                        if int(r.get("Time") or 0) <= 300:
                            continue
                        info = str(r.get("Info") or "")
                        if "information_schema" in info or "stats_cache" in info:
                            continue
                        try:
                            with c.cursor() as kcur:
                                kcur.execute(f"KILL {int(rid)}")
                            killed += 1
                        except Exception:
                            pass
                c.close()
                msg = f"已成功清理 {killed} 个耗时过长查询"
                self.after(0, lambda: self.lbl_tip.configure(text=msg))
                self.after(0, lambda: self._append_log(msg, tag="accent"))
            except Exception as exc:
                self.after(0, lambda e=str(exc): messagebox.showerror("清理失败", e))

        threading.Thread(target=worker, daemon=True).start()

    def _open_db(self) -> None:
        top = self.winfo_toplevel()
        if hasattr(top, "_open_db_config"):
            top._open_db_config()

    def refresh(self) -> None:
        cfg = get_config()
        w = getattr(cfg, "mysql_write", cfg.mysql)
        r = getattr(cfg, "mysql_archive", cfg.mysql)
        self.lbl_d_addr.configure(text=f"{w['host']}:{w['port']}/{w['database']}")
        self.lbl_e_addr.configure(text=f"{r['host']}:{r['port']}/{r['database']}")
        self.lbl_d_svc.configure(text="检测中...")
        self.lbl_e_svc.configure(text="检测中...")
        self.lbl_d_conn.configure(text="检测中...")
        self.lbl_e_conn.configure(text="检测中...")
        self.badge_d.set_status("检测中", status="info")
        self.badge_e.set_status("检测中", status="info")
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self) -> None:
        cfg = get_config()
        w = getattr(cfg, "mysql_write", cfg.mysql)
        r = getattr(cfg, "mysql_archive", cfg.mysql)

        svc_d = _service_status("MySQL80")
        ok_d, msg_d = _test_conn(w)
        svc_e = _service_status("MySQL80-E")
        ok_e, msg_e = _test_conn(r)

        def upd():
            self.lbl_d_svc.configure(text=svc_d)
            self.lbl_d_conn.configure(text=msg_d)
            self.badge_d.set_status(
                "运行中" if (svc_d == "Running" and ok_d) else "服务异常",
                status="success" if (svc_d == "Running" and ok_d) else "error",
            )

            self.lbl_e_svc.configure(text=svc_e)
            self.lbl_e_conn.configure(text=msg_e)
            self.badge_e.set_status(
                "正常挂载" if (svc_e == "Running" and ok_e) else "离线",
                status="success" if (svc_e == "Running" and ok_e) else "warning",
            )

            if ok_d and ok_e:
                self.lbl_tip.configure(text="双库健康在线，读写隔离正常")
            else:
                self.lbl_tip.configure(text="部分服务未连通 (E 离线时查询自动回退到 D)")

            try:
                from storage.db import clear_manual_offline

                if ok_d:
                    clear_manual_offline("write")
                if ok_e:
                    clear_manual_offline("read")
            except Exception:
                pass

        try:
            self.after(0, upd)
        except Exception:
            pass
