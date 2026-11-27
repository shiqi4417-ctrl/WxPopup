# -*- coding: utf-8 -*-
"""「特别关注成员」选择器：免打扰群 → 勾选成员（真勾选框）→ 一键写入规则。

既可以在设置窗口里点按钮弹出（嵌进去用），也可以单独运行（--star / 特别关注.bat）。
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Tuple

from . import config as cfgmod
from . import wxsource

CHECKED = "√"
UNCHECKED = ""


class StarPicker(tk.Toplevel):
    def __init__(self, master, cfg, on_saved=None):
        super().__init__(master)
        self.cfg = cfg
        self.on_saved = on_saved
        self.title("选择特别关注的群成员")
        self.geometry("860x580")
        self.minsize(720, 480)
        try:
            # 注意：owner 隐藏时 transient 会把自己也藏起来（独立模式就是这样），
            # 所以只在设置窗口真的可见时才挂 owner。
            if master is not None and master.winfo_viewable():
                self.transient(master)
            self.attributes("-topmost", True)
        except tk.TclError:
            pass

        self.source = wxsource.WeChatSource(cfgmod.app_root())
        self.groups: List[dict] = []
        self.members: List[Tuple[str, str]] = []
        self.checked: Dict[str, str] = {}       # wxid -> 显示名
        self._drafts: Dict[str, tuple] = {}     # 群 username -> (群名, {wxid: 显示名})
        self.current = None
        self._filtered: List[Tuple[str, str]] = []
        self._build()
        self.after(100, self.reload_groups)

    # -- 界面 -----------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.only_muted = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="只看「消息免打扰」的群", variable=self.only_muted,
                        command=self.reload_groups).pack(side="left")
        ttk.Button(top, text="重新读取", command=self.reload_groups).pack(side="left", padx=10)
        self.status = ttk.Label(top, text="读取中…", foreground="#666")
        self.status.pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=12)

        # 左：群列表
        left = ttk.Frame(body)
        ttk.Label(left, text="群（[静] = 微信里设了免打扰）").pack(anchor="w")
        self.lb_groups = tk.Listbox(left, width=28, exportselection=False, activestyle="dotbox",
                                    font=("Microsoft YaHei UI", 9))
        sb1 = ttk.Scrollbar(left, orient="vertical", command=self.lb_groups.yview)
        self.lb_groups.configure(yscrollcommand=sb1.set)
        self.lb_groups.pack(side="left", fill="both", expand=True)
        sb1.pack(side="left", fill="y")
        self.lb_groups.bind("<<ListboxSelect>>", self._on_group)
        body.add(left, weight=1)

        # 右：成员勾选表
        right = ttk.Frame(body)
        head = ttk.Frame(right)
        head.pack(fill="x")
        ttk.Label(head, text="点任意一行勾选/取消，已勾选的会置顶").pack(side="left")
        self.search = ttk.Entry(head, width=16)
        self.search.pack(side="right")
        self.search.bind("<KeyRelease>", lambda e: self._fill_members())
        ttk.Label(head, text="搜索").pack(side="right", padx=(0, 4))

        wrap = ttk.Frame(right)
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        cols = ("chk", "name", "wxid")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("chk", text="选")
        self.tree.heading("name", text="成员")
        self.tree.heading("wxid", text="wxid")
        self.tree.column("chk", width=40, anchor="center", stretch=False)
        self.tree.column("name", width=210, anchor="w")
        self.tree.column("wxid", width=190, anchor="w")
        sb2 = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb2.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb2.pack(side="left", fill="y")
        self.tree.tag_configure("on", background="#E8F7EE")
        self.tree.bind("<Button-1>", self._on_click, add="+")

        tools = ttk.Frame(right)
        tools.pack(fill="x", pady=(6, 0))
        ttk.Button(tools, text="全选当前", command=lambda: self._check_all(True)).pack(side="left")
        ttk.Button(tools, text="全不选", command=lambda: self._check_all(False)).pack(side="left", padx=6)
        self.count_lbl = ttk.Label(tools, text="已选 0 人", foreground="#0a7d43")
        self.count_lbl.pack(side="right")
        body.add(right, weight=3)

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(fill="x")
        self.hint = ttk.Label(bottom, text="先选群，再勾人，最后点「保存」", foreground="#666")
        self.hint.pack(side="left")
        ttk.Button(bottom, text="保存", command=self.save).pack(side="right")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right", padx=8)

    # -- 群 / 成员数据 ---------------------------------------------------
    def reload_groups(self) -> None:
        self.status.configure(text="读取中…")
        self.update_idletasks()
        try:
            acc = self.source.choose_account()
            if not acc:
                self.status.configure(text="没找到微信（需已登录）")
                return
            all_groups = self._all_groups()
            self.groups = [g for g in all_groups if (not self.only_muted.get()) or g["muted"]]
            self.lb_groups.delete(0, "end")
            for g in self.groups:
                self.lb_groups.insert("end", "%s%s%s" % (
                    "[静] " if g["muted"] else "", g["name"],
                    "  %d 未读" % g["unread"] if g["unread"] else ""))
            self.status.configure(text="%d 个群" % len(self.groups))
        except Exception as e:
            self.status.configure(text="读取失败: %s" % e)

    def _all_groups(self) -> List[dict]:
        rows = self.source.sessions()
        names = [r["username"] for r in rows if wxsource.is_group(r["username"])]
        self.source.names(names)
        out = []
        for r in rows:
            u = r["username"]
            if not wxsource.is_group(u):
                continue
            out.append({
                "username": u,
                "name": self.source.display_name(u),
                "unread": int(r.get("unread") or 0),
                "last_time": int(r.get("last_time") or 0),
                "muted": self.source.is_muted_by_wechat(u),
            })
        out.sort(key=lambda x: -x["last_time"])
        return out

    def _on_group(self, _evt=None) -> None:
        sel = self.lb_groups.curselection()
        if not sel:
            return
        # 先把上一个群的勾选存进草稿（按 username 分开，重名群也不会串）
        if self.current:
            self._drafts[self.current["username"]] = (self.current["name"], dict(self.checked))
        self.current = self.groups[sel[0]]
        gname = self.current["name"]
        self.status.configure(text="读取成员…")
        self.update_idletasks()
        self.members = self.source.group_members(self.current["username"])
        draft = self._drafts.get(self.current["username"])
        if draft:
            self.checked = dict(draft[1])
        else:
            self.checked = self._checked_from_saved(gname, self.members)
        self._fill_members()
        self.status.configure(text="%s：%d 位成员" % (gname, len(self.members)))

    def _fill_members(self) -> None:
        kw = self.search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self._filtered = [(n, w) for (n, w) in self.members
                          if not kw or kw in n.lower() or kw in w.lower()]
        # 已勾选的排前面（置顶），组内按名字排
        self._filtered.sort(key=lambda m: (0 if m[1] in self.checked else 1, m[0].lower()))
        for name, wxid in self._filtered:
            on = wxid in self.checked
            self.tree.insert("", "end", iid=wxid,
                             values=(CHECKED if on else UNCHECKED, name, wxid),
                             tags=("on",) if on else ())
        self._update_count()

    def _resort(self) -> None:
        """把已勾选的挪到最上面，组内按名字排（用 move，不重建列表，不闪）。"""
        kids = list(self.tree.get_children())
        kids.sort(key=lambda i: (0 if i in self.checked else 1,
                                 self.tree.set(i, "name").lower()))
        for idx, iid in enumerate(kids):
            self.tree.move(iid, "", idx)

    def _on_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row in self.checked:
            del self.checked[row]
        else:
            self.checked[row] = self.tree.set(row, "name")
        on = row in self.checked
        self.tree.set(row, "chk", CHECKED if on else UNCHECKED)
        self.tree.item(row, tags=("on",) if on else ())
        self._resort()
        self.tree.see(row)          # 刚点的那行保持在视野里
        self._update_count()

    def _check_all(self, on: bool) -> None:
        for name, wxid in self._filtered:
            if on:
                self.checked[wxid] = name
            else:
                self.checked.pop(wxid, None)
        self._fill_members()

    def _update_count(self) -> None:
        self.count_lbl.configure(text="已选 %d 人" % len(self.checked))

    # -- 规则读写 -------------------------------------------------------
    def _rules(self) -> List[str]:
        return [r for r in (self.cfg.get("star_rules") or []) if str(r).strip()]

    def _saved_for(self, gname: str) -> Dict[str, str]:
        out = {}
        for rule in self._rules():
            if not self._rule_matches_group(rule, gname):
                continue
            for sep in (">", "＞", "->", "=>", ":", "："):
                if sep in rule:
                    for m in rule.split(sep, 1)[1].replace("，", ",").split(","):
                        m = m.strip()
                        if m:
                            out[m] = m
                    break
        return out

    def _checked_from_saved(self, gname: str, members: List[Tuple[str, str]]) -> Dict[str, str]:
        """把规则里存的名字还原成 {wxid: 显示名} —— 勾选状态按 wxid 记，不能直接拿名字当键。"""
        saved = set(self._saved_for(gname))
        if not saved:
            return {}
        out: Dict[str, str] = {}
        for name, wxid in members:
            if wxid in saved or name in saved:
                out[wxid] = name
        return out

    def _write_rule(self, gname: str, members: List[str], save: bool = True) -> None:
        rules = [r for r in self._rules() if not self._rule_matches_group(r, gname)]
        if members:
            rules.append("%s>%s" % (gname, ",".join(members)))
        self.cfg["star_rules"] = rules
        if save:
            self.cfg.save()
            try:
                with open(os.path.join(cfgmod.app_root(), "reload.flag"), "w") as f:
                    f.write("1")
            except OSError:
                pass
            if self.on_saved:
                try:
                    self.on_saved()
                except Exception:
                    pass

    def save(self) -> None:
        if not self.current:
            self.hint.configure(text="先选一个群")
            return
        self._drafts[self.current["username"]] = (self.current["name"], dict(self.checked))
        # 规则是按「群名」存的，重名群要先合并再写，否则空的那份会把规则删掉
        merged: Dict[str, List[str]] = {}
        for gname, chk in self._drafts.values():
            lst = merged.setdefault(gname, [])
            for member in chk.values():
                if member not in lst:
                    lst.append(member)
        for gname, members in merged.items():
            self._write_rule(gname, members, save=False)
        self.cfg.save()
        try:
            with open(os.path.join(cfgmod.app_root(), "reload.flag"), "w") as f:
                f.write("1")
        except OSError:
            pass
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                pass
        self.hint.configure(text="已保存：%d 个群（运行中的程序已重载配置）" % len(self._drafts))
        self.status.configure(text=self.hint.cget("text"))

    @staticmethod
    def _rule_matches_group(rule: str, gname: str) -> bool:
        for sep in (">", "＞", "->", "=>", ":", "："):
            if sep in rule:
                return rule.split(sep, 1)[0].strip() in gname
        return False


def open_picker(cfg, on_saved=None):
    """独立窗口模式（--star / 特别关注.bat）。"""
    root = tk.Tk()
    root.withdraw()
    picker = StarPicker(root, cfg, on_saved)
    picker.deiconify()
    picker.lift()

    def on_close():
        try:
            root.destroy()
        except Exception:
            pass

    picker.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
