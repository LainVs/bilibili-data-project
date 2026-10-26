"""轻量 tooltip 气泡 —— 跟随主题色，悬停延迟淡入（macOS 质感）。"""
from __future__ import annotations

import tkinter as tk

from gui.theme import C, F

_SHOW_DELAY_MS = 420   # 悬停停留后才出现，避免划过侧栏时气泡乱闪
_FADE_MS = 90          # 淡入时长


class ToolTip:
    """mouseEnter 延迟显示、leave 隐藏，自动跟随窗口位置，淡入过渡。"""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tip: tk.Toplevel | None = None
        self._job: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._job = self.widget.after(_SHOW_DELAY_MS, self.show)
        except Exception:
            pass

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def show(self, _event=None) -> None:
        self._cancel()
        if self.tip is not None:
            return
        try:
            wx = self.widget.winfo_rootx() + 18
            wy = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except tk.TclError:
            return
        try:
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{wx}+{wy}")
            lbl = tk.Label(self.tip, text=self.text, justify="left",
                           background=C.TIP_BG, foreground=C.TIP_FG,
                           font=F.SMALL, padx=10, pady=6)
            lbl.pack()
            self.tip.attributes("-topmost", True)
            # 淡入：Windows 支持 -alpha；失败则静默保持原样
            try:
                self.tip.attributes("-alpha", 0.0)
                steps = max(2, _FADE_MS // 16)
                state = {"i": 0}

                def _fade() -> None:
                    if self.tip is None:
                        return
                    state["i"] += 1
                    a = min(1.0, state["i"] / steps)
                    try:
                        self.tip.attributes("-alpha", a)
                    except tk.TclError:
                        return
                    if a < 1.0:
                        self.tip.after(16, _fade)

                self.tip.after(16, _fade)
            except tk.TclError:
                pass
        except tk.TclError:
            self.tip = None

    def hide(self, _event=None) -> None:
        self._cancel()
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None
