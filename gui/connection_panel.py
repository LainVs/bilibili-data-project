"""连接状态面板：显示 D(3306) / E(3307) 状态，点击自动检测并尝试重连。"""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import ttk

import pymysql

from config import get_config


def _test_conn(cfg: dict, timeout: int = 3) -> tuple[bool, str]:
    try:
        c = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], database=cfg["database"], charset=cfg.get("charset","utf8mb4"), connect_timeout=timeout, read_timeout=5)
        with c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        c.close()
        return True, "正常"
    except Exception as exc:
        return False, str(exc).splitlines()[0][:120]


def _service_status(name: str) -> str:
    try:
        r = subprocess.run(["sc","query",name], capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if "RUNNING" in r.stdout:
            return "Running"
        if "STOPPED" in r.stdout:
            return "Stopped"
        if "START_PENDING" in r.stdout:
            return "Starting"
        return "Unknown"
    except Exception:
        return "Unknown"


def _try_start(name: str) -> bool:
    try:
        r = subprocess.run(["net","start",name], capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return r.returncode == 0
    except Exception:
        return False


class ConnectionPanel(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._build()

    def _build(self):
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text="连接状态（点击检测，失败自动重连）", font=("", 10, "bold")).grid(row=0, column=0, sticky="w", padx=8, pady=(8,4))

        # D
        f1 = ttk.LabelFrame(self, text="缓冲库 D（写入 3306）", padding=8)
        f1.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        f1.columnconfigure(1, weight=1)
        ttk.Label(f1, text="地址:").grid(row=0, column=0, sticky="e")
        self.lbl_d_addr = ttk.Label(f1, text="--")
        self.lbl_d_addr.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(f1, text="服务:").grid(row=1, column=0, sticky="e")
        self.lbl_d_svc = ttk.Label(f1, text="--")
        self.lbl_d_svc.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(f1, text="连接:").grid(row=2, column=0, sticky="e")
        self.lbl_d_conn = ttk.Label(f1, text="--")
        self.lbl_d_conn.grid(row=2, column=1, sticky="w", padx=4)

        # E
        f2 = ttk.LabelFrame(self, text="归档库 E（查询 3307）", padding=8)
        f2.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        f2.columnconfigure(1, weight=1)
        ttk.Label(f2, text="地址:").grid(row=0, column=0, sticky="e")
        self.lbl_e_addr = ttk.Label(f2, text="--")
        self.lbl_e_addr.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(f2, text="服务:").grid(row=1, column=0, sticky="e")
        self.lbl_e_svc = ttk.Label(f2, text="--")
        self.lbl_e_svc.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(f2, text="连接:").grid(row=2, column=0, sticky="e")
        self.lbl_e_conn = ttk.Label(f2, text="--")
        self.lbl_e_conn.grid(row=2, column=1, sticky="w", padx=4)

        btns = ttk.Frame(self)
        btns.grid(row=3, column=0, sticky="ew", padx=8, pady=8)
        ttk.Button(btns, text="检测/重连", command=self.refresh).pack(side="left")
        ttk.Button(btns, text="管理员重连D", command=self._admin_start_d).pack(side="left", padx=6)
        ttk.Button(btns, text="断开D", command=self._admin_stop_d).pack(side="left", padx=6)
        ttk.Button(btns, text="管理员重连E", command=self._admin_start_e).pack(side="left", padx=6)
        ttk.Button(btns, text="断开E", command=self._admin_stop_e).pack(side="left", padx=6)
        self.btn_archive = ttk.Button(btns, text="手动归档 D→E", command=self._manual_archive)
        self.btn_archive.pack(side="left", padx=6)
        self._archive_thread: threading.Thread | None = None
        self.btn_archive_cancel = ttk.Button(btns, text="取消归档", command=self._cancel_archive, state="disabled")
        self.btn_archive_cancel.pack(side="left", padx=6)
        ttk.Button(btns, text="清理卡死", command=self._kill_stuck).pack(side="left", padx=6)
        ttk.Button(btns, text="打开数据库设置", command=self._open_db).pack(side="left", padx=6)
        self.lbl_tip = ttk.Label(btns, text="检测仅查看状态，重连/断开请点对应按钮（断开需管理员提权）", foreground="#888")
        self.lbl_tip.pack(side="left", padx=8)
        self._archive_stop = threading.Event()

        # 归档进度与日志（空白处）
        prog_row = ttk.Frame(self)
        prog_row.grid(row=4, column=0, sticky="ew", padx=8, pady=(4,2))
        self.prog = ttk.Progressbar(prog_row, mode="determinate", maximum=100)
        self.prog.pack(side="left", fill="x", expand=True)
        self.lbl_prog = ttk.Label(prog_row, text="待归档")
        self.lbl_prog.pack(side="left", padx=8)
        log_frame = ttk.LabelFrame(self, text="归档日志", padding=4)
        log_frame.grid(row=5, column=0, sticky="nsew", padx=8, pady=4)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(log_frame, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(5, weight=1)

        # 断点续拷提示：若上次未完成，显示可继续
        self.after(800, self._check_resume)
        # 首次自动检测
        self.after(500, self.refresh)

    def _check_resume(self):
        try:
            from storage.archive_sync import _load_progress
            prog = _load_progress()
            if prog and prog.get("shard"):
                shard = prog["shard"]
                offs = prog.get("offsets", {})
                total = sum(offs.values()) if offs else 0
                self.lbl_tip.configure(text=f"上次归档 {shard} 未完成（已拷 {total} 行），点 手动归档 继续")
                self.lbl_prog.configure(text=f"断点 {shard} 可续")
                self._append_log(f"检测到断点 {shard}，可续拷")
        except: pass

    def _admin_start_d(self):
        import ctypes
        try:
            from storage.db import clear_manual_offline
            clear_manual_offline("write")  # 用户点了重连：允许守护线程恢复自启
        except Exception:
            pass
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", "-NoProfile -Command \"net start MySQL80; Start-Sleep 2\"", None, 0)
            if ret <= 32:
                from tkinter import messagebox
                messagebox.showwarning("提示", f"提权失败({ret})")
        except Exception as exc:
            from tkinter import messagebox
            messagebox.showerror("错误", str(exc))
        self.after(4000, self.refresh)

    def _admin_stop_d(self):
        """断开 D：提权 net stop MySQL80，并挂手动标记防守护线程拉起。"""
        from tkinter import messagebox
        if not messagebox.askokcancel("断开D",
                                      "将停止 MySQL80（缓冲库D，3306）。\n\n"
                                      "· 采集/归档/写入将失败；\n"
                                      "· 守护线程不会自动拉起，直到你点「管理员重连D」。\n\n"
                                      "确定断开吗？"):
            return
        import ctypes
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", "-NoProfile -Command \"net stop MySQL80\"", None, 0)
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
                _rclose_all()  # 丢掉本线程复用池里的旧连接，下次快速失败而非挂死
            except Exception:
                pass
            self.lbl_tip.configure(text="已请求断开D，5秒后自动检测...")
        except Exception as exc:
            messagebox.showerror("错误", str(exc))
        self.after(5000, self.refresh)

    def _admin_stop_e(self):
        """断开 E：提权 net stop MySQL80-E（拔移动硬盘前先点此）。"""
        from tkinter import messagebox
        if not messagebox.askokcancel("断开E",
                                      "将停止 MySQL80-E（归档库E，3307）。\n\n"
                                      "· 查询自动回退到D；\n"
                                      "· 拔移动硬盘/关机前请先断开E；\n"
                                      "· 重连请点「管理员重连E」。\n\n"
                                      "确定断开吗？"):
            return
        import ctypes
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", "-NoProfile -Command \"net stop MySQL80-E\"", None, 0)
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
            self.lbl_tip.configure(text="已请求断开E，5秒后自动检测...")
        except Exception as exc:
            messagebox.showerror("错误", str(exc))
        self.after(5000, self.refresh)

    def _admin_start_e(self):
        import ctypes, subprocess, tempfile, os
        try:
            from storage.db import clear_manual_offline
            clear_manual_offline("read")  # 用户点了重连：清除手动断开标记
        except Exception:
            pass
        # 若已 Running 直接检测
        try:
            r = subprocess.run(["sc","query","MySQL80-E"], capture_output=True, text=True, timeout=3, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            if "RUNNING" in r.stdout:
                self.lbl_tip.configure(text="E已Running，直接检测")
                self.after(500, self.refresh)
                return
        except Exception:
            pass
        # 一键修复：只动E，不碰D
        ps = r'''
$ErrorActionPreference="SilentlyContinue"
Write-Host "=== Fix E ==="
net stop MySQL80-E 2>&1 | Out-Host
Start-Sleep 2
if (Test-Path "E:\") {
    Write-Host "[1] chkdsk E: /f /x"
    echo Y | chkdsk E: /f /x | Out-Host
    Write-Host "[2] grant NETWORK SERVICE"
    icacls "E:\MySQLData\data" /grant "NETWORK SERVICE:(OI)(CI)F" /T | Out-Null
}
Write-Host "[3] start MySQL80-E"
net start MySQL80-E 2>&1 | Out-Host
Start-Sleep 3
Get-Service MySQL80-E | Out-Host
'''
        try:
            tf = tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", delete=False, encoding="utf-8")
            tf.write(ps)
            tf.close()
            ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", f'-NoProfile -ExecutionPolicy Bypass -File "{tf.name}"', None, 1)
            if ret <= 32:
                from tkinter import messagebox
                messagebox.showwarning("提示", f"提权失败({ret})")
            else:
                self.lbl_tip.configure(text="已提权执行修复，60秒后自动检测...")
        except Exception as exc:
            from tkinter import messagebox
            messagebox.showerror("错误", str(exc))
        self.after(15000, self.refresh)

    def _append_log(self, line: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _cancel_archive(self):
        self._archive_stop.set()
        self.after(0, lambda: self.lbl_tip.configure(text="已请求取消归档..."))

    def _manual_archive(self):
        import threading, time, re
        from tkinter import messagebox
        # 先检测 E 是否在线，离线直接提示不崩
        try:
            from config import get_config as _gc
            import pymysql as _pm
            _cfg = _gc().mysql_archive
            _c = _pm.connect(host=_cfg["host"], port=_cfg["port"], user=_cfg["user"],
                             password=_cfg["password"], database=_cfg["database"],
                             charset=_cfg.get("charset","utf8mb4"), connect_timeout=3, read_timeout=5)
            _c.close()
        except Exception as _e:
            messagebox.showwarning("归档", f"E 未连通，无法归档：{_e}\n请先点“管理员重连E”")
            return
        # 单飞：已有归档在跑时直接提示，不再开新线程（防连点开出重复仓）
        _th = getattr(self, "_archive_thread", None)
        if _th is not None and _th.is_alive():
            messagebox.showwarning("归档", "已有归档正在运行，请等它完成或点「取消归档」后再试。")
            return
        self._archive_stop.clear()
        self.btn_archive.configure(state="disabled", text="归档中…")
        self.btn_archive_cancel.configure(state="normal")
        # 预估行数改为后台取，避免主线程卡死
        total_est = 1
        self.prog.configure(mode="determinate", maximum=1, value=0)
        self.prog.stop()
        self.txt_log.configure(state="normal"); self.txt_log.delete("1.0","end"); self.txt_log.configure(state="disabled")
        self.lbl_tip.configure(text="拷文件归档中（可取消）...")
        self.lbl_prog.configure(text="预估计算中…")
        def worker():
            import re
            # 预检：D 各表行数 + 建库门槛，一次说清楚。
            # （以前 0 行/查询失败都显示“预估 1 行”，看不出原因，像卡死。）
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
                count_err = (str(exc).splitlines()[0][:200] if str(exc) else repr(exc))
            _total_est = counts.get("raw_comments", 0)
            self.after(0, lambda v=_total_est: self.prog.configure(maximum=max(1, v), value=0))
            if count_err:
                self.after(0, lambda e=count_err: self._append_log(f"预检：D行数查询失败：{e}（请先检查D连接/服务）"))
                self.after(0, lambda: self.lbl_prog.configure(text="预估未知（D查询失败）"))
            else:
                self.after(0, lambda v=_total_est: self.lbl_prog.configure(text=f"预估 {v} 行 0%"))
                self.after(0, lambda c=dict(counts): self._append_log(
                    f"预检 D：评论{c.get('raw_comments', 0)} / 弹幕{c.get('raw_danmaku', 0)} / "
                    f"视频{c.get('meta_video', 0)} / 用户{c.get('dim_user', 0)}"))
                if _total_est < _TH:
                    self.after(0, lambda v=_total_est, t=_TH: self._append_log(
                        f"预检：D评论{v}行 < 建库门槛{t}行，本次很可能被拒绝（继续采集攒数据；有未完成仓则续拷不受限）"))
            total_est = _total_est
            def on_prog(msg: str):
                self.after(0, lambda: self.lbl_tip.configure(text=msg))
                self.after(0, lambda m=msg: self._append_log(m))
                m = re.search(r"拷\s*(\d+)-(\d+)", msg)
                if m:
                    try:
                        cur = int(m.group(2))
                        self.after(0, lambda c=cur, t=total_est: self.prog.configure(value=min(c, t)))
                        self.after(0, lambda c=cur, t=total_est: self.lbl_prog.configure(text=f"预估 {t} 行 {min(100, int(c*100/t))}%"))
                    except: pass
                elif "拷完" in msg:
                    m2 = re.search(r"(\d+)\s*行", msg)
                    if m2:
                        try:
                            cur = int(m2.group(1))
                            self.after(0, lambda c=cur, t=total_est: self.prog.configure(value=min(c, t)))
                        except: pass
            try:
                from storage.archive_sync import shard_archive
                res = shard_archive(progress_cb=on_prog, stop_flag=self._archive_stop)
                self.after(0, lambda: self.prog.stop())
                self.after(0, lambda: self.prog.configure(mode="determinate", maximum=max(1, total_est), value=total_est))
                self.after(0, lambda: self.lbl_prog.configure(text=f"预估 {total_est} 行 100%"))
                if res.get("ok"):
                    self.after(0, lambda: self.lbl_tip.configure(text=res["msg"]))
                    self.after(0, lambda: self.lbl_prog.configure(text=res["msg"]))
                    try:
                        from gui.util import ping_main as _ping
                        self.after(0, lambda: _ping(self, "连接状态"))
                    except Exception:
                        pass
                else:
                    self.after(0, lambda: messagebox.showwarning("归档", res["msg"]))
                    self.after(0, lambda: self.lbl_tip.configure(text=res["msg"]))
                self.after(0, lambda: self._append_log(res["msg"]))
                self.after(0, self.refresh)
            except Exception as exc:
                self.after(0, lambda: self.prog.stop())
                self.after(0, lambda: messagebox.showerror("归档失败", str(exc)))
                self.after(0, lambda: self.lbl_tip.configure(text="归档失败"))
            finally:
                self.after(0, lambda: self.btn_archive_cancel.configure(state="disabled"))
                self.after(0, lambda: self.btn_archive.configure(state="normal", text="手动归档 D→E"))
        self._archive_thread = threading.Thread(target=worker, daemon=True)
        self._archive_thread.start()

    def _kill_stuck(self):
        import threading
        from tkinter import messagebox
        # 排行榜 GROUP BY 在 HDD 上合法跑几分钟，误杀会导致统计部分失败；
        # 先确认，且只杀真正卡住的（>300s），元数据/缓存查询不动。
        if not messagebox.askokcancel("清理卡死",
                                      "将 KILL 运行超过 300 秒的查询（Sleep/自己/元数据查询除外）。\n"
                                      "注意：排行榜聚合合法跑几分钟，正在统计时请先用「取消统计」。\n确定继续吗？"):
            return
        self.lbl_tip.configure(text="清理中...")
        def worker():
            try:
                from storage.db import get_conn
                c = get_conn("read")
                with c.cursor() as cur:
                    cur.execute("SHOW FULL PROCESSLIST")
                    rows = cur.fetchall()
                    me = cur.connection.thread_id()
                    killed = 0
                    skipped_info = 0
                    for r in rows:
                        rid = r.get("Id")
                        if rid == me:
                            continue
                        if (r.get("Command") or "") == "Sleep":
                            continue
                        try:
                            t = int(r.get("Time") or 0)
                        except Exception:
                            continue
                        if t <= 300:
                            continue
                        info = str(r.get("Info") or "")
                        # 元数据与缓存读写不是卡死，不杀
                        if ("information_schema" in info or "stats_cache" in info
                                or info.strip().upper().startswith("SHOW")):
                            skipped_info += 1
                            continue
                        try:
                            with c.cursor() as kcur:
                                kcur.execute(f"KILL {int(rid)}")
                            killed += 1
                        except Exception:
                            pass
                c.close()
                msg = f"已清理 {killed} 个卡死查询" + (f"（跳过元数据 {skipped_info} 个）" if skipped_info else "")
                self.after(0, lambda: self.lbl_tip.configure(text=msg))
                self.after(0, lambda: self._append_log(msg))
            except Exception as exc:
                msg = str(exc)
                self.after(0, lambda m=msg: messagebox.showerror("清理失败", m))
        threading.Thread(target=worker, daemon=True).start()

    def _open_db(self):
        top = self.winfo_toplevel()
        if hasattr(top, "_open_db_config"):
            top._open_db_config()

    def refresh(self):
        cfg = get_config()
        w = getattr(cfg, "mysql_write", cfg.mysql)
        r = getattr(cfg, "mysql_archive", cfg.mysql)
        self.lbl_d_addr.configure(text=f"{w['host']}:{w['port']}/{w['database']}")
        self.lbl_e_addr.configure(text=f"{r['host']}:{r['port']}/{r['database']}")
        self.lbl_d_svc.configure(text="检测中...")
        self.lbl_e_svc.configure(text="检测中...")
        self.lbl_d_conn.configure(text="检测中...")
        self.lbl_e_conn.configure(text="检测中...")
        threading.Thread(target=self._do_check, daemon=True).start()

    def _do_check(self):
        cfg = get_config()
        w = getattr(cfg, "mysql_write", cfg.mysql)
        r = getattr(cfg, "mysql_archive", cfg.mysql)

        # D / E 只检测，不自动重连
        svc_d = _service_status("MySQL80")
        ok_d, msg_d = _test_conn(w)
        svc_e = _service_status("MySQL80-E")
        ok_e, msg_e = _test_conn(r)

        def upd():
            self.lbl_d_svc.configure(text=svc_d, foreground="#080" if svc_d=="Running" else "#a00")
            self.lbl_d_conn.configure(text="✓ "+msg_d if ok_d else "✗ "+msg_d, foreground="#080" if ok_d else "#a00")
            self.lbl_e_svc.configure(text=svc_e, foreground="#080" if svc_e=="Running" else "#a00")
            self.lbl_e_conn.configure(text="✓ "+msg_e if ok_e else "✗ "+msg_e, foreground="#080" if ok_e else "#a00")
            self.lbl_tip.configure(text="检测完成" if (ok_d and ok_e) else "部分未连通，E离线时查询回退到D")
            # 连接恢复即清除手动断开标记（服务可能被外部拉起，标记过期）
            try:
                from storage.db import clear_manual_offline
                if ok_d:
                    clear_manual_offline("write")
                if ok_e:
                    clear_manual_offline("read")
            except Exception:
                pass
        self.after(0, upd)
