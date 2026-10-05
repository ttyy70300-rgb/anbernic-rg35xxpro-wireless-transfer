#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐字节 dump fb ioctl 结果，反推正确的字段偏移。

思路：fcntl.ioctl 的可变 buffer 形式，内核写入的是结构体二进制。
把 192 / 112 字节按 4 字节 int 全打印出来，再对照已知真值
（640x480, bpp=32, line_length=2560, red=16 green=8 blue=0）
就能定位到底偏了几个字节。
"""
import fcntl
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fb as F

fd = os.open("/dev/fb0", os.O_RDWR)

print("=" * 70)
print("fb_var_screeninfo 原始 dump (FBIOGET_VSCREENINFO=0x4600)")
print("=" * 70)
buf = bytearray(256)
fcntl.ioctl(fd, F.FBIOGET_VSCREENINFO, buf, True)
raw = bytes(buf)
for i in range(0, 192, 4):
    v = struct.unpack_from("<I", raw, i)[0]
    # 标注这个偏移在我的定义里是什么字段
    tag = ""
    for j, (n, t) in enumerate(F._VAR_FIELDS):
        if j * 4 == i:
            tag = n
            break
    mark = " <== " + tag if tag else ""
    if v != 0 or tag:
        print(f"  [{i:3d}] {v:12d}  0x{v:08x}{mark}")

print()
print("=" * 70)
print("fb_fix_screeninfo 原始 dump (FBIOGET_FSCREENINFO=0x4602)")
print("=" * 70)
buf2 = bytearray(160)
fcntl.ioctl(fd, F.FBIOGET_FSCREENINFO, buf2, True)
raw2 = bytes(buf2)
for i in range(0, 112, 4):
    v = struct.unpack_from("<I", raw2, i)[0]
    tag = ""
    for j, (n, t) in enumerate(F._FIX_FIELDS):
        # Q 类型占 8 字节
        off = 0
        for k, (n2, t2) in enumerate(F._FIX_FIELDS[:j]):
            off += struct.calcsize("<" + t2)
        if off == i:
            tag = n2
            break
    mark = " <== " + tag if tag else ""
    if v != 0 or tag:
        print(f"  [{i:3d}] {v:12d}  0x{v:08x}{mark}")

print()
print("=" * 70)
print("反推结论")
print("=" * 70)
# 找 640 和 480 在 var 里的位置
for i in range(0, 100, 4):
    v = struct.unpack_from("<I", raw, i)[0]
    if v in (640, 480, 960, 32, 16, 8, 0):
        pass
print("var 中 640 出现于:",
      [i for i in range(0, 120, 4) if struct.unpack_from("<I", raw, i)[0] == 640])
print("var 中 480 出现于:",
      [i for i in range(0, 120, 4) if struct.unpack_from("<I", raw, i)[0] == 480])
print("var 中 960 出现于:",
      [i for i in range(0, 120, 4) if struct.unpack_from("<I", raw, i)[0] == 960])
print("var 中 32(bpp) 出现于:",
      [i for i in range(0, 120, 4) if struct.unpack_from("<I", raw, i)[0] == 32])
print("var 中 16(red offset) 出现于:",
      [i for i in range(0, 120, 4) if struct.unpack_from("<I", raw, i)[0] == 16])
print("fix 中 2560(line_length) 出现于:",
      [i for i in range(0, 112, 4) if struct.unpack_from("<I", raw2, i)[0] == 2560])

os.close(fd)
