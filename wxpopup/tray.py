# -*- coding: utf-8 -*-
"""系统托盘图标（Shell_NotifyIcon，纯 ctypes，无第三方依赖）。

用一个独立线程创建「消息窗口」并跑自己的消息循环，托盘菜单的点击
通过回调抛回主线程（调用方负责塞进队列，别在托盘线程里碰 Tk）。
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable, Optional

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONDBLCLK = 0x0203
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 16
MF_STRING = 0x0000
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

_u32.CreateWindowExW.restype = wintypes.HWND
_u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                 wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                 wintypes.HINSTANCE, ctypes.c_void_p]
_u32.DefWindowProcW.restype = ctypes.c_ssize_t
_u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.RegisterClassW.restype = wintypes.ATOM
_u32.LoadImageW.restype = wintypes.HANDLE
_u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
_u32.CreatePopupMenu.restype = wintypes.HMENU
_u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_u32.TrackPopupMenu.restype = ctypes.c_int
_u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.DestroyWindow.argtypes = [wintypes.HWND]
_u32.RegisterWindowMessageW.restype = wintypes.UINT
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


# 菜单项 id
ID_SETTINGS, ID_PAUSE, ID_OPEN, ID_QUIT = 1001, 1002, 1003, 1009


def make_icon(path: str) -> Optional[str]:
    """画一个绿底圆角「微」字图标（PIL 可用时）；失败就退回系统默认图标。"""
    if os.path.exists(path):
        return path
    try:
        from PIL import Image, ImageDraw, ImageFont
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2, 2, size - 3, size - 3], radius=16, fill=(7, 193, 96, 255))
        font = None
        for name in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
                     r"C:\Windows\Fonts\simhei.ttf"):
            if os.path.exists(name):
                try:
                    font = ImageFont.truetype(name, 40)
                    break
                except OSError:
                    continue
        if font:
            box = d.textbbox((0, 0), "微", font=font)
            w, h = box[2] - box[0], box[3] - box[1]
            d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), "微",
                   font=font, fill=(255, 255, 255, 255))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
        return path
    except Exception:
        return None


class Tray:
    """托盘图标 + 右键菜单。命令通过 on_command(str) 回调抛回调用方。"""

    def __init__(self, tip: str, on_command: Callable[[str], None], icon_path: Optional[str] = None):
        self.tip = tip
        self.on_command = on_command
        self.icon_path = icon_path
        self.hwnd = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._nid = None
        self._wndproc_ref = None
        self._taskbar_created = 0
        self._icon = None

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True, name="wxpopup-tray")
        self._thread.start()
        return self._ready.wait(3.0)

    def _run(self) -> None:
        try:
            self._taskbar_created = _u32.RegisterWindowMessageW("TaskbarCreated")
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = _k32.GetModuleHandleW(None)
            wc.lpszClassName = "WxPopupTrayWnd"
            _u32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = _u32.CreateWindowExW(0, "WxPopupTrayWnd", "wxpopup-tray", 0,
                                             0, 0, 0, 0, None, None,
                                             wc.hInstance, None)
            if not self.hwnd:
                return
            self._add_icon()
            self._ready.set()
            msg = wintypes.MSG()
            while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _u32.TranslateMessage(ctypes.byref(msg))
                _u32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            self._ready.set()
        finally:
            self._delete_icon()

    def stop(self) -> None:
        if self.hwnd:
            try:
                _u32.PostMessageW(wintypes.HWND(self.hwnd), WM_CLOSE, 0, 0)
            except Exception:
                pass

    # -- 图标 -----------------------------------------------------------
    def _load_icon(self):
        if self.icon_path:
            h = _u32.LoadImageW(None, self.icon_path, IMAGE_ICON, 32, 32,
                                LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return _u32.LoadIconW(None, ctypes.c_void_p(32512))     # IDI_APPLICATION

    def _fill(self, tip: str) -> _NOTIFYICONDATAW:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = wintypes.HWND(self.hwnd)
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        nid.szTip = tip[:127]
        return nid

    def _add_icon(self) -> None:
        self._icon = self._load_icon()
        nid = self._fill(self.tip)
        if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self.tip = "微信消息弹窗（托盘注册失败）"
        self._nid = nid

    def _delete_icon(self) -> None:
        if self._nid is not None:
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None

    def set_tip(self, text: str) -> None:
        """更新悬停提示（运行中/已暂停、监听多少会话）。"""
        self.tip = text
        if not self.hwnd or self._nid is None:
            return
        nid = self._fill(text)
        try:
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    # -- 菜单 -----------------------------------------------------------
    def _menu(self) -> None:
        menu = _u32.CreatePopupMenu()
        _u32.AppendMenuW(menu, MF_STRING, ID_OPEN, "打开微信")
        _u32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "设置…")
        _u32.AppendMenuW(menu, MF_STRING, ID_PAUSE, "暂停提醒 30 分钟")
        _u32.AppendMenuW(menu, 0x0008, 0, None)            # MF_SEPARATOR
        _u32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")
        pt = wintypes.POINT()
        _u32.GetCursorPos(ctypes.byref(pt))
        _u32.SetForegroundWindow(wintypes.HWND(self.hwnd))
        cmd = _u32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                  pt.x, pt.y, 0, wintypes.HWND(self.hwnd), None)
        _u32.DestroyMenu(menu)
        actions = {ID_OPEN: "open", ID_SETTINGS: "settings",
                   ID_PAUSE: "pause", ID_QUIT: "quit"}
        act = actions.get(cmd)
        if act:
            try:
                self.on_command(act)
            except Exception:
                pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            low = lparam & 0xFFFF
            if low in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            elif low in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                try:
                    self.on_command("settings")
                except Exception:
                    pass
            return 0
        if msg == self._taskbar_created and msg:
            self._add_icon()          # 资源管理器重启后重新注册
            return 0
        if msg == WM_CLOSE:
            self._delete_icon()
            _u32.DestroyWindow(wintypes.HWND(hwnd))
            return 0
        if msg == WM_DESTROY:
            _u32.PostQuitMessage(0)
            return 0
        return _u32.DefWindowProcW(wintypes.HWND(hwnd), msg, wparam, lparam)
