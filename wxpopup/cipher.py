# -*- coding: utf-8 -*-
"""SQLCipher 4 页级解密 / WAL 合并（微信 4.x WCDB 变体）。

微信 4.x 的库文件格式（实测确认）：
    页大小 4096，reserve 80 = IV(16) + HMAC-SHA512(64)
    页 1 前 16 字节是明文 salt，加密区从第 16 字节开始
    AES-256-CBC，密钥就是内存里抓到的那 32 字节（raw key 模式，不走 256000 轮 KDF）
    MAC key = PBKDF2-HMAC-SHA512(key, salt ^ 0x3A, 2 轮)
    HMAC 覆盖 [salt 之后 .. IV 结束] 共 4016 字节，并追加页号(uint32 LE)

本模块只做纯字节运算，不依赖微信进程，可独立自测（见 selftest()）。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import os
import struct
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SZ = 4096
RESERVE_SZ = 80          # IV(16) + HMAC(64)
KDF_ITER = 256000        # 仅主密钥 -> 库密钥 的派生需要
WAL_HEADER_SZ = 32
WAL_FRAME_SZ = 4120      # 帧头 24B + 加密页 4096B


# --------------------------------------------------------------------------
# 基础运算
# --------------------------------------------------------------------------
def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key[:32]), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key[:32]), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def _mac_key(enc_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key[:32], mac_salt, 2, dklen=32)


def derive_from_master(master: bytes, salt: bytes) -> bytes:
    """主密钥 -> 单库密钥（SQLCipher passphrase 语义，256000 轮）。"""
    return hashlib.pbkdf2_hmac("sha512", master, salt, KDF_ITER, dklen=32)


def verify_key(enc_key: bytes, page1: bytes, salt: Optional[bytes] = None) -> bool:
    """用页 1 的 HMAC 校验密钥是否正确（唯一的权威判据）。"""
    if len(enc_key) not in (32, 48) or len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48:
        enc_key, ex_salt = enc_key[:32], enc_key[32:]
        salt = salt or ex_salt
    if salt is None:
        salt = page1[:16]
    try:
        mk = _mac_key(enc_key, salt)
        h = hmac_mod.new(mk, page1[16: PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
        h.update(struct.pack("<I", 1))
        return hmac_mod.compare_digest(h.digest(), page1[PAGE_SZ - 64:])
    except Exception:
        return False


# --------------------------------------------------------------------------
# 页解密
# --------------------------------------------------------------------------
def decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    """把一页密文还原成合法的 SQLite 页（reserve 区补零）。"""
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        body = page[16: PAGE_SZ - RESERVE_SZ]
        plain_head = page[:16] if len(enc_key) == 48 else b"SQLite format 3\x00"
        return plain_head + aes_cbc_decrypt(enc_key, iv, body) + b"\x00" * RESERVE_SZ
    body = page[: PAGE_SZ - RESERVE_SZ]
    return aes_cbc_decrypt(enc_key, iv, body) + b"\x00" * RESERVE_SZ


def encrypt_page(enc_key: bytes, plain_page: bytes, pgno: int, salt: bytes,
                 iv: Optional[bytes] = None) -> bytes:
    """decrypt_page 的逆运算 —— 只用于自测造样本。"""
    iv = iv or bytes((i * 7 + pgno) & 0xFF for i in range(16))
    if pgno == 1:
        body = plain_page[16: PAGE_SZ - RESERVE_SZ]
        head = salt
    else:
        body = plain_page[: PAGE_SZ - RESERVE_SZ]
        head = b""
    ct = aes_cbc_encrypt(enc_key, iv, body)
    mac_data = ct + iv
    h = hmac_mod.new(_mac_key(enc_key, salt), mac_data, hashlib.sha512)
    h.update(struct.pack("<I", pgno))
    tag = h.digest()
    out = head + ct + iv + tag
    assert len(out) == PAGE_SZ, len(out)
    return out


# --------------------------------------------------------------------------
# 整库解密 + WAL 合并
# --------------------------------------------------------------------------
def decrypt_db(src: str, dst: str, key: bytes) -> int:
    """解密整库到 dst，返回页数。"""
    size = os.path.getsize(src)
    pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    # 临时文件名带上 pid：两个进程（主程序 + 诊断）同时解密同一个库时不会互相覆盖
    tmp = "%s.%d.tmp" % (dst, os.getpid())
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            for pgno in range(1, pages + 1):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page = page + b"\x00" * (PAGE_SZ - len(page))
                fout.write(decrypt_page(key, page, pgno))
        _atomic_replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return pages


def _atomic_replace(tmp: str, dst: str, tries: int = 12) -> None:
    """替换目标文件；目标正被别的进程（sqlite 只读打开）占用时重试。"""
    last = None
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as e:      # 另一个进程正拿着它
            last = e
            time.sleep(0.08 + 0.04 * i)
    raise last if last else OSError("replace failed")


def merge_wal(dst: str, wal_path: str, key: bytes) -> int:
    """把 -wal 里的加密帧覆盖进已解密的主库，返回应用的帧数。

    只合并 salt 与 WAL 头一致的帧：微信 checkpoint 会重置 WAL（salt+1、写游标清零），
    旧世代帧若被合并会用过期页覆盖新数据，直接把库弄坏。
    """
    if not os.path.exists(wal_path) or not os.path.exists(dst):
        return 0
    if os.path.getsize(wal_path) < WAL_HEADER_SZ + WAL_FRAME_SZ:
        return 0
    applied = 0
    max_pgno = 0
    with open(wal_path, "rb") as wal, open(dst, "r+b") as out:
        hdr = wal.read(WAL_HEADER_SZ)
        if len(hdr) < WAL_HEADER_SZ:
            return 0
        wal_salt = hdr[16:24]
        total = (os.path.getsize(wal_path) - WAL_HEADER_SZ) // WAL_FRAME_SZ
        for i in range(total):
            wal.seek(WAL_HEADER_SZ + i * WAL_FRAME_SZ)
            fhdr = wal.read(24)
            page = wal.read(PAGE_SZ)
            if len(fhdr) < 24 or len(page) < PAGE_SZ:
                break
            pgno = struct.unpack(">I", fhdr[0:4])[0]
            if pgno == 0:
                continue
            if fhdr[8:16] != wal_salt:
                continue           # 旧世代帧
            pt = decrypt_page(key, page, pgno)
            if pgno == 1:
                if pt[:16] != b"SQLite format 3\x00":
                    continue
            elif pt[0] not in (0, 2, 5, 10, 13):
                continue           # 页类型非法 → 多半不是本库的帧
            out.seek((pgno - 1) * PAGE_SZ)
            out.write(pt)
            max_pgno = max(max_pgno, pgno)
            applied += 1
        # WAL 里可能带来比主库头部记录更多的页，需要同步页数，否则多出来的页读不到
        out.flush()
        db_pages = (os.path.getsize(dst) + PAGE_SZ - 1) // PAGE_SZ
        out.seek(0)
        page1 = out.read(PAGE_SZ)
        hdr_pages = struct.unpack(">I", page1[28:32])[0]
        new_pages = max(hdr_pages, max_pgno, db_pages)
        if new_pages != hdr_pages:
            out.seek(0)
            out.write(page1[:28] + struct.pack(">I", new_pages) + page1[32:])
            out.flush()
    return applied


def quick_check(path: str) -> bool:
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        finally:
            conn.close()
        return bool(rows) and all(str(r[0]).lower() == "ok" for r in rows)
    except sqlite3.Error:
        return False


def build_plain(src: str, dst: str, key: bytes, with_wal: bool = True) -> bool:
    """解密 + 合并 WAL + 完整性校验，失败时退回不带 WAL 的版本。"""
    decrypt_db(src, dst, key)
    wal = src + "-wal"
    if with_wal and os.path.exists(wal):
        try:
            merge_wal(dst, wal, key)
        except Exception:
            pass
        if not quick_check(dst):
            decrypt_db(src, dst, key)      # WAL 带进坏页 → 丢弃 WAL 重来
    return quick_check(dst)


# --------------------------------------------------------------------------
# 自测：造一个 SQLCipher4 样本库，验证「加密 -> 校验 -> 解密 -> 查询」闭环
# --------------------------------------------------------------------------
def selftest() -> bool:
    import sqlite3
    import tempfile

    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".selftest")
    tmpdir = os.path.join(base, "work")
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    plain = os.path.join(tmpdir, "plain.db")
    enc = os.path.join(tmpdir, "enc.db")
    back = os.path.join(tmpdir, "back.db")

    conn = sqlite3.connect(plain)
    conn.execute("CREATE TABLE t(a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO t VALUES(?,?)",
                     [(i, "消息%d" % i) for i in range(500)])
    conn.commit()
    conn.close()
    # 关键：真实 SQLCipher 库每页尾部 80 字节是 IV+HMAC，sqlite 只用前 4016 字节。
    # 普通库会把数据写进页尾，直接加密再解密就会「丢掉页尾 80 字节」→ malformed。
    raw = bytearray(open(plain, "rb").read())
    raw[20] = RESERVE_SZ                      # header: reserved bytes per page
    open(plain, "wb").write(raw)
    conn = sqlite3.connect(plain)
    conn.execute("VACUUM")                    # 按 4016 可用区重排
    conn.close()

    key = bytes(range(32))
    salt = bytes((i * 11 + 3) & 0xFF for i in range(16))
    size = os.path.getsize(plain)
    pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)

    with open(plain, "rb") as fin, open(enc, "wb") as fout:
        for pgno in range(1, pages + 1):
            page = fin.read(PAGE_SZ)
            page = page + b"\x00" * (PAGE_SZ - len(page))
            fout.write(encrypt_page(key, page, pgno, salt))

    ok = True
    with open(enc, "rb") as f:
        page1 = f.read(PAGE_SZ)
    ok &= verify_key(key, page1)
    ok &= not verify_key(bytes(32), page1)          # 错密钥必须被拒
    ok &= not verify_key(key, page1, salt=b"\x00" * 16)

    decrypt_db(enc, back, key)
    c = sqlite3.connect(back)
    n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    txt = c.execute("SELECT b FROM t WHERE a=499").fetchone()[0]
    c.close()
    ok &= (n == 500 and txt == "消息499")
    ok &= quick_check(back)

    # WAL 合并：造一帧覆盖页 2，验证能被正确解出
    if ok:
        with open(plain, "rb") as f:
            f.seek(PAGE_SZ)
            p2 = f.read(PAGE_SZ)
        newp2 = bytearray(p2)
        newp2[0] = 13                                # 改成 overflow 页头，仅验证落盘
        with open(enc + "-wal", "wb") as w:
            salt1, salt2 = 0x11223344, 0x55667788
            # WAL 头 32B: magic(4) version(4) page_size(4) ckpt_seq(4) salt1(4) salt2(4) ck1(4) ck2(4)
            w.write(b"\x37\x7f\x06\x82" + struct.pack(">III", 3007000, PAGE_SZ, 0)
                    + struct.pack(">IIII", salt1, salt2, 0, 0))
            # 帧头 24B: pgno(4) commit(4) salt1(4) salt2(4) ck1(4) ck2(4)
            w.write(struct.pack(">IIIIII", 2, 1, salt1, salt2, 0, 0))
            w.write(encrypt_page(key, bytes(newp2), 2, salt, iv=b"\xAB" * 16))
        decrypt_db(enc, back, key)
        applied = merge_wal(back, enc + "-wal", key)
        with open(back, "rb") as f:
            f.seek(PAGE_SZ)
            ok &= f.read(1) == bytes([13])
        ok &= (applied == 1)

    print("[自测] 页大小/页数:", pages, "| 校验/解密/WAL:", "全部通过" if ok else "失败")
    if not ok:
        print("[自测] 临时目录:", tmpdir)
    else:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return bool(ok)


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
