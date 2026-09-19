#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
紫色晶石 (Stoneshard) 加速器
=============================

原理
----
Stoneshard 是 GameMaker 引擎做的回合制游戏：走一格就是一个回合，
但"这一格走多久"是由引擎主循环的真实时间决定的。

本工具往游戏进程里注入一小段自己拼出来的 x64 机器码，把游戏主程序
导入的计时函数（QueryPerformanceCounter / timeGetTime / GetTickCount64）
换成"快 N 倍"的版本。游戏主循环靠这些计时器排帧，于是整个游戏
（角色行走动画、AI 回合、技能动作）在现实时间里就跑快 N 倍。

和 Cheat Engine 的 Speedhack 是同一种做法，只不过这个是独立程序，
不需要装 CE，也不需要编译器（机器码是脚本里按字节拼出来的）。

安全性
------
* 只改游戏进程内的内存，不写盘、不改游戏文件，退出游戏即恢复。
* 只挂 StoneShard.exe 自己的导入表，不碰系统 DLL。
* 点"还原"或关掉工具，就把原始函数地址写回去。
* 单机游戏，没有联机对战，不存在影响他人的问题。

用法
----
    python stoneshard_accel.py            # 打开图形界面（推荐）
    python stoneshard_accel.py --test     # 命令行：注入并自检加速比例
    python stoneshard_accel.py --set 2.5  # 命令行：把倍率调成 2.5
    python stoneshard_accel.py --restore  # 命令行：还原游戏
    python stoneshard_accel.py --selftest # 不碰游戏，本地验证机器码正确性
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from ctypes import wintypes as wt

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

STEAM_APPID = 625960
PROCESS_NAME = "StoneShard.exe"
RUN_URL = "steam://rungameid/%d" % STEAM_APPID

PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020

PROCESS_ACCESS = (
    PROCESS_CREATE_THREAD
    | PROCESS_QUERY_INFORMATION
    | PROCESS_VM_OPERATION
    | PROCESS_VM_READ
    | PROCESS_VM_WRITE
)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40

INFINITE = 0xFFFFFFFF

BLOCK_SIZE = 0x1000
DATA_OFF = 0x800
SLOT_BASE = DATA_OFF + 0x20
SLOT_SIZE = 0x20
FACTOR_OFF = DATA_OFF + 0x00
FREQ_OFF = DATA_OFF + 0x08


# --------------------------------------------------------------------------
# Win32 原型
# --------------------------------------------------------------------------

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CloseHandle.restype = wt.BOOL

k32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
k32.ReadProcessMemory.restype = wt.BOOL
k32.WriteProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
k32.WriteProcessMemory.restype = wt.BOOL

k32.VirtualAllocEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD,
]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
k32.VirtualFreeEx.restype = wt.BOOL

k32.CreateRemoteThread.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
]
k32.CreateRemoteThread.restype = wt.HANDLE
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.WaitForSingleObject.restype = wt.DWORD
k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetExitCodeThread.restype = wt.BOOL

k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Process32FirstW.restype = wt.BOOL
k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Process32NextW.restype = wt.BOOL
k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Module32FirstW.restype = wt.BOOL
k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Module32NextW.restype = wt.BOOL

k32.FlushInstructionCache.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
k32.FlushInstructionCache.restype = wt.BOOL

k32.VirtualProtectEx.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, ctypes.POINTER(wt.DWORD),
]
k32.VirtualProtectEx.restype = wt.BOOL
k32.VirtualAlloc.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD,
]
k32.VirtualAlloc.restype = ctypes.c_void_p
k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
k32.VirtualFree.restype = wt.BOOL
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetExitCodeProcess.restype = wt.BOOL


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


k32.GetProcessTimes.argtypes = [
    wt.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
]
k32.GetProcessTimes.restype = wt.BOOL


def cpu_seconds(handle):
    """进程累计占用的 CPU 秒数（内核+用户）。"""
    c, e, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                               ctypes.byref(k), ctypes.byref(u)):
        return None
    def val(f):
        return (f.dwHighDateTime << 32) | f.dwLowDateTime
    return (val(k) + val(u)) / 1e7


# --------------------------------------------------------------------------
# 全局快捷键（开关加速）
# --------------------------------------------------------------------------

u32 = ctypes.WinDLL("user32", use_last_error=True)

HOTKEY_ID = 0xB0B1
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
# 依次尝试，第一个能注册上的就用（避免和别的软件撞车）
HOTKEY_CANDIDATES = [("F8", 0x77), ("F9", 0x78), ("F10", 0x79), ("F11", 0x7A)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", wt.UINT), ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT),
    ]


u32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.UINT, wt.UINT]
u32.RegisterHotKey.restype = wt.BOOL
u32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
u32.UnregisterHotKey.restype = wt.BOOL
u32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
u32.GetMessageW.restype = ctypes.c_int
u32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
u32.PostThreadMessageW.restype = wt.BOOL

k32.GetCurrentThreadId.restype = wt.DWORD

WM_QUIT = 0x0012

_hotkey_state = {"name": None}


def register_toggle_hotkey():
    """注册一个全局开关热键，返回键名（失败返回 None）。"""
    for name, vk in HOTKEY_CANDIDATES:
        if u32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, vk):
            _hotkey_state["name"] = name
            return name
    return None


def unregister_toggle_hotkey():
    if _hotkey_state["name"]:
        u32.UnregisterHotKey(None, HOTKEY_ID)
        _hotkey_state["name"] = None


def hotkey_worker(out_queue):
    """在独立线程里收全局热键。

    RegisterHotKey 把热键消息投递到"注册它的那个线程"的消息队列，所以必须在
    自己的线程里注册 + GetMessage；这样也不会和 tkinter (Tcl) 的消息循环打架。
    收到按键就往队列里放一个 "toggle"，由界面线程安全地处理。
    """
    tid = k32.GetCurrentThreadId()
    name = register_toggle_hotkey()
    out_queue.put(("key", name))
    if not name:
        return
    msg = MSG()
    try:
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                out_queue.put(("toggle", tid))
    finally:
        unregister_toggle_hotkey()
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetProcAddress.argtypes = [wt.HMODULE, wt.LPCSTR]
k32.GetProcAddress.restype = ctypes.c_void_p

k32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_int64)]
k32.QueryPerformanceCounter.restype = wt.BOOL
k32.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(ctypes.c_int64)]
k32.QueryPerformanceFrequency.restype = wt.BOOL
k32.GetTickCount64.restype = ctypes.c_uint64


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                               ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_GUARD = 0x100
WRITABLE_PROTECT = (PAGE_READWRITE, PAGE_WRITECOPY, PAGE_EXECUTE_READWRITE,
                    PAGE_EXECUTE_WRITECOPY)


def qpc_now():
    v = ctypes.c_int64()
    k32.QueryPerformanceCounter(ctypes.byref(v))
    return v.value


def qpc_freq():
    v = ctypes.c_int64()
    k32.QueryPerformanceFrequency(ctypes.byref(v))
    return v.value


def find_pid(name=PROCESS_NAME):
    """按进程名找 PID，找不到返回 None。"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wt.HANDLE(-1).value:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        name_l = name.lower()
        while ok:
            if entry.szExeFile.lower() == name_l:
                return int(entry.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        return None
    finally:
        k32.CloseHandle(snap)


def list_modules(pid):
    """返回 [(模块名, 基址, 大小)]"""
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == wt.HANDLE(-1).value:
        return []
    out = []
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.szModule, int(entry.modBaseAddr or 0),
                        int(entry.modBaseSize)))
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


# --------------------------------------------------------------------------
# 进程内存读写
# --------------------------------------------------------------------------

class Mem:
    def __init__(self, handle):
        self.h = handle

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size,
                                     ctypes.byref(got)):
            raise OSError("ReadProcessMemory 失败 @ %#x, err=%d"
                          % (addr, ctypes.get_last_error()))
        return buf.raw[:got.value]

    def write(self, addr, data):
        buf = ctypes.create_string_buffer(data, len(data))
        done = ctypes.c_size_t()
        if not k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr), buf,
                                      len(data), ctypes.byref(done)):
            raise OSError("WriteProcessMemory 失败 @ %#x, err=%d"
                          % (addr, ctypes.get_last_error()))

    def r64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def r32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def u64(self, addr, val):
        self.write(addr, struct.pack("<Q", val & 0xFFFFFFFFFFFFFFFF))

    def f64(self, addr, val):
        self.write(addr, struct.pack("<d", float(val)))


# --------------------------------------------------------------------------
# 迷你 x64 汇编器：只实现本工具用到的那几条指令
# --------------------------------------------------------------------------

class Code:
    """按字节拼机器码。所有 rip 相对引用都指向"同一块内存"内的偏移，
    这样整块内存搬到哪里都不需要重定位。"""

    def __init__(self):
        self.b = bytearray()
        self._rips = []
        self._imms = []

    def pos(self):
        return len(self.b)

    def emit(self, data):
        self.b += data
        return self

    def rip(self, target_off):
        p = len(self.b)
        self.b += b"\x00\x00\x00\x00"
        self._rips.append((p, target_off))
        return self

    def imm64(self, key):
        p = len(self.b)
        self.b += b"\x00" * 8
        self._imms.append((p, key))
        return self

    def resolve(self, abs_map=None):
        for p, tgt in self._rips:
            struct.pack_into("<i", self.b, p, tgt - (p + 4))
        for p, key in self._imms:
            struct.pack_into("<Q", self.b, p, (abs_map or {})[key])
        return bytes(self.b)


def _emit_scale_tail(c, slot):
    """把 rax 里的（原始时间增量）按 factor 累加进 slot+16 的 double，
    结果放回 rax。"""
    c.emit(b"\xF2\x48\x0F\x2A\xC0")              # cvtsi2sd xmm0, rax
    c.emit(b"\xF2\x0F\x59\x05").rip(FACTOR_OFF)  # mulsd    xmm0, [factor]
    c.emit(b"\xF2\x0F\x58\x05").rip(slot + 16)   # addsd    xmm0, [acc]
    c.emit(b"\xF2\x0F\x11\x05").rip(slot + 16)   # movsd    [acc], xmm0
    c.emit(b"\xF2\x48\x0F\x2C\xC0")              # cvttsd2si rax, xmm0


def _emit_counter(c, slot):
    """统计被调用次数（槽位 +24），用来量游戏主循环每秒跑多少次。"""
    c.emit(b"\x48\xFF\x05").rip(slot + 24)       # inc qword [count]


def emit_ptr_stub(c, slot):
    """BOOL f(LARGE_INTEGER *p) 形式，例如 QueryPerformanceCounter。"""
    # 注意：rax/rcx/rdx/r8~r11 是易失寄存器，被调用的系统函数可以随便改，
    # 所以出参指针必须存进非易失寄存器 rbx（并负责恢复）。
    c.emit(b"\x53")                              # push rbx
    _emit_counter(c, slot)                       # inc [count]
    c.emit(b"\x48\x89\xCB")                      # mov  rbx, rcx    (保存出参指针)
    c.emit(b"\x48\x83\xEC\x20")                  # sub  rsp, 0x20   (影子空间)
    c.emit(b"\x48\x8B\x05").rip(slot + 0)        # mov  rax, [orig]
    c.emit(b"\xFF\xD0")                          # call rax
    c.emit(b"\x85\xC0")                          # test eax, eax
    c.emit(b"\x74\x00")                          # je   done  (先占位)
    je_disp = c.pos() - 1
    c.emit(b"\x48\x8B\x0B")                      # mov  rcx, [rbx]
    c.emit(b"\x48\x89\xC8")                      # mov  rax, rcx
    c.emit(b"\x48\x2B\x05").rip(slot + 8)        # sub  rax, [last]
    c.emit(b"\x48\x89\x0D").rip(slot + 8)        # mov  [last], rcx
    _emit_scale_tail(c, slot)
    c.emit(b"\x48\x89\x03")                      # mov  [rbx], rax
    c.emit(b"\xB8\x01\x00\x00\x00")              # mov  eax, 1
    done = c.pos()
    c.b[je_disp] = (done - (je_disp + 1)) & 0xFF
    c.emit(b"\x48\x83\xC4\x20")                  # add  rsp, 0x20
    c.emit(b"\x5B")                              # pop  rbx
    c.emit(b"\xC3")                              # ret


def emit_ret_stub(c, slot, is32):
    """返回值形式，例如 GetTickCount64 / GetTickCount / timeGetTime。"""
    _emit_counter(c, slot)                       # inc [count]
    c.emit(b"\x48\x83\xEC\x28")                  # sub rsp, 0x28
    c.emit(b"\x48\x8B\x05").rip(slot + 0)        # mov rax, [orig]
    c.emit(b"\xFF\xD0")                          # call rax
    if is32:
        c.emit(b"\x89\xC1")                      # mov ecx, eax   (零扩展)
        c.emit(b"\x89\xCA")                      # mov edx, ecx
        c.emit(b"\x2B\x15").rip(slot + 8)        # sub edx, [last]  (32 位减法，天然处理回绕)
        c.emit(b"\x89\x0D").rip(slot + 8)        # mov [last], ecx
        c.emit(b"\x89\xD0")                      # mov eax, edx
    else:
        c.emit(b"\x48\x89\xC1")                  # mov rcx, rax
        c.emit(b"\x48\x2B\x05").rip(slot + 8)    # sub rax, [last]
        c.emit(b"\x48\x89\x0D").rip(slot + 8)    # mov [last], rcx
    _emit_scale_tail(c, slot)
    c.emit(b"\x48\x83\xC4\x28")                  # add rsp, 0x28
    c.emit(b"\xC3")                              # ret


# 计时函数 -> 挂钩形式
STYLES = {
    "QueryPerformanceCounter": "ptr",
    "GetTickCount64": "ret64",
    "GetTickCount": "ret32",
    "timeGetTime": "ret32",
}

DEFAULT_HOOKS = ["QueryPerformanceCounter", "timeGetTime", "GetTickCount64"]


def build_hooks(names):
    """生成挂钩代码块。返回 (代码字节, [(名字, 形式, 槽偏移, 代码偏移)])"""
    c = Code()
    layout = []
    for i, name in enumerate(names):
        style = STYLES[name]
        slot = SLOT_BASE + i * SLOT_SIZE
        off = c.pos()
        if style == "ptr":
            emit_ptr_stub(c, slot)
        else:
            emit_ret_stub(c, slot, style == "ret32")
        layout.append((name, style, slot, off))
    if c.pos() > DATA_OFF:
        raise RuntimeError("代码块超出预留空间")
    return c, layout


# --------------------------------------------------------------------------
# 读目标进程的导入表（IAT），拿每个 API 的调用槽地址
# --------------------------------------------------------------------------

def read_imports(mem, base, max_rva=0x20000000):
    """解析模块导入表，返回 {函数名: [(IAT槽绝对地址, DLL名), ...]}"""
    try:
        dos = mem.read(base, 0x40)
        e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
        if e_lfanew <= 0 or e_lfanew > 0x1000:
            return {}
        nt = mem.read(base + e_lfanew, 0x130)
        if nt[:4] != b"PE\x00\x00":
            return {}
        magic = struct.unpack_from("<H", nt, 0x18)[0]
        dd = 0x70 if magic == 0x20B else 0x60      # 可选头内 DataDirectory 偏移(相对 opthdr)
        imp_rva, imp_size = struct.unpack_from("<II", nt, 0x18 + dd + 8)
        if not imp_rva or imp_rva > max_rva:
            return {}
    except (OSError, struct.error, IndexError):
        return {}

    out = {}
    for di in range(64):
        try:
            desc = mem.read(base + imp_rva + di * 20, 20)
        except OSError:
            break
        oft, _ts, _fc, name_rva, first_thunk = struct.unpack("<IIIII", desc)
        if not (oft or first_thunk or name_rva):
            break
        if not name_rva or name_rva > max_rva:
            continue
        try:
            dll = mem.read(base + name_rva, 64).split(b"\x00")[0].decode("latin1")
        except OSError:
            dll = "?"
        thunk_rva = oft or first_thunk
        if not thunk_rva or thunk_rva > max_rva:
            continue
        for k in range(4096):
            try:
                ent = mem.read(base + thunk_rva + k * 8, 8)
            except OSError:
                break
            v = struct.unpack("<Q", ent)[0]
            if v == 0:
                break
            if v & 0x8000000000000000:            # 按序号导入，忽略
                continue
            if v > max_rva:
                continue
            try:
                nm = mem.read(base + v + 2, 128).split(b"\x00")[0].decode("latin1")
            except OSError:
                continue
            if nm:
                out.setdefault(nm, []).append((base + first_thunk + k * 8, dll))
    return out


# --------------------------------------------------------------------------
# 加速器主体
# --------------------------------------------------------------------------

STILL_ACTIVE = 259
MEAS_OUT_OFF = 0x100
MEAS_RET_OFF = 0x108


def _init_slots(mem, block, layout, now_qpc, now_tick, origs):
    """写好每个挂钩槽的：原始函数地址 / 上次真实时间 / 加速累计值"""
    for name, style, slot, _off in layout:
        orig = origs.get(name, 0)
        if style == "ptr":
            last, acc = now_qpc, float(now_qpc)
        elif style == "ret64":
            last, acc = now_tick, float(now_tick)
        else:
            last, acc = now_tick & 0xFFFFFFFF, float(now_tick & 0xFFFFFFFF)
        mem.u64(block + slot + 0, orig)
        mem.u64(block + slot + 8, last)
        mem.f64(block + slot + 16, acc)


def _build_measure_page(page_addr, stub_addr):
    """生成"调用一次挂钩函数并把返回值/出参记下来"的小代码。"""
    c = Code()
    c.emit(b"\x48\x83\xEC\x28")                # sub rsp, 0x28
    c.emit(b"\x48\x8D\x0D").rip(MEAS_OUT_OFF)  # lea rcx, [出参槽]
    c.emit(b"\x48\xB8").imm64("stub")          # mov rax, stub
    c.emit(b"\xFF\xD0")                        # call rax
    c.emit(b"\x48\x89\x05").rip(MEAS_RET_OFF)  # mov [返回值槽], rax
    c.emit(b"\x48\x83\xC4\x28")                # add rsp, 0x28
    c.emit(b"\xC3")                            # ret
    return c.resolve({"stub": stub_addr})


class Accelerator:
    """负责注入、调速、还原、自检。"""

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)
        self.pid = None
        self.h = None
        self.mem = None
        self.block = None
        self.factor = 1.0
        self.qfreq = qpc_freq() or 10000000
        # name -> (style, slot, stub_off)
        self.layout = {}
        self.patches = []      # [(iat地址, 原始函数地址, 名字)]
        self._pages = []
        self._module_ranges = []

    # ---------------- 状态 ----------------

    @property
    def attached(self):
        return self.block is not None

    def alive(self):
        if not self.h:
            return False
        code = wt.DWORD()
        if not k32.GetExitCodeProcess(self.h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
        self.h = None
        self.mem = None

    # ---------------- 注入 ----------------

    def attach(self, pid=None, hooks=None, hook_all_modules=False):
        if self.attached:
            return self.pid
        hooks = list(hooks or DEFAULT_HOOKS)
        pid = pid or find_pid()
        if not pid:
            raise RuntimeError("没有找到 %s，请先启动游戏。" % PROCESS_NAME)

        h = k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not h:
            err = ctypes.get_last_error()
            tip = "（权限不足，请右键以管理员身份运行本工具）" if err == 5 else ""
            raise RuntimeError("打开游戏进程失败，错误码 %d%s" % (err, tip))
        self.pid = pid
        self.h = h
        self.mem = Mem(h)

        try:
            mods = list_modules(pid)
            self._module_ranges = [(b, b + s) for _n, b, s in mods if s]
            exe = None
            for name, base, size in mods:
                if name.lower() == PROCESS_NAME.lower():
                    exe = (name, base, size)
                    break
            if not exe:
                raise RuntimeError("进程里找不到 %s 模块。" % PROCESS_NAME)
            exe_base = exe[1]

            targets = [exe] if not hook_all_modules else mods
            imports = {}
            for name, base, _size in targets:
                for fn, entries in read_imports(self.mem, base).items():
                    imports.setdefault(fn, []).extend(entries)

            present = [n for n in hooks if n in imports]
            if not present:
                raise RuntimeError(
                    "在游戏导入表里没找到任何计时函数（游戏版本可能变了）。")
            missing = [n for n in hooks if n not in imports]
            if missing:
                self.log("注意：游戏没有导入 %s，跳过。" % "、".join(missing))
            hooks = present

            code_obj, layout = build_hooks(hooks)

            block = k32.VirtualAllocEx(
                h, None, BLOCK_SIZE, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
            if not block:
                raise RuntimeError("在游戏里分配内存失败，错误码 %d"
                                   % ctypes.get_last_error())
            block = int(block)
            self.block = block

            # 先取原始函数地址
            origs = {}
            chosen = []          # [(名字, style, slot, stub_off, [(iat, dll)])]
            for name, style, slot, off in layout:
                ents = imports[name]
                iat, _dll = ents[0]
                origs[name] = self._true_original(iat)
                chosen.append((name, style, slot, off, ents))

            now_qpc = qpc_now()
            now_tick = int(k32.GetTickCount64())

            # 写代码 + 数据（务必在改导入表之前写完，否则游戏可能跳到空指针）
            self.mem.write(block, code_obj.resolve())
            self.mem.f64(block + FACTOR_OFF, 1.0)
            self.mem.u64(block + FREQ_OFF, self.qfreq)
            _init_slots(self.mem, block, layout, now_qpc, now_tick, origs)

            # 改导入表
            for name, style, slot, off, ents in chosen:
                self.layout[name] = (style, slot, off)
                for iat, dll in ents:
                    cur = self.mem.r64(iat)
                    if not self._in_module(cur):
                        # 该槽指向的不是模块内地址（上次注入残留），自愈恢复真地址
                        cur = self._true_original(iat)
                    self._patch_iat(iat, block + off)
                    self.patches.append((iat, cur, name))

            self.factor = 1.0
            self.log("已注入游戏 (PID %d)，共挂 %d 处计时函数。"
                     % (pid, len(self.patches)))
            return pid
        except Exception:
            self.close()
            self.block = None
            raise

    def _patch_iat(self, addr, value):
        old = wt.DWORD()
        if not k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), 8,
                                    PAGE_READWRITE, ctypes.byref(old)):
            raise OSError("VirtualProtectEx 失败，错误码 %d" % ctypes.get_last_error())
        try:
            self.mem.u64(addr, value)
        finally:
            tmp = wt.DWORD()
            k32.VirtualProtectEx(self.h, ctypes.c_void_p(addr), 8, old.value,
                                 ctypes.byref(tmp))
        k32.FlushInstructionCache(self.h, ctypes.c_void_p(addr), 8)

    def _in_module(self, addr):
        return any(lo <= addr < hi for lo, hi in self._module_ranges)

    def _next_in_chain(self, stub_addr):
        """从替身函数的代码里取出它内部记着的"上一个"函数地址。"""
        try:
            code = self.mem.read(stub_addr, 32)
        except OSError:
            return None
        i = code.find(b"\x48\x8B\x05")          # mov rax, [rip+disp32]
        if i < 0 or i + 7 > len(code):
            return None
        disp = struct.unpack_from("<i", code, i + 3)[0]
        try:
            return self.mem.r64(stub_addr + i + 7 + disp)
        except OSError:
            return None

    def _true_original(self, iat_addr, max_depth=32):
        """读导入表里现在的函数地址。

        如果它指向的不是任何已加载模块的内存，说明是本工具以前注入留下的替身
        函数（例如上次没还原就退出了）。替身可能套了好几层，所以顺着链一路找，
        直到找到真正在系统 DLL 里的那个函数地址 —— 避免还原时只剥掉一层，
        或者"替身套替身"越套越深。
        """
        cur = self.mem.r64(iat_addr)
        for _ in range(max_depth):
            if self._in_module(cur):
                return cur
            nxt = self._next_in_chain(cur)
            if not nxt or nxt == cur:
                return cur
            cur = nxt
        return cur

    # ---------------- 调速 ----------------

    def set_factor(self, k):
        k = max(1.0, min(20.0, float(k)))
        if not self.attached:
            raise RuntimeError("还没连接游戏。")
        self.mem.f64(self.block + FACTOR_OFF, k)
        self.factor = k
        return k

    def get_factor(self):
        if not self.attached:
            return None
        return struct.unpack("<d", self.mem.read(self.block + FACTOR_OFF, 8))[0]

    def restore(self):
        """把原始函数地址写回去（内存块保留，避免有线程仍在其中执行）。"""
        if not self.attached:
            return False
        try:
            self.mem.f64(self.block + FACTOR_OFF, 1.0)
            for iat, orig, _name in self.patches:
                self._patch_iat(iat, orig)
        finally:
            self.patches = []
            self.layout = {}
            self.block = None
            self.factor = 1.0
        self.log("已还原游戏原始速度。")
        return True

    def detach(self):
        self.restore()
        self.close()

    # ---------------- 自检：真的加速了吗 ----------------

    def _alloc_page(self):
        page = k32.VirtualAllocEx(self.h, None, BLOCK_SIZE,
                                  MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
        if not page:
            raise OSError("分配自检内存失败，错误码 %d" % ctypes.get_last_error())
        page = int(page)
        self._pages.append(page)
        return page

    def _run_page(self, page, stub_addr):
        code = _build_measure_page(page, stub_addr)
        self.mem.write(page, code)
        self.mem.u64(page + MEAS_OUT_OFF, 0)
        self.mem.u64(page + MEAS_RET_OFF, 0)
        tid = wt.DWORD()
        th = k32.CreateRemoteThread(self.h, None, 0, ctypes.c_void_p(page),
                                    None, 0, ctypes.byref(tid))
        if not th:
            raise OSError("启动远程线程失败，错误码 %d" % ctypes.get_last_error())
        try:
            k32.WaitForSingleObject(th, 5000)
        finally:
            k32.CloseHandle(th)
        return self.mem.r64(page + MEAS_OUT_OFF), self.mem.r64(page + MEAS_RET_OFF)

    def measure(self, name, seconds=1.0):
        """在游戏进程里真实调用一次被挂钩的计时函数，量出实际加速倍数。"""
        if name not in self.layout:
            raise RuntimeError("没有挂钩 %s" % name)
        style, _slot, stub_off = self.layout[name]
        page = self._alloc_page()
        stub_addr = self.block + stub_off

        t0 = qpc_now()
        out_a, ret_a = self._run_page(page, stub_addr)
        time.sleep(seconds)
        t1 = qpc_now()
        out_b, ret_b = self._run_page(page, stub_addr)

        real_ticks = t1 - t0
        if real_ticks <= 0:
            raise RuntimeError("测量失败")
        if style == "ptr":
            return (out_b - out_a) / float(real_ticks)
        real_ms = real_ticks * 1000.0 / self.qfreq
        if style == "ret64":
            delta = (ret_b - ret_a) & 0xFFFFFFFFFFFFFFFF
        else:
            delta = (ret_b - ret_a) & 0xFFFFFFFF
        return delta / real_ms

    def counts(self):
        """各挂钩函数被调用过的累计次数。"""
        out = {}
        for name, (_style, slot, _off) in self.layout.items():
            out[name] = self.mem.r64(self.block + slot + 24)
        return out


# --------------------------------------------------------------------------
# 本地自检：不碰游戏，直接在本进程里跑一遍机器码
# --------------------------------------------------------------------------

class _LocalMem:
    """把本进程内存伪装成和 Mem 一样的接口。"""

    @staticmethod
    def _w(addr, data):
        ctypes.memmove(ctypes.c_void_p(addr), data, len(data))

    def u64(self, addr, val):
        self._w(addr, struct.pack("<Q", val & 0xFFFFFFFFFFFFFFFF))

    def f64(self, addr, val):
        self._w(addr, struct.pack("<d", float(val)))


def local_selftest(factor=2.0, log=print):
    """验证拼出来的机器码在真实 CPU 上工作正常。

    做法：把挂钩代码放到本进程一块可执行内存里，让它去调用真正的系统计时
    函数，然后量它吐出来的时间流速。期望每个函数的比值都约等于 factor。
    """
    page = k32.VirtualAlloc(None, BLOCK_SIZE, MEM_COMMIT | MEM_RESERVE,
                            PAGE_EXECUTE_READWRITE)
    if not page:
        raise OSError("VirtualAlloc 失败")
    page = int(page)
    try:
        winmm = ctypes.WinDLL("winmm")
        kern = k32.GetModuleHandleW("kernel32.dll")
        names = ["QueryPerformanceCounter", "GetTickCount64", "timeGetTime"]
        origs = {
            "QueryPerformanceCounter": k32.GetProcAddress(kern, b"QueryPerformanceCounter"),
            "GetTickCount64": k32.GetProcAddress(kern, b"GetTickCount64"),
            "timeGetTime": k32.GetProcAddress(winmm._handle, b"timeGetTime"),
        }

        code_obj, layout = build_hooks(names)
        _LocalMem()._w(page, code_obj.resolve())
        _LocalMem().f64(page + FACTOR_OFF, factor)
        _init_slots(_LocalMem(), page, layout, qpc_now(),
                    int(k32.GetTickCount64()), origs)

        out = []
        for name, style, _slot, off in layout:
            if style == "ptr":
                proto = ctypes.CFUNCTYPE(ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int64))
                fn = proto(page + off)
                box = [ctypes.c_int64(), ctypes.c_int64()]
            elif style == "ret64":
                proto = ctypes.CFUNCTYPE(ctypes.c_uint64)
                fn = proto(page + off)
            else:
                proto = ctypes.CFUNCTYPE(ctypes.c_uint32)
                fn = proto(page + off)

            t0 = qpc_now()
            if style == "ptr":
                fn(ctypes.byref(box[0]))
            else:
                first = fn()
            time.sleep(1.0)
            t1 = qpc_now()
            if style == "ptr":
                fn(ctypes.byref(box[1]))
                a, b = box[0].value, box[1].value
                ratio = (b - a) / float(t1 - t0)
            else:
                second = fn()
                real_ms = (t1 - t0) * 1000.0 / qpc_freq()
                mask = 0xFFFFFFFFFFFFFFFF if style == "ret64" else 0xFFFFFFFF
                ratio = ((second - first) & mask) / real_ms
            out.append((name, ratio))
        return out
    finally:
        k32.VirtualFree(page, 0, MEM_RELEASE)


# --------------------------------------------------------------------------
# 洗点：直接改存档文件
# --------------------------------------------------------------------------
# 注意：这条路走不通！游戏对存档做了完整性校验（文件里的 32 位串不是常见的
# md5/sha1/sha256/blake2 中的任何一种），自己改出来的存档会被判为"存档损坏"。
# 下面这些代码保留着用于**读取**存档（拿角色当前数值），改数值请用内存方式
# （见 AttrLocator），让游戏自己把新数值写回存档。

# 存档格式（自己解出来的）：
#     zlib( JSON 正文 )  +  32 位随机 ID  +  \0
# 正文里 characterDataMap 就是玩家的角色数据，属性字段是 STR / AGL / PRC /
# Vitality / WIL，另有 AP（未分配的属性点）和 SP（未分配的技能点）。
# 那个 32 位 ID 各种哈希算法都验过，跟内容无关（不是校验和），原样保留即可。

SAVE_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Stoneshard")
CHARS_DIR = os.path.join(SAVE_ROOT, "characters_v1")
BACKUP_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Stoneshard_Backup")

# 属性字段名 -> 中文名
ATTRS = [
    ("STR", "力量"),
    ("AGL", "敏捷"),
    ("PRC", "感知"),
    ("Vitality", "体质"),
    ("WIL", "意志"),
]
ATTR_FLOOR = 10          # 属性重置后的最低值（游戏里任何角色都没低过这个数）
SLOT_ORDER = ["save_1", "save_2", "exitsave_1", "autosave_1", "autosave_2", "autosave_3"]


def decode_save(path):
    """读存档：返回 (JSON 正文字符串, 尾部 32 位 ID, 解析后的对象)"""
    with open(path, "rb") as f:
        text = zlib.decompress(f.read()).decode("utf-8")
    obj, end = json.JSONDecoder().raw_decode(text)
    return text[:end], text[end:].strip("\x00"), obj


def encode_save(body, tail):
    # 注意：尾部那串 ID 和结束符也在压缩流里面，整段一起 zlib 才不会写坏存档
    return zlib.compress((body + tail + "\x00").encode("utf-8"))


def player_span(body):
    """定位玩家自己的 characterDataMap 在正文里的 [起, 止) 字符区间。"""
    i = body.find('"characterDataMap"')
    if i < 0:
        return None
    j = body.find("{", body.find(":", i))
    if j < 0:
        return None
    depth, in_str, esc = 0, False, False
    for p in range(j, len(body)):
        ch = body[p]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (j, p + 1)
    return None


def text_diff(a, b, path=""):
    """比较两个 JSON 对象，列出所有差异路径。"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append((path + "/" + k, None, b[k]))
            elif k not in b:
                out.append((path + "/" + k, a[k], None))
            else:
                out += text_diff(a[k], b[k], path + "/" + k)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path, "长度 %d" % len(a), "长度 %d" % len(b)))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                out += text_diff(x, y, "%s[%d]" % (path, i))
    elif a != b:
        out.append((path, a, b))
    return out


def _fmt_num(v):
    """按游戏的写法输出数字：整数写成 21.0 这种。"""
    return "%.1f" % float(v) if float(v) == int(v) else repr(float(v))


def _list_span(body, key, start=0):
    """定位 "key": [ ... ] 这个数组在正文里的 [起, 止) 区间。"""
    i = body.find('"%s"' % key, start)
    if i < 0:
        return None
    j = body.find("[", body.find(":", i))
    if j < 0:
        return None
    depth, in_str, esc = 0, False, False
    for p in range(j, len(body)):
        ch = body[p]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return (j, p + 1)
    return None


def _split_items(seg):
    """把 "[ a, b, c ]" 拆成元素文本列表（这些元素都是简单值，不含嵌套）。"""
    inner = seg.strip()[1:-1]
    if "[" in inner or "]" in inner:
        raise RuntimeError("列表结构比预期复杂，放弃")
    items, cur, in_str, esc = [], [], False, False
    for ch in inner:
        if in_str:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    rest = "".join(cur).strip()
    if rest:
        items.append(rest)
    return items


def _set_numbers(body, span, pairs):
    """在指定区间里，把若干 "键": 数字 替换成新值；每个键只替换第一次出现。"""
    seg = body[span[0]:span[1]]
    for key, value in pairs:
        pat = re.compile(r'("%s"\s*:\s*)(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)'
                         % re.escape(key))
        def rep(m):
            return m.group(1) + _fmt_num(value)
        seg, n = pat.subn(rep, seg, count=1)
        if n != 1:
            raise RuntimeError("没找到字段 %s（游戏版本可能变了）" % key)
    return body[:span[0]] + seg + body[span[1]:]


def iter_regions(handle):
    """遍历目标进程里所有已提交、可读的内存区域 -> (基址, 大小, 是否可写)"""
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    limit = 0x7FFFFFFFFFFF
    while addr < limit:
        got = k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                 ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not got:
            break
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize)
        if size <= 0:
            break
        prot = int(mbi.Protect)
        if (int(mbi.State) == MEM_COMMIT and not (prot & PAGE_GUARD)
                and not (prot & PAGE_NOACCESS)):
            yield base, size, ((prot & 0xFF) in WRITABLE_PROTECT)
        addr = base + size


class AttrLocator:
    """在游戏进程内存里定位角色属性数值（不碰存档文件）。

    存档只用来"读出角色当前数值"，改数值全部在内存里做，游戏自己会写回存档，
    这样就绕开了存档的完整性校验。
    """

    def __init__(self, pid=None, log=None):
        self.log = log or (lambda m: None)
        self.pid = pid or find_pid()
        if not self.pid:
            raise RuntimeError("游戏没在运行，请先启动游戏并读取角色")
        self.h = k32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE
            | PROCESS_VM_OPERATION, False, self.pid)
        if not self.h:
            err = ctypes.get_last_error()
            tip = "（权限不足，试试用管理员身份运行）" if err == 5 else ""
            raise RuntimeError("打开游戏进程失败，错误码 %d%s" % (err, tip))
        self.mem = Mem(self.h)

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
        self.h = None

    # ---------- 扫描 ----------

    def scan(self, values, extra=(), radius=0x600, chunk=4 << 20):
        """找形如 [STR, AGL, PRC, Vitality, WIL] 的连续 double。

        values: 5 个属性值（从存档里读出来的）
        extra : 其它已知数值（等级、经验、血蓝…），用来给候选打分
        返回按可信度排序的候选列表。
        """
        vals = [float(v) for v in values]
        pat = struct.pack("<d", vals[0])
        extras = [struct.pack("<d", float(v)) for v in extra if float(v)]
        hits = []
        for base, size, writable in iter_regions(self.h):
            off = 0
            while off < size:
                n = min(chunk, size - off)
                try:
                    buf = self.mem.read(base + off, n)
                except OSError:
                    off += n
                    continue
                pos = 0
                while True:
                    i = buf.find(pat, pos)
                    if i < 0:
                        break
                    pos = i + 1
                    addr = base + off + i
                    for stride in (8, 16):
                        ok = True
                        for k in range(1, 5):
                            p = i + k * stride
                            try:
                                if p + 8 <= len(buf):
                                    v = struct.unpack_from("<d", buf, p)[0]
                                else:
                                    v = struct.unpack(
                                        "<d", self.mem.read(addr + k * stride, 8))[0]
                            except (OSError, struct.error):
                                ok = False
                                break
                            if v != vals[k]:
                                ok = False
                                break
                        if not ok:
                            continue
                        lo = max(0, i - radius)
                        hi = min(len(buf), i + 5 * stride + radius)
                        window = buf[lo:hi] if hi > lo else b""
                        score = sum(1 for e in extras if e in window)
                        hits.append({"addr": addr, "stride": stride, "score": score,
                                     "writable": writable, "region": base,
                                     "region_size": size})
                off += n
        hits.sort(key=lambda d: -d["score"])
        return hits

    def read_values(self, addr, stride, count=5):
        return [struct.unpack("<d", self.mem.read(addr + i * stride, 8))[0]
                for i in range(count)]

    def write_values(self, addr, stride, values):
        for i, v in enumerate(values):
            self.mem.write(addr + i * stride, struct.pack("<d", float(v)))

    def dump(self, addr, span=0x60):
        """把某地址附近的 double 打出来，方便人工确认。"""
        out = []
        for o in range(0, span, 8):
            try:
                v = struct.unpack("<d", self.mem.read(addr + o, 8))[0]
            except OSError:
                break
            if abs(v) < 1e6 and v == v:
                out.append("%+.2f" % v)
        return out


def scan_characters():
    """扫描所有角色的存档，返回 [{角色, 存档槽, 姓名, 种族, 职业, 等级, 属性, AP, SP, 路径, 时间}]"""
    out = []
    if not os.path.isdir(CHARS_DIR):
        return out
    for char in sorted(os.listdir(CHARS_DIR)):
        cdir = os.path.join(CHARS_DIR, char)
        if not os.path.isdir(cdir):
            continue
        meta = {}
        mpath = os.path.join(cdir, "character.map")
        if os.path.exists(mpath):
            try:
                _b, _t, m = decode_save(mpath)
                meta = m
            except Exception:
                pass
        for slot in SLOT_ORDER:
            path = os.path.join(cdir, slot, "data.sav")
            if not os.path.exists(path):
                continue
            try:
                _body, _tail, obj = decode_save(path)
                c = obj.get("characterDataMap", {})
                attrs = [float(c.get(k, 0.0)) for k, _cn in ATTRS]
            except Exception as exc:
                out.append({"char": char, "slot": slot, "error": str(exc), "path": path})
                continue
            out.append({
                "char": char, "slot": slot,
                "name": meta.get("nameKey") or c.get("nameKey"),
                "race": c.get("raceKey"), "class": c.get("playerClass"),
                "level": float(c.get("LVL", 0)),
                "attrs": attrs,
                "ap": float(c.get("AP", 0.0)),
                "sp": float(c.get("SP", 0.0)),
                "path": path,
                "mtime": os.path.getmtime(path),
                "permadeath": float(meta.get("permadeath", 0.0)),
            })
    return out


class SaveEditor:
    """洗点 / 改点数：改动前自动备份整份存档目录。"""

    SAVE_WRITE_DISABLED = (
        "改存档这条路已经停用：实测游戏会对存档做完整性校验，改过的存档会被判为"
        "「存档损坏」。请改用内存方式（界面上的洗点窗口选完后用内存洗点，或命令行 "
        "--mem-scan / --mem-write），让游戏自己把新数值写回存档。")

    def __init__(self, log=None):
        self.log = log or (lambda m: None)
        self.last_backup = None

    # ---------- 备份 ----------

    def backup(self):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_ROOT, stamp)
        os.makedirs(dst, exist_ok=True)
        for name in ("characters_v1", "characters.map"):
            src = os.path.join(SAVE_ROOT, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dst, name))
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
        for name in os.listdir(SAVE_ROOT):
            p = os.path.join(SAVE_ROOT, name)
            if os.path.isdir(p) and name not in ("characters_v1",):
                shutil.copytree(p, os.path.join(dst, name))
        self.last_backup = dst
        self.log("已备份存档到 %s" % dst)
        return dst

    def list_backups(self):
        if not os.path.isdir(BACKUP_ROOT):
            return []
        return sorted((d for d in os.listdir(BACKUP_ROOT)
                       if os.path.isdir(os.path.join(BACKUP_ROOT, d))), reverse=True)

    def restore(self, stamp):
        src = os.path.join(BACKUP_ROOT, stamp)
        if not os.path.isdir(src):
            raise RuntimeError("找不到备份 %s" % stamp)
        for name in os.listdir(src):
            s = os.path.join(src, name)
            d = os.path.join(SAVE_ROOT, name)
            if os.path.isdir(s):
                if os.path.isdir(d):
                    shutil.rmtree(d)
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
        self.log("已从备份 %s 恢复存档" % stamp)

    # ---------- 改档 ----------

    def _apply(self, path, body, tail, before, after, expect_keys, do_write):
        diff = text_diff(before, after)
        def leaf(d):
            return re.sub(r"(\[\d+\])+$", "", d[0].rsplit("/", 1)[-1])
        bad = [d for d in diff if leaf(d) not in expect_keys]
        if bad:
            raise RuntimeError("改动范围异常，已放弃：%s" % bad[:3])
        if not diff:
            raise RuntimeError("没有产生任何改动")
        if do_write:
            tmp = path + ".new"
            with open(tmp, "wb") as f:
                f.write(encode_save(body, tail))
            os.replace(tmp, path)
        return diff

    def respec_attrs(self, path, floor=ATTR_FLOOR, do_write=True):
        """属性洗点：五项属性回到 floor，省下的点数全部退回 AP。"""
        if do_write:
            raise RuntimeError(self.SAVE_WRITE_DISABLED)
        body, tail, obj = decode_save(path)
        c = obj["characterDataMap"]
        old = {k: float(c.get(k, 0.0)) for k, _cn in ATTRS}
        old_ap = float(c.get("AP", 0.0))
        total = sum(old.values()) + old_ap
        new_ap = total - floor * len(ATTRS)
        if new_ap < 0:
            raise RuntimeError("点数不够回退（总点数 %.0f < 下限 %d）"
                               % (total, floor * len(ATTRS)))
        span = player_span(body)
        if not span:
            raise RuntimeError("存档里找不到角色数据段")
        pairs = [(k, floor) for k, _cn in ATTRS] + [("AP", new_ap)]
        new_body = _set_numbers(body, span, pairs)
        new_obj = json.loads(new_body)
        diff = self._apply(path, new_body, tail, obj, new_obj,
                           set(k for k, _cn in ATTRS) | {"AP"}, do_write)
        return {"old": old, "old_ap": old_ap, "new_ap": new_ap, "diff": diff,
                "total": total}

    def set_sp(self, path, value, do_write=True):
        """直接设定未分配的技能点（技能树已经学过的技能不会退掉）。"""
        if do_write:
            raise RuntimeError(self.SAVE_WRITE_DISABLED)
        body, tail, obj = decode_save(path)
        old = float(obj["characterDataMap"].get("SP", 0.0))
        span = player_span(body)
        if not span:
            raise RuntimeError("存档里找不到角色数据段")
        new_body = _set_numbers(body, span, [("SP", value)])
        new_obj = json.loads(new_body)
        diff = self._apply(path, new_body, tail, obj, new_obj, {"SP"}, do_write)
        return {"old": old, "new": float(value), "diff": diff}

    def respec_skills(self, path, do_write=True):
        """技能洗点：清空已学技能（连技能栏一起清），并按清掉的技能数返还技能点。

        游戏里加点规则实测是「每级 1 点、每个技能 1 点」，所以清掉 N 个技能就
        返还 N 点；职业初始自带的技能也会一起清掉并计入返还，这样你重新加回来
        不会亏（想换个流派也正好）。
        """
        if do_write:
            raise RuntimeError(self.SAVE_WRITE_DISABLED)
        body, tail, obj = decode_save(path)
        sdm = obj["skillsDataMap"]
        lst = sdm["skillsAllDataList"]
        groups = len(lst) // 5
        learned = [i for i in range(groups) if lst[i * 5 + 1] == 1.0]
        if not learned:
            raise RuntimeError("这个存档已经没有可洗的技能了")

        # 1) 已学标记清零
        span = _list_span(body, "skillsAllDataList")
        if not span:
            raise RuntimeError("找不到技能列表")
        items = _split_items(body[span[0]:span[1]])
        if len(items) != len(lst):
            raise RuntimeError("技能列表长度不符（%d vs %d）" % (len(items), len(lst)))
        for i in learned:
            items[i * 5 + 1] = "0.0"
        body = (body[:span[0]] + "[ " + ", ".join(items) + " ]" + body[span[1]:])

        # 2) 技能栏清空（空位用 -4.0 表示）
        span = _list_span(body, "skillsPanelDataList")
        if not span:
            raise RuntimeError("找不到技能栏数据")
        seg = body[span[0]:span[1]]
        # skillsPanelDataList 是「数组的数组」，逐页处理
        new_pages = []
        for page in re.findall(r"\[[^\[\]]*\]", seg):
            cells = _split_items(page)
            new_pages.append("[ " + ", ".join(
                ("-4.0" if c.strip().startswith('"') else c) for c in cells) + " ]")
        body = body[:span[0]] + "[ " + ", ".join(new_pages) + " ]" + body[span[1]:]

        # 3) 返还技能点
        sp_old = float(obj["characterDataMap"].get("SP", 0.0))
        sp_new = sp_old + len(learned)
        body = _set_numbers(body, player_span(body), [("SP", sp_new)])

        new_obj = json.loads(body)
        diff = self._apply(path, body, tail, obj, new_obj,
                           {"SP", "skillsAllDataList", "skillsPanelDataList"}, do_write)
        return {"cleared": len(learned), "sp_old": sp_old, "sp_new": sp_new, "diff": diff,
                "names": [lst[i * 5] for i in learned]}


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------

def respec_window(parent, log):
    """洗点窗口：列出所有角色存档，支持属性洗点和补技能点。"""
    import tkinter as tk
    from tkinter import messagebox, ttk

    win = tk.Toplevel(parent)
    win.title("洗点 / 改存档")
    win.geometry("880x580+120+60")
    win.transient(parent)

    ed = SaveEditor(log)
    rows = {}
    state = {"only_one": tk.BooleanVar(value=False)}

    tk.Label(win, text="⚠ 改存档已停用：游戏会校验存档，改过的文件会被判为「存档损坏」。"
                       "下面的列表只用来查看角色当前数值/备份；改数值请用内存方式。",
             fg="#a02000", font=("Microsoft YaHei UI", 9, "bold")).pack(
        anchor="w", padx=10, pady=(10, 4))
    tk.Label(win, text="属性洗点 = 五项属性回到下限、省下的点数全部退回 AP（属性点），"
                       "总点数不变；进游戏后在角色界面重新分配。",
             fg="#555555", justify="left", wraplength=820).pack(anchor="w", padx=10)

    cols = ("char", "slot", "name", "race", "lvl", "attrs", "ap", "sp", "time")
    heads = ("角色", "存档", "姓名", "种族", "等级", "力/敏/感/体/意", "属性点", "技能点", "存档时间")
    widths = (78, 92, 70, 62, 48, 130, 62, 62, 132)
    wrap = ttk.Frame(win)
    wrap.pack(fill="both", expand=True, padx=10, pady=6)
    tree = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
    for c, h, w in zip(cols, heads, widths):
        tree.heading(c, text=h)
        tree.column(c, width=w, anchor="center")
    tree.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    sb.pack(side="right", fill="y")
    tree.configure(yscrollcommand=sb.set)

    def refresh():
        tree.delete(*tree.get_children())
        rows.clear()
        data = scan_characters()
        if not data:
            log("没找到存档（%s 不存在？）" % CHARS_DIR)
        for d in data:
            if "error" in d:
                tree.insert("", "end", values=(d["char"], d["slot"], "读取失败", "", "", "",
                                               "", "", ""), tags=("err",))
                continue
            t = time.strftime("%m-%d %H:%M", time.localtime(d["mtime"]))
            iid = tree.insert("", "end", values=(
                d["char"], d["slot"], d["name"], d["race"], "%g" % d["level"],
                "/".join("%g" % a for a in d["attrs"]),
                "%g" % d["ap"], "%g" % d["sp"], t))
            rows[iid] = d
        log("扫描到 %d 份存档" % len(rows))

    def selected():
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("先选一个", "请先在列表里点一行存档。")
            return None
        return rows.get(sel[0])

    def targets(d):
        """默认把该角色的所有存档一起改，避免游戏读的是另一份。"""
        if state["only_one"].get():
            return [d]
        return [x for x in rows.values() if x.get("char") == d["char"]]

    def do_respec():
        d = selected()
        if not d:
            return
        if find_pid():
            if not messagebox.askyesno("游戏还在运行",
                                       "检测到游戏正在运行，改动可能被覆盖。\n仍然继续吗？"):
                return
        tgt = targets(d)
        if not messagebox.askyesno("确认洗点",
                                   "将对 %s 的 %d 份存档做属性洗点：\n"
                                   "属性回到 %d，省下的点数退回「属性点」。\n\n继续吗？"
                                   % (d["char"], len(tgt), ATTR_FLOOR)):
            return
        try:
            ed.backup()
            for t in tgt:
                r = ed.respec_attrs(t["path"])
                log("  %s/%s：属性点 %.0f -> %.0f（总点数 %.0f）"
                    % (t["char"], t["slot"], r["old_ap"], r["new_ap"], r["total"]))
            refresh()
            messagebox.showinfo("完成", "洗点完成。\n进游戏后在角色界面重新分配点数。\n"
                                        "如果游戏不认存档，用「恢复备份」还原。")
        except Exception as exc:
            log("洗点失败：%s" % exc)
            messagebox.showerror("失败", str(exc))

    def do_sp():
        d = selected()
        if not d:
            return
        try:
            value = float(sp_var.get())
        except ValueError:
            messagebox.showerror("数值不对", "技能点要填数字。")
            return
        if find_pid() and not messagebox.askyesno(
                "游戏还在运行", "检测到游戏正在运行，改动可能被覆盖。仍然继续吗？"):
            return
        tgt = targets(d)
        try:
            ed.backup()
            for t in tgt:
                r = ed.set_sp(t["path"], value)
                log("  %s/%s：技能点 %.0f -> %.0f" % (t["char"], t["slot"], r["old"], r["new"]))
            refresh()
            messagebox.showinfo("完成", "技能点已改为 %g。" % value)
        except Exception as exc:
            log("改技能点失败：%s" % exc)
            messagebox.showerror("失败", str(exc))

    def do_respec_skills():
        d = selected()
        if not d:
            return
        if find_pid() and not messagebox.askyesno(
                "游戏还在运行", "检测到游戏正在运行，改动可能被覆盖。仍然继续吗？"):
            return
        tgt = targets(d)
        if not messagebox.askyesno(
                "确认技能洗点",
                "将清空 %s 的 %d 份存档里「学会的技能」（技能栏也一起清空），\n"
                "并按清掉的技能数量返还技能点。\n\n"
                "注意：职业初始自带的技能也会一起清掉，所以返还的点数会比你自己\n"
                "花掉的多几个（那几个正好用来把初始技能学回来），不会亏。\n\n继续吗？"
                % (d["char"], len(tgt))):
            return
        try:
            ed.backup()
            for t in tgt:
                r = ed.respec_skills(t["path"])
                log("  %s/%s：清空 %d 个技能，技能点 %g -> %g"
                    % (t["char"], t["slot"], r["cleared"], r["sp_old"], r["sp_new"]))
            refresh()
            messagebox.showinfo("完成", "技能已清空、点数已返还。\n"
                                        "进游戏后在技能面板里重新学。")
        except Exception as exc:
            log("技能洗点失败：%s" % exc)
            messagebox.showerror("失败", str(exc))

    def do_backup():
        try:
            ed.backup()
            messagebox.showinfo("已备份", "备份目录：\n%s" % ed.last_backup)
        except Exception as exc:
            messagebox.showerror("备份失败", str(exc))

    def do_restore():
        backups = ed.list_backups()
        if not backups:
            messagebox.showinfo("没有备份", "还没有备份。")
            return
        top = tk.Toplevel(win)
        top.title("选择要恢复的备份")
        top.geometry("360x260")
        lb = tk.Listbox(top)
        for b in backups:
            lb.insert("end", b)
        lb.pack(fill="both", expand=True, padx=8, pady=8)

        def go():
            sel = lb.curselection()
            if not sel:
                return
            stamp = backups[sel[0]]
            if not messagebox.askyesno("确认恢复",
                                       "用备份 %s 覆盖当前存档？当前存档会被替换。" % stamp):
                return
            try:
                ed.restore(stamp)
                refresh()
                top.destroy()
                messagebox.showinfo("完成", "已恢复备份 %s" % stamp)
            except Exception as exc:
                messagebox.showerror("恢复失败", str(exc))

        ttk.Button(top, text="恢复选中的备份", command=go).pack(pady=8)

    bar = ttk.Frame(win)
    bar.pack(fill="x", padx=10)
    ttk.Button(bar, text="刷新列表", command=refresh, width=10).pack(side="left")
    ttk.Button(bar, text="备份全部存档", command=do_backup, width=14).pack(side="left", padx=6)
    ttk.Button(bar, text="恢复备份…", command=do_restore, width=12).pack(side="left")
    ttk.Checkbutton(bar, text="只改选中的这一份存档", variable=state["only_one"]).pack(
        side="left", padx=12)

    act = ttk.LabelFrame(win, text="操作", padding=8)
    act.pack(fill="x", padx=10, pady=8)
    r1 = ttk.Frame(act)
    r1.pack(fill="x")
    ttk.Label(r1, text="属性洗点（五项属性回到 %d，点数退回属性点）：" % ATTR_FLOOR).pack(side="left")
    ttk.Button(r1, text="洗属性", command=do_respec, width=10).pack(side="left", padx=6)
    r2 = ttk.Frame(act)
    r2.pack(fill="x", pady=(6, 0))
    ttk.Label(r2, text="技能洗点（清空已学技能，按数量返还技能点）：").pack(side="left")
    ttk.Button(r2, text="洗技能", command=do_respec_skills, width=10).pack(side="left", padx=6)
    ttk.Label(r2, text="　技能点直接设为：").pack(side="left")
    sp_var = tk.StringVar(value="1")
    ttk.Entry(r2, textvariable=sp_var, width=6).pack(side="left")
    ttk.Button(r2, text="应用", command=do_sp, width=8).pack(side="left", padx=6)

    logbox = tk.Text(win, height=8, state="disabled", wrap="word",
                     bg="#111111", fg="#d0d0d0", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def log_to_window(msg):
        log(msg)
        logbox.configure(state="normal")
        logbox.insert("end", "%s %s\n" % (time.strftime("%H:%M:%S"), msg))
        logbox.see("end")
        logbox.configure(state="disabled")

    ed.log = log_to_window
    refresh()
    return win


def run_gui(open_respec=False):
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("紫色晶石 加速器")
    root.geometry("520x430")
    root.minsize(480, 400)

    accel = Accelerator()
    state = {"auto": tk.BooleanVar(value=True), "want": 1.5, "on": True}

    # ---- 日志 ----
    def log(msg):
        ts = time.strftime("%H:%M:%S")
        logbox.configure(state="normal")
        logbox.insert("end", "[%s] %s\n" % (ts, msg))
        logbox.see("end")
        logbox.configure(state="disabled")

    accel.log = log

    # ---- 全局开关热键（在独立线程里收）----
    hk_queue = queue.Queue()
    hk_thread_id = {"id": 0}

    def hotkey_boot():
        hk_thread_id["id"] = k32.GetCurrentThreadId()
        hotkey_worker(hk_queue)

    threading.Thread(target=hotkey_boot, daemon=True).start()

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    status = ttk.Label(top, text="状态：未连接", font=("Microsoft YaHei UI", 10, "bold"))
    status.pack(anchor="w")
    hint = ttk.Label(top, text="倍率越高角色走得越快（1.0 = 原始速度）",
                     foreground="#666666")
    hint.pack(anchor="w", pady=(2, 0))
    hk_var = tk.StringVar(value="快捷键：正在注册…")
    ttk.Label(top, textvariable=hk_var, foreground="#666666").pack(anchor="w",
                                                                  pady=(2, 0))

    box = ttk.LabelFrame(root, text="速度倍率", padding=10)
    box.pack(fill="x", padx=10, pady=6)

    val_text = tk.StringVar(value="1.5 x")
    row = ttk.Frame(box)
    row.pack(fill="x")
    ttk.Label(row, textvariable=val_text, width=8,
              font=("Consolas", 14, "bold")).pack(side="left")
    slider = ttk.Scale(row, from_=1.0, to=5.0, orient="horizontal")
    slider.set(1.5)
    slider.pack(side="left", fill="x", expand=True, padx=8)

    def apply_effective():
        """把「开关状态 + 目标倍率」落到游戏里。"""
        if not accel.attached:
            return
        try:
            accel.set_factor(state["want"] if state["on"] else 1.0)
        except Exception as exc:
            log("调速失败：%s" % exc)

    def apply_factor(_evt=None):
        k = round(float(slider.get()), 2)
        state["want"] = k
        state["on"] = True
        val_text.set("%.1f x" % k)
        apply_effective()
        refresh_status()

    slider.configure(command=apply_factor)

    quick = ttk.Frame(box)
    quick.pack(fill="x", pady=(8, 0))
    ttk.Label(quick, text="快捷：").pack(side="left")
    for k in (1.0, 1.5, 2.0, 3.0, 4.0, 5.0):
        def mk(v):
            def go():
                slider.set(v)
                apply_factor()
            return go
        ttk.Button(quick, text="%.1fx" % k, width=6, command=mk(k)).pack(side="left", padx=2)

    btns = ttk.Frame(root, padding=(10, 0))
    btns.pack(fill="x")

    def refresh_status():
        pid = find_pid()
        if accel.attached and not accel.alive():
            accel.restore()
            accel.close()
        if accel.attached:
            if state["on"] and accel.factor > 1.001:
                txt = "状态：已连接（PID %d）  加速中 %.1fx" % (accel.pid, accel.factor)
                color = "#0a7a0a"
            else:
                txt = "状态：已连接（PID %d）  加速已关闭（原速）" % accel.pid
                color = "#666666"
            if state.get("hotkey"):
                txt += "   [%s 开关]" % state["hotkey"]
            status.configure(text=txt, foreground=color)
        elif pid:
            status.configure(text="状态：发现游戏（PID %d），未注入" % pid,
                             foreground="#a06000")
        else:
            status.configure(text="状态：游戏未运行", foreground="#666666")

    def do_attach(silent=False):
        try:
            accel.attach()
            apply_effective()
            log("连接成功，%s" % ("当前倍率 %.1fx" % state["want"] if state["on"]
                                else "加速处于关闭状态"))
        except Exception as exc:
            if not silent:
                messagebox.showwarning("连接失败", str(exc))
            else:
                log("自动连接失败：%s" % exc)
        refresh_status()

    def on_toggle():
        """开关加速：关 = 恢复原速但不断开注入，开 = 回到滑块上的倍率。"""
        state["on"] = not state["on"]
        if state["on"] and not accel.attached and find_pid():
            do_attach(silent=True)
        apply_effective()
        log("快捷键：%s" % ("已开启加速 %.1fx" % state["want"] if state["on"]
                          else "已关闭加速，恢复原速"))
        refresh_status()

    def on_connect():
        if find_pid() is None:
            if messagebox.askyesno("游戏未运行", "现在没有检测到游戏，要帮你启动吗？\n"
                                                "（启动后本工具会自动连接）"):
                do_launch()
            return
        do_attach()

    def do_launch():
        try:
            os.startfile(RUN_URL)
            log("已请求 Steam 启动游戏，启动后会自动连接…")
        except Exception as exc:
            log("启动失败：%s" % exc)

    def on_restore():
        try:
            accel.restore()
            slider.set(1.0)
            state["want"] = 1.0
            state["on"] = True
            val_text.set("1.0 x")
        except Exception as exc:
            log("还原失败：%s" % exc)
        refresh_status()

    def on_check():
        if not accel.attached:
            messagebox.showinfo("还没连接", "先点「连接游戏」再自检。")
            return
        log("自检中（约 4 秒，期间会临时切到 2.0x）…")
        keep = state["want"]

        def work():
            try:
                accel.set_factor(2.0)
                time.sleep(0.3)
                res = [(n, accel.measure(n)) for n in list(accel.layout)]
                accel.set_factor(keep)
                msg = "自检结果（设 2.0x 实测）：" + "，".join(
                    "%s %.2fx" % (n, r) for n, r in res)
            except Exception as exc:
                msg = "自检失败：%s" % exc
            root.after(0, lambda: log(msg))

        threading.Thread(target=work, daemon=True).start()

    def on_quit():
        if hk_thread_id["id"]:
            u32.PostThreadMessageW(hk_thread_id["id"], WM_QUIT, 0, 0)
        if accel.attached:
            try:
                accel.restore()
            except Exception:
                pass
        accel.close()
        root.destroy()

    ttk.Button(btns, text="连接游戏", command=on_connect, width=12).pack(side="left")
    ttk.Button(btns, text="启动游戏", command=do_launch, width=12).pack(side="left", padx=6)
    ttk.Button(btns, text="还原原速", command=on_restore, width=12).pack(side="left")
    ttk.Button(btns, text="自检", command=on_check, width=8).pack(side="left", padx=6)
    ttk.Checkbutton(btns, text="自动连接", variable=state["auto"]).pack(side="left", padx=10)
    ttk.Button(btns, text="退出", command=on_quit, width=8).pack(side="right")

    row2 = ttk.Frame(root, padding=(10, 6))
    row2.pack(fill="x")
    ttk.Button(row2, text="洗点 / 改存档", width=16,
               command=lambda: respec_window(root, log)).pack(side="left")
    ttk.Label(row2, text="（改存档需要先完全退出游戏）",
              foreground="#888888").pack(side="left", padx=8)

    logbox = tk.Text(root, height=12, state="disabled", wrap="word",
                     bg="#111111", fg="#d0d0d0", font=("Consolas", 9))
    logbox.pack(fill="both", expand=True, padx=10, pady=10)

    log("工具已就绪。游戏启动后会自动注入。")
    log("提示：注入后先用 1.5~2.0 倍，太快了再往下调。")

    def poll_hotkey_queue():
        while True:
            try:
                kind, val = hk_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "key":
                if val:
                    state["hotkey"] = val
                    hk_var.set("快捷键 %s：开启 / 关闭加速（全局有效，游戏里也能按）"
                               % val)
                    log("快捷键已就绪：按 %s 开关加速。" % val)
                else:
                    hk_var.set("快捷键注册失败（可能被其它软件占用了）")
                    log("警告：F8~F11 都被占用了，全局快捷键没启用。")
            elif kind == "toggle":
                on_toggle()
        root.after(100, poll_hotkey_queue)

    def tick():
        refresh_status()
        if state["auto"].get() and not accel.attached and find_pid():
            do_attach(silent=True)
        root.after(1500, tick)

    root.protocol("WM_DELETE_WINDOW", on_quit)
    root.after(500, tick)
    root.after(100, poll_hotkey_queue)
    if open_respec:
        root.after(300, lambda: respec_window(root, log))
    root.mainloop()


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="紫色晶石 (Stoneshard) 加速器 —— 提高角色移动速度")
    ap.add_argument("--set", type=float, metavar="倍数",
                    help="连接游戏并把倍率设为该值，例如 --set 2")
    ap.add_argument("--test", action="store_true",
                    help="连接游戏并实测加速比例（自检）")
    ap.add_argument("--benchmark", type=float, metavar="高倍率", default=None,
                    help="对比 1x 与指定倍率下游戏的 CPU 占用，验证主循环真的变快")
    ap.add_argument("--restore", action="store_true", help="还原游戏原始速度")
    ap.add_argument("--selftest", action="store_true",
                    help="不碰游戏，本地验证机器码")
    ap.add_argument("--hooks", default=",".join(DEFAULT_HOOKS),
                    help="要挂钩的计时函数，逗号分隔")
    ap.add_argument("--hook-all-modules", action="store_true",
                    help="连游戏目录下所有 DLL 的导入表一起挂（默认只挂主程序）")
    ap.add_argument("--respec-list", action="store_true",
                    help="列出所有角色存档（等级、属性、属性点、技能点）")
    ap.add_argument("--respec", metavar="角色[/存档槽]",
                    help="属性洗点：属性回到下限，点数退回属性点（会自动先备份）")
    ap.add_argument("--respec-skills", metavar="角色[/存档槽]",
                    help="技能洗点：清空已学技能，按清掉的技能数返还技能点")
    ap.add_argument("--respec-sp", metavar="角色[/存档槽]=数值",
                    help="把未分配的技能点设为指定值")
    ap.add_argument("--respec-backup", action="store_true", help="备份全部存档")
    ap.add_argument("--respec-restore", metavar="备份时间戳", help="从备份恢复存档")
    ap.add_argument("--respec-gui", action="store_true",
                    help="打开界面并直接弹出洗点窗口")
    ap.add_argument("--mem-scan", metavar="角色[/存档槽] 或 属性值",
                    help="在游戏内存里定位角色属性数值（例如 --mem-scan character_3）")
    ap.add_argument("--mem-extra", metavar="数值列表", default="",
                    help="扫描时用于加分的额外已知数值，如 25,4563")
    ap.add_argument("--mem-write", metavar="地址:步长",
                    help="把新属性写到指定地址（先用 --mem-scan 找到地址）")
    ap.add_argument("--mem-attrs", metavar="力,敏,感,体,意",
                    help="要写入的五项属性值")
    ap.add_argument("--mem-dump", metavar="地址",
                    help="打印该地址附近的数值，人工确认用")
    args = ap.parse_args(argv)

    log = lambda m: print(m, flush=True)

    # ---------- 内存方式（不碰存档） ----------
    if args.mem_dump:
        loc = AttrLocator(log=log)
        try:
            addr = int(args.mem_dump, 0)
            print("地址 %#x 附近的 8 字节浮点数：" % addr)
            print("  " + "  ".join(loc.dump(addr)))
        finally:
            loc.close()
        return 0

    if args.mem_write:
        loc = AttrLocator(log=log)
        try:
            addr_s, stride_s = args.mem_write.split(":")
            addr, stride = int(addr_s, 0), int(stride_s)
            if not args.mem_attrs:
                print("要写属性就用 --mem-attrs 力,敏,感,体,意")
                return 1
            new = [float(x) for x in re.split(r"[,，]", args.mem_attrs) if x.strip()]
            if len(new) != 5:
                print("--mem-attrs 要正好 5 个数值")
                return 1
            old = loc.read_values(addr, stride)
            print("写入前: %s" % "/".join("%g" % v for v in old))
            loc.write_values(addr, stride, new)
            now = loc.read_values(addr, stride)
            print("写入后: %s" % "/".join("%g" % v for v in now))
            print("现在切回游戏看看角色面板，数值应该已经变了。")
        finally:
            loc.close()
        return 0

    if args.mem_scan:
        spec = args.mem_scan.strip()
        extra = [float(x) for x in re.split(r"[,，]", args.mem_extra) if x.strip()]
        if re.fullmatch(r"[\d.,，\s]+", spec):
            values = [float(x) for x in re.split(r"[,，]", spec) if x.strip()]
        else:
            char, _, slot = spec.partition("/")
            cand = [d for d in scan_characters()
                    if d.get("char") == char and (not slot or d.get("slot") == slot)]
            if not cand:
                print("没找到存档：%s" % spec)
                return 1
            d = cand[0]
            values = list(d["attrs"])
            extra += [d["level"], d["ap"], d["sp"]]
            print("用 %s/%s（%s 等级%g）的属性做特征：%s"
                  % (d["char"], d["slot"], d["name"], d["level"],
                     "/".join("%g" % v for v in values)))
        if len(values) != 5:
            print("需要 5 个属性值")
            return 1
        loc = AttrLocator(log=log)
        try:
            print("扫描进程内存中…（内存大，可能要十几秒）")
            t0 = time.time()
            hits = loc.scan(values, extra)
            print("用了 %.1f 秒，找到 %d 个候选：" % (time.time() - t0, len(hits)))
            for h in hits[:12]:
                mark = "可写" if h["writable"] else "只读"
                print("  地址 %#012x 步长 %-2d  额外命中 %d  %s"
                      % (h["addr"], h["stride"], h["score"], mark))
            if hits:
                best = hits[0]
                print("\n最可能的地址附近的值：")
                print("  " + "  ".join(loc.dump(best["addr"] - 0x40)))
        finally:
            loc.close()
        return 0

    # ---------- 洗点 / 改存档（命令行） ----------
    if args.respec_list:
        data = scan_characters()
        if not data:
            print("没找到存档：%s" % CHARS_DIR)
            return 1
        print("%-13s %-11s %-8s %-8s %5s %-24s %6s %6s" %
              ("角色", "存档", "姓名", "种族", "等级", "力/敏/感/体/意", "属性点", "技能点"))
        for d in data:
            if "error" in d:
                print("%-13s %-11s 读取失败: %s" % (d["char"], d["slot"], d["error"]))
                continue
            print("%-13s %-11s %-8s %-8s %5g %-24s %6g %6g" % (
                d["char"], d["slot"], d["name"], d["race"], d["level"],
                "/".join("%g" % a for a in d["attrs"]), d["ap"], d["sp"]))
        print("\n备份目录: %s" % BACKUP_ROOT)
        for b in SaveEditor().list_backups()[:5]:
            print("  已有备份: %s" % b)
        return 0

    if args.respec_backup:
        ed = SaveEditor(log)
        ed.backup()
        return 0

    if args.respec_restore:
        ed = SaveEditor(log)
        ed.restore(args.respec_restore)
        return 0

    if args.respec or args.respec_sp or args.respec_skills:
        spec = args.respec or args.respec_sp or args.respec_skills
        value = None
        if args.respec_sp:
            if "=" not in spec:
                print("格式：--respec-sp 角色=数值")
                return 1
            spec, _, v = spec.partition("=")
            value = float(v)
        char, _, slot = spec.partition("/")
        data = [d for d in scan_characters()
                if d.get("char") == char and (not slot or d.get("slot") == slot)]
        if not data:
            print("没找到匹配的存档：%s" % spec)
            return 1
        ed = SaveEditor(log)
        ed.backup()
        for d in data:
            if args.respec_sp:
                r = ed.set_sp(d["path"], value)
                print("%s/%s：技能点 %g -> %g" % (d["char"], d["slot"], r["old"], r["new"]))
            elif args.respec_skills:
                r = ed.respec_skills(d["path"])
                print("%s/%s：清空 %d 个已学技能，技能点 %g -> %g"
                      % (d["char"], d["slot"], r["cleared"], r["sp_old"], r["sp_new"]))
            else:
                r = ed.respec_attrs(d["path"])
                print("%s/%s：属性 %s -> %s，属性点 %g -> %g" % (
                    d["char"], d["slot"],
                    "/".join("%g" % x for x in r["old"].values()),
                    "/".join("%g" % ATTR_FLOOR for _ in ATTRS),
                    r["old_ap"], r["new_ap"]))
        print("完成。进游戏后在角色界面重新分配点数；不满意可用 --respec-restore 还原。")
        return 0

    if args.selftest:
        print("本地自检：把计时函数加速 2.0 倍，量 1 秒内它走了多少…")
        res = local_selftest(2.0, log)
        ok = True
        for name, ratio in res:
            good = 1.85 <= ratio <= 2.15
            ok = ok and good
            print("  %-26s 实测 %.3f x   %s" % (name, ratio, "OK" if good else "异常"))
        print("自检结果：", "通过" if ok else "失败")
        return 0 if ok else 1

    if args.restore:
        acc = Accelerator(log)
        pid = find_pid()
        if not pid:
            print("游戏没在运行。")
            return 0
        try:
            acc.attach(pid)
            acc.restore()
            acc.close()
        except Exception as exc:
            print("还原失败：%s" % exc)
            return 1
        return 0

    if args.test or args.set is not None or args.benchmark is not None:
        acc = Accelerator(log)
        hooks = [h.strip() for h in args.hooks.split(",") if h.strip()]
        try:
            acc.attach(hooks=hooks, hook_all_modules=args.hook_all_modules)
        except Exception as exc:
            print("注入失败：%s" % exc)
            return 1
        try:
            if args.benchmark is not None:
                print("测量游戏主循环速度 + CPU 占用（每档 4 秒）…")
                for k in (1.0, args.benchmark, 1.0):
                    acc.set_factor(k)
                    time.sleep(1.0)
                    n0 = acc.counts()
                    c0 = cpu_seconds(acc.h)
                    time.sleep(4.0)
                    n1 = acc.counts()
                    c1 = cpu_seconds(acc.h)
                    rate = " ".join(
                        "%s=%.0f/s" % (nm, (n1[nm] - n0[nm]) / 4.0)
                        for nm in sorted(n0))
                    print("  %.2fx -> 单核占用 %5.1f%%   计时函数调用 %s"
                          % (k, (c1 - c0) / 4.0 * 100, rate))
                print("调用速率随倍率成比例上升 = 游戏主循环确实按这个时钟跑得更快。")
            if args.test:
                acc.set_factor(2.0)
                time.sleep(0.5)
                print("实测加速比例（应为 2.0）：")
                for name in list(acc.layout):
                    try:
                        print("  %-26s %.3f x" % (name, acc.measure(name)))
                    except Exception as exc:
                        print("  %-26s 测量失败：%s" % (name, exc))
                acc.set_factor(1.0)
                print("测量完毕，已恢复 1.0x。")
            if args.set is not None:
                acc.set_factor(args.set)
                print("倍率已设为 %.2fx（游戏里立刻生效）" % acc.get_factor())
            print("注入保持有效；用 --restore 或图形界面的「还原原速」恢复。")
        finally:
            acc.close()
        return 0

    run_gui(open_respec=args.respec_gui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
