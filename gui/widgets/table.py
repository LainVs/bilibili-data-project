"""统一表格控件：冻结表头 + 鼠标拖拽调整列宽 + 统一外观。

树形表头天然固定（不随内容滚动）；此处额外提供：
    * 表头分割线上可拖拽改列宽（跨主题可靠实现，非依赖平台行为）
    * 统一样式（表头/斑马纹/选中色，跟随 ttk 主题）
    * 便捷建列 API：set_columns(cols) / assign_dummy() 等
现有页面用 `.insert/.delete/.item/.heading/.column/.bind` 的代码无需改动，
因为它就是  ttk.Treeview 的子类（drop-in 替换）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

_GRIP = 5  # 表头分割线附近多少像素内判定为"可拖"


class SmartTable(ttk.Treeview):
    """可拖拽列宽 + 冻结表头的 Treeview 子类。"""

    def __init__(self, master, *, columns=(), show="headings", **kw):
        super().__init__(master, columns=columns, show=show, **kw)
        self._resize_anchor: str | None = None  # 当前 hover 的分隔线所在列
        self._drag_col: str | None = None       # 正在拖拽的列
        self._drag_start_x: int = 0
        self._drag_start_w: int = 0
        self._style()
        # 事件绑定（add=以不覆盖页面已有的绑定）
        self.bind("<Motion>", self._on_motion, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<B1-Motion>", self._on_drag, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.bind("<Leave>", self._on_leave, add="+")

    # ---------------- 样式 ----------------
    def _style(self) -> None:
        """一次性注册全局表格外观（Headings 底色/分隔线/选中色），幂等。"""
        st = ttk.Style(self)
        try:
            st.theme_use(st.theme_use())  # 确认当前主题已加载
        except tk.TclError:
            pass
        try:
            st.configure("Smart.Treeview", rowheight=24)
            st.configure(
                "Smart.Treeview.Heading",
                padding=(6, 4),
                relief="flat",
                font="TkDefaultFont",
            )
            # 选中行颜色随平台主题，仅确保非默认也有高可见性
            bg = getattr(st, "lookup", None) and st.lookup("TSelection", "background")
            if bg:
                st.map("Smart.Treeview", background=[("selected", bg)],
                       foreground=[("selected", "#ffffff")])
        except tk.TclError:
            pass

    # ---------------- 便捷 API ----------------
    def set_columns(self, columns: list[tuple[str, str, int]]) -> None:
        """列定义：[(key, 标题, 宽度字符数), ...]。
        自动设置 headings + column 配置（左对齐，宽度 = 字符数 * 7）。"""
        self.configure(columns=[c[0] for c in columns])
        for key, text, w in columns:
            self.heading(key, text=text)
            self.column(key, width=w * 7, anchor="w", stretch=False)

    def autosize(self, columns: list[tuple[str, str, int]]) -> None:
        self.set_columns(columns)

    # ---------------- 列宽拖拽 ----------------
    def _col_edges(self) -> dict[str, int]:
        """返回 {列key: 该列右边界x坐标}，按当前列宽顺序推算。"""
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
            return  # 正在拖，由 _on_drag 处理
        near = None
        for cid, x in self._col_edges().items():
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

    def _on_press(self, event) -> None:
        if not self._resize_anchor:
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
        return "break"

    def _on_release(self, event) -> None:
        self._drag_col = None

    def _on_leave(self, event) -> None:
        if not self._drag_col:
            try:
                self.configure(cursor="")
            except tk.TclError:
                pass
            self._resize_anchor = None