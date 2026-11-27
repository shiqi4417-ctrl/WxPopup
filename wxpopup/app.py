# -*- coding: utf-8 -*-
"""入口逻辑：正常启动 / 诊断 / 自测 / 演示。"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import time

from . import banner as bannermod
from . import cipher
from . import config as cfgmod
from . import keys as keymod
from . import settings_ui
from . import watcher as watchermod
from . import wxsource

LOG = logging.getLogger("wxpopup")
MUTEX_NAME = "Local\\WxMessagePopup_1_0"
SETTINGS_FLAG = "show_settings.flag"
_MUTEX = None


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def setup_logging(silent: bool) -> None:
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    try:
        fh = logging.FileHandler(cfgmod.log_path(), encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass
    if not silent and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        LOG.addHandler(sh)


def single_instance() -> bool:
    """已经在跑就返回 False（并给老实例留一个「打开设置」的信号）。"""
    global _MUTEX
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        try:
            with open(os.path.join(cfgmod.app_root(), SETTINGS_FLAG), "w") as f:
                f.write("1")
        except OSError:
            pass
        return False
    _MUTEX = handle
    return True


def _consume_settings_flag() -> bool:
    path = os.path.join(cfgmod.app_root(), SETTINGS_FLAG)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    return False


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------
def doctor() -> int:
    def say(msg="", *a):
        print(msg % a if a else msg)

    say("=" * 64)
    say("微信消息弹窗 · 环境诊断")
    say("=" * 64)
    ok = True

    pids = keymod.weixin_pids()
    say("1) 微信进程:      %s", pids if pids else "× 没在运行（请打开并登录微信）")
    ok &= bool(pids)

    accounts = wxsource.find_accounts()
    say("2) 账号目录:      %s", ", ".join(os.path.basename(a) for a in accounts) if accounts
        else "× 没找到 xwechat_files")
    ok &= bool(accounts)

    acc = wxsource.newest_account()
    say("3) 当前账号:      %s", os.path.basename(acc) if acc else "×")
    if not acc:
        return 1

    sdb, cdb = wxsource.session_db(acc), wxsource.contact_db(acc)
    for label, p in (("session.db", sdb), ("contact.db", cdb)):
        size = os.path.getsize(p) / 1024 if os.path.exists(p) else 0
        say("   %-11s %8.0f KB  %s", label, size, p)

    say("4) 密钥提取:")
    store = keymod.KeyStore(os.path.join(cfgmod.app_root(), "keys.json"), say)
    got = keymod.acquire_keys(wxsource.account_id(acc),
                              [p for p in (sdb, cdb) if os.path.exists(p)], store, say)
    if sdb not in got:
        say("   × 没拿到 session.db 的密钥 —— 弹窗无法工作")
        say("     常见原因：微信没登录 / 微信以管理员身份运行而本程序不是 / 微信改版")
        return 1
    say("   √ 密钥就绪")

    say("5) 解密会话库:")
    src = wxsource.WeChatSource(cfgmod.app_root(), say)
    src.account_dir, src.account_id, src.keys = acc, wxsource.account_id(acc), got
    t0 = time.time()
    path = src.session_plain()
    say("   耗时 %.2fs → %s", time.time() - t0, path)
    if not path:
        say("   × 解密失败")
        return 1

    say("6) 会话样例（最近 8 个；「免打扰」= 微信里设了消息免打扰，不会弹）:")
    w = watchermod.Watcher(cfgmod.Config.load(), lambda ev: None, say)
    w.source = src
    say("   %-16s %-6s %-11s %-6s %s", "会话", "未读", "时间", "免打扰", "摘要")
    for row in w.snapshot(8):
        say("   %-16s %-6s %-11s %-6s %s", row["会话"][:16], row["未读"], row["时间"],
            row["免打扰"] or "—", row["摘要"][:26])

    say("-" * 64)
    say("结论: %s", "√ 一切正常，可以启动弹窗" if ok else "! 上面有叉，先解决它们")
    say("=" * 64)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 演示 / 自测
# --------------------------------------------------------------------------
def _settings_factory(master, cfg, on_saved):
    return settings_ui.SettingsWindow(master, cfg, on_saved)


def demo() -> int:
    cfg = cfgmod.Config.load()
    app = bannermod.BannerApp(cfg, settings_factory=_settings_factory)
    app.root._banner_app = app
    samples = [
        {"title": "文件传输助手", "subtitle": "", "body": "这是一条测试消息，看到横幅说明弹窗工作正常。"},
        {"title": "产品评审群", "subtitle": "小王", "body": "今晚 8 点评审，记得把原型发群里。", "is_group": True},
        {"title": "张伟", "subtitle": "", "body": "刚发你的报价单，帮我看下第三页单价是不是写错了。"},
        {"title": "技术交流群", "subtitle": "李工", "body": "[图片]"},
    ]
    state = {"i": 0}

    def tick():
        s = samples[state["i"] % len(samples)]
        state["i"] += 1
        ev = dict(s)
        ev.setdefault("username", "demo_%d" % state["i"])
        ev["time"] = time.time()
        ev["count"] = 1
        app.push(ev)
        app.root.after(2800, tick)

    app.root.after(500, tick)
    app.root.mainloop()
    return 0


def selfcheck() -> int:
    ok = cipher.selftest()
    print("[自测] SQLCipher 解密引擎:", "通过" if ok else "失败")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# 正常模式
# --------------------------------------------------------------------------
def run(silent: bool, open_settings: bool) -> int:
    cfg = cfgmod.Config.load()
    started_at = time.time()

    def emit(ev: dict) -> None:
        LOG.info("新消息 · %s%s: %s", ev.get("title"),
                 (" / " + ev["subtitle"]) if ev.get("subtitle") else "", ev.get("body"))
        app.push(ev)

    app = bannermod.BannerApp(cfg, settings_factory=_settings_factory)
    app.root._banner_app = app

    w = watchermod.Watcher(cfg, emit, lambda m: LOG.info("%s", m))
    w.start()
    LOG.info("=" * 40)
    LOG.info("微信消息弹窗已启动")

    # 托盘图标：一眼看到「有没有在跑」，右键能设置/暂停/退出
    tray = None
    try:
        from . import tray as traymod
        icon = traymod.make_icon(os.path.join(cfgmod.app_root(), "tray.ico"))
        tray = traymod.Tray("微信消息弹窗 · 启动中…", app.post_command, icon)
        if tray.start():
            LOG.info("托盘图标已就绪")
        else:
            LOG.info("托盘图标注册失败（不影响弹窗）")
            tray = None
    except Exception as e:
        LOG.info("托盘不可用：%s", e)
        tray = None

    def update_tip():
        if tray is None:
            return
        if app.muted_now():
            tray.set_tip("微信消息弹窗 · 已暂停（右键恢复）")
        elif w.last_count:
            tray.set_tip("微信消息弹窗 · 运行中 · 监听 %d 个会话" % w.last_count)
        elif w.last_error:
            tray.set_tip("微信消息弹窗 · 等待微信（%s）" % w.last_error[:40])
        else:
            tray.set_tip("微信消息弹窗 · 等待微信登录")
        app.root.after(2000, update_tip)

    app.root.after(1500, update_tip)

    def on_quit():
        w.stop()
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        LOG.info("已退出")

    app.on_quit = on_quit

    if open_settings:
        app.root.after(300, app.open_settings)

    def write_status():
        """把当前状态写到 status.txt，供 `--status` / 状态.bat 读取。"""
        mins = int((time.time() - started_at) // 60)
        acc = w.source.account_id or "（未取到，微信可能没登录）"
        lines = [
            "微信消息弹窗：✅ 在运行",
            "进程: %d · 已运行 %d 分钟" % (os.getpid(), mins),
            "账号: %s" % acc,
            "监听: %d 个会话（已轮询 %d 次）" % (w.last_count, w.polls),
            "提醒: %s" % ("已暂停（托盘右键可恢复）" if app.muted_now() else "正常"),
            "托盘图标: %s" % ("已就绪" if tray is not None else "未注册（不影响弹窗）"),
            "日志: %s" % cfgmod.log_path(),
        ]
        try:
            with open(os.path.join(cfgmod.app_root(), "status.txt"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def watch_flag():
        if _consume_settings_flag():
            app.open_settings()
        req = os.path.join(cfgmod.app_root(), "status_request.flag")
        if os.path.exists(req):
            try:
                os.remove(req)
            except OSError:
                pass
            write_status()
        reload_path = os.path.join(cfgmod.app_root(), "reload.flag")
        if os.path.exists(reload_path):
            try:
                os.remove(reload_path)
            except OSError:
                pass
            fresh = cfgmod.Config.load()
            cfg.clear()
            cfg.update(fresh)
            app.apply_cfg()
            LOG.info("配置已重载（特别关注 / 免打扰等即时生效）")
        if os.path.exists(os.path.join(cfgmod.app_root(), "quit.flag")):
            try:
                os.remove(os.path.join(cfgmod.app_root(), "quit.flag"))
            except OSError:
                pass
            LOG.info("收到退出信号")
            app.quit()
            return
        app.root.after(1000, watch_flag)

    app.root.after(1500, watch_flag)
    try:
        app.root.mainloop()
    finally:
        w.stop()
    return 0


def request_quit() -> int:
    """给正在运行的实例发退出信号（路径交给 Python 处理，避免 cmd 的中文编码坑）。"""
    flag = os.path.join(cfgmod.app_root(), "quit.flag")
    try:
        with open(flag, "w", encoding="ascii") as f:
            f.write("1")
    except OSError as e:
        print("写退出信号失败: %s" % e)
        return 1
    print("已发送退出信号（窗口会在 2 秒内关闭）")
    return 0


def is_running() -> bool:
    """用互斥体判断实例是否在跑（不会误判成一个僵尸文件）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return True
    try:
        return ctypes.get_last_error() == 183      # ERROR_ALREADY_EXISTS
    finally:
        k32.CloseHandle(h)


def request_status() -> int:
    """问一声「你到底在不在跑」——让运行中的实例写一份状态。"""
    if not is_running():
        print("微信消息弹窗：❌ 没有在运行")
        print("（双击 启动.bat 启动；若启动后仍显示未运行，看 诊断.bat 的输出）")
        return 1
    root = cfgmod.app_root()
    req = os.path.join(root, "status_request.flag")
    out = os.path.join(root, "status.txt")
    try:
        if os.path.exists(out):
            os.remove(out)
        with open(req, "w", encoding="ascii") as f:
            f.write("1")
    except OSError as e:
        print("无法请求状态: %s" % e)
        return 1
    for _ in range(80):
        time.sleep(0.1)
        if os.path.exists(out):
            try:
                with open(out, "r", encoding="utf-8") as f:
                    print(f.read().rstrip())
            except OSError:
                pass
            return 0
    print("微信消息弹窗：✅ 在运行，但没在 3 秒内响应状态请求（可能正忙）")
    return 0


def _with_output(fn):
    """打包成客户端（无控制台）时，把命令行输出存成文本并打开给人看。"""
    if sys.stdout is not None:
        return fn()
    import io
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        rc = fn()
    finally:
        sys.stdout = old
    path = os.path.join(cfgmod.app_root(), "输出.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        os.startfile(path)          # 记事本打开，双击 exe 也能看到结果
    except OSError:
        pass
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="微信消息弹窗：给电脑版微信加上 QQ 式右下角提醒")
    ap.add_argument("--demo", action="store_true", help="演示模式（不需要微信）")
    ap.add_argument("--doctor", action="store_true", help="环境诊断")
    ap.add_argument("--selftest", action="store_true", help="自测解密引擎")
    ap.add_argument("--settings", action="store_true", help="启动并直接打开设置")
    ap.add_argument("--quit", action="store_true", help="让正在运行的实例退出")
    ap.add_argument("--status", action="store_true", help="查看是否在运行 / 运行状态")
    ap.add_argument("--star", action="store_true", help="打开「特别关注成员」选择器")
    ap.add_argument("--silent", action="store_true", help="不输出到控制台（开机自启用）")
    args = ap.parse_args(argv)

    if args.quit:
        return request_quit()
    if args.status:
        return _with_output(request_status)
    if args.star:
        from . import star_picker
        setup_logging(False)
        star_picker.open_picker(cfgmod.Config.load())
        return 0
    if args.selftest:
        return _with_output(selfcheck)
    if args.doctor:
        setup_logging(False)
        return _with_output(doctor)
    if args.demo:
        setup_logging(False)
        return demo()

    setup_logging(args.silent)
    if not single_instance():
        print("「微信消息弹窗」已经在运行了，已把它的设置面板叫出来。")
        return 0
    return run(args.silent, args.settings)


if __name__ == "__main__":
    raise SystemExit(main())
