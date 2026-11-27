# -*- coding: utf-8 -*-
"""配置与运行目录。"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

APP_NAME = "微信消息弹窗"
CONFIG_VERSION = 1

DEFAULTS: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "position": "bottom-right",     # bottom-right / bottom-left / top-right / top-left
    "duration": 7,                  # 每条弹窗停留秒数
    "max_stack": 4,                 # 同时最多堆叠几条
    "width": 372,
    "theme": "dark",                # dark / light
    "opacity": 0.97,
    "poll_interval": 1.0,
    "show_content": True,           # 是否显示消息正文（关闭后只显示「收到一条新消息」）
    "only_when_unfocused": True,    # 微信在前台时不打扰
    "skip_official": True,          # 跳过公众号/服务号
    "follow_wechat_mute": True,     # 微信里设了「消息免打扰」的会话不弹
    "star_rules": [],               # 「免打扰群里，这些人照样弹」：每条形如 "群关键词>成员A,成员B"
    "sound": False,
    "autostart": False,
    "account_dir": "",              # 手动指定的账号目录（空=自动挑最新的）
}


def app_root() -> str:
    # WXPOPUP_HOME 可把运行数据挪到别处（便携模式 / 测试用）
    env = os.environ.get("WXPOPUP_HOME")
    if env:
        root = os.path.abspath(env)
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        root = os.path.join(base, APP_NAME)
    os.makedirs(root, exist_ok=True)
    return root


def config_path() -> str:
    return os.path.join(app_root(), "config.json")


def log_path() -> str:
    return os.path.join(app_root(), "运行日志.txt")


class Config(dict):
    def __init__(self, data: Dict[str, Any] | None = None):
        super().__init__(DEFAULTS)
        if data:
            self.update({k: v for k, v in data.items() if k in DEFAULTS or k == "version"})

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                return cls(json.load(f))
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        self["version"] = CONFIG_VERSION
        try:
            tmp = config_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, ensure_ascii=False, indent=1)
            os.replace(tmp, config_path())
        except OSError:
            pass

    # -- 免打扰群里的「特别关注」成员 -----------------------------------
    def star_rules(self) -> list:
        """把配置里的规则解析成 [(群关键词, [成员...]), ...]。"""
        out = []
        for raw in (self.get("star_rules") or []):
            line = str(raw).strip()
            if not line:
                continue
            sep = None
            for cand in (">", "＞", "->", "=>", ":", "："):
                if cand in line:
                    sep = cand
                    break
            if sep is None:
                continue
            group, rest = line.split(sep, 1)
            members = [m.strip() for m in rest.replace("，", ",").replace(";", ",")
                       .replace("；", ",").split(",") if m.strip()]
            if group.strip() and members:
                out.append((group.strip(), members))
        return out
