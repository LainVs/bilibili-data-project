"""数据库连接配置对话框：多配置管理 + 数据目录迁移 + 测试连接 + 保存。

settings.ini 结构：
  [mysql]          — 当前活跃配置（程序读取）
  [connections]    — 已保存的配置预设（name = JSON）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pymysql

from config import get_config, update_mysql_archive_config, update_mysql_config
from gui.theme import C
from gui.util import busy_cursor, friendly_error

logger = logging.getLogger(__name__)

try:
    import pymysql.cursors
except Exception:
    pass

# 迁移脚本模板（需要管理员权限运行）
_MIGRATE_SCRIPT = '''# MySQL data migration: {src} -> {dst}
$ErrorActionPreference = "Stop"
$src = "{src}"
$dst = "{dst}"
$ini = "{ini}"

Write-Host "=== MySQL Migration ==="
Write-Host "Src: $src"
Write-Host "Dst: $dst"

Write-Host "`n[1/5] Stopping MySQL80..."
Stop-Service MySQL80 -Force
Start-Sleep -Seconds 2
if ((Get-Service MySQL80).Status -ne "Stopped") {{
    Write-Host "FAILED: service not stopped" -ForegroundColor Red; exit 1
}}
Write-Host "  Stopped" -ForegroundColor Green

Write-Host "`n[2/5] Creating target dir..."
if (!(Test-Path $dst)) {{ New-Item -ItemType Directory -Path $dst -Force | Out-Null }}

Write-Host "`n[3/5] Copying data (~19GB, may take 10-20 min)..."
robocopy $src $dst /E /COPYALL /R:2 /W:3 /NFL /NDL /NP
if ($LASTEXITCODE -le 7) {{ Write-Host "  Done" -ForegroundColor Green }} else {{ Write-Host "  Error $LASTEXITCODE" -ForegroundColor Red; exit 1 }}

Write-Host "`n[4/5] Updating my.ini..."
$iniBak = "$ini.bak_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
Copy-Item $ini $iniBak -Force
$content = Get-Content $ini -Raw
$dstEsc = $dst.Replace('\','\\\\')
$content = $content -replace '(?m)^datadir\\s*=\\s*.*$', "datadir=$dstEsc"
Set-Content $ini $content -Encoding UTF8
Write-Host "  datadir -> $dst" -ForegroundColor Green

Write-Host "`n[5/5] Starting MySQL80..."
Start-Service MySQL80
Start-Sleep -Seconds 3
if ((Get-Service MySQL80).Status -eq "Running") {{
    Write-Host "  Running" -ForegroundColor Green
}} else {{
    Write-Host "  Start FAILED" -ForegroundColor Red; exit 1
}}

Write-Host "`n=== Verify ===" -ForegroundColor Cyan
& "C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin\\mysql.exe" -uroot -p{pwd} --connect-timeout=10 -e "SELECT @@datadir;" 2>&1

Write-Host "`n=== DONE ===" -ForegroundColor Green
Write-Host "Old dir $src can be deleted after confirming."
Read-Host "Press Enter to exit"
'''


class DbConfigDialog(tk.Toplevel):
    """模态对话框：配置选择 + 连接参数 + 数据目录迁移 + 测试 + 保存。"""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        self.title("数据库连接设置")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._cfg = get_config()
        self._connections = self._load_connections()

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        # ---- 配置选择行 ----
        preset_row = ttk.Frame(frm)
        preset_row.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(preset_row, text="配置:").pack(side="left")
        self._preset_var = tk.StringVar()
        self._preset_combo = ttk.Combobox(
            preset_row, textvariable=self._preset_var, state="readonly", width=22,
            values=list(self._connections.keys()))
        self._preset_combo.pack(side="left", padx=4)
        self._preset_combo.bind("<<ComboboxSelected>>", self._on_preset)
        ttk.Button(preset_row, text="加载", command=self._on_preset).pack(side="left")
        ttk.Button(preset_row, text="删除", command=self._delete_preset).pack(side="left", padx=(4, 0))
        ttk.Separator(preset_row, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(preset_row, text="另存为:", foreground="#888").pack(side="left")
        self._saveas_var = tk.StringVar()
        ttk.Entry(preset_row, textvariable=self._saveas_var, width=16).pack(side="left", padx=2)
        ttk.Button(preset_row, text="保存配置", command=self._save_preset).pack(side="left", padx=2)

        # ---- 连接参数行 ----
        cfg = self._cfg.mysql
        rows = [
            ("主机:", "host", cfg["host"], 28),
            ("端口:", "port", str(cfg["port"]), 10),
            ("用户名:", "user", cfg["user"], 28),
            ("密码:", "password", cfg["password"], 28),
            ("数据库:", "database", cfg["database"], 28),
        ]
        self._vars: dict[str, tk.StringVar] = {}
        for i, (label, key, default, width) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i + 1, column=0, sticky="e", pady=3)
            var = tk.StringVar(value=default)
            self._vars[key] = var
            show = "*" if key == "password" else ""
            ent = ttk.Entry(frm, textvariable=var, width=width, show=show)
            ent.grid(row=i + 1, column=1, sticky="we", pady=3, padx=(6, 0))
            if key == "password":
                self._show_pw = tk.BooleanVar(value=False)
                ttk.Checkbutton(frm, text="显示", variable=self._show_pw,
                                command=lambda _ent=ent: _ent.configure(
                                    show="" if self._show_pw.get() else "*")
                                ).grid(row=i + 1, column=2, sticky="w", padx=4)

        # ---- 归档库E（查询/统计） ----
        ttk.Separator(frm, orient="horizontal").grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        ttk.Label(frm, text="归档库E（查询/统计，HDD）  端口3307", font=("", 9, "bold")).grid(row=8, column=0, columnspan=3, sticky="w")
        cfg_r = getattr(self._cfg, "mysql_archive", self._cfg.mysql)
        rows_r = [
            ("归档主机:", "host", cfg_r["host"], 22),
            ("归档端口:", "port", str(cfg_r["port"]), 8),
            ("归档库名:", "database", cfg_r["database"], 22),
        ]
        self._vars_r: dict[str, tk.StringVar] = {}
        for j, (label, key, default, width) in enumerate(rows_r):
            ttk.Label(frm, text=label).grid(row=9+j, column=0, sticky="e", pady=2)
            var = tk.StringVar(value=default)
            self._vars_r[key] = var
            ttk.Entry(frm, textvariable=var, width=width).grid(row=9+j, column=1, sticky="we", pady=2, padx=(6,0))
        ttk.Label(frm, text="归档库账号与写库一致，无需重复填写", foreground="#888", font=("", 8)).grid(row=12, column=0, columnspan=3, sticky="w", pady=(0,4))

        # ---- 数据目录迁移区 ----
        sep = ttk.Separator(frm, orient="horizontal")
        sep.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(6, 6))
        ttk.Label(frm, text="数据目录（MySQL 数据文件存储位置）",
                  font=("", 9, "bold")).grid(row=14, column=0, columnspan=3, sticky="w")

        dir_row = ttk.Frame(frm)
        dir_row.grid(row=15, column=0, columnspan=3, sticky="ew", pady=2)
        self._lbl_datadir = ttk.Label(dir_row, text="当前: 查询中...", foreground="#555")
        self._lbl_datadir.pack(side="left", fill="x", expand=True)
        ttk.Button(dir_row, text="刷新", width=4, command=self._refresh_datadir).pack(side="right")

        ttk.Label(frm, text="新路径:", foreground="#555").grid(row=16, column=0, sticky="e")
        self._var_newdir = tk.StringVar()
        ttk.Entry(frm, textvariable=self._var_newdir, width=38).grid(
            row=16, column=1, sticky="we", padx=(6, 0))
        ttk.Button(frm, text="浏览...", width=6, command=self._browse_dir).grid(
            row=16, column=2, padx=4)

        dir_btn_row = ttk.Frame(frm)
        dir_btn_row.grid(row=17, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self._btn_migrate = ttk.Button(dir_btn_row, text="生成迁移脚本",
                                       command=self._migrate_datadir)
        self._btn_migrate.pack(side="left")
        self._lbl_migrate_hint = ttk.Label(
            dir_btn_row,
            text="需要管理员权限运行，会停止MySQL服务并拷贝全部数据",
            foreground="#888", font=("", 8))
        self._lbl_migrate_hint.pack(side="left", padx=8)

        # ---- 状态 / 按钮 ----
        self._status = ttk.Label(frm, text="填写连接信息后可点击「测试连接」。", foreground="#888")
        self._status.grid(row=18, column=0, columnspan=3, pady=(10, 2))

        btns = ttk.Frame(frm)
        btns.grid(row=19, column=0, columnspan=3, pady=(6, 0))
        ttk.Button(btns, text="测试连接", command=self._test).pack(side="left", padx=4)
        ttk.Button(btns, text="保存并切换", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=4)

        frm.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda e: self._test())
        self._center(master)

        # 异步读取当前数据目录（不阻塞UI）
        self.after(200, self._refresh_datadir)

    # ---- 配置预设管理 ----

    def _load_connections(self) -> dict[str, dict]:
        """从 settings.ini [connections] 读所有预设。"""
        out: dict[str, dict] = {}
        parser = getattr(self._cfg, "_parser", None)
        if parser is not None and parser.has_section("connections"):
            for name, val in parser.items("connections"):
                try:
                    out[name] = json.loads(val) if isinstance(val, str) else val
                except (json.JSONDecodeError, TypeError):
                    pass
        return out

    def _on_preset(self, _e=None) -> None:
        name = self._preset_var.get()
        conn = self._connections.get(name)
        if not conn:
            return
        for key in ("host", "port", "user", "password", "database"):
            if key in conn:
                self._vars[key].set(str(conn[key]))

    def _delete_preset(self) -> None:
        name = self._preset_var.get()
        if not name or name not in self._connections:
            return
        if not messagebox.askyesno("确认", f"删除配置「{name}」？"):
            return
        del self._connections[name]
        self._save_connections_section()
        self._preset_combo["values"] = list(self._connections.keys())
        self._preset_var.set("")

    def _save_preset(self) -> None:
        name = self._saveas_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入配置名称")
            return
        try:
            host, port, user, password, database = self._values()
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字")
            return
        self._connections[name] = {
            "host": host, "port": port, "user": user,
            "password": password, "database": database,
        }
        self._save_connections_section()
        self._preset_combo["values"] = list(self._connections.keys())
        self._preset_var.set(name)
        self._status.configure(text=f"配置「{name}」已保存", foreground=C.SUCCESS)

    def _save_connections_section(self) -> None:
        """把所有预设写回 settings.ini [connections] section。"""
        import configparser

        from config import CONFIG_FILE
        cp = configparser.RawConfigParser()
        cp.read(str(CONFIG_FILE), encoding="utf-8")
        if not cp.has_section("connections"):
            cp.add_section("connections")
        for k in list(cp.options("connections")) if cp.has_section("connections") else []:
            cp.remove_option("connections", k)
        for name, val in self._connections.items():
            cp.set("connections", name, json.dumps(val, ensure_ascii=False))
        with open(str(CONFIG_FILE), "w", encoding="utf-8") as f:
            cp.write(f)
        # 刷新内存缓存
        import config as cfg_mod
        from config import get_config
        cfg_mod._config = None
        self._cfg = get_config()
        self._cfg._parser.read(str(CONFIG_FILE), encoding="utf-8")

    # ---- 数据目录管理 ----

    def _refresh_datadir(self) -> None:
        """异步读取 MySQL @@datadir，不阻塞UI。"""
        def worker() -> None:
            try:
                cfg = self._cfg.mysql
                conn = pymysql.connect(
                    host=cfg["host"], port=cfg["port"],
                    user=cfg["user"], password=cfg["password"],
                    database=cfg["database"], charset="utf8mb4",
                    connect_timeout=5, read_timeout=5)
                with conn.cursor() as cur:
                    cur.execute("SELECT @@datadir AS d")
                    row = cur.fetchone()
                conn.close()
                self.after(0, lambda: self._lbl_datadir.configure(
                    text=f"当前: {row['d']}" if row else "当前: 查询失败"))
            except Exception as exc:
                # lambda 默认参数绑定，避免 except 结束后 exc 被解绑导致 NameError
                self.after(0, lambda e=str(exc): self._lbl_datadir.configure(
                    text=f"当前: 无法读取（{e}）", foreground=C.ERROR))

        threading.Thread(target=worker, daemon=True).start()

    def _browse_dir(self) -> None:
        """选择目标目录。"""
        path = filedialog.askdirectory(title="选择数据目录目标位置")
        if path:
            self._var_newdir.set(path)

    def _migrate_datadir(self) -> None:
        """生成迁移脚本到桌面，用户右键管理员运行。"""
        newdir = self._var_newdir.get().strip()
        if not newdir:
            messagebox.showwarning("提示", "请输入或选择新的数据目录路径")
            return

        # 读取当前 datadir
        curdir = self._lbl_datadir.cget("text").replace("当前: ", "").strip()
        if not curdir or "无法读取" in curdir or "查询失败" in curdir:
            messagebox.showerror("错误", "无法读取当前数据目录，请先确认MySQL连接正常")
            return

        if os.path.normpath(curdir) == os.path.normpath(newdir):
            messagebox.showwarning("提示", "新路径与当前路径相同，无需迁移")
            return

        # 生成脚本
        ini_path = r"C:\ProgramData\MySQL\MySQL Server 8.0\my.ini"
        script = _MIGRATE_SCRIPT.format(
            src=curdir, dst=newdir, ini=ini_path,
            pwd=self._vars["password"].get())

        # 保存到桌面
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        script_path = os.path.join(desktop, "mysql_migrate.ps1")
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(script)
        except Exception as exc:
            messagebox.showerror("错误", f"保存脚本失败：{exc}")
            return

        messagebox.showinfo(
            "迁移脚本已生成",
            f"脚本位置：\n{script_path}\n\n"
            f"迁移内容：\n{curdir} → {newdir}\n\n"
            "请按以下步骤操作：\n"
            "1. 关闭本软件\n"
            "2. 右键「以管理员身份运行」PowerShell\n"
            "3. 执行：\n"
            f"   powershell -ExecutionPolicy Bypass -File \"{script_path}\"\n"
            "4. 等待完成（约10-20分钟）\n"
            "5. 重启本软件")

    # ---- 原有方法 ----

    def _center(self, master: tk.Tk) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self._status.focus_set()

    def _values(self) -> tuple[str, int, str, str, str]:
        return (self._vars["host"].get().strip(),
                int(self._vars["port"].get().strip() or 3306),
                self._vars["user"].get().strip(),
                self._vars["password"].get(),
                self._vars["database"].get().strip())

    def _connect(self, host, port, user, password, database) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4",
            connect_timeout=5, read_timeout=10,
        )

    def _test(self, silent: bool = False) -> bool:
        try:
            host, port, user, password, database = self._values()
        except ValueError:
            self._status.configure(text="端口必须是数字", foreground=C.ERROR)
            return False
        # 连接测试为同步阻塞（每个库 connect_timeout=5s），两个库都不可达时
        # 可能卡十余秒——期间切沙漏光标，让用户知道程序在忙而非卡死。
        with busy_cursor(self):
            return self._test_inner(silent, host, port, user, password, database)

    def _test_inner(self, silent: bool, host, port, user, password, database) -> bool:
        # 写库 D
        try:
            conn = self._connect(host, port, user, password, database)
        except Exception as exc:
            logger.warning("测试连接写库D失败: %s", exc)
            self._status.configure(text="写库D连接失败", foreground=C.ERROR)
            if not silent:
                messagebox.showerror("写库失败", friendly_error(exc, "连接写库 D"))
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION() AS v")
                ver = cur.fetchone()["v"]
        except Exception:
            self._status.configure(text="写库D已连通但库不存在，保存后自动建库", foreground=C.WARNING)
            ver = "?"
        finally:
            try: conn.close()
            except Exception: pass
        # 归档库 E
        try:
            rh = self._vars_r["host"].get().strip() or host
            rp = int(self._vars_r["port"].get().strip() or 3307)
            rdb = self._vars_r["database"].get().strip() or database
            conn2 = self._connect(rh, rp, user, password, rdb)
            with conn2.cursor() as cur:
                cur.execute("SELECT VERSION() AS v")
                ver2 = cur.fetchone()["v"]
            conn2.close()
            msg = f"写库D OK({ver})  归档库E OK({ver2})"
            self._status.configure(text=msg, foreground=C.SUCCESS)
            if not silent:
                messagebox.showinfo("连接成功", msg)
            return True
        except Exception as exc2:
            logger.warning("测试连接归档库E失败: %s", exc2)
            msg = f"写库D OK({ver})  归档库E未连通 — 采集可用，查询将回退到D"
            self._status.configure(text=msg, foreground=C.WARNING)
            if not silent:
                messagebox.showwarning("归档库未连通", friendly_error(exc2, "连接归档库 E"))
            return True

    def _save(self) -> None:
        try:
            host, port, user, password, database = self._values()
        except ValueError:
            self._status.configure(text="端口必须是数字", foreground=C.ERROR)
            return
        if not host or not user:
            self._status.configure(text="主机和用户名不能为空", foreground=C.ERROR)
            return
        # 归档库
        try:
            rh = self._vars_r["host"].get().strip() or host
            rp = int(self._vars_r["port"].get().strip() or port)
            rdb = self._vars_r["database"].get().strip() or database
        except ValueError:
            self._status.configure(text="归档端口必须是数字", foreground=C.ERROR)
            return
        try:
            update_mysql_config(host=host, port=port, user=user, password=password, database=database)
            update_mysql_archive_config(host=rh, port=rp, user=user, password=password, database=rdb)
        except Exception as exc:
            messagebox.showerror("保存失败", f"写入配置文件失败：{exc}")
            return
        if self._test(silent=True):
            self.destroy()
        else:
            self._status.configure(text="已保存，但有连接未通过", foreground=C.WARNING)
