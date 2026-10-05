#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
只读输入事件诊断 —— 找出按键/摇杆的真实事件源

背景
====
原厂 input.py 只从 /dev/input/event1 读 KEY 事件（24 字节 llHHI），
映射表里有 17/16 标成 "DX"/"DY"（摇杆轴），但没有 18/19/20/21（上下左右）。
用户实测：XYAB/SELECT 有反应，上下左右拨摇杆无反应。

推断：摇杆很可能是 EV_ABS（绝对轴）事件，在 event0 或 event1 上，
      而方向键可能根本没有独立 KEY 码，或走 event0。

本脚本做的事（纯只读）
====================
1. 枚举 /dev/input/event*，读每个设备的 name / phys / 支持的事件类型
2. 用 EVIOCGBIT 查询每个设备支持的 EV_KEY / EV_ABS 位图
3. 打开所有 event 设备（O_RDONLY，非阻塞），监听 N 秒
4. 把期间收到的**每一个事件**原样打印（type/code/value + 解读）
5. 分别统计各设备的 EV_KEY 与 EV_ABS 出现次数

用法：
  python3 /tmp/probe-input.py 20        # 监听 20 秒
  期间请：拨摇杆（上下左右各几下）、按 XYAB、按 START/SELECT、按 L1/R1/L2/R2
"""
import ctypes
import os
import select
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
    0: "SYN", 1: "KEY", 2: "REL", 3: "ABS",
    4: "MSC", 5: "SW", 17: "LED", 18: "SND", 21: "FF",
}

# 原厂 input.py 的 KEY 映射（用于对照）
KEY_MAP = {
    304: "A", 305: "B", 306: "Y", 307: "X",
    308: "L1", 309: "R1", 314: "L2", 315: "R2",
    17: "DY(?)", 16: "DX(?)", 18: "D-UP?", 19: "D-DOWN?",
    20: "D-LEFT?", 21: "D-RIGHT?",
    310: "SELECT", 311: "START", 312: "MENUF",
    114: "V+", 115: "V-",
    116: "POWER",
}

# 常见 ABS 轴名
ABS_MAP = {
    0: "ABS_X", 1: "ABS_Y", 2: "ABS_Z", 3: "ABS_RX", 4: "ABS_RY",
    5: "ABS_RZ", 16: "ABS_HAT0X", 17: "ABS_HAT0Y",
    0x28: "ABS_MISC",
}


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


class InputDevice:
    def __init__(self, path):
        self.path = path
        self.name = read_text(os.path.join(path, "device/name"), "?")
        self.phys = read_text(os.path.join(path, "device/phys"), "?")
        ev_path = f"/dev/input/{os.path.basename(path)}"
        self.ev_path = ev_path
        self.fd = None
        self.ekey = b""
        self.eabs = b""
        self.typemask = b""

    def probe_bits(self):
        """用 EVIOCGBIT 查支持的事件类型和按键/轴位图（只读 ioctl）"""
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        try:
            fd = os.open(self.ev_path, os.O_RDONLY)
        except OSError as e:
            print(f"  !! 打不开 {self.ev_path}: {e}")
            return False
        try:
            # EVIOCGBIT(0, len) = _IOC(_IOC_READ, 'E', 0x20, len)
            def eviocgbit(ev, length):
                return (2 << 30) | (length << 16) | (ord('E') << 8) | (0x20 + ev)

            buf = ctypes.create_string_buffer(8)
            r = libc.ioctl(fd, eviocgbit(0, 8), buf)
            if r >= 0:
                self.typemask = buf.raw[:8]

            # EV_KEY 位图：最多 0x300+ 个键，取 96 字节
            kb = ctypes.create_string_buffer(96)
            r = libc.ioctl(fd, eviocgbit(EV_KEY, 96), kb)
            if r >= 0:
                self.ekey = kb.raw[:96]

            ab = ctypes.create_string_buffer(96)
            r = libc.ioctl(fd, eviocgbit(EV_ABS, 96), ab)
            if r >= 0:
                self.eabs = ab.raw[:96]
        finally:
            os.close(fd)
        return True

    def open_stream(self):
        try:
            self.fd = os.open(self.ev_path, os.O_RDONLY | os.O_NONBLOCK)
            return True
        except OSError as e:
            print(f"  !! 监听打不开 {self.ev_path}: {e}")
            return False

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None


def main():
    listen_sec = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0

    print("=" * 66)
    print("输入设备枚举")
    print("=" * 66)

    devs = []
    for name in sorted(os.listdir(DEV_DIR)):
        if not name.startswith("event"):
            continue
        p = os.path.join(DEV_DIR, name)
        d = InputDevice(p)
        print(f"\n--- {name} ---")
        print(f"  name = {d.name}")
        print(f"  phys = {d.phys}")
        if d.probe_bits():
            types = [EV_TYPE_NAME.get(t, str(t)) for t in range(32)
                     if bit_is_set(d.typemask, t)]
            print(f"  支持事件类型: {', '.join(types) if types else '(读不到)'}")

            if d.ekey:
                keys = [f"{k}({KEY_MAP.get(k, '?')})" for k in range(0, 0x300)
                        if bit_is_set(d.ekey, k)]
                print(f"  EV_KEY 支持 ({len(keys)} 个): {', '.join(keys[:40])}"
                      + (" ..." if len(keys) > 40 else ""))
            if d.eabs:
                axes = [f"{a}({ABS_MAP.get(a, '?')})" for a in range(0, 0x40)
                        if bit_is_set(d.eabs, a)]
                print(f"  EV_ABS 支持 ({len(axes)} 个): {', '.join(axes)}")
        devs.append(d)

    # --- 开始监听 ---
    print()
    print("=" * 66)
    print(f"开始监听 {listen_sec:.0f} 秒 —— 请现在操作：")
    print("  1) 摇杆上下左右各拨几下")
    print("  2) 按 XYAB、L1/R1/L2/R2、SELECT、START")
    print("=" * 66)

    live = []
    for d in devs:
        if d.open_stream():
            live.append(d)

    if not live:
        print("!! 没有可监听的设备")
        return 1

    counts = {d.ev_path: {EV_KEY: 0, EV_ABS: 0, EV_REL: 0} for d in live}
    fds = {d.fd: d for d in live}
    deadline = time.time() + listen_sec
    seen_keys = set()
    seen_axes = set()
    tail = []

    while time.time() < deadline:
        ready, _, _ = select.select(list(fds.keys()), [], [],
                                   max(0.05, deadline - time.time()))
        for fd in ready:
            d = fds[fd]
            try:
                data = os.read(fd, 24 * 256)
            except BlockingIOError:
                continue
            except OSError:
                continue
            n = len(data) // 24
            for i in range(n):
                chunk = data[i * 24:(i + 1) * 24]
                if len(chunk) < 24:
                    break
                tv_sec, tv_usec, etype, code, value = struct.unpack("llHHI", chunk)
                if etype == EV_SYN:
                    continue
                tname = EV_TYPE_NAME.get(etype, str(etype))
                if etype == EV_KEY:
                    counts[d.ev_path][EV_KEY] += 1
                    seen_keys.add(code)
                    desc = KEY_MAP.get(code, f"code{code}")
                    tag = "按下" if value == 1 else ("抬起" if value == 0 else "长按")
                elif etype == EV_ABS:
                    counts[d.ev_path][EV_ABS] += 1
                    seen_axes.add(code)
                    desc = ABS_MAP.get(code, f"abs{code}")
                    tag = f"值={value}"
                elif etype == EV_REL:
                    counts[d.ev_path][EV_REL] += 1
                    desc = f"rel{code}"
                    tag = f"值={value}"
                else:
                    desc = f"code{code}"
                    tag = f"值={value}"
                line = f"  [{os.path.basename(d.ev_path)}] {tname} {desc} ({tag})"
                tail.append(line)
                print(line)
                if len(tail) > 400:
                    tail.pop(0)

    # --- 汇总 ---
    print()
    print("=" * 66)
    print("统计汇总")
    print("=" * 66)
    for d in live:
        c = counts[d.ev_path]
        print(f"  {os.path.basename(d.ev_path)} ({d.name}): "
              f"KEY={c[EV_KEY]} ABS={c[EV_ABS]} REL={c[EV_REL]}")
        d.close()

    print()
    print(f"期间观测到的 KEY code: "
          f"{sorted(seen_keys) if seen_keys else '(无)'}")
    print(f"  → 解读: " + ", ".join(
        f"{k}={KEY_MAP.get(k, '?')}" for k in sorted(seen_keys)) if seen_keys else "  → (无按键事件)")
    print(f"期间观测到的 ABS 轴: "
          f"{sorted(seen_axes) if seen_axes else '(无)'}")
    if seen_axes:
        print("  → 解读: " + ", ".join(
            f"{a}={ABS_MAP.get(a, '?')}" for a in sorted(seen_axes)))

    print()
    print("对照：原厂 KEY 映射 =", {k: v for k, v in KEY_MAP.items()
                                 if k >= 16 and k <= 21})
    print("完成（全程只读）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
