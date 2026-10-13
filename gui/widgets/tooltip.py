"""轻量 tooltip 气泡 —— 跟随主题色。"""
from __future__ import annotations

import tkinter as tk

from gui.theme import C, F


class ToolTip:
    """mouseEnter 显示、leave 隐藏，自动跟随窗口位置。"""

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
        try:
            wx = self.widget.winfo_rootx() + 18
            wy = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{wx}+{wy}")
        lbl = tk.Label(self.tip, text=self.text, justify="left",
                       background=C.TIP_BG, foreground=C.TIP_FG,
                       font=F.SMALL, padx=10, pady=6)
        lbl.pack()
        self.tip.attributes("-topmost", True)

    def hide(self, _event=None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
