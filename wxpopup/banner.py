# -*- coding: utf-8 -*-
"""QQ 式的右下角消息横幅。

要点：
    * 置顶、不抢焦点（WS_EX_NOACTIVATE + 不进 Alt-Tab）
    * 从屏幕边缘滑入 + 淡入，鼠标悬停暂停倒计时
    * 同一个会话连续来消息会合并成「N 条」而不是刷屏
    * 点一下打开微信，右键出菜单
"""
from __future__ import annotations

import ctypes
import os
import queue
import time
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from typing import Dict, List, Optional

from . import config as cfgmod
from . import wxsource

THEMES = {
    "dark": {
        "card": "#2A2C31", "title": "#F2F3F5", "body": "#C4C7CE", "meta": "#8A8F99",
        "border": "#3B3E46", "hover": "#33363D", "accent": "#07C160", "avatar_fg": "#FFFFFF",
    },
    "light": {
        "card": "#FFFFFF", "title": "#17181B", "body": "#4C5058", "meta": "#9AA0AA",
        "border": "#E4E6EB", "hover": "#F5F6F8", "accent": "#07C160", "avatar_fg": "#FFFFFF",
    },
}

AVATAR_COLORS = ["#4E8CE0", "#E0704E", "#3FB27F", "#9B6BD6", "#D2546D", "#3AAFB9", "#C79A2E"]

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_g32 = ctypes.WinDLL("gdi32", use_last_error=True)
_u32.SetWindowLongPtrW = getattr(_u32, "SetWindowLongPtrW", _u32.SetWindowLongW)
_u32.SetWindowLongPtrW.restype = ctypes.c_void_p
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_g32.CreateRoundRectRgn.restype = wintypes.HRGN
_g32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_u32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

SPI_GETWORKAREA = 0x0030


def work_area() -> tuple:
    r = wintypes.RECT()
    if _u32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return r.left, r.top, r.right, r.bottom
    return 0, 0, _u32.GetSystemMetrics(0), _u32.GetSystemMetrics(1)


def _round_region(hwnd: int, w: int, h: int, radius: int) -> None:
    try:
        hwnd = int(hwnd)
        rgn = _g32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
        _u32.SetWindowRgn(wintypes.HWND(hwnd), rgn, True)
    except Exception:
        pass


def _no_activate(hwnd: int) -> None:
    try:
        hwnd = int(hwnd)
        style = _u32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE) or 0
        _u32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE,
                               ctypes.c_void_p(style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW))
    except Exception:
        pass


class Banner:
    """一条横幅。"""

    GAP = 10
    PAD_X = 18

    def __init__(self, app: "BannerApp", ev: dict):
        self.app = app
        self.ev = ev
        self.scale = app.scale
        self.theme = app.theme
        self.width = int(app.cfg["width"] * self.scale)
        self.height = int((86 if not ev.get("subtitle") else 96) * self.scale)
        self.count = int(ev.get("count", 1))
        self.created = time.time()
        self.target_x, self.target_y = 0, 0
        self._closing = False
        self._hover = False
        self._remaining = float(app.cfg["duration"])
        self._tick_job = None
        self._anim_job = None
        self._alpha = 0.0

        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self._build()

    # -- 外观 -----------------------------------------------------------
    def _build(self) -> None:
        t = self.theme
        s = self.scale
        self.win.configure(bg=t["card"])
        self.body_frame = tk.Frame(self.win, bg=t["card"], highlightthickness=1,
                                   highlightbackground=t["border"])
        self.body_frame.place(x=0, y=0, width=self.width, height=self.height)

        accent = tk.Frame(self.body_frame, bg=t["accent"])
        accent.place(x=0, y=int(6 * s), width=int(3 * s), height=self.height - int(12 * s))

        # 头像（用首字上色代替真实头像，零依赖）
        size = int(38 * s)
        self.avatar = tk.Canvas(self.body_frame, width=size, height=size,
                                bg=t["card"], highlightthickness=0)
        self.avatar.place(x=self.PAD_X, y=int((self.height - size) / 2))
        color = AVATAR_COLORS[abs(hash(self.ev.get("username") or "x")) % len(AVATAR_COLORS)]
        self.avatar.create_oval(0, 0, size - 1, size - 1, fill=color, outline=color)
        initial = (self.ev.get("title") or "?").strip()[:1]
        self.avatar.create_text(size / 2, size / 2 + 1, text=initial,
                                fill=t["avatar_fg"], font=(self.app.font_family, int(13 * s), "bold"))

        left = self.PAD_X + size + int(12 * s)
        right = self.width - int(14 * s)
        char_w = max(6.0, 9.5 * s * (96.0 / 72.0) * 1.05)
        per_line = max(10, int((right - left - 8) / char_w))
        body_limit = per_line * 2 - 1
        title_limit = max(8, int((right - left - 50 * s) / (char_w * 1.16)))
        self._body_limit, self._title_limit = body_limit, title_limit

        title = self.ev.get("title") or ""
        if self.count > 1:
            title = "%s  (%d 条)" % (title, self.count)
        self.lbl_title = tk.Label(self.body_frame, text=self._ellipsis(title, title_limit),
                                  bg=t["card"], fg=t["title"], anchor="w", justify="left",
                                  font=(self.app.font_family, int(11 * s), "bold"))
        self.lbl_title.place(x=left, y=int(14 * s), width=int(right - left - 46 * s))

        self.lbl_time = tk.Label(self.body_frame, text=time.strftime("%H:%M", time.localtime(self.ev.get("time") or time.time())),
                                 bg=t["card"], fg=t["meta"], anchor="e",
                                 font=(self.app.font_family, int(8.5 * s)))
        self.lbl_time.place(x=right - int(76 * s), y=int(16 * s), width=int(42 * s))

        # 关闭按钮（悬停才显示）
        self.btn_close = tk.Label(self.body_frame, text="✕", bg=t["card"], fg=t["card"],
                                  font=(self.app.font_family, int(9 * s)), cursor="hand2")
        self.btn_close.place(x=right - int(16 * s), y=int(13 * s))
        self.btn_close.bind("<Button-1>", lambda e: self.close())

        body_top = int(38 * s) if self.ev.get("subtitle") else int(36 * s)
        if self.ev.get("subtitle"):
            self.lbl_sub = tk.Label(self.body_frame, text=self._ellipsis(self.ev["subtitle"], 14),
                                    bg=t["card"], fg=t["meta"], anchor="w",
                                    font=(self.app.font_family, int(8.5 * s)))
            self.lbl_sub.place(x=left, y=int(34 * s), width=right - left)
            body_top = int(52 * s)

        self.lbl_body = tk.Label(self.body_frame, text=self._ellipsis(self.ev.get("body") or "", body_limit),
                                 bg=t["card"], fg=t["body"], anchor="nw", justify="left",
                                 wraplength=int(right - left - 8),
                                 font=(self.app.font_family, int(9.5 * s)))
        self.lbl_body.place(x=left, y=body_top, width=right - left,
                            height=self.height - body_top - int(8 * s))

        for w in (self.body_frame, self.avatar, self.lbl_title, self.lbl_body):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_click)
            w.bind("<Button-3>", self._on_menu)
        self.btn_close.bind("<Enter>", self._on_enter)
        self.btn_close.bind("<Leave>", self._on_leave)

    @staticmethod
    def _ellipsis(text: str, limit: int) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    # -- 交互 -----------------------------------------------------------
    def _on_enter(self, _=None) -> None:
        self._hover = True
        self.btn_close.configure(fg=self.theme["title"])
        self._cancel_tick()

    def _on_leave(self, _=None) -> None:
        self._hover = False
        self.btn_close.configure(fg=self.theme["card"])
        self._schedule_tick()

    def _on_click(self, _=None) -> None:
        wxsource.focus_wechat()
        self.close()

    def _on_menu(self, event=None) -> None:
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="打开微信", command=lambda: (wxsource.focus_wechat(), self.close()))
        menu.add_command(label="暂停提醒 30 分钟", command=lambda: (self.app.pause(30), self.close()))
        menu.add_separator()
        menu.add_command(label="设置…", command=self.app.open_settings)
        menu.add_separator()
        menu.add_command(label="退出", command=self.app.quit)
        try:
            menu.tk_popup(event.x_root if event else self.win.winfo_pointerx(),
                          event.y_root if event else self.win.winfo_pointery())
        finally:
            menu.grab_release()

    # -- 生命周期 -------------------------------------------------------
    def merge(self, ev: dict) -> None:
        self.count += 1
        self.ev.update({"body": ev.get("body"), "time": ev.get("time")})
        t = self.theme
        title = self.ev.get("title") or ""
        self.lbl_title.configure(text=self._ellipsis("%s  (%d 条)" % (title, self.count),
                                                     self._title_limit))
        self.lbl_body.configure(text=self._ellipsis(ev.get("body") or "", self._body_limit))
        self.lbl_time.configure(text=time.strftime("%H:%M", time.localtime(ev.get("time") or time.time())))
        self.body_frame.configure(highlightbackground=t["accent"])
        self.win.after(220, lambda: self.body_frame.configure(highlightbackground=t["border"]))
        self._remaining = float(self.app.cfg["duration"])
        self._cancel_tick()
        self._schedule_tick()

    def show(self, x: int, y: int) -> None:
        self.target_x, self.target_y = x, y
        start_x = x + (self.width + 40) if "right" in str(self.app.cfg["position"]) else x - (self.width + 40)
        self.win.geometry("%dx%d+%d+%d" % (self.width, self.height, start_x, y))
        self.win.deiconify()
        hwnd = int(self.win.winfo_id())
        _no_activate(hwnd)
        _round_region(hwnd, self.width, self.height, int(10 * self.scale))
        self._animate(start_x, x, 0)
        self._schedule_tick()

    def move_to(self, x: int, y: int) -> None:
        self.target_x, self.target_y = x, y
        self.win.geometry("+%d+%d" % (x, y))

    def _animate(self, x0: int, x1: int, step: int, steps: int = 9) -> None:
        if self._closing:
            return
        if step > steps:
            try:
                self.win.attributes("-alpha", float(self.app.cfg["opacity"]))
            except tk.TclError:
                pass
            self.win.geometry("+%d+%d" % (x1, self.target_y))
            return
        k = step / steps
        ease = 1 - (1 - k) ** 3
        x = int(x0 + (x1 - x0) * ease)
        try:
            self.win.attributes("-alpha", float(self.app.cfg["opacity"]) * (0.35 + 0.65 * ease))
        except tk.TclError:
            pass
        self.win.geometry("+%d+%d" % (x, self.target_y))
        self._anim_job = self.win.after(16, lambda: self._animate(x0, x1, step + 1, steps))

    def _schedule_tick(self) -> None:
        self._cancel_tick()
        self._tick_job = self.win.after(int(self._remaining * 1000), self.close)

    def _cancel_tick(self) -> None:
        if self._tick_job:
            try:
                self.win.after_cancel(self._tick_job)
            except Exception:
                pass
            self._tick_job = None

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel_tick()
        if self._anim_job:
            try:
                self.win.after_cancel(self._anim_job)
            except Exception:
                pass
        self.app._remove(self)


class BannerApp:
    def __init__(self, cfg, settings_factory=None, on_quit=None):
        self.cfg = cfg
        self.theme = THEMES.get(str(cfg.get("theme", "dark")), THEMES["dark"])
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.cmd_queue: "queue.Queue[str]" = queue.Queue()      # 托盘菜单等外部命令
        self.banners: List[Banner] = []
        self._paused_until = 0.0
        self._settings_win = None
        self.settings_factory = settings_factory
        self.on_quit = on_quit

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(cfgmod.APP_NAME)
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.font_family = self._pick_font()
        self.root.after(120, self._pump)

    @staticmethod
    def _pick_font() -> str:
        try:
            families = set(tkfont.families())
        except Exception:
            return "Microsoft YaHei UI"
        for name in ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "SimHei"):
            if name in families:
                return name
        return "TkDefaultFont"

    # -- 对外 -----------------------------------------------------------
    def push(self, ev: dict) -> None:
        self.queue.put(ev)

    def pause(self, minutes: int) -> None:
        self._paused_until = time.time() + minutes * 60

    def muted_now(self) -> bool:
        return time.time() < self._paused_until

    def open_settings(self) -> None:
        self.root._banner_app = self          # 设置窗口靠这个拿到弹窗实例
        if self.settings_factory:
            if self._settings_win is not None:
                try:
                    self._settings_win.lift()
                    return
                except Exception:
                    self._settings_win = None
            self._settings_win = self.settings_factory(self.root, self.cfg, self.apply_cfg)

    def apply_cfg(self) -> None:
        self.theme = THEMES.get(str(self.cfg.get("theme", "dark")), THEMES["dark"])
        for b in list(self.banners):
            b.close()
        self._relayout()

    def quit(self) -> None:
        try:
            if self.on_quit:
                self.on_quit()
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        # Tk 有时候在 destroy 上抛 TclError 把 mainloop 留在半死状态，
        # 这里兜底：无论卡在哪一步，进程都必须真的退出。
        os._exit(0)

    # -- 队列 / 布局 -----------------------------------------------------
    def _pump(self) -> None:
        try:
            while True:
                ev = self.queue.get_nowait()
                self._show(ev)
        except queue.Empty:
            pass
        try:
            while True:
                act = self.cmd_queue.get_nowait()
                if act == "settings":
                    self.open_settings()
                elif act == "quit":
                    self.quit()
                elif act == "open":
                    wxsource.focus_wechat()
                elif act == "pause":
                    self.pause(30)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def post_command(self, act: str) -> None:
        """给主线程派活（托盘线程调这个，别直接碰 Tk）。"""
        self.cmd_queue.put(act)

    def _show(self, ev: dict) -> None:
        if self.muted_now():
            return
        for b in self.banners:
            if b.ev.get("username") == ev.get("username"):
                b.merge(ev)
                b.win.lift()
                self._relayout()
                return
        b = Banner(self, ev)
        self.banners.append(b)
        self._relayout()
        b.show(b.target_x, b.target_y)
        max_stack = max(1, int(self.cfg.get("max_stack", 4)))
        while len(self.banners) > max_stack:
            self.banners[0].close()
        if self.cfg.get("sound"):
            try:
                import winsound
                winsound.PlaySound("SystemNotification", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except Exception:
                pass

    def _remove(self, b: Banner) -> None:
        if b in self.banners:
            self.banners.remove(b)
        try:
            b.win.destroy()
        except Exception:
            pass
        self.root.after(10, self._relayout)

    def _relayout(self) -> None:
        if not self.banners:
            return
        left, top, right, bottom = work_area()
        pos = str(self.cfg.get("position", "bottom-right"))
        gap = int(Banner.GAP * self.scale)
        y = bottom - gap
        top_limit = top + gap
        for b in reversed(self.banners):          # 新的在下/靠近边缘
            if "bottom" in pos:
                y -= b.height
                ty = y
                y -= gap
            else:
                ty = y + gap
                y = ty + b.height
                if y > bottom:
                    break
            if ty < top_limit:
                break
            if "right" in pos:
                tx = right - b.width - gap
            else:
                tx = left + gap
            b.move_to(tx, ty)
