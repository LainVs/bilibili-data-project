"""GUI 全局主题系统 —— 简约现代风。

统一管理配色、字体、间距、ttk 样式，
所有面板共享同一套视觉语言，只需在启动时调用 apply_theme(root) 即可。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ─── 配色 ───────────────────────────────────────────────
# 以「石墨 + 雪白 + 一抹蓝」为基调；极简但不失层次。
class C:
    """Color palette."""
    # 背景层级（从深到浅）
    BG_SIDEBAR  = "#1e1e2e"    # 侧边栏深底
    BG_NAV_HOVER = "#2a2a3d"   # 侧栏按钮 hover
    BG_NAV_ACTIVE = "#363650"  # 侧栏按钮选中
    BG_MAIN     = "#fafbfc"    # 主内容区底
    BG_CARD     = "#ffffff"    # 卡片/分组框背景
    BG_INPUT    = "#f4f5f7"    # 输入框底色
    BG_TABLE_ALT = "#f8f9fb"   # 表格斑马纹
    BG_TABLE_SEL = "#dbeafe"   # 表格选中行

    # 前景
    FG_PRIMARY   = "#1a1a2e"   # 主文字
    FG_SECONDARY = "#6b7280"   # 辅助文字
    FG_HINT      = "#9ca3af"   # 提示/占位
    FG_INVERSE   = "#e2e8f0"   # 深底上的文字
    FG_NAV       = "#94a3b8"   # 侧栏未选中文字
    FG_NAV_ACTIVE = "#ffffff"  # 侧栏选中文字

    # 功能色
    ACCENT       = "#3b82f6"   # 主按钮 / 选中态
    ACCENT_HOVER = "#2563eb"   # 主按钮 hover
    ACCENT_LIGHT = "#dbeafe"   # 浅蓝底（tag / badge）
    SUCCESS      = "#22c55e"
    WARNING      = "#f59e0b"
    ERROR        = "#ef4444"

    # 边框
    BORDER       = "#e5e7eb"
    BORDER_FOCUS = "#93c5fd"

    # 通知红点
    DOT_RED      = "#ef4444"

    # Tooltip
    TIP_BG       = "#1e293b"
    TIP_FG       = "#f1f5f9"


# ─── 字体 ───────────────────────────────────────────────
class F:
    """Font presets (family, size, weight)."""
    _UI = "Microsoft YaHei UI"
    _MONO = "Consolas"

    NAV       = (_UI, 11, "bold")          # 侧栏按钮
    NAV_HINT  = (_UI, 8)                   # 侧栏标签文字
    TITLE     = (_UI, 13, "bold")          # 页面标题
    SUBTITLE  = (_UI, 11, "bold")          # 分区小标题
    BODY      = (_UI, 10)                  # 正文
    SMALL     = (_UI, 9)                   # 小字 / 提示
    TINY      = (_UI, 8)                   # 极小提示
    MONO      = (_MONO, 10)                # 日志 / 代码
    MONO_SM   = (_MONO, 9)                 # 小号等宽
    TABLE_HD  = (_UI, 9, "bold")           # 表头
    TABLE_BD  = (_UI, 9)                   # 表格正文


# ─── 间距 ───────────────────────────────────────────────
class S:
    """Spacing / sizing constants."""
    PAD_PAGE   = 20        # 面板四周内边距
    PAD_CARD   = 12        # 卡片内边距
    PAD_ITEM   = 8         # 控件间隙
    PAD_SECTION = 16       # 小节间距
    SIDEBAR_W  = 64        # 侧边栏宽度
    RADIUS     = 8         # 圆角（仅记录意图；Tkinter 无 CSS 圆角，用 relief 替代）
    ROW_HEIGHT = 28        # 表格行高
    BTN_PADX   = 16        # 按钮水平内边距
    BTN_PADY   = 6         # 按钮垂直内边距


# ─── ttk.Style 注册 ─────────────────────────────────────

def apply_theme(root: tk.Tk) -> None:
    """在 root 上一次性注册并激活全局 ttk 样式。调用后立即生效。"""
    root.configure(bg=C.BG_MAIN)

    st = ttk.Style(root)
    # 使用 clam 主题作为基底（最容易自定义的跨平台主题）
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass

    # ── 全局默认 ──
    st.configure(".", font=F.BODY, background=C.BG_MAIN,
                 foreground=C.FG_PRIMARY, borderwidth=0,
                 focuscolor=C.ACCENT, selectbackground=C.ACCENT,
                 selectforeground="#ffffff")

    # ── TFrame ──
    st.configure("TFrame", background=C.BG_MAIN)
    st.configure("Card.TFrame", background=C.BG_CARD, relief="flat")
    st.configure("Sidebar.TFrame", background=C.BG_SIDEBAR)

    # ── TLabel ──
    st.configure("TLabel", background=C.BG_MAIN, foreground=C.FG_PRIMARY, font=F.BODY)
    st.configure("Title.TLabel", font=F.TITLE, foreground=C.FG_PRIMARY, background=C.BG_MAIN)
    st.configure("Subtitle.TLabel", font=F.SUBTITLE, foreground=C.FG_PRIMARY, background=C.BG_MAIN)
    st.configure("Hint.TLabel", font=F.SMALL, foreground=C.FG_HINT, background=C.BG_MAIN)
    st.configure("Secondary.TLabel", font=F.SMALL, foreground=C.FG_SECONDARY, background=C.BG_MAIN)
    st.configure("Success.TLabel", foreground=C.SUCCESS, background=C.BG_MAIN)
    st.configure("Error.TLabel", foreground=C.ERROR, background=C.BG_MAIN)
    st.configure("Card.TLabel", background=C.BG_CARD, foreground=C.FG_PRIMARY)
    st.configure("CardHint.TLabel", background=C.BG_CARD, foreground=C.FG_HINT, font=F.SMALL)
    st.configure("CardTitle.TLabel", background=C.BG_CARD, foreground=C.FG_PRIMARY, font=F.SUBTITLE)
    st.configure("CardValue.TLabel", background=C.BG_CARD, foreground=C.ACCENT, font=F.TITLE)

    # ── TButton ──
    st.configure("TButton", font=F.BODY, padding=(S.BTN_PADX, S.BTN_PADY),
                 background=C.BG_INPUT, foreground=C.FG_PRIMARY,
                 borderwidth=1, relief="flat")
    st.map("TButton",
           background=[("active", C.BORDER), ("disabled", C.BG_INPUT)],
           foreground=[("disabled", C.FG_HINT)],
           relief=[("pressed", "sunken")])

    # 强调按钮
    st.configure("Accent.TButton", font=F.BODY,
                 background=C.ACCENT, foreground="#ffffff",
                 padding=(S.BTN_PADX, S.BTN_PADY))
    st.map("Accent.TButton",
           background=[("active", C.ACCENT_HOVER), ("disabled", C.BORDER)],
           foreground=[("disabled", C.FG_HINT)])

    # 危险按钮
    st.configure("Danger.TButton",
                 background=C.ERROR, foreground="#ffffff",
                 padding=(S.BTN_PADX, S.BTN_PADY))
    st.map("Danger.TButton",
           background=[("active", "#dc2626"), ("disabled", C.BORDER)],
           foreground=[("disabled", C.FG_HINT)])

    # 小号按钮
    st.configure("Small.TButton", font=F.SMALL,
                 padding=(10, 4), background=C.BG_INPUT)
    st.map("Small.TButton",
           background=[("active", C.BORDER), ("disabled", C.BG_INPUT)])

    # ── TEntry ──
    st.configure("TEntry", font=F.BODY, padding=(8, 6),
                 fieldbackground=C.BG_INPUT, foreground=C.FG_PRIMARY,
                 borderwidth=1, relief="flat")
    st.map("TEntry",
           fieldbackground=[("focus", "#ffffff"), ("readonly", C.BG_INPUT)],
           bordercolor=[("focus", C.BORDER_FOCUS)],
           lightcolor=[("focus", C.BORDER_FOCUS)])

    # ── TCombobox ──
    st.configure("TCombobox", font=F.BODY, padding=(8, 4),
                 fieldbackground=C.BG_INPUT, foreground=C.FG_PRIMARY,
                 arrowsize=14)
    st.map("TCombobox",
           fieldbackground=[("readonly", C.BG_INPUT), ("focus", "#ffffff")],
           selectbackground=[("readonly", C.BG_INPUT)],
           selectforeground=[("readonly", C.FG_PRIMARY)])

    # ── TSpinbox ──
    st.configure("TSpinbox", font=F.BODY, padding=(8, 4),
                 fieldbackground=C.BG_INPUT, arrowsize=14)
    st.map("TSpinbox",
           fieldbackground=[("focus", "#ffffff")])

    # ── TCheckbutton / TRadiobutton ──
    st.configure("TCheckbutton", font=F.BODY, background=C.BG_MAIN,
                 foreground=C.FG_PRIMARY, indicatorsize=16)
    st.map("TCheckbutton",
           background=[("active", C.BG_MAIN)],
           indicatorcolor=[("selected", C.ACCENT), ("!selected", C.BG_INPUT)])
    st.configure("TRadiobutton", font=F.BODY, background=C.BG_MAIN,
                 foreground=C.FG_PRIMARY)
    st.map("TRadiobutton",
           background=[("active", C.BG_MAIN)],
           indicatorcolor=[("selected", C.ACCENT), ("!selected", C.BG_INPUT)])

    # Card 内的 checks/radios
    st.configure("Card.TCheckbutton", background=C.BG_CARD)
    st.map("Card.TCheckbutton", background=[("active", C.BG_CARD)])
    st.configure("Card.TRadiobutton", background=C.BG_CARD)
    st.map("Card.TRadiobutton", background=[("active", C.BG_CARD)])

    # ── TLabelframe ──
    st.configure("TLabelframe", background=C.BG_CARD, foreground=C.FG_PRIMARY,
                 borderwidth=1, relief="flat", padding=S.PAD_CARD)
    st.configure("TLabelframe.Label", font=F.SUBTITLE, background=C.BG_CARD,
                 foreground=C.FG_SECONDARY)

    # ── Treeview（表格） ──
    st.configure("Treeview", font=F.TABLE_BD, rowheight=S.ROW_HEIGHT,
                 background=C.BG_CARD, foreground=C.FG_PRIMARY,
                 fieldbackground=C.BG_CARD, borderwidth=0)
    st.configure("Treeview.Heading", font=F.TABLE_HD,
                 background=C.BG_INPUT, foreground=C.FG_SECONDARY,
                 relief="flat", padding=(8, 6), borderwidth=0)
    st.map("Treeview",
           background=[("selected", C.BG_TABLE_SEL)],
           foreground=[("selected", C.FG_PRIMARY)])
    st.map("Treeview.Heading",
           background=[("active", C.BORDER)])

    # ── TNotebook ──
    st.configure("TNotebook", background=C.BG_MAIN, borderwidth=0, padding=0)
    st.configure("TNotebook.Tab", font=F.BODY,
                 padding=(16, 8), background=C.BG_MAIN,
                 foreground=C.FG_SECONDARY, borderwidth=0)
    st.map("TNotebook.Tab",
           background=[("selected", C.BG_CARD), ("active", C.BG_INPUT)],
           foreground=[("selected", C.ACCENT)],
           expand=[("selected", (0, 0, 0, 2))])

    # ── TProgressbar ──
    st.configure("TProgressbar", background=C.ACCENT,
                 troughcolor=C.BG_INPUT, borderwidth=0,
                 thickness=6)

    # ── TSeparator ──
    st.configure("TSeparator", background=C.BORDER)

    # ── TScrollbar ──
    st.configure("TScrollbar", background=C.BG_INPUT,
                 troughcolor=C.BG_MAIN, borderwidth=0,
                 arrowsize=12)
    st.map("TScrollbar",
           background=[("active", C.BORDER), ("disabled", C.BG_INPUT)])

    # ── TPanedwindow ──
    st.configure("TPanedwindow", background=C.BG_MAIN)
    st.configure("Sash", sashthickness=4, gripcount=0)

    # ── Menu fallback（原生菜单只能改有限属性） ──
    root.option_add("*Menu.font", F.BODY)
    root.option_add("*Menu.background", C.BG_CARD)
    root.option_add("*Menu.foreground", C.FG_PRIMARY)
    root.option_add("*Menu.activeBackground", C.ACCENT)
    root.option_add("*Menu.activeForeground", "#ffffff")
    root.option_add("*Menu.borderWidth", 1)
    root.option_add("*Menu.relief", "flat")
    root.option_add("*Toplevel.background", C.BG_MAIN)

    # tk.Text 默认外观（各面板自行创建的 tk.Text 将在 theme 之后生效）
    root.option_add("*Text.font", F.MONO)
    root.option_add("*Text.background", C.BG_CARD)
    root.option_add("*Text.foreground", C.FG_PRIMARY)
    root.option_add("*Text.relief", "flat")
    root.option_add("*Text.borderWidth", 0)
    root.option_add("*Text.highlightThickness", 1)
    root.option_add("*Text.highlightColor", C.BORDER_FOCUS)
    root.option_add("*Text.highlightBackgroundColor", C.BORDER)
    root.option_add("*Text.selectBackground", C.ACCENT_LIGHT)
    root.option_add("*Text.selectForeground", C.FG_PRIMARY)
    root.option_add("*Text.insertBackground", C.ACCENT)

    # ---- 注册辅助函数 ----
    _patch_text_defaults(root)


def _patch_text_defaults(root: tk.Tk) -> None:
    """对 tk.Text 进行 monkey-patch，使新建实例统一应用主题色。"""
    _orig_init = tk.Text.__init__

    def _themed_init(self, master=None, **kw):
        kw.setdefault("font", F.MONO)
        kw.setdefault("bg", C.BG_CARD)
        kw.setdefault("fg", C.FG_PRIMARY)
        kw.setdefault("relief", "flat")
        kw.setdefault("bd", 0)
        kw.setdefault("highlightthickness", 1)
        kw.setdefault("highlightcolor", C.BORDER_FOCUS)
        kw.setdefault("highlightbackground", C.BORDER)
        kw.setdefault("selectbackground", C.ACCENT_LIGHT)
        kw.setdefault("insertbackground", C.ACCENT)
        _orig_init(self, master, **kw)

    tk.Text.__init__ = _themed_init
