"""GUI 主窗口：深色侧边栏导航 + 浅色内容区，简约现代风。"""
from __future__ import annotations

import importlib
import logging
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from gui.theme import C, F, S, apply_theme
from gui.widgets import ToolTip
from storage.session import load_session, save_session

logger = logging.getLogger(__name__)

# 导航定义：图标字符 + 描述标签 + 悬停提示 + 面板类路径（懒加载）
PAGES: list[tuple[str, str, str]] = [
    ("连", "连接状态", "gui.connection_panel:ConnectionPanel"),
    ("采", "定向采集", "gui.collect_panel:CollectPanel"),
    ("增", "增量轮询", "gui.update_panel:UpdatePanel"),
    ("查", "用户查询", "gui.user_query_panel:UserQueryPanel"),
    ("广", "广泛采集", "gui.broad_panel:BroadPanel"),
    ("统", "统计",     "gui.stats_panel:StatsPanel"),
    ("搜", "全局搜索", "gui.search_panel:SearchPanel"),
]


class MainWindow(tk.Tk):
    """应用主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("B站弹幕/评论采集系统")
        self.minsize(960, 640)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 应用全局主题 ──
        apply_theme(self)

        # 居中且不超屏
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = 1080, 720
        w, h = min(w, sw - 40), min(h, sh - 80)
        x, y = (sw - w) // 2, (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        self._pages: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[tk.Canvas, str] = {}
        self._nav_btn_by_label: dict[str, tk.Canvas] = {}
        self._dots: dict[str, tk.Canvas] = {}
        self._current_key: str | None = None

        self._session = load_session()

        self._build_menu()
        self._build_nav()

        self._restore_session()
        self.after(400, self._maybe_resume_task)
        self.after(800, self._check_db_conn)
        # 守护线程
        self._wdog_stop = threading.Event()
        threading.Thread(target=self._service_watchdog, daemon=True).start()
        def _keepalive():
            import os
            while not self._wdog_stop.is_set():
                try:
                    os.listdir("E:\\")
                    with open(r"E:\MySQLData\data\ibdata1", "rb") as f:
                        f.read(1)
                except Exception:
                    pass
                self._wdog_stop.wait(300)
        threading.Thread(target=_keepalive, daemon=True).start()

    # ═══════════════════ 侧边栏导航（Canvas 绘制） ═══════════════════

    def _build_nav(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ── 侧边栏容器 ──
        sidebar = tk.Frame(self, bg=C.BG_SIDEBAR, width=S.SIDEBAR_W)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)

        # 顶部 Logo 区
        logo = tk.Canvas(sidebar, width=S.SIDEBAR_W, height=48,
                         bg=C.BG_SIDEBAR, highlightthickness=0, bd=0)
        logo.grid(row=0, column=0, sticky="ew")
        logo.create_text(S.SIDEBAR_W // 2, 24, text="B",
                         font=("Microsoft YaHei UI", 18, "bold"),
                         fill=C.ACCENT)

        # 分隔线
        sep = tk.Frame(sidebar, bg="#2a2a3d", height=1)
        sep.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))

        # ── 导航按钮 ──
        nav_frame = tk.Frame(sidebar, bg=C.BG_SIDEBAR)
        nav_frame.grid(row=2, column=0, sticky="new")
        nav_frame.columnconfigure(0, weight=1)

        for i, (icon, label, _path) in enumerate(PAGES):
            btn = self._make_nav_btn(nav_frame, icon, label)
            btn.grid(row=i, column=0, padx=6, pady=2, sticky="ew")
            self._nav_buttons[btn] = label
            self._nav_btn_by_label[label] = btn

        # 底部弹簧
        spacer = tk.Frame(sidebar, bg=C.BG_SIDEBAR)
        spacer.grid(row=3, column=0, sticky="nsew")
        sidebar.rowconfigure(3, weight=1)

        # ── 右侧内容区 ──
        holder = ttk.Frame(self, style="TFrame")
        holder.grid(row=0, column=1, sticky="nsew", padx=0, pady=0)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        self._holder = holder

        self._switch(PAGES[0][1])

    def _make_nav_btn(self, parent: tk.Frame, icon: str, label: str) -> tk.Canvas:
        """用 Canvas 绘制一个导航按钮：圆角选中态 + 图标 + 小标签。"""
        h = 50
        c = tk.Canvas(parent, width=S.SIDEBAR_W - 12, height=h,
                      bg=C.BG_SIDEBAR, highlightthickness=0, bd=0,
                      cursor="hand2")
        # 存储引用
        c._nav_icon = icon
        c._nav_label = label
        c._nav_active = False

        # 绘制背景矩形（初始透明 = 同色）
        c._bg_rect = c.create_rectangle(
            2, 2, S.SIDEBAR_W - 14, h - 2,
            fill=C.BG_SIDEBAR, outline="", width=0)
        # 图标文字
        c._icon_id = c.create_text(
            (S.SIDEBAR_W - 12) // 2, h // 2 - 7,
            text=icon, font=F.NAV, fill=C.FG_NAV)
        # 小标签
        c._label_id = c.create_text(
            (S.SIDEBAR_W - 12) // 2, h // 2 + 12,
            text=label[:2], font=F.NAV_HINT, fill=C.FG_NAV)

        # 事件绑定
        c.bind("<Enter>", lambda e, cv=c: self._nav_hover(cv, True))
        c.bind("<Leave>", lambda e, cv=c: self._nav_hover(cv, False))
        c.bind("<ButtonRelease-1>", lambda e, cv=c: self._switch(cv._nav_label))

        ToolTip(c, label)
        return c

    def _nav_hover(self, cv: tk.Canvas, enter: bool) -> None:
        if cv._nav_active:
            return
        try:
            cv.itemconfigure(cv._bg_rect,
                             fill=C.BG_NAV_HOVER if enter else C.BG_SIDEBAR)
        except tk.TclError:
            pass

    def _nav_set_active(self, cv: tk.Canvas, active: bool) -> None:
        cv._nav_active = active
        try:
            cv.itemconfigure(cv._bg_rect,
                             fill=C.BG_NAV_ACTIVE if active else C.BG_SIDEBAR)
            cv.itemconfigure(cv._icon_id,
                             fill=C.FG_NAV_ACTIVE if active else C.FG_NAV)
            cv.itemconfigure(cv._label_id,
                             fill=C.FG_NAV_ACTIVE if active else C.FG_NAV)
        except tk.TclError:
            pass

    # ═══════════════════ 面板切换 ═══════════════════

    def _get_panel(self, key: str) -> ttk.Frame:
        """懒构建面板：首次访问才 import 并实例化。"""
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
        """切换右侧面板，高亮导航按钮。"""
        if key == self._current_key and key in self._pages:
            return
        self._current_key = key
        panel = self._get_panel(key)
        panel.tkraise()
        self._clear_dot(key)
        for cv, k in self._nav_buttons.items():
            self._nav_set_active(cv, k == key)

    # ═══════════════════ 通知小红点 ═══════════════════

    def notify_done(self, label: str | None) -> None:
        """某面板工作完成：导航按钮亮小红点。线程安全。"""
        def _cb() -> None:
            try:
                if not label or label == self._current_key:
                    return
                btn = self._nav_btn_by_label.get(label)
                if btn is None:
                    return
                dot = self._dots.get(label)
                if dot is None:
                    dot = tk.Canvas(btn, width=8, height=8,
                                    highlightthickness=0, bd=0,
                                    bg=C.BG_SIDEBAR)
                    try:
                        dot.create_oval(1, 1, 7, 7, fill=C.DOT_RED, outline=C.DOT_RED)
                    except tk.TclError:
                        return
                    self._dots[label] = dot
                dot.place(relx=1.0, rely=0.0, anchor="ne", x=-3, y=3)
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

    # ═══════════════════ 会话恢复 ═══════════════════

    def _restore_session(self) -> None:
        data = self._session or {}
        geom = (data.get("window") or {}).get("geometry")
        if geom:
            try:
                self.update_idletasks()
                sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
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
        label = None
        try:
            for k, p in self._pages.items():
                if p is panel:
                    label = k
                    break
        except Exception:
            pass
        self.notify_done(label or self._current_key)

    def _maybe_resume_task(self) -> None:
        lt = (self._session or {}).get("last_task")
        if not isinstance(lt, dict) or lt.get("done"):
            return
        if not any(label == lt.get("panel") for _, label, _ in PAGES):
            return
        go = messagebox.askyesno(
            "继续上次任务？",
            f"上次的「{lt.get('desc', '采集任务')}」\n"
            f"（{lt.get('started_at', '')} 启动）尚未完成，\n"
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

    # ═══════════════════ 关闭 ═══════════════════

    def _on_close(self) -> None:
        stopped: list[threading.Thread] = []
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
        self._wdog_stop.set()
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

    # ═══════════════════ 菜单 ═══════════════════

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        m_set = tk.Menu(menubar, tearoff=0)
        m_set.add_command(label="数据库连接...", command=self._open_db_config)
        m_set.add_separator()
        m_set.add_command(label="导出数据包...", command=self._export_dump)
        m_set.add_command(label="导入数据包...", command=self._import_dump)
        menubar.add_cascade(label="设置", menu=m_set)
        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.configure(menu=menubar)

    # ═══════════════════ 数据包迁移 ═══════════════════

    def _export_dump(self) -> None:
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
        box.geometry("480x140")
        box.configure(bg=C.BG_MAIN)
        ttk.Label(box, text="导出", style="Title.TLabel").pack(padx=20, pady=(16, 4), anchor="w")
        lbl = ttk.Label(box, text="正在导出（大库可能需要十几分钟）...", style="Hint.TLabel")
        lbl.pack(fill="x", padx=20, pady=4)
        ttk.Progressbar(box, mode="indeterminate").pack(fill="x", padx=20, pady=(0, 16))

        def _worker() -> None:
            def prog(t): self._push_toplevel_text(lbl, t)
            r = export_dump(path, on_progress=prog, on_log=prog)
            if r["ok"]:
                self._push_toplevel_text(lbl, f"导出完成: {r['path']} ({r['size_mb']} MB)")
            else:
                self._push_toplevel_text(lbl, f"导出失败: {r['error']}")
        threading.Thread(target=_worker, daemon=True).start()

    def _import_dump(self) -> None:
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
        box.geometry("480x140")
        box.configure(bg=C.BG_MAIN)
        ttk.Label(box, text="导入", style="Title.TLabel").pack(padx=20, pady=(16, 4), anchor="w")
        lbl = ttk.Label(box, text="正在导入（数据量大请耐心等待）...", style="Hint.TLabel")
        lbl.pack(fill="x", padx=20, pady=4)
        ttk.Progressbar(box, mode="indeterminate").pack(fill="x", padx=20, pady=(0, 16))

        def _worker() -> None:
            def prog(t): self._push_toplevel_text(lbl, t)
            r = import_dump(path, on_progress=prog, on_log=prog)
            if r["ok"]:
                self._push_toplevel_text(lbl, "导入完成: 数据已就绪")
            else:
                self._push_toplevel_text(lbl, f"导入失败: {r['error']}")
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

    # ═══════════════════ DB ═══════════════════

    def _open_db_config(self) -> None:
        from gui.db_config_dialog import DbConfigDialog
        DbConfigDialog(self)

    def _check_db_conn(self) -> None:
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
                    init_db()
            except Exception as exc:
                logger.warning("启动数据库连接检测失败: %s", exc)
        threading.Thread(target=_do_check, daemon=True).start()

    # ═══════════════════ MySQL 守护 ═══════════════════

    def _service_watchdog(self) -> None:
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
            import string
            letter = drive[0].upper()
            if letter not in string.ascii_uppercase:
                return False
            import ctypes
            return ctypes.windll.kernel32.GetLogicalDrives() & (1 << (ord(letter) - 65)) != 0

        def _start_service() -> bool:
            try:
                r = subprocess.run(
                    ["net", "start", "MySQL80"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return r.returncode == 0
            except Exception:
                return False

        def _get_datadir_drive() -> str:
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
            self._wdog_stop.wait(30)
            if self._wdog_stop.is_set():
                break
            if _ping_db():
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            logger.warning("MySQL守护: 连接失败 (连续%d次)", consecutive_failures)
            try:
                from storage.db import is_manual_offline
                if is_manual_offline("write"):
                    if consecutive_failures == 1:
                        logger.info("MySQL守护: D为手动断开，跳过自动拉起")
                    continue
            except Exception:
                pass
            drive = _get_datadir_drive()
            disk_ok = _disk_ok(drive)
            if not disk_ok:
                logger.warning("MySQL守护: %s 盘离线，等待...", drive)
                for _ in range(12):
                    if self._wdog_stop.is_set():
                        return
                    if _disk_ok(drive):
                        logger.info("MySQL守护: %s 盘恢复", drive)
                        time.sleep(3)
                        break
                    self._wdog_stop.wait(5)
                else:
                    continue
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

    # ═══════════════════ 关于 ═══════════════════

    def _show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            "B站弹幕/评论采集与存储系统\n\n"
            "采集: 视频/UP主/收藏夹/关注粉丝/批量UP/帖子专栏\n"
            "增量轮询: 定时补采新数据\n"
            "用户查询: 按 UID 检索全部评论与弹幕\n"
            "广泛采集: BFS 自动扩展\n"
            "统计: 总览/排行榜/趋势洞察\n"
            "搜索: 跨评论/弹幕/视频全局搜索\n\n"
            "数据源: B站公开接口\n"
            "存储: MySQL (bilibili_data)",
        )


def run() -> None:
    app = MainWindow()
    app.mainloop()
