# -*- coding: utf-8 -*-
"""定位微信数据目录、解密会话库、读出会话列表。"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sqlite3
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

from . import cipher, keys as keymod

ACCOUNT_RE = re.compile(r"^(wxid_[0-9a-zA-Z]+?)(_[0-9a-f]{4})?$")
GH_RE = re.compile(r"^gh_[0-9a-zA-Z]+$")
CHATROOM_SUFFIX = "@chatroom"

# 微信内部的系统会话，不是「有人找你」，弹出来只会是噪声
SYSTEM_USERS = {
    "brandsessionholder", "brandsessionholder_weapp", "brandservicesessionholder",
    "brandservicesessionholder_weapp", "medianote", "newsapp",
    "notifymessage", "floatbottle", "qqmail", "fmessage", "qqsafe", "weibo",
    "weixin", "officialaccounts", "mphelper", "weixinreminder", "exmail_tool",
    "tmessage", "qmessage", "qqsync", "voipnotify", "voipmessage", "wxitil",
    "shakeapp", "voipapp", "feedsapp", "blogapp", "weixinpay", "cardpackage",
    "recover", "pc_work", "wepkg", "wxpay", "wechattools", "qqmailplugin",
}


def is_system(username: str) -> bool:
    return (username or "").lower() in SYSTEM_USERS


# --------------------------------------------------------------------------
# 目录定位
# --------------------------------------------------------------------------
def _config_dirs() -> List[str]:
    out = []
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        cfg = os.path.join(appdata, "Tencent", "xwechat", "config")
        if os.path.isdir(cfg):
            for name in os.listdir(cfg):
                if not name.endswith(".ini"):
                    continue
                try:
                    with open(os.path.join(cfg, name), "r", encoding="utf-8",
                              errors="ignore") as f:
                        txt = f.read().strip()
                except OSError:
                    continue
                if txt and os.path.isdir(txt):
                    out.append(txt)
    return out


def _candidate_roots() -> List[str]:
    roots: List[str] = []
    roots += _config_dirs()
    home = os.path.expanduser("~")
    for p in (
        os.path.join(home, "xwechat_files"),
        os.path.join(home, "Documents", "xwechat_files"),
        os.path.join(home, "Documents", "WeChat Files"),
    ):
        roots.append(p)
    for drive in "CDEFGHIJ":
        roots.append("%s:\\xwechat_files" % drive)
    seen, out = set(), []
    for r in roots:
        r = os.path.normpath(os.path.abspath(r)) if r else ""
        if r and r not in seen and os.path.isdir(r):
            seen.add(r)
            out.append(r)
    return out


def find_accounts() -> List[str]:
    """所有账号目录（形如 ...\\xwechat_files\\wxid_xxx_abcd）。"""
    out: List[str] = []
    for root in _candidate_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            full = os.path.join(root, n)
            if n.startswith("wxid_") and os.path.isdir(full):
                if os.path.isdir(os.path.join(full, "db_storage")):
                    out.append(full)
    return out


def account_id(account_dir: str) -> str:
    name = os.path.basename(account_dir.rstrip("\\/"))
    m = ACCOUNT_RE.match(name)
    return m.group(1) if m else name


def db_path(account_dir: str, rel: str) -> str:
    return os.path.join(account_dir, "db_storage", rel.replace("/", os.sep))


def session_db(account_dir: str) -> str:
    return db_path(account_dir, "session/session.db")


def contact_db(account_dir: str) -> str:
    return db_path(account_dir, "contact/contact.db")


def newest_account() -> Optional[str]:
    best, best_t = None, -1.0
    for acc in find_accounts():
        try:
            t = os.path.getmtime(session_db(acc))
        except OSError:
            t = 0.0
        if t > best_t:
            best, best_t = acc, t
    return best


# --------------------------------------------------------------------------
# 解密缓存
# --------------------------------------------------------------------------
def _stamp(src: str) -> dict:
    st: Dict[str, object] = {}
    for suffix in ("", "-wal"):
        p = src + suffix
        try:
            s = os.stat(p)
            st[suffix or "db"] = [s.st_mtime, s.st_size]
        except OSError:
            st[suffix or "db"] = None
    return st


class PlainDB:
    """把加密库解密到缓存目录；源文件（含 WAL）一变就重建。"""

    def __init__(self, cache_dir: str, log=None):
        self.cache_dir = cache_dir
        self.log = log or (lambda *_: None)

    def _cache_path(self, src: str) -> str:
        tag = os.path.basename(os.path.dirname(src)) + "_" + os.path.basename(src)
        return os.path.join(self.cache_dir, tag)

    def get(self, src: str, key: bytes) -> Optional[str]:
        dst = self._cache_path(src)
        meta_path = dst + ".json"
        stamp = _stamp(src)
        stamp["key"] = key.hex()[:16]
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                if json.load(f) == stamp and os.path.exists(dst) and cipher.quick_check(dst):
                    return dst
        except (OSError, ValueError):
            pass
        os.makedirs(self.cache_dir, exist_ok=True)
        try:
            ok = cipher.build_plain(src, dst, key, with_wal=True)
        except OSError as e:
            # 另一个进程（比如同时开了诊断）正占着缓存文件：能用旧的就用旧的
            if os.path.exists(dst) and cipher.quick_check(dst):
                self.log("  [!] 缓存被占用（%s），先用现有缓存" % type(e).__name__)
                return dst
            raise
        if not ok:
            self.log("  [!] %s 解密后完整性校验未通过（已重试，仍用它继续）"
                     % os.path.basename(src))
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(stamp, f)
        except OSError:
            pass
        return dst


# --------------------------------------------------------------------------
# 会话读取
# --------------------------------------------------------------------------
def _text_factory(data: bytes):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    conn.text_factory = _text_factory
    return conn


_APPMSG_TITLE = re.compile(r"<title>(.*?)</title>", re.S)

_SUMMARY_HINTS = (
    ("<img", "[图片]"),
    ("<videomsg", "[视频]"),
    ("<emoji", "[动画表情]"),
    ("<voipmsg", "[语音通话]"),
    ("<voip", "[语音通话]"),
    ("<location", "[位置]"),
    ("<wcpay", "[微信支付]"),
    ("<transfers", "[转账]"),
    ("<sysmsg", "[系统消息]"),
)


def friendly_summary(raw, limit: int = 140) -> str:
    """把会话摘要变成可以直接显示的一行文字。"""
    if raw is None:
        return "[新消息]"
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "[新消息]"
    text = str(raw).strip()
    if not text:
        return "[新消息]"
    if text.startswith("<"):
        for token, label in _SUMMARY_HINTS:
            if token in text:
                return label
        m = _APPMSG_TITLE.search(text)
        if m:
            text = m.group(1).strip() or "[链接/文件]"
        else:
            return "[消息]"
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def is_group(username: str) -> bool:
    return username.endswith(CHATROOM_SUFFIX)


def is_official(username: str) -> bool:
    return bool(GH_RE.match(username))


# --------------------------------------------------------------------------
# chat_room.ext_buffer 里的群成员缓存（protobuf）
# --------------------------------------------------------------------------
def _pb_varint(buf: bytes, i: int):
    val = shift = 0
    while i < len(buf):
        c = buf[i]
        val |= (c & 0x7F) << shift
        shift += 7
        i += 1
        if not c & 0x80:
            break
    return val, i


def _pb_fields(buf: bytes):
    """把 protobuf 摊平成 [(字段号, 值), ...]，够用就行。"""
    out = []
    i = 0
    while i < len(buf):
        key, i = _pb_varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 2:
            n, i = _pb_varint(buf, i)
            v = buf[i:i + n]
            i += n
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        else:
            break
        out.append((f, v))
    return out


def parse_room_members(blob) -> List[Tuple[str, str]]:
    """从 chat_room.ext_buffer 解析 [(wxid, 群昵称或空), ...]。"""
    if not blob:
        return []
    out = []
    for f, v in _pb_fields(blob):
        if f != 1 or not isinstance(v, bytes):
            continue
        entry = {}
        for k, x in _pb_fields(v):
            if isinstance(x, bytes):
                try:
                    entry[k] = x.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            else:
                entry[k] = x
        wxid = entry.get(1)
        if wxid:
            out.append((str(wxid), str(entry.get(2) or "")))
    return out


class WeChatSource:
    def __init__(self, work_root: str, log=None):
        self.log = log or (lambda *_: None)
        self.account_dir: Optional[str] = None
        self.account_id: Optional[str] = None
        self.keys: Dict[str, bytes] = {}
        self._plain = PlainDB(os.path.join(work_root, "cache"), self.log)
        self._store = keymod.KeyStore(os.path.join(work_root, "keys.json"), self.log)
        self._name_cache: Dict[str, str] = {}
        self._notify: Dict[str, int] = {}
        self._self_wxid: str = ""

    # -- 定位 -------------------------------------------------------------
    def choose_account(self, prefer: Optional[str] = None) -> Optional[str]:
        """先试密钥缓存，再「扫一次内存」逐个账号校验，谁校验通过就是当前登录账号。"""
        accounts = find_accounts()
        if not accounts:
            self.log("  [x] 没找到微信数据目录（xwechat_files）")
            return None
        if prefer and prefer in accounts:
            accounts.remove(prefer)
            accounts.insert(0, prefer)
        else:
            accounts.sort(key=lambda a: os.path.getmtime(session_db(a))
                          if os.path.exists(session_db(a)) else 0, reverse=True)

        # 1) 缓存命中（几毫秒）
        for acc in accounts:
            got: Dict[str, bytes] = {}
            sdb, cdb = session_db(acc), contact_db(acc)
            for p in (sdb, cdb):
                if os.path.exists(p):
                    k = self._store.get(account_id(acc), p)
                    if k:
                        got[p] = k
            if sdb in got:
                self._use(acc, got)
                self.log("  [√] 账号 %s（密钥来自缓存）" % self.account_id)
                return acc

        # 2) 内存扫描一次，再按账号逐个校验（避免每个账号各扫一遍）
        pids = keymod.weixin_pids()
        if not pids:
            self.log("  [x] 没有发现 Weixin.exe —— 请先打开并登录微信")
            return None
        self.log("  [.] 微信进程 %s，开始只读扫描内存" % pids)
        cands = set()
        for pid in pids:
            cands |= keymod.scan_keys(pid, self.log)
        if not cands:
            self.log("  [x] 内存里没找到候选密钥")
            return None
        for acc in accounts:
            paths = [p for p in (session_db(acc), contact_db(acc)) if os.path.exists(p)]
            got = keymod.match_keys(cands, paths, self.log)
            if session_db(acc) in got:
                for p, k in got.items():
                    self._store.put(account_id(acc), p, k)
                self._store.save()
                self._use(acc, got)
                self.log("  [√] 账号 %s（密钥新提取）" % self.account_id)
                return acc
        self.log("  [x] 有 %d 个候选密钥，但都不匹配本机任何一个账号目录" % len(cands))
        return None

    def _use(self, acc: str, got: Dict[str, bytes]) -> None:        self.account_dir, self.account_id, self.keys = acc, account_id(acc), got

    def _prepare(self, acc: str) -> bool:
        sdb, cdb = session_db(acc), contact_db(acc)
        if not os.path.exists(sdb):
            return False
        acct = account_id(acc)
        got = keymod.acquire_keys(acct, [p for p in (sdb, cdb) if os.path.exists(p)],
                                  self._store, self.log)
        if sdb not in got:
            return False
        self._use(acc, got)
        return True

    def session_plain(self) -> Optional[str]:
        if not self.account_dir:
            return None
        src = session_db(self.account_dir)
        key = self.keys.get(src) or self._store.get(self.account_id, src)
        if not key:
            return None
        return self._plain.get(src, key)

    def contact_plain(self) -> Optional[str]:
        if not self.account_dir:
            return None
        src = contact_db(self.account_dir)
        if not os.path.exists(src):
            return None
        key = self.keys.get(src) or self._store.get(self.account_id, src)
        if not key:
            return None
        return self._plain.get(src, key)

    # -- 查询 -------------------------------------------------------------
    def self_wxid(self) -> str:
        if self._self_wxid:
            return self._self_wxid
        if self.account_id:
            self._self_wxid = self.account_id
        return self._self_wxid

    def sessions(self) -> List[dict]:
        path = self.session_plain()
        if not path:
            return []
        tried = [
            ("SELECT username, unread_count, summary, last_timestamp, last_msg_sender,"
             " last_sender_display_name FROM SessionTable"
             " ORDER BY sort_timestamp DESC"),
            ("SELECT username, unread_count, summary, last_timestamp, last_msg_sender,"
             " NULL AS last_sender_display_name FROM SessionTable"
             " ORDER BY last_timestamp DESC"),
            ("SELECT username, unread_count, summary, last_timestamp, last_msg_sender"
             " FROM SessionTable ORDER BY last_timestamp DESC"),
        ]
        last_err = None
        for sql in tried:
            try:
                conn = _connect(path)
                try:
                    rows = conn.execute(sql).fetchall()
                finally:
                    conn.close()
                out = []
                for r in rows:
                    keys_ = r.keys()
                    out.append({
                        "username": r["username"],
                        "unread": r["unread_count"] if "unread_count" in keys_ else 0,
                        "summary": r["summary"] if "summary" in keys_ else None,
                        "last_time": r["last_timestamp"] if "last_timestamp" in keys_ else 0,
                        "last_sender": (r["last_msg_sender"]
                                        if "last_msg_sender" in keys_ else ""),
                        "last_sender_name": (r["last_sender_display_name"]
                                             if "last_sender_display_name" in keys_ else ""),
                    })
                return out
            except sqlite3.Error as e:
                last_err = e
        if last_err:
            self.log("  [!] 读会话表失败: %s" % last_err)
        return []

    def names(self, usernames: List[str]) -> Dict[str, str]:
        want = [u for u in usernames if u and u not in self._name_cache]
        if want:
            path = self.contact_plain()
            if path:
                for i in range(0, len(want), 200):
                    chunk = want[i:i + 200]
                    sql = ("SELECT username, nick_name, remark, chat_room_notify "
                           "FROM contact WHERE username IN (%s)" % ",".join("?" * len(chunk)))
                    try:
                        conn = _connect(path)
                        try:
                            for r in conn.execute(sql, chunk):
                                name = (r["remark"] or r["nick_name"] or "").strip()
                                if name:
                                    self._name_cache[r["username"]] = name
                                try:
                                    self._notify[r["username"]] = int(r["chat_room_notify"] or 0)
                                except (TypeError, ValueError, IndexError):
                                    pass
                        finally:
                            conn.close()
                    except sqlite3.Error as e:
                        self.log("  [!] 读联系人失败: %s" % e)
                        break
        out = {}
        for u in usernames:
            if not u:
                continue
            name = self._name_cache.get(u)
            if not name:
                if u == "filehelper":
                    name = "文件传输助手"
                elif is_group(u):
                    name = u.split("@")[0]
                else:
                    name = u
            out[u] = name
        return out

    def is_muted_by_wechat(self, username: str) -> bool:
        """微信里这个群是不是「消息免打扰」。

        实测（4.1.9.57）：群聊的开关就是 contact.chat_room_notify，
            chat_room_notify = 0 → 界面上有「铃铛加斜杠」图标 → 免打扰
            chat_room_notify = 1 → 正常提醒
        1:1 会话这个字段恒为 0（那个开关存在别处），所以只对群聊判定，
        避免把所有人当成静音。
        """
        if not username or not username.endswith(CHATROOM_SUFFIX):
            return False
        if username not in self._notify:
            return False
        return self._notify[username] == 0

    # -- 给「特别关注」选择器用 ----------------------------------------
    def display_name(self, username: str) -> str:
        return self.names([username]).get(username, username)

    def muted_groups(self, limit: int = 300) -> List[dict]:
        """所有「免打扰」的群（按最近消息排序）。"""
        rows = self.sessions()
        want = [r["username"] for r in rows if is_group(r["username"])][:limit]
        if want:
            self.names(want)
        out = []
        for r in rows:
            u = r["username"]
            if not is_group(u) or not self.is_muted_by_wechat(u):
                continue
            out.append({
                "username": u,
                "name": self.display_name(u),
                "unread": int(r.get("unread") or 0),
                "last_time": int(r.get("last_time") or 0),
            })
        out.sort(key=lambda x: -x["last_time"])
        return out

    def group_members(self, username: str) -> List[Tuple[str, str]]:
        """[(显示名, wxid)]：名字优先群昵称，其次联系人备注/昵称。"""
        path = self.contact_plain()
        if not path:
            return []
        try:
            conn = _connect(path)
        except sqlite3.Error:
            return []
        found: Dict[str, str] = {}
        try:
            row = conn.execute("SELECT ext_buffer FROM chat_room WHERE username=?",
                               (username,)).fetchone()
            members = parse_room_members(row["ext_buffer"] if row else None)
            missing = [w for w, nick in members if not nick]
            for i in range(0, len(missing), 120):
                chunk = missing[i:i + 120]
                sql = ("SELECT username, nick_name, remark FROM contact WHERE username IN (%s)"
                       % ",".join("?" * len(chunk)))
                try:
                    for r in conn.execute(sql, chunk):
                        found[r["username"]] = (r["remark"] or r["nick_name"] or "").strip()
                except sqlite3.Error:
                    break
        finally:
            conn.close()
        out = [((nick or found.get(w) or w), w) for w, nick in members]
        out.sort(key=lambda x: x[0].lower())
        return out


# --------------------------------------------------------------------------
# 把微信窗口叫到前台
# --------------------------------------------------------------------------
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
_u32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]

_ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _windows_of(pid: int) -> List[Tuple[int, str, str]]:
    out: List[Tuple[int, str, str]] = []

    def cb(hwnd, _):
        pidd = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pidd))
        if pidd.value == pid:
            buf = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(hwnd, buf, 256)
            cls = ctypes.create_unicode_buffer(256)
            _u32.GetClassNameW(hwnd, cls, 256)
            vis = bool(_u32.IsWindowVisible(hwnd))
            if vis and int(hwnd) and (buf.value or cls.value):
                out.append((int(hwnd), buf.value, cls.value))
        return True

    _u32.EnumWindows(_ENUM(cb), 0)
    return out


def find_wechat_window() -> Optional[int]:
    """找微信主窗口（可见、有标题、进程名 Weixin.exe）。"""
    best, best_area = None, 0
    rect = wintypes.RECT()
    for pid in keymod.weixin_pids():
        for hwnd, title, cls in _windows_of(pid):
            if "Qt" not in cls and "mmui" not in cls.lower() and not title:
                continue
            _u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            if _u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                if area > best_area:
                    best, best_area = hwnd, area
    return best


def focus_wechat() -> bool:
    """把微信叫到前台；窗口不存在时尝试拉起 Weixin.exe。"""
    hwnd = find_wechat_window()
    if hwnd:
        SW_RESTORE = 9
        _u32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        _u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        if _u32.SetForegroundWindow(wintypes.HWND(hwnd)):
            return True
        # 前台锁住了：先模拟 Alt 键解锁（Windows 的常规绕法）
        VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
        _u32.keybd_event(VK_MENU, 0, 0, 0)
        _u32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        return bool(_u32.SetForegroundWindow(wintypes.HWND(hwnd)))
    for cand in (r"C:\Program Files\Tencent\Weixin\Weixin.exe",
                 r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"):
        if os.path.exists(cand):
            try:
                os.startfile(cand)     # noqa: S606 - 拉起官方客户端
                return True
            except OSError:
                pass
    return False


def wechat_is_foreground() -> bool:
    hwnd = _u32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value in set(keymod.weixin_pids())
