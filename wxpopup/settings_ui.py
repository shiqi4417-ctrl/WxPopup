# -*- coding: utf-8 -*-
"""设置面板。"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import banner as bannermod
from . import config as cfgmod

POSITIONS = [("右下角", "bottom-right"), ("左下角", "bottom-left"),
             ("右上角", "top-right"), ("左上角", "top-left")]
THEMES = [("深色", "dark"), ("浅色", "light")]

STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_FILE = os.path.join(STARTUP_DIR, "微信消息弹窗.vbs")


def set_autostart(enable: bool) -> bool:
    """用「启动文件夹里放一个 vbs」的方式做开机自启，无需注册表、无需管理员。"""
    try:
        if enable:
            os.makedirs(STARTUP_DIR, exist_ok=True)
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = sys.executable
            script = os.path.join(root, "main.py")
            body = ('Set s = CreateObject("WScript.Shell")\r\n'
                    's.CurrentDirectory = "%s"\r\n'
                    's.Run """%s"" ""%s"" --silent", 0, False\r\n' % (root, pyw, script))
            with open(STARTUP_FILE, "w", encoding="utf-8") as f:
                f.write(body)
        elif os.path.exists(STARTUP_FILE):
            os.remove(STARTUP_FILE)
        return True
    except OSError:
        return False


class SettingsWindow(tk.Toplevel):
    def __init__(self, master, cfg, on_saved: Callable[[], None]):
        super().__init__(master)
        self.cfg = cfg
        self.on_saved = on_saved
        self.title("微信消息弹窗 · 设置")
        self.resizable(False, False)
        self.configure(bg="#F5F6F8")
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.vars = {}
        self._build()
        self._center()

    # ------------------------------------------------------------------
    def _center(self) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, (sh - h) // 3)))

    def _var(self, key, value=None):
        v = tk.Variable(value=self.cfg.get(key) if value is None else value)
        self.vars[key] = v
        return v

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        box = ttk.Frame(self, padding=14)
        box.pack(fill="both", expand=True)

        row = 0
        ttk.Label(box, text="弹出位置").grid(row=row, column=0, sticky="w", **pad)
        self._var("position")
        ttk.Combobox(box, textvariable=self.vars["position"], state="readonly", width=12,
                     values=[label for label, _ in POSITIONS]).grid(row=row, column=1, sticky="w", **pad)
        # 显示用的中文 -> 内部值 映射
        self._pos_map = {label: val for label, val in POSITIONS}
        self.vars["position"].set(next((l for l, v in POSITIONS if v == self.cfg.get("position")),
                                       POSITIONS[0][0]))

        row += 1
        ttk.Label(box, text="主题").grid(row=row, column=0, sticky="w", **pad)
        self._var("theme")
        ttk.Combobox(box, textvariable=self.vars["theme"], state="readonly", width=12,
                     values=[label for label, _ in THEMES]).grid(row=row, column=1, sticky="w", **pad)
        self._theme_map = {label: val for label, val in THEMES}
        self.vars["theme"].set(next((l for l, v in THEMES if v == self.cfg.get("theme")),
                                    THEMES[0][0]))

        row += 1
        ttk.Label(box, text="停留秒数").grid(row=row, column=0, sticky="w", **pad)
        self._var("duration")
        ttk.Spinbox(box, from_=2, to=30, width=6, textvariable=self.vars["duration"]).grid(
            row=row, column=1, sticky="w", **pad)

        row += 1
        ttk.Label(box, text="最多同时显示").grid(row=row, column=0, sticky="w", **pad)
        self._var("max_stack")
        ttk.Spinbox(box, from_=1, to=8, width=6, textvariable=self.vars["max_stack"]).grid(
            row=row, column=1, sticky="w", **pad)

        row += 1
        ttk.Label(box, text="横幅宽度").grid(row=row, column=0, sticky="w", **pad)
        self._var("width")
        ttk.Spinbox(box, from_=280, to=560, increment=10, width=6,
                    textvariable=self.vars["width"]).grid(row=row, column=1, sticky="w", **pad)

        row += 1
        ttk.Label(box, text="刷新间隔(秒)").grid(row=row, column=0, sticky="w", **pad)
        self._var("poll_interval")
        ttk.Spinbox(box, from_=0.5, to=5, increment=0.5, width=6,
                    textvariable=self.vars["poll_interval"]).grid(row=row, column=1, sticky="w", **pad)

        row += 1
        self._var("autostart")
        ttk.Checkbutton(box, text="开机自动启动", variable=self.vars["autostart"]).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=12)

        row += 1
        ttk.Separator(box, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=8)
        row += 1
        ttk.Label(box, text="特别关注成员").grid(row=row, column=0, sticky="nw", **pad)
        star_box = ttk.Frame(box)
        star_box.grid(row=row, column=1, sticky="w", **pad)
        self.star_text = tk.Text(star_box, width=26, height=3, wrap="word")
        self.star_text.pack(side="left")
        ttk.Button(star_box, text="选择群成员…", command=self._open_picker).pack(
            side="left", padx=(8, 0), anchor="n")

        row += 1
        ttk.Label(box, text="（点左边按钮勾人即可，也可手写「群名>成员A,成员B」）",
                  foreground="#888").grid(row=row, column=1, sticky="w", padx=12)

        row += 1
        btns = ttk.Frame(box)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="测试弹窗", command=self._test).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="保存", command=self._save).pack(side="left", padx=6)

        self._refresh_star_text()

        row += 1
        self.hint = ttk.Label(box, text="关闭本窗口不会退出程序；退出请右键「托盘图标」→ 退出",
                              foreground="#888")
        self.hint.grid(row=row, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 0))

    # ------------------------------------------------------------------
    def _open_picker(self) -> None:
        """在设置窗口里直接弹出成员勾选器（不用另开一个程序）。"""
        from . import star_picker
        if getattr(self, "_picker", None) is not None:
            try:
                self._picker.lift()
                return
            except Exception:
                self._picker = None
        self._picker = star_picker.StarPicker(self, self.cfg, self._refresh_star_text)

    def _refresh_star_text(self) -> None:
        self.star_text.delete("1.0", "end")
        self.star_text.insert("1.0", "\n".join(self.cfg.get("star_rules") or []))

    def _collect(self) -> None:
        for key in ("duration", "max_stack", "width"):
            try:
                self.vars[key].set(max(1, int(float(self.vars[key].get()))))
            except (ValueError, tk.TclError):
                self.vars[key].set(cfgmod.DEFAULTS[key])
        try:
            self.vars["poll_interval"].set(max(0.5, float(self.vars["poll_interval"].get())))
        except (ValueError, tk.TclError):
            self.vars["poll_interval"].set(1.0)
        for key in ("autostart",):
            try:
                self.vars[key].set(bool(self.vars[key].get()))
            except tk.TclError:
                self.vars[key].set(False)
        self.vars["position"].set(self._pos_map.get(self.vars["position"].get(), "bottom-right"))
        self.vars["theme"].set(self._theme_map.get(self.vars["theme"].get(), "dark"))

    def _save(self) -> None:
        self._collect()
        for key, var in self.vars.items():
            self.cfg[key] = var.get()
        raw_star = self.star_text.get("1.0", "end")
        self.cfg["star_rules"] = [x.strip() for x in raw_star.splitlines() if x.strip()]
        set_autostart(bool(self.cfg.get("autostart")))
        self.cfg.save()
        try:
            self.on_saved()
        except Exception:
            pass
        self.destroy()

    def _test(self) -> None:
        # 注意：winfo_toplevel() 返回的是「设置窗口自己」，拿不到根窗口上的 _banner_app，
        # 必须顺着 master 链走到真正的 Tk 根。
        widget = self
        while widget.master is not None:
            widget = widget.master
        app = getattr(widget, "_banner_app", None)
        if app is None:
            self._hint("测试失败：没拿到弹窗实例（程序刚启动时偶发，稍后再试）")
            return
        app.push({"username": "__test__", "title": "文件传输助手", "subtitle": "",
                  "body": "这是一条测试消息，看到横幅就说明弹窗正常。", "is_group": False,
                  "time": __import__("time").time(), "count": 1})
        self._hint("已发送一条测试弹窗，看看屏幕右下角")

    def _hint(self, text: str) -> None:
        try:
            self.hint.configure(text=text)      # 设置窗口底部那行灰字
        except tk.TclError:
            pass
