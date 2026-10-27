"""Claude 质感设计系统组件库。

提供高审美、呼吸感、现代化的 UI 原语：
- ClaudeCard: 白色呼吸感卡片容器（细边框、可选标题栏、右侧操作区）
- StatCard: 数据大指标卡（数字大字阶、副标、徽标）
- ClaudeBadge: 优雅药丸微徽章（绿/琥珀/红/蓝/赤陶，含状态微圆点）
- PillTabs / SegmentedControl: 药丸分段选择器（替代粗糙的 Radiobutton 和传统 Notebook）
- ModernConsole: 优雅黑曜石暖调终端/日志控制台（语法高亮微标签、防卡死平滑滚动）
- ClaudeButton: 质感按钮辅助
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk

from gui.theme import C, F
from gui.widgets.anim import fade_canvas_item


class ClaudeBadge(tk.Frame):
    """优雅药丸状态徽章：● 运行中 / 离线 / 待机。"""

    THEMES = {
        "success": (C.SUCCESS_BG, C.SUCCESS_FG, C.SUCCESS_DOT),
        "warning": (C.WARNING_BG, C.WARNING_FG, C.WARNING_DOT),
        "error":   (C.ERROR_BG,   C.ERROR_FG,   C.ERROR_DOT),
        "info":    (C.INFO_BG,    C.INFO_FG,    C.INFO_DOT),
        "accent":  (C.ACCENT_LIGHT, C.ACCENT,   C.ACCENT),
        "muted":   (C.BG_CARD_MUTED, C.FG_MUTED, C.FG_MUTED),
    }

    def __init__(self, master: tk.Misc, text: str = "", status: str = "success", **kw):
        bg, fg, dot_c = self.THEMES.get(status, self.THEMES["info"])
        super().__init__(master, bg=bg, padx=8, pady=3, **kw)
        self._status = status
        self._dot_c = dot_c

        # 小圆点
        self._canvas = tk.Canvas(self, width=8, height=8, bg=bg, highlightthickness=0, bd=0)
        self._canvas.pack(side="left", padx=(0, 4))
        self._dot_id = self._canvas.create_oval(1, 1, 7, 7, fill=dot_c, outline="")

        # 文字
        self._lbl = tk.Label(self, text=text, font=F.SMALL, bg=bg, fg=fg)
        self._lbl.pack(side="left")

    def set_status(self, text: str, status: str = "success") -> None:
        bg, fg, dot_c = self.THEMES.get(status, self.THEMES["info"])
        self.configure(bg=bg)
        self._canvas.configure(bg=bg)
        self._canvas.itemconfig(self._dot_id, fill=dot_c)
        self._lbl.configure(text=text, bg=bg, fg=fg)


class ClaudeCard(tk.Frame):
    """呼吸感现代卡片：纯白背景、温润 1px 细边框、卡片头与内容区。"""

    def __init__(
        self,
        master: tk.Misc,
        title: str = "",
        subtitle: str = "",
        padding: int = 14,
        bg: str = C.BG_CARD,
        **kw
    ):
        super().__init__(
            master,
            bg=bg,
            highlightthickness=1,
            highlightbackground=C.BORDER,
            highlightcolor=C.BORDER,
            bd=0,
            padx=padding,
            pady=padding,
            **kw
        )
        self._bg = bg
        self._header = None
        self._lbl_title = None
        self._lbl_sub = None
        self._header_right = None

        if title or subtitle:
            self._build_header(title, subtitle)

        # 核心：提供独立的 body 容器，内部无论用 pack 还是 grid 绝不与 header 冲突！
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(fill="both", expand=True)

    def columnconfigure(self, index, **kw):
        if hasattr(self, "body"):
            return self.body.columnconfigure(index, **kw)
        return super().columnconfigure(index, **kw)

    def rowconfigure(self, index, **kw):
        if hasattr(self, "body"):
            return self.body.rowconfigure(index, **kw)
        return super().rowconfigure(index, **kw)

    def _build_header(self, title: str, subtitle: str) -> None:
        self._header = tk.Frame(self, bg=self._bg)
        self._header.pack(fill="x", pady=(0, 12))

        left = tk.Frame(self._header, bg=self._bg)
        left.pack(side="left", fill="y")

        if title:
            self._lbl_title = tk.Label(
                left, text=title, font=F.SUBTITLE, fg=C.FG_PRIMARY, bg=self._bg, anchor="w"
            )
            self._lbl_title.pack(anchor="w")

        if subtitle:
            self._lbl_sub = tk.Label(
                left, text=subtitle, font=F.SMALL, fg=C.FG_MUTED, bg=self._bg, anchor="w"
            )
            self._lbl_sub.pack(anchor="w", pady=(2, 0))

        self._header_right = tk.Frame(self._header, bg=self._bg)
        self._header_right.pack(side="right", fill="y")

    @property
    def header_right(self) -> tk.Frame | None:
        return self._header_right

    def update_header(self, title: str = "", subtitle: str = "") -> None:
        if self._lbl_title and title:
            self._lbl_title.configure(text=title)
        if self._lbl_sub and subtitle:
            self._lbl_sub.configure(text=subtitle)


class StatCard(tk.Frame):
    """指标卡片：小字标签 + 醒目大号数值 + 辅助说明或徽章。"""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        value: str = "--",
        subtext: str = "",
        badge_text: str = "",
        badge_status: str = "success",
        width: int = 200,
        **kw
    ):
        super().__init__(
            master,
            bg=C.BG_CARD,
            highlightthickness=1,
            highlightbackground=C.BORDER,
            bd=0,
            padx=16,
            pady=14,
            width=width,
            **kw
        )
        self.pack_propagate(False)

        top_row = tk.Frame(self, bg=C.BG_CARD)
        top_row.pack(fill="x")

        self.lbl_title = tk.Label(
            top_row, text=title.upper(), font=F.NAV_HINT, fg=C.FG_MUTED, bg=C.BG_CARD, anchor="w"
        )
        self.lbl_title.pack(side="left")

        self.badge = None
        if badge_text:
            self.badge = ClaudeBadge(top_row, text=badge_text, status=badge_status)
            self.badge.pack(side="right")

        self.lbl_val = tk.Label(
            self, text=value, font=F.HERO, fg=C.FG_PRIMARY, bg=C.BG_CARD, anchor="w"
        )
        self.lbl_val.pack(anchor="w", pady=(6, 2))

        self.lbl_sub = tk.Label(
            self, text=subtext, font=F.SMALL, fg=C.FG_MUTED, bg=C.BG_CARD, anchor="w"
        )
        self.lbl_sub.pack(anchor="w")

    def update_val(self, value: str, subtext: str | None = None) -> None:
        self.lbl_val.configure(text=value)
        if subtext is not None:
            self.lbl_sub.configure(text=subtext)


class PillTabs(tk.Frame):
    """药丸分段选择器（Segmented Control）：
    优雅平滑的胶囊外观，纯白/赤陶色激活态，极大提升设计质感。
    """

    def __init__(
        self,
        master: tk.Misc,
        tabs: Sequence[tuple[str, str]],  # [(key, label), ...]
        initial_key: str | None = None,
        on_change: Callable[[str], None] | None = None,
        bg: str = C.BG_CARD_MUTED,
        **kw
    ):
        super().__init__(master, bg=bg, padx=3, pady=3, **kw)
        self._tabs = tabs
        self._on_change = on_change
        self._current_key = initial_key or (tabs[0][0] if tabs else "")
        self._buttons: dict[str, tk.Canvas] = {}

        self._build_tabs(bg)

    def _build_tabs(self, container_bg: str) -> None:
        for _i, (key, label) in enumerate(self._tabs):
            c = tk.Canvas(
                self,
                height=28,
                bg=container_bg,
                highlightthickness=0,
                bd=0,
                cursor="hand2"
            )
            c.pack(side="left", padx=2)
            c._key = key
            c._label = label
            c._active = (key == self._current_key)

            # 计算文字宽度动态确定 canvas 尺寸
            text_w = max(40, len(label) * 14 + 16)
            c.configure(width=text_w)

            # 绘制药丸背景矩形
            c._bg_rect = c.create_rectangle(
                0, 0, text_w, 28,
                fill=C.BG_CARD if c._active else container_bg,
                outline=C.BORDER if c._active else "",
                width=1 if c._active else 0
            )
            # 文字
            c._text_id = c.create_text(
                text_w // 2, 14,
                text=label,
                font=F.BODY,
                fill=C.FG_PRIMARY if c._active else C.FG_SECONDARY
            )

            c.bind("<Enter>", lambda e, cv=c: self._on_hover(cv, True))
            c.bind("<Leave>", lambda e, cv=c: self._on_hover(cv, False))
            c.bind("<ButtonRelease-1>", lambda e, cv=c: self._select(cv._key))
            self._buttons[key] = c

    def _on_hover(self, cv: tk.Canvas, enter: bool) -> None:
        if cv._active:
            return
        # 药丸悬停颜色缓动：微过渡替代生硬跳变
        fade_canvas_item(cv, cv._bg_rect, C.BG_HOVER if enter else self.cget("bg"))
        cv.itemconfig(cv._text_id, fill=C.FG_PRIMARY if enter else C.FG_SECONDARY)

    def _select(self, key: str) -> None:
        if key == self._current_key:
            return
        self._current_key = key
        for k, cv in self._buttons.items():
            is_act = (k == key)
            cv._active = is_act
            cv.itemconfig(
                cv._bg_rect,
                fill=C.BG_CARD if is_act else self.cget("bg"),
                outline=C.BORDER if is_act else "",
                width=1 if is_act else 0
            )
            cv.itemconfig(
                cv._text_id,
                fill=C.ACCENT if is_act else C.FG_SECONDARY,
                font=F.SUBTITLE if is_act else F.BODY
            )
        if self._on_change:
            self._on_change(key)

    def get(self) -> str:
        return self._current_key

    def set(self, key: str) -> None:
        self._select(key)


class ModernConsole(tk.Frame):
    """暖调黑曜石控制台终端：深色背景、平滑流式输出、语法高亮微标签。"""

    def __init__(self, master: tk.Misc, height: int = 12, **kw):
        super().__init__(
            master,
            bg=C.TERM_BG,
            highlightthickness=1,
            highlightbackground=C.BORDER,
            bd=0,
            **kw
        )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.txt = tk.Text(
            self,
            height=height,
            wrap="none",
            state="disabled",
            bg=C.TERM_BG,
            fg=C.TERM_FG,
            font=F.MONO,
            relief="flat",
            bd=0,
            padx=12,
            pady=10,
            insertbackground=C.ACCENT,
            selectbackground="#33312b",
            selectforeground=C.FG_INVERSE,
        )
        self.txt.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(self, orient="vertical", command=self.txt.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=sb.set)
        # 注意：Text 大文本每帧全量重排，不做动画滚动（与实时流面板同理）

        # 语法标签
        self.txt.tag_configure("time", foreground=C.TERM_MUTED)
        self.txt.tag_configure("info", foreground=C.TERM_FG)
        self.txt.tag_configure("success", foreground=C.TERM_SUCCESS)
        self.txt.tag_configure("warn", foreground=C.WARNING_DOT)
        self.txt.tag_configure("error", foreground=C.ERROR_DOT)
        self.txt.tag_configure("accent", foreground=C.TERM_ACCENT)

    def append(self, text: str, tag: str = "info") -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def clear(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
