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
import os
import queue
import struct
import subprocess
import sys
import threading
import time
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
# 图形界面
# --------------------------------------------------------------------------

def run_gui():
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
    args = ap.parse_args(argv)

    log = lambda m: print(m, flush=True)

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

    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
