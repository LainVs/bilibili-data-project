"""GUI 主窗口：左侧图标导航栏 + 右侧单面板切换区（Obsidian 风格）。"""
from __future__ import annotations

import importlib
import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from gui.widgets import ToolTip
from storage.session import load_session, save_session

logger = logging.getLogger(__name__)

# 导航定义:字符图标 + 悬停提示 + 面板类路径（懒加载：首次切换才构建，加速启动）
PAGES: list[tuple[str, str, str]] = [
    ("连", "连接状态", "gui.connection_panel:ConnectionPanel"),
    ("采", "定向采集", "gui.collect_panel:CollectPanel"),
    ("增", "增量轮询", "gui.update_panel:UpdatePanel"),
    ("查", "用户查询", "gui.user_query_panel:UserQueryPanel"),
    ("广", "广泛采集", "gui.broad_panel:BroadPanel"),
    ("统", "统计", "gui.stats_panel:StatsPanel"),
    ("搜", "全局搜索", "gui.search_panel:SearchPanel"),
]


class MainWindow(tk.Tk):
    """应用主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("B站弹幕/评论采集与查询 GUI")
        self.minsize(860, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 居中且不超屏，初始别太大
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = 960, 640
        w, h = min(w, sw - 40), min(h, sh - 80)
        x, y = (sw - w) // 2, (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        self._pages: dict[str, ttk.Frame] = {}   # 已构建的面板（懒加载）
        self._nav_buttons: dict[tk.Button, str] = {}
        self._nav_btn_by_label: dict[str, tk.Button] = {}
        self._dots: dict[str, tk.Canvas] = {}  # 面板label -> 右上角小红点
        self._current_key: str | None = None

        self._session = load_session()  # 懒加载面板恢复表单需要，故提前

        self._build_menu()
        self._build_nav()

        # 会话恢复：面板表单 + 上次面板 + 断点续采提示
        self._restore_session()
        self.after(400, self._maybe_resume_task)
        self.after(800, self._check_db_conn)
        # 守护线程：MySQL保活 + 自动重启
        self._wdog_stop = threading.Event()
        threading.Thread(target=self._service_watchdog, daemon=True).start()
        # E盘 keepalive：每5分钟轻读一次防HDD/盒子休眠
        def _keepalive():
            import os
            while not self._wdog_stop.is_set():
                try:
                    os.listdir("E:\\")
                    # 轻触 ibdata1
                    with open(r"E:\MySQLData\data\ibdata1", "rb") as f:
                        f.read(1)
                except Exception:
                    pass
                self._wdog_stop.wait(300)
        threading.Thread(target=_keepalive, daemon=True).start()
        # 归档同步改为手动：不在启动时自动跑，需在“连”面板点“手动归档”

    # ---------------- 会话状态（重启恢复） ----------------
    def _restore_session(self) -> None:
        """启动时恢复窗口尺寸、上次面板（其余面板懒加载时再恢复表单）。"""
        data = self._session or {}
        geom = (data.get("window") or {}).get("geometry")
        if geom:
            try:
                # 校验是否在屏内，否则保持居中
                self.update_idletasks()
                sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
                # 解析 geom "WxH+X+Y"，若超屏则忽略
                import re
                m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geom)
                if m:
                    w, h, x, y = map(int, m.groups())
                    if 0 <= x < sw and 0 <= y < sh and w <= sw and h <= sh:
                        self.geometry(geom)
            except Exception:
                pass
        active = data.get("active_panel")
        if any(label == active for _, label, _ in PAGES):
            self._switch(active)

    def _save_session(self) -> None:
        """快照全部面板表单 + 窗口状态，落盘 session.json。"""
        panels = {}
        for key, panel in self._pages.items():
            snap = getattr(panel, "snapshot_state", None)
            if callable(snap):
                try:
                    panels[key] = snap()
                except Exception as exc:
                    logger.warning("快照面板 %s 失败: %s", key, exc)
        old_lt = (self._session or {}).get("last_task")
        self._session = {
            "version": 1,
            "active_panel": self._current_key,
            "window": {"geometry": self.geometry()},
            "panels": panels,
            "last_task": old_lt if isinstance(old_lt, dict) else None,
        }
        save_session(self._session)

    def _on_task_started(self, desc: str) -> None:
        """任务启动时记录断点（done=False），关窗/杀进程后重启可续采。"""
        import datetime as _dt

        form = {}
        if self._current_key in self._pages:
            snap = getattr(self._pages[self._current_key], "snapshot_state", None)
            if callable(snap):
                try:
                    form = snap()
                except Exception:
                    form = {}
        self._session["last_task"] = {
            "panel": self._current_key,
            "desc": desc,
            "form": form,
            "started_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "done": False,
        }
        save_session(self._session)
        logger.info("任务已记录断点: %s", desc)

    def _on_task_done(self, panel=None) -> None:
        lt = (self._session or {}).get("last_task")
        if isinstance(lt, dict) and not lt.get("done"):
            lt["done"] = True
            save_session(self._session)
        # 任务面板不在前台时，导航按钮右上角亮小红点
        label = None
        try:
            for k, p in self._pages.items():
                if p is panel:
                    label = k
                    break
        except Exception:
            pass
        self.notify_done(label or self._current_key)

    def notify_done(self, label: str | None) -> None:
        """某面板工作完成：在其导航按钮右上角亮小红点（正在看则不亮）。线程安全。"""
        def _cb() -> None:
            try:
                if not label or label == self._current_key:
                    return
                btn = self._nav_btn_by_label.get(label)
                if btn is None:
                    return
                dot = self._dots.get(label)
                if dot is None:
                    dot = tk.Canvas(btn, width=10, height=10,
                                    highlightthickness=0, bd=0)
                    try:
                        dot.create_oval(1, 1, 9, 9, fill="#e5484d", outline="#e5484d")
                    except tk.TclError:
                        return
                    self._dots[label] = dot
                try:
                    dot.configure(bg=btn.cget("bg"))
                except tk.TclError:
                    pass
                dot.place(relx=1.0, rely=0.0, anchor="ne", x=-1, y=1)
                try:
                    dot.lift()
                except tk.TclError:
                    pass
            except tk.TclError:
                pass
        try:
            self.after(0, _cb)
        except tk.TclError:
            pass

    def _clear_dot(self, label: str | None) -> None:
        dot = self._dots.get(label or "")
        if dot is not None:
            try:
                dot.place_forget()
            except tk.TclError:
                pass

    def _maybe_resume_task(self) -> None:
        """启动后若上次任务未完成，询问是否断点续采（增量模式自动跳过已完成）。"""
        lt = (self._session or {}).get("last_task")
        if not isinstance(lt, dict) or lt.get("done"):
            return
        if not any(label == lt.get("panel") for _, label, _ in PAGES):
            return
        go = messagebox.askyesno(
            "继续上次任务？",
            f"上次的「{lt.get('desc', '采集任务')}」\n"
            f"（{lt.get('started_at', '')} 启动）尚未完成，"
            f"已采集的部分已存入数据库。\n\n"
            "是否继续采集剩余部分？\n（增量模式自动跳过已完成视频，不会重复）",
        )
        if not go:
            lt["done"] = True
            save_session(self._session)
            return
        self._switch(lt["panel"])
        panel = self._pages[lt["panel"]]
        restore = getattr(panel, "restore_state", None)
        if callable(restore):
            try:
                restore(lt.get("form") or {})
            except Exception as exc:
                logger.warning("恢复续采表单失败: %s", exc)
        self.after(250, lambda: self._launch_resume(panel))

    @staticmethod
    def _launch_resume(panel) -> None:
        start = getattr(panel, "start_task", None)
        if callable(start):
            start()

    # ---------------- 关闭：优雅停止后台任务 ----------------
    def _on_close(self) -> None:
        """关窗时先请求所有采集/轮询任务停止，等其自然退出落盘后再销毁窗口。

        采集任务在 run 尾会 flush 写库队列（AsyncDbWriter.close），
        直接销毁窗口会进程级硬杀线程导致最后一批数据丢失。
        停止后写线程可能仍在排空队列（用户停止场景），
        关窗会等所有写线程落库完成；超时(60s)后强制退出。
        """
        stopped: list[threading.Thread] = []
        stop_flags: list = []
        for label, panel in self._pages.items():
            worker = getattr(panel, "worker", None)
            if isinstance(worker, threading.Thread) and worker.is_alive():
                if callable(getattr(worker, "stop", None)):
                    worker.stop()
                stopped.append(worker)
            flag = getattr(panel, "_stop_flag", None)
            if isinstance(flag, threading.Event):
                flag.set()
            elif callable(getattr(panel, "stop", None)):
                getattr(panel, "stop")()
        self._closing = {"threads": stopped, "deadline": time.time() + 60.0}
        self._wdog_stop.set()  # 停止MySQL守护线程
        try:
            from storage.archive_sync import stop as _arch_stop
            _arch_stop()
        except Exception:
            pass
        self._save_session()
        self._check_exit()
        logger.info("窗口关闭：已请求 %s 个后台任务停止", len(stopped))

    def _check_exit(self) -> None:
        closing = getattr(self, "_closing", None)
        if closing is None:
            return
        from storage.async_writer import AsyncDbWriter
        alive = [t for t in closing["threads"] if t.is_alive()]
        if (alive or AsyncDbWriter.any_active()) and time.time() <= closing["deadline"]:
            self.after(300, self._check_exit)
            return
        if alive:
            logger.warning("等待后台任务超时(%s 个仍在运行)，强制退出", len(alive))
        self.destroy()

    # ---------------- 顶部菜单 ----------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        m_set = tk.Menu(menubar, tearoff=0)
        m_set.add_command(label="数据库连接…", command=self._open_db_config)
        m_set.add_separator()
        m_set.add_command(label="导出数据包…", command=self._export_dump)
        m_set.add_command(label="导入数据包…", command=self._import_dump)
        menubar.add_cascade(label="设置", menu=m_set)
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.configure(menu=menubar)

    # ---------------- 数据包迁移 ----------------
    def _export_dump(self) -> None:
        """全库导出：选路径 → 后台 mysqldump+gzip → 完成提示。"""
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            title="导出全库数据包",
            defaultextension=".sql.gz",
            filetypes=[("SQL 数据包", "*.sql.gz")],
            initialfile="bilibili_data.sql.gz",
        )
        if not path:
            return
        import threading
        from tools.db_transfer import export_dump

        box = tk.Toplevel(self)
        box.title("导出数据包")
        box.geometry("460x120")
        lbl = ttk.Label(box, text="正在导出（大库可能需要十几分钟）…", anchor="w")
        lbl.pack(fill="x", padx=12, pady=8)
        ttk.Progressbar(box, mode="indeterminate").pack(fill="x", padx=12)

        def _worker() -> None:
            def prog(t):
                self._push_toplevel_text(lbl, t)

            r = export_dump(path, on_progress=prog, on_log=prog)
            if r["ok"]:
                self._push_toplevel_text(
                    lbl, f"导出完成：{r['path']}（{r['size_mb']} MB）")
            else:
                self._push_toplevel_text(lbl, f"导出失败：{r['error']}")

        threading.Thread(target=_worker, daemon=True).start()

    def _import_dump(self) -> None:
        """导入数据包：选 .sql.gz → 后台自动建库导入 → 完成提示。"""
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="选择数据包",
            filetypes=[("SQL 数据包", "*.sql.gz"), ("SQL 文件", "*.sql")],
        )
        if not path:
            return
        import threading
        from tools.db_transfer import import_dump

        box = tk.Toplevel(self)
        box.title("导入数据包")
        box.geometry("460x120")
        lbl = ttk.Label(box, text="正在导入（数据量大请耐心等待）…", anchor="w")
        lbl.pack(fill="x", padx=12, pady=8)
        ttk.Progressbar(box, mode="indeterminate").pack(fill="x", padx=12)

        def _worker() -> None:
            def prog(t):
                self._push_toplevel_text(lbl, t)

            r = import_dump(path, on_progress=prog, on_log=prog)
            if r["ok"]:
                self._push_toplevel_text(lbl, "导入完成：数据已就绪，可正常使用")
            else:
                self._push_toplevel_text(lbl, f"导入失败：{r['error']}")

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _push_toplevel_text(lbl, text: str) -> None:
        def _cb() -> None:
            try:
                lbl.configure(text=text)
            except tk.TclError:
                pass
        try:
            lbl.after(0, _cb)
        except tk.TclError:
            pass

    # ---------------- 数据库连接 ----------------
    def _open_db_config(self) -> None:
        from gui.db_config_dialog import DbConfigDialog

        DbConfigDialog(self)

    def _check_db_conn(self) -> None:
        """启动后检测数据库连接；失败则弹配置对话框引导填写。"""
        if getattr(self, "_db_conn_checked", False):
            return
        self._db_conn_checked = True
        if getattr(self, "_db_conn_cancelled", False):
            return

        def _do_check() -> None:
            try:
                import pymysql

                from config import get_config
                from storage.db import init_db

                cfg = get_config().mysql
                conn = pymysql.connect(host=cfg["host"], port=cfg["port"],
                                       user=cfg["user"], password=cfg["password"],
                                       database=cfg["database"], charset=cfg["charset"],
                                       connect_timeout=5)
                try:
                    with conn.cursor() as cur:
                        cur.execute("SHOW TABLES LIKE 'raw_comments'")
                        missing = not cur.fetchone()
                finally:
                    conn.close()
                if missing:
                    # 库/表不存在才自动补建（幂等，老库仅轻量迁移）
                    init_db()
            except Exception as exc:
                logger.warning("启动数据库连接检测失败: %s", exc)
                # 不自动弹窗，只在状态栏提示；用户可手动从菜单打开

        threading.Thread(target=_do_check, daemon=True).start()

    def _service_watchdog(self) -> None:
        """守护线程：检测MySQL挂了→检查磁盘→自动重启→验证连接。"""
        import subprocess, time, pymysql
        from config import get_config

        def _ping_db() -> bool:
            try:
                cfg = get_config().mysql
                c = pymysql.connect(host=cfg["host"], port=cfg["port"],
                                    user=cfg["user"], password=cfg["password"],
                                    database=cfg["database"], charset="utf8mb4",
                                    connect_timeout=3)
                c.close()
                return True
            except Exception:
                return False

        def _service_running() -> bool:
            try:
                r = subprocess.run(
                    ["sc", "query", "MySQL80"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return "RUNNING" in r.stdout
            except Exception:
                return False

        def _disk_ok(drive: str) -> bool:
            """检测磁盘盘符是否在线。"""
            import string
            letter = drive[0].upper()
            if letter not in string.ascii_uppercase:
                return False
            import ctypes
            return ctypes.windll.kernel32.GetLogicalDrives() & (1 << (ord(letter) - 65)) != 0

        def _start_service() -> bool:
            """以管理员权限启动MySQL服务。"""
            try:
                r = subprocess.run(
                    ["net", "start", "MySQL80"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return r.returncode == 0
            except Exception:
                return False

        def _get_datadir_drive() -> str:
            """从my.ini读取datadir的盘符。"""
            try:
                ini = r"C:\ProgramData\MySQL\MySQL Server 8.0\my.ini"
                with open(ini, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.strip().startswith("datadir"):
                            val = line.split("=", 1)[1].strip()
                            return val[0].upper() + ":"
            except Exception:
                pass
            return "E:"

        consecutive_failures = 0
        while not self._wdog_stop.is_set():
            self._wdog_stop.wait(30)  # 每30秒检测一次
            if self._wdog_stop.is_set():
                break

            if _ping_db():
                consecutive_failures = 0
                continue

            consecutive_failures += 1
            logger.warning("MySQL守护: 连接失败 (连续%d次)", consecutive_failures)

            # 用户手动断开D：跳过自启（只记一次，避免每30秒刷屏），等用户点重连
            try:
                from storage.db import is_manual_offline
                if is_manual_offline("write"):
                    if consecutive_failures == 1:
                        logger.info("MySQL守护: D为手动断开，跳过自动拉起")
                    continue
            except Exception:
                pass

            # 检测磁盘
            drive = _get_datadir_drive()
            disk_ok = _disk_ok(drive)

            if not disk_ok:
                logger.warning("MySQL守护: %s 盘离线，等待...", drive)
                # 等磁盘恢复（最多60秒）
                for _ in range(12):
                    if self._wdog_stop.is_set():
                        return
                    if _disk_ok(drive):
                        logger.info("MySQL守护: %s 盘恢复", drive)
                        time.sleep(3)  # 等系统挂载
                        break
                    self._wdog_stop.wait(5)
                else:
                    continue

            # 磁盘在线但MySQL挂了，尝试重启
            if not _service_running():
                logger.info("MySQL守护: 尝试启动MySQL80...")
                ok = _start_service()
                if ok:
                    time.sleep(3)
                    if _ping_db():
                        logger.info("MySQL守护: 重启成功")
                        consecutive_failures = 0
                    else:
                        logger.error("MySQL守护: 重启后仍无法连接")
                else:
                    logger.error("MySQL守护: 启动服务失败（可能需要管理员权限）")

    # ---------------- 左侧导航栏 + 右侧面板 ----------------
    def _build_nav(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        bar = ttk.Frame(self, relief="raised", width=44, borderwidth=1)
        bar.grid(row=0, column=0, sticky="ns")
        bar.grid_propagate(False)
        bar.columnconfigure(0, weight=1)

        holder = ttk.Frame(self)
        holder.grid(row=0, column=1, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self._holder = holder

        for i, (icon, label, path) in enumerate(PAGES):
            btn = tk.Button(
                bar,
                text=icon,
                width=2,
                relief="flat",
                bg="#f0f0f0",
                activebackground="#cde3ff",
                font=("Microsoft YaHei UI", 13, "bold"),
                command=lambda k=label: self._switch(k),
            )
            btn.grid(row=i, column=0, padx=3, pady=(3, 0), sticky="ew")
            self._nav_buttons[btn] = label
            self._nav_btn_by_label[label] = btn
            ToolTip(btn, label)

        self._switch(PAGES[0][1])

    def _get_panel(self, key: str) -> ttk.Frame:
        """懒构建面板：首次访问才 import 并实例化（加速启动），并恢复会话表单。"""
        panel = self._pages.get(key)
        if panel is not None:
            return panel
        mod_name, _, cls_name = next(p for _, label, p in PAGES if label == key).partition(":")
        cls = getattr(importlib.import_module(mod_name), cls_name)
        panel = cls(self._holder)
        panel.grid(row=0, column=0, sticky="nsew")
        self._pages[key] = panel
        snap = ((self._session or {}).get("panels") or {}).get(key)
        restore = getattr(panel, "restore_state", None)
        if callable(restore):
            try:
                restore(snap or {})
            except Exception as exc:
                logger.warning("恢复面板 %s 状态失败: %s", key, exc)
        return panel

    def _switch(self, key: str) -> None:
        """切换右侧面板，并高亮导航按钮。"""
        if key == self._current_key and key in self._pages:
            return
        self._current_key = key
        panel = self._get_panel(key)
        panel.tkraise()
        self._clear_dot(key)  # 点开即视为已读，小红点消失
        for btn, k in self._nav_buttons.items():
            if k == key:
                btn.configure(bg="#cde3ff", relief="raised",
                              font=("Microsoft YaHei UI", 13, "bold"))
            else:
                btn.configure(bg="#f0f0f0", relief="flat",
                              font=("Microsoft YaHei UI", 13, "bold"))

    def _show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            "B站弹幕/评论采集与存储 GUI\n\n"
            "采集页：三种模式——\n"
            "  视频：输入 BV 或链接，选择评论/弹幕，实时预览。\n"
            "  UP主：输入 UID，批量采集其全部视频评论（可勾选增量去重）。\n"
            "  收藏夹：输入 UID，批量采集其全部收藏夹内视频的评论/弹幕。\n"
            "  关注/粉丝：输入 UID，对其关注/粉丝列表中的每人采集视频评论/弹幕。\n"
            "  帖子/专栏：粘贴动态/帖子链接或专栏 read/cvID 采集评论。\n"
            "增量轮询：对已采集视频定时补采新数据。\n"
            "用户查询：按 UID 检索库内该用户全部评论与弹幕。\n"
            "广泛采集：以种子 UP 起 BFS 自动扩展，从评论者反推新 UP 批量采评论，"
            "可设存储上限防溢出。\n"
            "统计：查看库内已采集的视频/评论/弹幕/UID 数量汇总。\n"
            "搜索：跨评论/弹幕/视频标题全局关键词搜索。\n\n"
            "增量模式会自动跳过已采集过的内容（断点续传，不重复）。\n"
            "数据源：B站公开接口；数据落 MySQL（bilibili_data）。",
        )


def run() -> None:
    app = MainWindow()
    app.mainloop()