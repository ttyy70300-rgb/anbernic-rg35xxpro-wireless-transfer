#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只读输入设备全面诊断（v2 —— 修复结构体错位）

背景
====
2026-10-05 真机实测发现两个问题：
  1. 所有 ABS 事件的 value 都被读成 +1，而不是 ±3700 的模拟量
     → 根因：struct 解包用了 "llHHI"（16字节），
       而 64 位 input_event 实际是 24 字节（"qqHHi"），整体错位 8 字节
  2. **十字方向键（D-Pad）完全没有任何事件**
     → 需要确认它走哪个 event 设备、什么事件类型

本脚本做的事（全程只读）
======================
1. 列出所有 /dev/input/event*，读 name / 支持的事件类型位图
2. **同时监听所有设备**，把每一个原始事件按 24 字节正确解包打印
3. 分开统计 KEY / ABS / REL，并打印每个轴的**真实值域**（min/max）
4. 结束时给出"哪根设备上出现了哪些键/轴"的汇总

用法：
    python tools/probe-input2.py 25

期间请依次操作（每一步间隔 1 秒左右）：
    十字键 上 / 下 / 左 / 右
    左摇杆 上 / 下 / 左 / 右
    右摇杆 上 / 下 / 左 / 右
    A / B / X / Y
    L1 / R1 / L2 / R2 / SELECT / START
"""
import ctypes
import os
import struct
import sys
import time

DEV_DIR = "/dev/input"

EV_SYN = 0
EV_KEY = 1
EV_REL = 2
EV_ABS = 3
EV_MSC = 4
EV_SW = 5

EV_TYPE_NAME = {
    0: "SYN", 1: "KEY", 2: "REL", 3: "ABS", 4: "MSC", 5: "SW",
    17: "LED", 18: "SND", 21: "FF",
}

KEY_MAP = {
    304: "A", 305: "B", 306: "Y", 307: "X",
    308: "L1", 309: "R1", 314: "L2", 315: "R2",
    310: "SELECT", 311: "START", 312: "MENUF",
    114: "V+", 115: "V-", 116: "POWER",
    # 标准 evdev 十字键码（如果 D-Pad 走 KEY 的话）
    103: "KEY_UP", 108: "KEY_DOWN", 105: "KEY_LEFT", 106: "KEY_RIGHT",
    # 手柄常见 BTN_DPAD_*
    544: "BTN_DPAD_UP", 545: "BTN_DPAD_DOWN",
    546: "BTN_DPAD_LEFT", 547: "BTN_DPAD_RIGHT",
    # HAT 走 KEY 的常见写法
    16: "HAT0X(键)", 17: "HAT0Y(键)",
    18: "HAT1X(键)", 19: "HAT1Y(键)",
}

ABS_MAP = {
    0: "ABS_X", 1: "ABS_Y", 2: "ABS_Z", 3: "ABS_RX", 4: "ABS_RY",
    5: "ABS_RZ", 16: "ABS_HAT0X", 17: "ABS_HAT0Y",
    18: "ABS_HAT1X", 19: "ABS_HAT1Y",
    0x28: "ABS_MISC",
}

# 每个 ABS 轴的观测值域（用于判断是模拟摇杆还是数字十字键）
axis_ranges = {}


def read_text(path, default=""):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return default


def bit_is_set(buf, bit):
    idx = bit // 8
    if idx >= len(buf):
        return False
    return (buf[idx] >> (bit % 8)) & 1


class Dev:
    def __init__(self, evname):
        self.evname = evname
        self.path = os.path.join(DEV_DIR, evname)
        self.name = read_text(os.path.join(self.path, "device/name"), "?")
        self.fd = None
        self.typemask = b""

    def probe(self):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return False
        try:
            def gbit(ev, ln):
                return (2 << 30) | (ln << 16) | (ord('E') << 8) | (0x20 + ev)
            buf = ctypes.create_string_buffer(8)
            if libc.ioctl(fd, gbit(0, 8), buf) >= 0:
                self.typemask = buf.raw[:8]
            return True
        finally:
            os.close(fd)

    def open_stream(self):
        try:
            import fcntl
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
            self.fd = fd
            return True
        except OSError as e:
            print(f"  !! 打不开 {self.path}: {e}")
            return False


def main():
    listen_sec = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0

    print("=" * 70)
    print("  输入设备全面诊断 v2（结构体已修正：qqHHi / 24 字节）")
    print("=" * 70)

    devs = []
    for nm in sorted(os.listdir(DEV_DIR)):
        if not nm.startswith("event"):
            continue
        d = Dev(nm)
        types = ""
        if d.probe():
            ts = [EV_TYPE_NAME.get(t, str(t)) for t in range(32)
                  if bit_is_set(d.typemask, t)]
            types = ", ".join(ts)
        print(f"  {nm}: name={d.name!r}  类型=[{types}]")
        devs.append(d)

    print()
    print("=" * 70)
    print(f"  监听 {listen_sec:.0f} 秒 —— 请依次操作：")
    print("    十字键 上/下/左/右   ← 本次重点！")
    print("    左摇杆 上/下/左/右")
    print("    右摇杆 上/下/左/右")
    print("    A/B/X/Y  L1/R1/L2/R2  SELECT/START")
    print("=" * 70)

    live = [d for d in devs if d.open_stream()]
    if not live:
        print("!! 没有可监听设备")
        return 1

    import select as sel
    fds = {d.fd: d for d in live}
    key_seen = {}      # devname -> set(code)
    abs_seen = {}      # devname -> set(code)
    n_events = 0
    deadline = time.time() + listen_sec

    while time.time() < deadline:
        ready, _, _ = sel.select(list(fds.keys()), [], [],
                                 max(0.05, deadline - time.time()))
        for fd in ready:
            d = fds[fd]
            try:
                data = os.read(fd, 24 * 512)
            except (BlockingIOError, OSError):
                continue
            n = len(data) // 24
            for i in range(n):
                chunk = data[i * 24:(i + 1) * 24]
                if len(chunk) < 24:
                    break
                try:
                    _s, _us, etype, code, value = struct.unpack("qqHHi", chunk)
                except Exception:
                    continue
                if etype == EV_SYN:
                    continue
                n_events += 1
                tname = EV_TYPE_NAME.get(etype, str(etype))
                if etype == EV_KEY:
                    key_seen.setdefault(d.evname, set()).add(code)
                    desc = KEY_MAP.get(code, f"code{code}")
                    print(f"  [{d.evname}] KEY {desc}({code}) value={value}")
                elif etype == EV_ABS:
                    abs_seen.setdefault(d.evname, set()).add(code)
                    r = axis_ranges.setdefault((d.evname, code),
                                               [value, value])
                    r[0] = min(r[0], value)
                    r[1] = max(r[1], value)
                    print(f"  [{d.evname}] ABS {ABS_MAP.get(code, code)}"
                          f"({code}) value={value}")
                else:
                    print(f"  [{d.evname}] {tname} code={code} value={value}")

    for d in live:
        try:
            os.close(d.fd)
        except Exception:
            pass

    print()
    print("=" * 70)
    print("  汇总")
    print("=" * 70)
    print(f"  共收到 {n_events} 个非 SYN 事件")
    for d in live:
        ks = sorted(key_seen.get(d.evname, ()))
        as_ = sorted(abs_seen.get(d.evname, ()))
        print(f"  {d.evname} ({d.name}):")
        if ks:
            print("     KEY: " + ", ".join(
                f"{k}={KEY_MAP.get(k, '?')}" for k in ks))
        if as_:
            print("     ABS: " + ", ".join(
                f"{a}={ABS_MAP.get(a, '?')}" for a in as_))
        if not ks and not as_:
            print("     （无事件）")

    print()
    print("  各 ABS 轴的观测值域（判断模拟摇杆 vs 数字十字键）:")
    if not axis_ranges:
        print("    （未观测到任何 ABS 事件）")
    for (dev, code), (lo, hi) in sorted(axis_ranges.items()):
        kind = "模拟摇杆" if (hi - lo) > 100 else "数字/开关式（值域很小）"
        print(f"    [{dev}] {ABS_MAP.get(code, code)}({code}): "
              f"[{lo}, {hi}]  → {kind}")

    print()
    print("  完成（全程只读）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
