"""统一表格控件：冻结表头 + 鼠标拖拽列宽 + 斑马纹 + 单元格/整行高亮选中复制 + 右键快捷菜单。

支持：
    * 单击选中单元格 / 整行，支持多选（Shift / Ctrl）
    * 键盘快捷键 Ctrl+C / Ctrl+Insert：优先复制当前点击的单元格（如单个 BV / UID / 标题），多选时复制全部选中行
    * 鼠标右键上下文菜单（Button-3 / Button-2）：
        - 📋 复制单元格: "BV1Z24y1Y7zP"
        - 📄 复制整行数据 (制表符分隔，便于粘贴到 Excel)
        - 📑 复制多选行 (N行)
        - 📊 复制整列全部数据
        - 🎯 智能识别 BV：快捷填入「定向采集」或「水军检测」
        - 👤 智能识别 UID：快捷跳转「用户查询」
    * 浮动微提示（Toast）：复制成功后指针旁显示 1.2 秒气泡反馈
"""
from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk

from gui.theme import C, F
from gui.widgets.anim import SmoothScroll

_GRIP = 5  # 表头分割线附近多少像素内判定为"可拖"


def show_toast(widget: tk.Widget, text: str = "✓ 已复制", x: int | None = None, y: int | None = None) -> None:
    """轻量复制反馈气泡：在光标附近展示 1.2 秒后自动销毁。"""
    try:
        if x is None or y is None:
            x = widget.winfo_pointerx() + 14
            y = widget.winfo_pointery() + 14
        top = tk.Toplevel(widget)
        top.wm_overrideredirect(True)
        top.wm_geometry(f"+{x}+{y}")
        top.attributes("-topmost", True)
        lbl = tk.Label(
            top,
            text=text,
            bg=C.TIP_BG,
            fg=C.TIP_FG,
            font=F.SMALL,
            padx=10,
            pady=5,
            relief="flat",
            bd=0,
        )
        lbl.pack()
        top.after(1200, top.destroy)
    except Exception:
        pass


class SmartTable(ttk.Treeview):
    """可拖拽列宽 + 冻结表头 + 斑马纹 + 复制与右键菜单的 Treeview 子类。"""

    _stripe_after_id: str | None = None

    def __init__(self, master, *, columns=(), show="headings", selectmode="extended", **kw):
        super().__init__(master, columns=columns, show=show, selectmode=selectmode, **kw)
        self._resize_anchor: str | None = None
        self._drag_col: str | None = None
        self._drag_start_x: int = 0
        self._drag_start_w: int = 0
        self._last_cell: tuple[str, int] | None = None  # (row_id, col_idx)
        self._edges_cache: dict[str, int] | None = None  # 列边界缓存（拖拽/设列时失效）

        # 斑马纹 tag
        self.tag_configure("stripe", background=C.BG_TABLE_ALT)

        # 事件绑定：拖拽列宽与单元格点击跟踪
        self.bind("<Motion>", self._on_motion, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<B1-Motion>", self._on_drag, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.bind("<Leave>", self._on_leave, add="+")

        # 键盘复制快捷键
        self.bind("<Control-c>", self._on_ctrl_c, add="+")
        self.bind("<Control-C>", self._on_ctrl_c, add="+")
        self.bind("<Control-Insert>", self._on_ctrl_c, add="+")

        # 鼠标右键上下文菜单
        self.bind("<Button-3>", self._on_right_click, add="+")
        self.bind("<Button-2>", self._on_right_click, add="+")  # macOS 兼容

        # macOS 般丝滑的滚轮缓动滚动
        self._smooth = SmoothScroll(self)
        self.bind("<MouseWheel>", self._smooth.on_wheel, add="+")
        self.bind("<Button-4>", lambda e: self._smooth.on_wheel_step(-1), add="+")
        self.bind("<Button-5>", lambda e: self._smooth.on_wheel_step(1), add="+")

    # ── 便捷 API ──
    def set_columns(self, columns: list[tuple[str, str, int]]) -> None:
        """列定义：[(key, 标题, 宽度字符数), ...]。"""
        self.configure(columns=[c[0] for c in columns])
        for key, text, w in columns:
            self.heading(key, text=text)
            self.column(key, width=w * 8, anchor="w", stretch=False)
        self._invalidate_edges()

    def autosize(self, columns: list[tuple[str, str, int]]) -> None:
        self.set_columns(columns)

    def insert(self, parent, index, **kw):
        """重写 insert 以自动应用斑马纹（50ms 防抖合批）。"""
        iid = super().insert(parent, index, **kw)
        if self._stripe_after_id is None:
            self._stripe_after_id = self.after(50, self._apply_stripes)
        return iid

    def delete(self, *items):
        super().delete(*items)
        if self._stripe_after_id is None:
            self._stripe_after_id = self.after(50, self._apply_stripes)

    def insert_many(self, rows: list[tuple]) -> list[str]:
        """批量插入：一次性灌入多行后再统一排斑马纹。

        大结果集（排行榜/检索表格）填充时比逐行 insert 快数倍，
        且整个过程只触发一次重排与一次重绘。返回与新行顺序对应的 iid 列表。
        """
        if not rows:
            return []
        iids = [super().insert("", "end", values=values) for values in rows]
        if self._stripe_after_id is None:
            self._stripe_after_id = self.after(50, self._apply_stripes)
        return iids

    def _apply_stripes(self) -> None:
        self._stripe_after_id = None
        try:
            for i, iid in enumerate(self.get_children()):
                tags = list(self.item(iid, "tags") or ())
                want = i % 2 == 1
                has = "stripe" in tags
                if want == has:
                    continue  # 已是目标状态，跳过昂贵的 Tcl 往返
                if want:
                    tags.append("stripe")
                else:
                    tags.remove("stripe")
                self.item(iid, tags=tags)
        except tk.TclError:
            pass

    # ── 复制核心逻辑 ──
    def _copy_text(self, text: str, desc: str = "") -> None:
        """复制指定纯文本到系统剪贴板，并弹出 Toast 反馈。"""
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            clean_preview = text.strip().replace("\n", " ").replace("\t", " ")
            preview = clean_preview if len(clean_preview) <= 24 else (clean_preview[:20] + "...")
            tip = f"✓ 已复制{desc}: {preview}" if preview else "✓ 已复制到剪贴板"
            show_toast(self, tip)
        except Exception:
            pass

    def _copy_rows(self, iids: list[str]) -> None:
        """复制指定行数据，列与列以 Tab 分隔，行与行以换行分隔（极适合粘贴到 Excel / CSV）。"""
        lines: list[str] = []
        for iid in iids:
            try:
                vals = [str(v) for v in (self.item(iid, "values") or ())]
                if vals:
                    lines.append("\t".join(vals))
            except Exception:
                continue
        if lines:
            text = "\n".join(lines)
            cnt = len(lines)
            desc = f" ({cnt}行)" if cnt > 1 else ""
            self._copy_text(text, desc=desc)

    def _copy_column(self, col_idx: int, col_name: str = "") -> None:
        """复制指定列的全部单元格数据。"""
        vals: list[str] = []
        for iid in self.get_children():
            try:
                row_vals = list(self.item(iid, "values") or ())
                if 0 <= col_idx < len(row_vals):
                    vals.append(str(row_vals[col_idx]))
            except Exception:
                continue
        if vals:
            text = "\n".join(vals)
            self._copy_text(text, desc=f"「{col_name}」列全部 ({len(vals)}条)")

    def _on_ctrl_c(self, event=None) -> str:
        """Ctrl+C 键盘快捷键响应：单行优先复制点击的单元格，多行复制整块。"""
        sel = self.selection()
        if not sel:
            return "break"

        # 若只选了一行且有明确点击的单元格：优先复制该单元格内容
        if len(sel) == 1 and self._last_cell and self._last_cell[0] == sel[0]:
            row_id, col_idx = self._last_cell
            try:
                vals = list(self.item(row_id, "values") or ())
                if 0 <= col_idx < len(vals):
                    val = str(vals[col_idx])
                    cols = list(self["columns"])
                    col_name = str(self.heading(cols[col_idx], "text") or "") if col_idx < len(cols) else ""
                    self._copy_text(val, desc=f"「{col_name}」" if col_name else "")
                    return "break"
            except Exception:
                pass

        # 否则复制所有选中的整行
        self._copy_rows(list(sel))
        return "break"

    # ── 右键菜单与跨面板智能联动 ──
    def _on_right_click(self, event) -> str:
        row_id = self.identify_row(event.y)
        col_str = self.identify_column(event.x)
        if not row_id:
            return "break"

        # 如果点击的行不在当前选中范围内，则切换为选中此行
        current_sel = self.selection()
        if row_id not in current_sel:
            self.selection_set(row_id)
            current_sel = (row_id,)

        vals = list(self.item(row_id, "values") or ())
        col_idx = 0
        if col_str:
            try:
                col_idx = max(0, int(col_str.replace("#", "")) - 1)
            except Exception:
                col_idx = 0
        self._last_cell = (row_id, col_idx)

        cols = list(self["columns"])
        col_name = ""
        if 0 <= col_idx < len(cols):
            try:
                col_name = str(self.heading(cols[col_idx], "text") or cols[col_idx])
            except Exception:
                col_name = str(cols[col_idx])

        cell_val = str(vals[col_idx]) if (0 <= col_idx < len(vals)) else ""

        menu = tk.Menu(self, tearoff=0)

        # 1. 复制单元格内容
        if cell_val:
            preview = cell_val if len(cell_val) <= 24 else (cell_val[:20] + "...")
            menu.add_command(
                label=f"📋 复制「{col_name}」: {preview}",
                command=lambda: self._copy_text(cell_val, desc=f"「{col_name}」"),
            )

        # 2. 复制整行
        menu.add_command(
            label=f"📄 复制整行数据 ({len(vals)} 列)",
            command=lambda: self._copy_rows([row_id]),
        )

        # 3. 复制多行（若多选）
        if len(current_sel) > 1:
            menu.add_command(
                label=f"📑 复制选中行 ({len(current_sel)} 行)",
                command=lambda: self._copy_rows(list(current_sel)),
            )

        menu.add_separator()

        # 4. 复制该列全部
        if col_name:
            menu.add_command(
                label=f"📊 复制整列「{col_name}」全部数据",
                command=lambda: self._copy_column(col_idx, col_name),
            )

        # 5. 智能识别与跨模块联动
        m_bv = re.search(r"(BV1[0-9A-Za-z]{9})", cell_val)
        clean_digit = cell_val.strip()
        if m_bv:
            bv = m_bv.group(1)
            menu.add_separator()
            menu.add_command(
                label=f"🎯 填入「定向采集」采集 {bv}",
                command=lambda: self._action_collect(bv),
            )
            menu.add_command(
                label=f"🛡️ 填入「水军检测」分析 {bv}",
                command=lambda: self._action_detect(bv),
            )
        elif clean_digit.isdigit() and int(clean_digit) > 100:
            uid = clean_digit
            menu.add_separator()
            menu.add_command(
                label=f"👤 在「用户查询」中检索 UID: {uid}",
                command=lambda: self._action_user_query(uid),
            )

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

        return "break"

    def _action_collect(self, bvid: str) -> None:
        try:
            top = self.winfo_toplevel()
            switch = getattr(top, "_switch", None)
            if callable(switch):
                switch("定向采集")
                panel = getattr(top, "_get_panel", lambda k: None)("定向采集")
                if panel and hasattr(panel, "var_target"):
                    panel.var_target.set(bvid)
        except Exception:
            pass

    def _action_detect(self, bvid: str) -> None:
        try:
            top = self.winfo_toplevel()
            switch = getattr(top, "_switch", None)
            if callable(switch):
                switch("统计洞察")
                panel = getattr(top, "_get_panel", lambda k: None)("统计洞察")
                if panel and hasattr(panel, "var_detect_bvid"):
                    panel.var_detect_bvid.set(bvid)
                    if hasattr(panel, "_nb"):
                        tabs = panel._nb.tabs()
                        if len(tabs) >= 7:
                            panel._nb.select(tabs[6])  # 切换到水军检测页签
                    panel.refresh_detect()
        except Exception:
            pass

    def _action_user_query(self, uid: str) -> None:
        try:
            top = self.winfo_toplevel()
            switch = getattr(top, "_switch", None)
            if callable(switch):
                switch("用户查询")
                panel = getattr(top, "_get_panel", lambda k: None)("用户查询")
                if panel and hasattr(panel, "var_uid"):
                    panel.var_uid.set(uid)
                    panel._query()
        except Exception:
            pass

    # ── 列宽拖拽与点击跟踪 ──
    def _col_edges(self) -> dict[str, int]:
        cols = list(self["columns"])
        edges: dict[str, int] = {}
        x = 0
        for cid in cols:
            try:
                w = int(self.column(cid, "width") or 0)
            except (tk.TclError, ValueError):
                w = 80
            x += w
            edges[cid] = x
        return edges

    def _on_motion(self, event) -> None:
        if self._drag_col:
            return
        # 缓存列边界：只在列宽实际变化后重算，避免每个鼠标移动事件
        # 都触发一轮全列 Tcl 往返（高频 Motion 下的隐形卡顿源）
        edges = self._edges_cache
        if edges is None:
            edges = self._col_edges()
            self._edges_cache = edges
        near = None
        for cid, x in edges.items():
            if abs(event.x - x) <= _GRIP:
                near = cid
                break
        if near != self._resize_anchor:
            if hasattr(self, "configure"):
                try:
                    self.configure(cursor="sb_h_double_arrow" if near else "")
                except tk.TclError:
                    pass
            self._resize_anchor = near

    def _invalidate_edges(self) -> None:
        self._edges_cache = None

    def _on_press(self, event) -> None:
        if not self._resize_anchor:
            # 记录用户单机点击的单元格位置
            row_id = self.identify_row(event.y)
            col_str = self.identify_column(event.x)
            if row_id and col_str:
                try:
                    col_idx = max(0, int(col_str.replace("#", "")) - 1)
                    self._last_cell = (row_id, col_idx)
                except Exception:
                    self._last_cell = None
            return
        self._drag_col = self._resize_anchor
        self._drag_start_x = event.x
        try:
            self._drag_start_w = int(self.column(self._drag_col, "width") or 0)
        except (tk.TclError, ValueError):
            self._drag_start_w = 80
        return "break"

    def _on_drag(self, event) -> None:
        if not self._drag_col:
            return
        w = max(40, self._drag_start_w + (event.x - self._drag_start_x))
        try:
            self.column(self._drag_col, width=w)
        except tk.TclError:
            pass
        self._invalidate_edges()
        return "break"

    def _on_release(self, event) -> None:
        self._drag_col = None
        self._invalidate_edges()

    def _on_leave(self, event) -> None:
        if not self._drag_col:
            try:
                self.configure(cursor="")
            except tk.TclError:
                pass
            self._resize_anchor = None
