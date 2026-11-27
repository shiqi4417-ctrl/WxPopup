# -*- coding: utf-8 -*-
"""从 Weixin.exe 进程内存里只读取回 SQLCipher 密钥。

微信 4.x 把每个库的密钥放在 WCDB 的 Config.Cipher 配置里，形如
    x'<64 hex 库密钥><32 hex salt>'
4.0.3.x 之后这段配置在内存里被 32 字节固定掩码 XOR 过，所以不能直接搜 'x'' 前缀。

本模块只用 ReadProcessMemory 只读扫描（不注入、不挂钩、不写内存）：
    1. 掩码扫描：搜「掩码后的 x' 前缀」，按 32 字节掩码反解，正则取 hex 串；
    2. 明文扫描：老版本配置未掩码，直接搜 x'<hex>'；
    3. 长 hex 串扫描：兜底，搜 64 位连续 hex；
    4. 主密钥回退：候选当作主密钥，PBKDF2-HMAC-SHA512(候选, 库头 salt, 256000) 派生。
候选一律用「页 1 的 HMAC」判定真假——这是唯一权威判据，猜不出来就是猜不出来。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import struct
import time
from ctypes import wintypes
from typing import Dict, Iterator, List, Optional, Set, Tuple

from . import cipher

# WCDB 配置字符串与它的 32 字节异或掩码（4.x 各小版本一致）
CONFIG_NAME = b"com.Tencent.WCDB.Config.Cipher"
XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
MASKED_PREFIX = bytes([0x78 ^ XOR_MASK[0], 0x27 ^ XOR_MASK[1]])   # b"x'" 的掩码形态

_HEX_LITERAL = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
_HEX_RUN = re.compile(rb"(?<![0-9a-fA-F])([0-9a-f]{64})(?![0-9a-fA-F])")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPPROCESS = 0x00000002
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)
CHUNK = 4 * 1024 * 1024
OVERLAP = 1024
NEED = 240

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.ReadProcessMemory.restype = wintypes.BOOL
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__align1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__align2", wintypes.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                ctypes.POINTER(_MBI), ctypes.c_size_t]
_k32.VirtualQueryEx.restype = ctypes.c_size_t
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]


# --------------------------------------------------------------------------
# 进程 / 内存
# --------------------------------------------------------------------------
def weixin_pids() -> List[int]:
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out: List[int] = []
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower() in ("weixin.exe", "wechat.exe"):
                out.append(int(e.th32ProcessID))
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def _open(pid: int):
    return _k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)


def _read(h, addr: int, n: int) -> Optional[bytes]:
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
        if got.value:
            return buf.raw[:got.value]
    return None


def _regions(h, private_only: bool = True) -> Iterator[Tuple[int, int]]:
    addr = 0
    mbi = _MBI()
    while addr < 0x7FFFFFFF0000:
        if not _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize or 0)
        if size <= 0:
            break
        if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in READABLE
                and not (mbi.Protect & PAGE_GUARD) and not (mbi.Protect & PAGE_NOACCESS)):
            # 密钥在堆上（MEM_PRIVATE）。IMAGE/MAPPED 多是 DLL 与数据库文件的映射，
            # 读它们不但没用，还会把文件页从磁盘拉进来，慢到无法接受。
            if not private_only or mbi.Type == MEM_PRIVATE:
                yield base, size
        addr = base + size


def _scan_pass(h, regions: List[Tuple[int, int]], found: Set[bytes],
               deep: bool, deadline: float, log=None) -> int:
    """扫一遍给定区域；返回读取的字节数。"""
    read_total = 0
    for base, size in regions:
        off = 0
        while off < size:
            if time.time() > deadline:
                return read_total
            n = min(CHUNK, size - off)
            data = _read(h, base + off, n)
            if data:
                read_total += len(data)
                _candidates_in(data, True, found)             # 掩码形态（4.0.3+）
                if not found:
                    _candidates_in(data, False, found)        # 明文形态（老版本）
                if deep and not found:
                    for m in _HEX_RUN.finditer(data):         # 兜底：裸 hex
                        found.add(bytes.fromhex(m.group(1).decode()))
            if off + n >= size:                               # 已读到区域末尾
                break
            off += n - OVERLAP
    return read_total


def scan_keys(pid: int, log=None, budget: float = 90.0, quiet: bool = False) -> Set[bytes]:
    """扫描一个微信进程，返回所有「长得像库密钥」的 32 字节候选。

    两轮：先扫堆（快，且密钥一定在堆上），没结果再退化成扫全部可读内存
    （含 DLL/文件映射，慢，作为兜底）。sleep 的进程读起来更快，所以有时间预算。
    """
    found: Set[bytes] = set()
    h = _open(pid)
    if not h:
        if log:
            log("  [x] 无法打开进程 %d（权限不足？试试以管理员身份运行）" % pid)
        return found
    try:
        deadline = time.time() + budget
        priv = sorted(_regions(h, private_only=True), key=lambda r: r[1])
        got = _scan_pass(h, priv, found, deep=True, deadline=deadline)
        if log and not quiet:
            log("  [.] 进程 %d 堆扫描 %.0f MB，候选 %d 个" % (pid, got / 1048576, len(found)))
        if not found:
            t0 = time.time()
            allr = sorted(_regions(h, private_only=False), key=lambda r: r[1])
            got = _scan_pass(h, allr, found, deep=False, deadline=deadline)
            if log and not quiet:
                log("  [.] 进程 %d 全内存兜底扫描 %.0f MB（%.1fs），候选 %d 个"
                    % (pid, got / 1048576, time.time() - t0, len(found)))
    finally:
        _k32.CloseHandle(h)
    return found


def _decode(blob: bytes, masked: bool) -> Optional[bytes]:
    if masked:
        blob = bytes(b ^ XOR_MASK[i % 32] for i, b in enumerate(blob))
    m = _HEX_LITERAL.match(blob)
    if not m:
        return None
    return m.group(1)


def _candidates_in(data: bytes, masked: bool, found: Set[bytes]) -> None:
    prefix = MASKED_PREFIX if masked else b"x'"
    start = 0
    while True:
        i = data.find(prefix, start)
        if i < 0:
            return
        start = i + 2
        hexrun = _decode(data[i:i + NEED], masked)
        if hexrun and len(hexrun) >= 64:
            key = bytes.fromhex(hexrun[:64].decode())
            found.add(key)


# --------------------------------------------------------------------------
# 校验 / 定位可用密钥
# --------------------------------------------------------------------------
def read_page1(db_path: str) -> Optional[bytes]:
    try:
        with open(db_path, "rb") as f:
            page1 = f.read(cipher.PAGE_SZ)
        return page1 if len(page1) == cipher.PAGE_SZ else None
    except OSError:
        return None


def match_keys(cands: Set[bytes], db_paths: List[str], log=None,
               deep: bool = True) -> Dict[str, bytes]:
    """把候选密钥对到具体库文件上（逐库 HMAC 校验）。"""
    need = {}
    for p in db_paths:
        page1 = read_page1(p)
        if page1:
            need[p] = page1
    out: Dict[str, bytes] = {}
    for p, page1 in need.items():
        for k in cands:
            if cipher.verify_key(k, page1):
                out[p] = k
                if log:
                    log("  [√] %s 密钥命中（%s…）" % (os.path.basename(p), k.hex()[:8]))
                break
    if deep and len(out) < len(need):
        # 候选可能是「主密钥」：按 256000 轮 PBKDF2 派生后重试。
        # 每派生一次约 0.1s，所以只取前 60 个候选、命中即停。
        tried = 0
        pool = list(cands)[:60]
        for p, page1 in need.items():
            if p in out:
                continue
            for k in pool:
                tried += 1
                derived = cipher.derive_from_master(k, page1[:16])
                if cipher.verify_key(derived, page1):
                    out[p] = derived
                    if log:
                        log("  [√] %s 由主密钥派生成功" % os.path.basename(p))
                    break
        if log and tried:
            log("  [.] 主密钥派生尝试 %d 次" % tried)
    return out


# --------------------------------------------------------------------------
# 对外：拿密钥（缓存 -> 内存扫描）
# --------------------------------------------------------------------------
class KeyStore:
    """密钥缓存：%APPDATA%\\微信消息弹窗\\keys.json

    存之前会用页 1 HMAC 复核，微信升级换了密钥就自动失效重扫。
    """

    def __init__(self, path: str, log=None):
        self.path = path
        self.log = log
        self.data: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, account: str, db_path: str) -> Optional[bytes]:
        hexkey = (self.data.get(account) or {}).get(os.path.basename(db_path))
        if not hexkey:
            return None
        try:
            key = bytes.fromhex(hexkey)
        except ValueError:
            return None
        page1 = read_page1(db_path)
        return key if page1 and cipher.verify_key(key, page1) else None

    def put(self, account: str, db_path: str, key: bytes) -> None:
        self.data.setdefault(account, {})[os.path.basename(db_path)] = key.hex()

    def forget(self, account: str) -> None:
        self.data.pop(account, None)


def acquire_keys(account: str, db_paths: List[str], store: KeyStore,
                 log=None) -> Dict[str, bytes]:
    """先看缓存，再扫内存；返回 {db_path: key}。"""
    log = log or (lambda *_: None)
    out: Dict[str, bytes] = {}
    for p in db_paths:
        k = store.get(account, p)
        if k:
            out[p] = k
    missing = [p for p in db_paths if p not in out]
    if not missing:
        log("  [√] 全部密钥来自缓存（%d 个库）" % len(out))
        return out

    pids = weixin_pids()
    if not pids:
        log("  [x] 没有发现 Weixin.exe —— 请先打开并登录微信")
        return out
    log("  [.] 发现微信进程 %s，开始只读扫描内存" % pids)
    cands: Set[bytes] = set()
    got: Dict[str, bytes] = {}
    for pid in pids:
        cands |= scan_keys(pid, log)
        if cands:
            got = match_keys(cands, missing, log, deep=False)
            if len(got) == len(missing):
                break
    if not cands:
        log("  [x] 内存里没找到任何候选密钥")
        return out
    if len(got) < len(missing):
        got = match_keys(cands, missing, log)   # 再试一轮主密钥派生
    for p, k in got.items():
        out[p] = k
        store.put(account, p, k)
    store.save()
    return out
