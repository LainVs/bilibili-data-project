"""GUI 全局主题系统 —— Claude 质感与温润美学。

以 Anthropic Claude 标志性的暖白纸质感底色（Warm Paper）、温润陶土红（Terracotta）
与深邃炭灰（Deep Charcoal）为基调，带来富有呼吸感、层次感的现代桌面体验。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class C:
    """Claude 标志性色彩系统。"""

    # ── 标志性主色（Claude Terracotta 赤陶/暖珊瑚） ──
    ACCENT        = "#da7756"   # Claude 标志性陶土红
    ACCENT_HOVER  = "#c26243"   # 悬停深陶土
    ACCENT_ACTIVE = "#ad5337"   # 点击压下
    ACCENT_LIGHT  = "#fbf2ed"   # 极浅柔和陶土粉底（标签/选中态）
    ACCENT_BORDER = "#f1d4c7"   # 陶土边框

    # ── 背景层级（暖白纸感 & 温润石灰） ──
    BG_WINDOW     = "#faf9f5"   # 窗口画布底色（自然暖白纸质）
    BG_MAIN       = "#faf9f5"   # 主内容区底色
    BG_CARD       = "#ffffff"   # 悬浮卡片纯白底
    BG_CARD_MUTED = "#f3f2ee"   # 微凹次级背景（输入框/分段器底色）
    BG_SIDEBAR    = "#1c1b18"   # 侧边栏（温润暖调黑曜石）
    BG_NAV_HOVER  = "#2a2924"   # 侧栏悬停
    BG_NAV_ACTIVE = "#36342e"   # 侧栏选中块
    BG_HOVER      = "#edece6"   # 浅色悬停态
    BG_ACTIVE     = "#e4e2d8"   # 浅色激活态

    # ── 表格配色 ──
    BG_TABLE_ALT  = "#fcfbfa"   # 极浅微暖斑马纹
    BG_TABLE_SEL  = "#fbf2ed"   # 表格选中行（赤陶浅底）
    FG_TABLE_SEL  = "#191816"   # 选中行文字保持深炭黑

    # ── 文字层级（Editorial Deep Charcoal，绝不用死黑） ──
    FG_PRIMARY    = "#191816"   # 主标题/重要正文（深邃暖炭灰）
    FG_SECONDARY  = "#615d56"   # 正文说明/副文本
    FG_MUTED      = "#8c877d"   # 辅助信息/表头标签
    FG_SUBTLE     = "#b5b0a4"   # 弱化占位/分割符
    FG_INVERSE    = "#fdfcfb"   # 反色白文字
    FG_NAV        = "#9c988f"   # 侧栏未激活文字
    FG_NAV_ACTIVE = "#ffffff"   # 侧栏激活文字

    # ── 极细发丝边框 ──
    BORDER        = "#e7e5dc"   # 核心发丝边框（温润淡灰）
    BORDER_LIGHT  = "#f0eee6"   # 超浅分割线
    BORDER_FOCUS  = "#da7756"   # 输入聚焦光圈

    # ── 语义状态色（Claude Pastel 柔和粉彩徽章体系） ──
    SUCCESS       = "#1f7a37"
    WARNING       = "#a36814"
    ERROR         = "#b9382f"
    DOT_RED       = "#da7756"
    FG_HINT       = "#8c877d"

    SUCCESS_BG    = "#edf7ed"
    SUCCESS_FG    = "#1f7a37"
    SUCCESS_DOT   = "#2e9e4f"

    WARNING_BG    = "#fff8e6"
    WARNING_FG    = "#a36814"
    WARNING_DOT   = "#e08d20"

    ERROR_BG      = "#fdf0ef"
    ERROR_FG      = "#b9382f"
    ERROR_DOT     = "#dc3e32"

    INFO_BG       = "#f0f4fd"
    INFO_FG       = "#2858b8"
    INFO_DOT      = "#3d75e6"

    # ── 暖调终端控制台 ──
    TERM_BG       = "#181816"
    TERM_FG       = "#e6e3dc"
    TERM_MUTED    = "#78746c"
    TERM_ACCENT   = "#f28b6d"
    TERM_SUCCESS  = "#4ade80"

    # ── Tooltip ──
    TIP_BG        = "#262420"
    TIP_FG        = "#fbfaf8"


class F:
    """字体规范阶梯。"""
    _UI = "Microsoft YaHei UI"
    _MONO = "Consolas"

    HERO      = (_UI, 20, "bold")   # 指标大数字
    TITLE     = (_UI, 13, "bold")   # 面板大标题
    SUBTITLE  = (_UI, 10, "bold")   # 分区副标 / 强调
    BODY      = (_UI, 9)            # 界面通用正文
    SMALL     = (_UI, 8)            # 说明微字
    NAV       = (_UI, 12, "bold")   # 侧栏图标
    NAV_HINT  = (_UI, 8)            # 侧栏文字
    MONO      = (_MONO, 9)          # 控制台代码
    MONO_SM   = (_MONO, 8)          # 小号等宽
    TABLE_HD  = (_UI, 9, "bold")    # 表头字
    TABLE_BD  = (_UI, 9)            # 表格正文字


class S:
    """间距与度量规范。"""
    PAD_PAGE    = 20
    PAD_CARD    = 16
    PAD_ITEM    = 8
    SIDEBAR_W   = 68
    ROW_HEIGHT  = 30
    BTN_PADX    = 14
    BTN_PADY    = 6


def apply_theme(root: tk.Tk) -> None:
    """初始化并注入 Claude 设计语言到全局 Tk/ttk 组件中。"""
    root.configure(bg=C.BG_WINDOW)

    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass

    # ── 全局根级重置 ──
    st.configure(".", font=F.BODY, background=C.BG_WINDOW,
                 foreground=C.FG_PRIMARY, borderwidth=0,
                 focuscolor=C.ACCENT, selectbackground=C.ACCENT_LIGHT,
                 selectforeground=C.FG_PRIMARY)

    # ── TFrame ──
    st.configure("TFrame", background=C.BG_WINDOW)
    st.configure("Card.TFrame", background=C.BG_CARD)
    st.configure("Muted.TFrame", background=C.BG_CARD_MUTED)

    # ── TLabel ──
    st.configure("TLabel", background=C.BG_WINDOW, foreground=C.FG_PRIMARY, font=F.BODY)
    st.configure("Title.TLabel", font=F.TITLE, foreground=C.FG_PRIMARY, background=C.BG_WINDOW)
    st.configure("Subtitle.TLabel", font=F.SUBTITLE, foreground=C.FG_PRIMARY, background=C.BG_WINDOW)
    st.configure("Hint.TLabel", font=F.SMALL, foreground=C.FG_MUTED, background=C.BG_WINDOW)
    st.configure("Secondary.TLabel", font=F.SMALL, foreground=C.FG_SECONDARY, background=C.BG_WINDOW)
    st.configure("Card.TLabel", background=C.BG_CARD, foreground=C.FG_PRIMARY, font=F.BODY)
    st.configure("CardHint.TLabel", background=C.BG_CARD, foreground=C.FG_MUTED, font=F.SMALL)
    st.configure("CardTitle.TLabel", background=C.BG_CARD, foreground=C.FG_PRIMARY, font=F.SUBTITLE)

    # ── TButton (现代扁平微边框设计) ──
    st.configure("TButton", font=F.BODY, padding=(S.BTN_PADX, S.BTN_PADY),
                 background=C.BG_CARD, foreground=C.FG_PRIMARY,
                 borderwidth=1, relief="flat", bordercolor=C.BORDER)
    st.map("TButton",
           background=[("active", C.BG_HOVER), ("disabled", C.BG_CARD_MUTED)],
           foreground=[("disabled", C.FG_MUTED)],
           bordercolor=[("active", C.BORDER)])

    # 赤陶色主按钮 (Primary Action)
    st.configure("Accent.TButton", font=F.BODY,
                 background=C.ACCENT, foreground=C.FG_INVERSE,
                 padding=(S.BTN_PADX, S.BTN_PADY), borderwidth=0)
    st.map("Accent.TButton",
           background=[("active", C.ACCENT_HOVER), ("disabled", C.BORDER)],
           foreground=[("disabled", C.FG_MUTED)])

    # 危险操作按钮 (Danger)
    st.configure("Danger.TButton", font=F.BODY,
                 background=C.ERROR_BG, foreground=C.ERROR_FG,
                 padding=(S.BTN_PADX, S.BTN_PADY), borderwidth=1, bordercolor=C.ERROR_DOT)
    st.map("Danger.TButton",
           background=[("active", "#fcdcd9"), ("disabled", C.BG_CARD_MUTED)],
           foreground=[("disabled", C.FG_MUTED)])

    # ── TEntry ──
    st.configure("TEntry", font=F.BODY, padding=(8, 6),
                 fieldbackground=C.BG_CARD, foreground=C.FG_PRIMARY,
                 borderwidth=1, relief="flat")
    st.map("TEntry",
           fieldbackground=[("focus", C.BG_CARD), ("readonly", C.BG_CARD_MUTED)],
           bordercolor=[("focus", C.BORDER_FOCUS)],
           lightcolor=[("focus", C.BORDER_FOCUS)])

    # ── TCombobox ──
    st.configure("TCombobox", font=F.BODY, padding=(8, 4),
                 fieldbackground=C.BG_CARD, foreground=C.FG_PRIMARY,
                 arrowsize=14, borderwidth=1)
    st.map("TCombobox",
           fieldbackground=[("readonly", C.BG_CARD), ("focus", C.BG_CARD)],
           selectbackground=[("readonly", C.BG_CARD)],
           selectforeground=[("readonly", C.FG_PRIMARY)])

    # ── TSpinbox ──
    st.configure("TSpinbox", font=F.BODY, padding=(6, 4),
                 fieldbackground=C.BG_CARD, arrowsize=13, borderwidth=1)

    # ── TCheckbutton & TRadiobutton ──
    st.configure("TCheckbutton", font=F.BODY, background=C.BG_WINDOW,
                 foreground=C.FG_PRIMARY)
    st.map("TCheckbutton",
           background=[("active", C.BG_WINDOW)],
           indicatorcolor=[("selected", C.ACCENT), ("!selected", C.BG_CARD_MUTED)])

    st.configure("TRadiobutton", font=F.BODY, background=C.BG_WINDOW,
                 foreground=C.FG_PRIMARY)
    st.map("TRadiobutton",
           background=[("active", C.BG_WINDOW)],
           indicatorcolor=[("selected", C.ACCENT), ("!selected", C.BG_CARD_MUTED)])

    # ── TLabelframe (优雅去框化，仅作浅卡片容器) ──
    st.configure("TLabelframe", background=C.BG_CARD, foreground=C.FG_PRIMARY,
                 borderwidth=1, relief="flat", bordercolor=C.BORDER, padding=S.PAD_CARD)
    st.configure("TLabelframe.Label", font=F.SUBTITLE, background=C.BG_CARD,
                 foreground=C.FG_SECONDARY)

    # ── SmartTable (Treeview 优雅表格) ──
    st.configure("Treeview", font=F.TABLE_BD, rowheight=S.ROW_HEIGHT,
                 background=C.BG_CARD, foreground=C.FG_PRIMARY,
                 fieldbackground=C.BG_CARD, borderwidth=0)
    st.configure("Treeview.Heading", font=F.TABLE_HD,
                 background=C.BG_CARD_MUTED, foreground=C.FG_MUTED,
                 relief="flat", padding=(10, 8), borderwidth=0)
    st.map("Treeview",
           background=[("selected", C.BG_TABLE_SEL)],
           foreground=[("selected", C.FG_PRIMARY)])
    st.map("Treeview.Heading",
           background=[("active", C.BG_HOVER)])

    # ── TNotebook (药丸切换) ──
    st.configure("TNotebook", background=C.BG_WINDOW, borderwidth=0, padding=0)
    st.configure("TNotebook.Tab", font=F.BODY,
                 padding=(16, 7), background=C.BG_WINDOW,
                 foreground=C.FG_SECONDARY, borderwidth=0)
    st.map("TNotebook.Tab",
           background=[("selected", C.BG_CARD), ("active", C.BG_CARD_MUTED)],
           foreground=[("selected", C.ACCENT)])

    # ── TProgressbar ──
    st.configure("TProgressbar", background=C.ACCENT,
                 troughcolor=C.BG_CARD_MUTED, borderwidth=0,
                 thickness=5)

    # ── TSeparator ──
    st.configure("TSeparator", background=C.BORDER)

    # ── 原生组件全局选项 ──
    root.option_add("*Menu.font", F.BODY)
    root.option_add("*Menu.background", C.BG_CARD)
    root.option_add("*Menu.foreground", C.FG_PRIMARY)
    root.option_add("*Menu.activeBackground", C.ACCENT_LIGHT)
    root.option_add("*Menu.activeForeground", C.ACCENT)
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*Menu.relief", "flat")

    # tk.Text 全局温润优雅默认值
    root.option_add("*Text.font", F.MONO)
    root.option_add("*Text.background", C.BG_CARD)
    root.option_add("*Text.foreground", C.FG_PRIMARY)
    root.option_add("*Text.relief", "flat")
    root.option_add("*Text.borderWidth", 0)
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightColor", C.BORDER)
    root.option_add("*Text.highlightBackgroundColor", C.BORDER)
    root.option_add("*Text.selectBackground", C.ACCENT_LIGHT)
    root.option_add("*Text.selectForeground", C.FG_PRIMARY)
    root.option_add("*Text.insertBackground", C.ACCENT)
