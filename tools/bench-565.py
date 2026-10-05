#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地验证 RGB565 打包正确性与速度（不依赖掌机）"""
import struct
import time


def pack(r, g, b):
    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return struct.pack("<H", v), v


print("=== 颜色编码核对 ===")
for name, (r, g, b) in [
    ("红", (220, 40, 40)), ("绿", (40, 200, 60)),
    ("蓝", (50, 80, 230)), ("白", (255, 255, 255)), ("黑", (0, 0, 0)),
]:
    bs, v = pack(r, g, b)
    back = (((v >> 11) & 31) * 8, ((v >> 5) & 63) * 4, (v & 31) * 8)
    print(f"  {name} ({r},{g},{b}) -> 0x{v:04x}  bytes={bs.hex()}  "
          f"5/6/5回读={back}")

print()
print("=== 切片方案 vs 逐像素方案 ===")
CASES = [(220, 40, 40), (40, 200, 60), (50, 80, 230), (255, 255, 255)]
n = len(CASES)
r = bytes(c[0] for c in CASES)
g = bytes(c[1] for c in CASES)
b = bytes(c[2] for c in CASES)

hi5 = bytes((i >> 3) for i in range(256))     # -> 5bit
g6 = bytes(((i >> 2) & 0x3F) for i in range(256))   # -> 6bit
r5 = r.translate(hi5)
b5 = b.translate(hi5)
gg = g.translate(g6)

out = bytearray(n * 2)
# u16 = r5<<11 | g6<<5 | b5
# 小端存储：byte0 = bit7..0，byte1 = bit15..8
#   byte1 = (r5 << 3) | (g6 >> 3)      因为 r5 占 bit11..15, g6 高3位占 bit8..10
#   byte0 = ((g6 & 0x07) << 5) | b5   因为 g6 低3位占 bit5..7, b5 占 bit0..4
out[0::2] = bytes(((gg[i] & 0x07) << 5) | b5[i] for i in range(n))
out[1::2] = bytes((r5[i] << 3) | (gg[i] >> 3) for i in range(n))

expect = b"".join(pack(*c)[0] for c in CASES)
print("  切片:", out.hex())
print("  期望:", expect.hex())
print("  结论:", "一致 OK" if bytes(out) == expect else "不一致 !!")
print()
print("  注意 g6 的 >>3 与 &0x03：6bit 值占 bit10..bit5，"
      "所以高字节取 >>3，低字节取 &0x03 后左移 5")

print()
print("=== 速度（模拟 1280 宽逐行）===")
W = 1280
row = bytes(range(256)) * (W * 3 // 256 + 1)
row = row[:W * 3]
hi5t = hi5
g6t = g6

t = time.time()
ROWS = 200
for y in range(ROWS):
    rr = row[0:W]
    gg2 = row[1:W * 3:3]
    bb = row[2:W * 3:3]
    r5 = rr.translate(hi5t)
    b5 = bb.translate(hi5t)
    g_ = gg2.translate(g6t)
    o = bytearray(W * 2)
    o[0::2] = bytes(((g_[i] & 0x07) << 5) | b5[i] for i in range(W))
    o[1::2] = bytes((r5[i] << 3) | (g_[i] >> 3) for i in range(W))
el = time.time() - t
print(f"  {ROWS} 行耗时 {el:.2f}s  -> 1024 行约 {el*1024/ROWS:.1f}s")
print("  (掌机 CPU 更慢，实际可能 3~5 倍)")

print()
print("=== 备选：查表法（每 2 像素一组，直接 12bit->16bit 查表）===")
# 5bit+6bit+5bit = 16bit，可以按 (r5<<11 | g6<<5 | b5) 直接组 u16
# 但 bytes 层面无法一次 translate 出交错，所以生成器不可避免
# 试试用 array 模块
import array
t = time.time()
for y in range(ROWS):
    rr = row[0:W]
    gg2 = row[1:W * 3:3]
    bb = row[2:W * 3:3]
    a = array.array("H", bytes(W * 2))
    a = array.array("H", [0]) * W
    for i in range(W):
        a[i] = ((rr[i] >> 3) << 11) | ((gg2[i] >> 2) << 5) | (bb[i] >> 3)
el2 = time.time() - t
print(f"  array 逐像素 {ROWS} 行耗时 {el2:.2f}s -> 1024 行约 {el2*1024/ROWS:.1f}s")
