"""微动画引擎 —— 让 Tkinter 拥有接近原生 macOS 的动效质感。

提供三类原子能力，全部零依赖、帧率可控、异常安全：

- SmoothScroll : 滚轮平滑滚动。把离散的整页跳变转成 60fps easeOut 缓动，
  同时自动识别"滚动条拖动"并立即停止动画，绝不与用户抢夺视口。
- fade_canvas_item : Canvas 元素颜色渐变（导航悬停/药丸切换/激活态过渡）。
- animate_place_y : 数值追踪缓动（侧栏滑动指示条等位移动画）。

所有动画都挂在 widget.after 上，widget 被销毁时静默自清理，不留孤儿回调。
"""
from __future__ import annotations

import time
import tkinter as tk

FRAME_MS = 16  # ~60fps


def ease_out_cubic(t: float) -> float:
    """缓出曲线：起速快、收尾柔，最接近 macOS 原生滚动手感。"""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def lerp_color(c1: str, c2: str, t: float) -> str:
    """两个 #rrggbb 颜色线性插值。"""
    try:
        a = c1.lstrip("#")
        b = c2.lstrip("#")
        r1, g1, b1 = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        r2, g2, b2 = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        return (
            f"#{round(r1 + (r2 - r1) * t):02x}"
            f"{round(g1 + (g2 - g1) * t):02x}"
            f"{round(b1 + (b2 - b1) * t):02x}"
        )
    except (ValueError, IndexError, TypeError):
        return c2


class SmoothScroll:
    """滚轮平滑滚动控制器。

    用法:
        sc = SmoothScroll(widget)
        widget.bind("<MouseWheel>", sc.on_wheel)

    特性:
    - 60fps easeOut 缓动，每次滚轮事件都会重设起止点，连续滚动如丝般连贯；
    - 每格滚动量按真实行高/视口高度换算成视图比例，任何内容密度下手感一致；
    - 自动拦截滚动条驱动的 yview("moveto"/"scroll") 调用并终止动画，
      用户拖动滚动条时动画立刻让位，不会出现"拉锯"；
    - 动画帧自身触发的 yscrollcommand 回环调用会被精确识别并忽略；
    - 高分屏精密触控板的小步长 delta 也能累积出细腻滚动。
    """

    DURATION_MS = 140.0   # 单次滚轮动画时长（偏短：跟手优先，避免拖泥带水）
    LINES_PER_NOTCH = 3   # 每格滚轮滚动 3 行（对齐 macOS 触控板手感）

    def __init__(self, widget: tk.Widget, fallback_frac: float = 0.16) -> None:
        self.w = widget
        self.fallback_frac = fallback_frac
        self._target: float | None = None
        self._from = 0.0
        self._t0 = 0.0
        self._job: str | None = None
        self._line_h = 0
        self._installed = False
        self._install_yview_guard()

    # ── 滚动条竞争守卫 ──
    def _install_yview_guard(self) -> None:
        """包装 widget.yview：外部（滚动条/键盘）调用时立刻终止动画。

        动画帧自身通过 yview_moveto 驱动，会触发 yscrollcommand → 部分面板
        的滚动条包装器又回调 yview(first, last)。这一回环与当前动画位置
        完全一致（<0.001），会被识别并放行，不误杀动画。
        """
        if self._installed:
            return
        try:
            orig = self.w.yview

            def _yview(*args):
                if args:
                    self._maybe_cancel_external(args, orig)
                return orig(*args)

            self.w.yview = _yview  # type: ignore[method-assign]
            self._installed = True
        except Exception:
            pass

    def _maybe_cancel_external(self, args: tuple, orig) -> None:
        try:
            if self._target is None:
                return
            cur = orig()[0]
            if args[0] == "moveto":
                want = float(args[1])
            elif len(args) >= 2 and isinstance(args[0], (int, float)):
                want = float(args[0])  # yview(first, last) 直设形态
            else:
                self.cancel()  # "scroll n units" 等命令式调用 → 外部操作
                return
            if abs(want - cur) > 0.001:
                self.cancel()
        except Exception:
            pass

    # ── 滚轮事件入口 ──
    def on_wheel(self, event) -> str:
        delta = getattr(event, "delta", 0)
        if not delta:
            return "break"
        try:
            cur = self.w.yview()[0]
        except tk.TclError:
            return "break"
        steps = delta / 120.0
        self._from = cur
        self._t0 = time.monotonic()
        if self._target is None:
            self._target = cur
        self._target = min(1.0, max(0.0, self._target - steps * self._unit_frac()))
        if self._job is None:
            self._loop()
        return "break"

    def on_wheel_step(self, direction: int) -> str:
        """X11 Button-4/5 兼容入口。direction: +1 向下 / -1 向上。"""
        try:
            cur = self.w.yview()[0]
        except tk.TclError:
            return "break"
        self._from = cur
        self._t0 = time.monotonic()
        if self._target is None:
            self._target = cur
        self._target = min(1.0, max(0.0, self._target + direction * self._unit_frac()))
        if self._job is None:
            self._loop()
        return "break"

    # ── 每格滚动量换算 ──
    def _unit_frac(self) -> float:
        try:
            h = self.w.winfo_height()
        except tk.TclError:
            return self.fallback_frac
        if h <= 1:
            return self.fallback_frac
        if self._line_h <= 0:
            self._line_h = self._detect_line_height()
        frac = (self.LINES_PER_NOTCH * (self._line_h or 20)) / h
        return min(0.5, max(0.02, frac))

    def _detect_line_height(self) -> int:
        w = self.w
        # Treeview：首行 bbox 高度最准
        try:
            kids = w.get_children()
            if kids:
                bb = w.bbox(kids[0])
                if bb and bb[3] > 4:
                    return int(bb[3])
        except Exception:
            pass
        # 通用：按字体行高推算
        try:
            import tkinter.font as tkfont

            return int(tkfont.Font(font=str(w.cget("font"))).metrics("linespace"))
        except Exception:
            return 20

    # ── 动画循环 ──
    def _loop(self) -> None:
        self._job = None
        if self._target is None:
            return
        try:
            cur = self.w.yview()[0]
            p = (time.monotonic() - self._t0) * 1000.0 / self.DURATION_MS
            goal = self._from + (self._target - self._from) * ease_out_cubic(p)
            nxt = cur + (goal - cur) * 0.55  # 指数追踪：新事件无缝续接不跳变
            if p >= 1.0 or abs(nxt - self._target) < 0.0015:
                self.w.yview_moveto(self._target)
                self._target = None
                return
            self.w.yview_moveto(nxt)
            self._job = self.w.after(FRAME_MS, self._loop)
        except tk.TclError:
            self._target = None
            self._job = None

    def cancel(self) -> None:
        self._target = None
        if self._job is not None:
            try:
                self.w.after_cancel(self._job)
            except Exception:
                pass
            self._job = None


# ────────────────────── Canvas 颜色渐变 ──────────────────────

def fade_canvas_item(cv: tk.Canvas, item_id, to_color: str,
                     duration_ms: int = 130, fps: int = 60) -> None:
    """Canvas 元素填充色平滑过渡（导航悬停、药丸切换、激活块）。

    同一元素的重复调用会自动取消上一次未完成的过渡，绝不叠加抖动。
    """
    try:
        start = str(cv.itemcget(item_id, "fill"))
    except tk.TclError:
        return
    if start == to_color:
        return
    if not (isinstance(start, str) and start.startswith("#") and len(start) == 7):
        try:
            cv.itemconfigure(item_id, fill=to_color)
        except tk.TclError:
            pass
        return

    tokens = getattr(cv, "_fade_tokens", None)
    if tokens is None:
        tokens = {}
        cv._fade_tokens = tokens
    old = tokens.get(item_id)
    if old is not None:
        try:
            cv.after_cancel(old)
        except Exception:
            pass

    frame_ms = max(8, 1000 // max(1, fps))
    steps = max(2, round(duration_ms / frame_ms))
    state = {"i": 0}

    def _step() -> None:
        state["i"] += 1
        t = state["i"] / steps
        try:
            if state["i"] >= steps:
                cv.itemconfigure(item_id, fill=to_color)
                tokens.pop(item_id, None)
                return
            cv.itemconfigure(item_id, fill=lerp_color(start, to_color, t))
            tokens[item_id] = cv.after(frame_ms, _step)
        except tk.TclError:
            tokens.pop(item_id, None)

    tokens[item_id] = cv.after(frame_ms, _step)


# ────────────────────── 位置追踪缓动 ──────────────────────

def animate_place_y(widget: tk.Widget, y_from: float, y_to: float,
                    duration_ms: int = 160, fps: int = 60) -> None:
    """place(y=...) 位置缓动：用于侧栏滑动指示条等微位移。"""
    frame_ms = max(8, 1000 // max(1, fps))
    steps = max(2, round(duration_ms / frame_ms))
    old = getattr(widget, "_pos_anim_job", None)
    if old is not None:
        try:
            widget.after_cancel(old)
        except Exception:
            pass

    def _step(i: int = 0) -> None:
        i += 1
        try:
            widget.place_configure(y=round(y_from + (y_to - y_from) * ease_out_cubic(i / steps)))
            if i < steps:
                widget._pos_anim_job = widget.after(frame_ms, _step)
            else:
                widget._pos_anim_job = None
        except tk.TclError:
            widget._pos_anim_job = None

    _step()
