"""列表/按钮悬停提示（Obsidian 风格窄栏的说明气泡）。"""
from __future__ import annotations

import tkinter as tk


class ToolTip:
    """轻量 tooltip：mouseEnter 显示、leave 隐藏，自动跟随窗口移动。"""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, _event=None) -> None:
        if self.tip is not None:
            return
        x, y, _, _ = self.widget.bbox("insert") if getattr(self.widget, "bbox", None) \
            else (0, 20, 0, 0)
        try:
            wx = self.widget.winfo_rootx() + x + 18
            wy = self.widget.winfo_rooty() + y + 24
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{wx}+{wy}")
        lbl = tk.Label(self.tip, text=self.text, justify="left",
                       background="#2b2b2b", foreground="#eee",
                       font=("Microsoft YaHei UI", 9), padx=8, pady=4)
        lbl.pack()
        self.tip.attributes("-topmost", True)

    def hide(self, _event=None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None