# -*- coding: utf-8 -*-
"""轮询会话库，把「新消息」变成事件。"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from . import wxsource


class Watcher(threading.Thread):
    """后台线程：每 N 秒读一次 session.db，比对 last_timestamp 找新消息。

    用会话库而不是消息库，是因为 session.db 只有几百 KB，一秒一读毫无压力，
    而 message_*.db 单个就几百 MB。会话摘要里已经有「谁说了什么」。
    """

    def __init__(self, cfg, emit: Callable[[dict], None],
                 status: Optional[Callable[[str], None]] = None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.emit = emit
        self.status = status or (lambda *_: None)
        self.source = wxsource.WeChatSource(self._work_root(), self.log)
        self._stop = threading.Event()
        self._last: Dict[str, dict] = {}
        self._primed = False
        self._name_cache: Dict[str, str] = {}
        self.last_error = ""
        self.polls = 0
        self.last_count = 0

    @staticmethod
    def _work_root() -> str:
        from . import config
        return config.app_root()

    def log(self, msg: str) -> None:
        self.status(msg)

    # -- 生命周期 -------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        interval = max(0.3, float(self.cfg.get("poll_interval", 1.0)))
        warned = False
        fails = 0
        while not self._stop.is_set():
            if not self.source.account_dir:
                self.log("  [.] 正在定位微信账号目录并取密钥…")
                if not self.source.choose_account(self.cfg.get("account_dir") or None):
                    if not warned:
                        self.log("  [x] 没有可用账号（微信需已打开并登录）。"
                                 "本程序每 10 秒重试一次，登录后会自动接上。")
                        warned = True
                    self._primed = False
                    self._stop.wait(10)
                    continue
                warned = False
                self._primed = False
                self.log("  [√] 使用账号: %s" % self.source.account_id)
            try:
                rows = self.poll_once()
                self.last_error = ""
                if rows is None:
                    fails += 1
                    if fails >= 5:          # 连着读不到 → 密钥可能失效，重新来过
                        self.log("  [!] 连续读不到会话，重新定位账号 / 重新取密钥")
                        self.source.account_dir = None
                        fails = 0
                else:
                    fails = 0
            except Exception as e:                       # 轮询线程绝不能死
                self.last_error = "%s: %s" % (type(e).__name__, e)
                self.log("  [!] 轮询出错: %s" % self.last_error)
            self._stop.wait(interval)

    # -- 单次轮询 -------------------------------------------------------
    def poll_once(self, force_emit: bool = False):
        """读一次会话库；返回事件列表，读不到库时返回 None（区别于「没有新消息」）。"""
        rows = self.source.sessions()
        self.polls += 1
        if not rows:
            return None
        self.last_count = len(rows)
        self._fill_names(rows)
        events: List[dict] = []
        for r in rows:
            u = r["username"]
            if not u:
                continue
            t = int(r.get("last_time") or 0)
            prev = self._last.get(u)
            self._last[u] = r
            if not self._primed:
                continue
            if prev is None and t <= 0:
                continue
            if prev is not None and t <= int(prev.get("last_time") or 0):
                continue
            if not t:
                continue
            ev = self._build_event(r)
            if ev:
                events.append(ev)
        if not self._primed:
            self._primed = True
            self.log("  [√] 已建立基线（%d 个会话），开始监听新消息" % len(rows))
        if force_emit:
            return events
        for ev in events:
            if self._allowed(ev):
                self.emit(ev)
        return events

    def _fill_names(self, rows: List[dict]) -> None:
        wanted = []
        for r in rows[:120]:
            u = r["username"]
            if u and u not in self._name_cache:
                wanted.append(u)
        if wanted:
            self._name_cache.update(self.source.names(wanted))

    def _display(self, username: str) -> str:
        name = self._name_cache.get(username)
        if name:
            return name
        if username == "filehelper":
            return "文件传输助手"
        if wxsource.is_group(username):
            return username.split("@")[0]
        return username

    def _build_event(self, r: dict) -> Optional[dict]:
        username = r["username"]
        if wxsource.is_system(username):
            return None
        if r.get("last_sender") and r["last_sender"] == self.source.self_wxid():
            return None                     # 自己发的（含手机端），不弹
        if self.cfg.get("skip_official") and wxsource.is_official(username):
            return None
        group = wxsource.is_group(username)
        name = self._display(username)
        sender_name = (r.get("last_sender_name") or "").strip()
        if group:
            sender = sender_name or "群成员"
            title = name
            sub = sender
        else:
            title = name
            sub = ""
        # 微信里设了「消息免打扰」的群：默认不弹，但「特别关注」的成员例外
        if self.cfg.get("follow_wechat_mute", True) and self.source.is_muted_by_wechat(username):
            if not self._is_starred(name, sender_name, r.get("last_sender") or ""):
                return None
        body = wxsource.friendly_summary(r.get("summary"))
        if not self.cfg.get("show_content", True):
            body = "你收到了一条新消息"
        return {
            "kind": "message",
            "username": username,
            "title": title,
            "subtitle": sub,
            "body": body,
            "is_group": group,
            "time": int(r.get("last_time") or time.time()),
            "unread": int(r.get("unread") or 0),
            "count": 1,
        }

    def _allowed(self, ev: dict) -> bool:
        if self.cfg.get("only_when_unfocused") and wxsource.wechat_is_foreground():
            return False
        return True

    def _is_starred(self, group_name: str, sender_name: str, sender_wxid: str) -> bool:
        """免打扰群里被「特别关注」的成员：他们的消息照样弹。

        规则形如 "群关键词>成员A,成员B"（写进设置里的「特别关注成员」）。
        成员既可以用群里显示的昵称，也可以直接写 wxid。
        """
        rules = self.cfg.star_rules()
        if not rules:
            return False
        for group_kw, members in rules:
            if group_kw not in group_name:
                continue
            for m in members:
                if (sender_name and (m == sender_name or m in sender_name)) or \
                   (sender_wxid and m == sender_wxid):
                    return True
        return False

    # -- 诊断用 ---------------------------------------------------------
    def snapshot(self, limit: int = 10) -> List[dict]:
        rows = self.source.sessions()
        self._fill_names(rows)
        out = []
        for r in rows[:limit]:
            out.append({
                "会话": self._display(r["username"]),
                "账号": r["username"],
                "未读": r.get("unread"),
                "免打扰": "是" if self.source.is_muted_by_wechat(r["username"]) else "",
                "摘要": wxsource.friendly_summary(r.get("summary")),
                "时间": time.strftime("%m-%d %H:%M", time.localtime(int(r.get("last_time") or 0))),
            })
        return out
