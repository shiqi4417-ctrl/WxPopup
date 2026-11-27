# -*- coding: utf-8 -*-
"""离线集成自测：不用真微信，验证「内存抓密钥 → 解密 → 读会话 → 新消息事件」全链路。

做法：
    1. 按微信 4.x 的页格式造 session.db / contact.db（各用一把独立密钥）；
    2. 另起一个进程，把 x'<key><salt>' 用微信那套 32 字节掩码异或后放进内存；
    3. 从那个进程里扫出密钥，解密自己造的库，比对数据；
    4. 改一行摘要再轮询，确认能产出一条「新消息」事件。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wxpopup import cipher, config as cfgmod, keys as keymod, watcher as watchermod, wxsource  # noqa: E402

WORK = os.path.join(ROOT, ".selftest", "integration")
ACCOUNT_DIR = os.path.join(WORK, "xwechat_files", "wxid_testacct_9f3c")

KEY1 = bytes(range(32))
KEY2 = bytes((0xF0 - i) & 0xFF for i in range(32))
SALT1 = bytes((i * 13 + 5) & 0xFF for i in range(16))
SALT2 = bytes((i * 29 + 9) & 0xFF for i in range(16))

HOLDER = r'''
import sys, time
key = bytes.fromhex(sys.argv[1]); salt = bytes.fromhex(sys.argv[2])
mask = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
text = b"x'" + key.hex().encode() + salt.hex().encode() + b"'"
buf = bytearray(b"\x00" * 40 + bytes(b ^ mask[i % 32] for i, b in enumerate(text)) + b"\x00" * 40)
# 让这段缓冲留在内存里等父进程来扫
while True:
    time.sleep(0.5)
    if buf[39] == 0:
        buf[39] = 0
'''


def encrypt_plain(path: str, key: bytes, salt: bytes) -> None:
    """把明文 sqlite 就地加密成微信那种格式。"""
    size = os.path.getsize(path)
    pages = size // cipher.PAGE_SZ + (1 if size % cipher.PAGE_SZ else 0)
    with open(path, "rb") as f:
        data = f.read()
    out = bytearray()
    for pgno in range(1, pages + 1):
        page = data[(pgno - 1) * cipher.PAGE_SZ: pgno * cipher.PAGE_SZ]
        page = page + b"\x00" * (cipher.PAGE_SZ - len(page))
        out += cipher.encrypt_page(key, page, pgno, salt)
    with open(path, "wb") as f:
        f.write(out)


def make_db(path: str, key: bytes, salt: bytes, ddl: str, rows_sql: str, params) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for p in (path, path + "-wal", path + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    conn = sqlite3.connect(path)
    conn.executescript(ddl)
    if rows_sql:
        conn.executemany(rows_sql, params)
    conn.commit()
    conn.close()
    # 真实 SQLCipher 库每页只用到前 4016 字节
    raw = bytearray(open(path, "rb").read())
    raw[20] = cipher.RESERVE_SZ
    open(path, "wb").write(raw)
    conn = sqlite3.connect(path)
    conn.execute("VACUUM")
    conn.close()
    encrypt_plain(path, key, salt)


def build() -> str:
    sdb = os.path.join(ACCOUNT_DIR, "db_storage", "session", "session.db")
    cdb = os.path.join(ACCOUNT_DIR, "db_storage", "contact", "contact.db")
    make_db(sdb, KEY1, SALT1,
            """CREATE TABLE SessionTable(
                   username TEXT, unread_count INT, summary TEXT, last_timestamp INT,
                   last_msg_sender TEXT, last_sender_display_name TEXT,
                   is_hidden INT DEFAULT 0, sort_timestamp INT);""",
            "INSERT INTO SessionTable VALUES(?,?,?,?,?,?,?,?)",
            [("wxid_friend_a", 2, "在吗？看下我发的合同", 1732690000, "wxid_friend_a", "", 0, 1732690000),
             ("456789@chatroom", 5, "明早九点站会别迟到", 1732689000, "wxid_member_b", "老王", 0, 1732689000),
             ("wxid_testacct", 0, "自己发的消息", 1732680000, "wxid_testacct", "", 0, 1732680000)])
    make_db(cdb, KEY2, SALT2,
            "CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, alias TEXT);",
            "INSERT INTO contact VALUES(?,?,?,?)",
            [("wxid_friend_a", "阿May", "May(客户)", ""),
             ("456789@chatroom", "项目攻坚组", "", ""),
             ("wxid_member_b", "王建国", "", "")])
    return sdb


def main() -> int:
    ok = True
    sdb = build()
    cdb = os.path.join(ACCOUNT_DIR, "db_storage", "contact", "contact.db")

    # 1) 起一个「持有密钥」的进程
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, KEY1.hex(), SALT1.hex()],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    holder2 = subprocess.Popen([sys.executable, "-c", HOLDER, KEY2.hex(), SALT2.hex()],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        t0 = time.time()
        cands = set()
        for p in (holder.pid, holder2.pid):
            cands |= keymod.scan_keys(p, print)
        print("[集成] 扫描进程内存 用时 %.2fs，候选 %d 个" % (time.time() - t0, len(cands)))
        ok &= (KEY1 in cands)
        ok &= (KEY2 in cands)
        print("[集成] 两把密钥都从内存里找回来了:", KEY1 in cands and KEY2 in cands)

        got = keymod.match_keys(cands, [sdb, cdb], print)
        ok &= (got.get(sdb) == KEY1 and got.get(cdb) == KEY2)
        print("[集成] 密钥与库文件配对正确:", got.get(sdb) == KEY1 and got.get(cdb) == KEY2)
    finally:
        for h in (holder, holder2):
            h.terminate()

    # 2) 解密 + 读会话
    src = wxsource.WeChatSource(WORK, print)
    src._use(ACCOUNT_DIR, {sdb: KEY1, cdb: KEY2})
    rows = src.sessions()
    print("[集成] 会话条数:", len(rows))
    ok &= len(rows) == 3
    names = src.names([r["username"] for r in rows])
    print("[集成] 名字解析:", names.get("wxid_friend_a"), "/", names.get("456789@chatroom"))
    ok &= names.get("wxid_friend_a") == "May(客户)"
    body = wxsource.friendly_summary(rows[0]["summary"])
    ok &= body == "在吗？看下我发的合同"

    # 3) 新消息事件
    cfg = cfgmod.Config({"only_when_unfocused": False, "dnd_enabled": False,
                         "skip_official": True, "show_content": True})
    events = []
    w = watchermod.Watcher(cfg, events.append, print)
    w.source = src
    w.poll_once()                                  # 建立基线
    base_len = len(events)
    ok &= base_len == 0
    # 改摘要 + 时间戳 → 触发一条新消息（注意要改「加密的源文件」，不是解密缓存）
    tmp_plain = os.path.join(WORK, "patch.db")
    if os.path.exists(tmp_plain):
        os.remove(tmp_plain)
    cipher.decrypt_db(sdb, tmp_plain, KEY1)
    conn = sqlite3.connect(tmp_plain)
    conn.execute("UPDATE SessionTable SET summary=?, last_timestamp=?, unread_count=3 "
                 "WHERE username=?", ("第三版方案我改完了", 1732691000, "456789@chatroom"))
    conn.commit()
    conn.close()
    encrypt_plain(tmp_plain, KEY1, SALT1)
    os.replace(tmp_plain, sdb)                            # 覆盖加密源，mtime 自然变化
    os.utime(sdb, (time.time() + 2, time.time() + 2))
    events.clear()
    w.poll_once()
    print("[集成] 新消息事件:", events)
    ok &= len(events) == 1
    if events:
        ev = events[0]
        ok &= ev["title"] == "项目攻坚组"
        ok &= ev["subtitle"] == "老王"
        ok &= ev["body"] == "第三版方案我改完了"
        ok &= ev["is_group"] is True

    print("=" * 56)
    print("[集成] 结论:", "全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 56)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
