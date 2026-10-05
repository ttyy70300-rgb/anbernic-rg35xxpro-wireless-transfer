#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【只读】抓 framebuffer 原始显存，分析我们画的内容到底去了哪一页。

严格只读：
    - /dev/fb0 以 O_RDONLY 打开
    - mmap 用 PROT_READ
    - 不写任何字节、不调任何 PUT ioctl、不碰 sysfs 写、不 kill 进程

用途（设备正处于"点了程序卡在加载中"状态时运行）：
    1) 读当前 fb_var / fix 参数（含 yoffset —— 双缓冲的当前显示页）
    2) 把整块显存按"页"切开，逐页统计非黑像素
       → 找出我们的 UI 画在了哪一页
    3) 对每一页生成 ASCII 缩略图，肉眼比对哪一页是「加载中」、哪一页是我们的卡片界面
"""
import ctypes
import mmap
import os
import struct
import sys

FB_DEV = "/dev/fb0"
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
VAR_SIZE = 192
FIX_SIZE = 112

# 标准 uapi 偏移（本机已用逐字节 dump 校准）
V_XRES, V_YRES, V_XRES_V, V_YRES_V = 0, 4, 8, 12
V_XOFF, V_YOFF, V_BPP, V_GRAY = 16, 20, 24, 28
V_RED, V_GREEN, V_BLUE, V_TRANSP = 32, 44, 56, 68
V_HEIGHT, V_WIDTH = 88, 92
F_SMEM_START, F_SMEM_LEN, F_LINE_LENGTH = 16, 24, 48


def load_libc():
    for n in ("libc.so.6", "libc.so", "libc.so.7"):
        try:
            return ctypes.CDLL(n, use_errno=True)
        except OSError:
            continue
    raise RuntimeError("no libc")


def ioctl_get(libc, fd, req, size):
    buf = (ctypes.c_char * size)()
    ctypes.set_errno(0)
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(req), ctypes.byref(buf))
    return rc, ctypes.get_errno(), bytes(buf)


def g32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def rd(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except Exception:
        return "?"


def ascii_thumb(data, w, h, bpp, stride, cols=56, rows=26, ox=0, oy=0):
    bps = bpp // 8
    out = []
    for ay in range(rows):
        line = []
        for ax in range(cols):
            x = ox + int(ax * w / cols)
            y = oy + int(ay * h / rows)
            off = y * stride + x * bps
            if off + bps > len(data):
                line.append(" ")
                continue
            px = data[off:off + bps]
            if px == b"\x00" * bps:
                line.append(".")
                continue
            if bpp == 16:
                v = struct.unpack_from("<H", px, 0)[0]
                lum = ((v >> 11) & 0x1F) * 3 + ((v >> 5) & 0x3F) * 2 + (v & 0x1F) * 3
            elif bpp == 32:
                b, g, r = px[0], px[1], px[2]
                lum = (r + g + b) // 3
            else:
                lum = 200
            line.append("#" if lum > 190 else ("+" if lum > 60 else "-"))
        out.append("    |" + "".join(line) + "|")
    return "\n".join(out)


def page_stats(data, page_bytes, stride, w, h, bpp):
    bps = bpp // 8
    nz = 0
    total = 0
    for y in range(h):
        base = y * stride
        row = data[base:base + w * bps]
        total += len(row)
        for i in range(0, len(row), bps * 16):
            chunk = row[i:i + bps]
            if chunk and chunk != b"\x00" * len(chunk):
                nz += 1
    return nz, total


def main():
    print("=" * 64)
    print("framebuffer 显存抓取（只读）—— 找我们的界面在哪一页")
    print("=" * 64)

    print("\n【1】sysfs")
    print("    virtual_size   =", rd("/sys/class/graphics/fb0/virtual_size"))
    print("    bits_per_pixel =", rd("/sys/class/graphics/fb0/bits_per_pixel"))
    print("    stride         =", rd("/sys/class/graphics/fb0/stride"))
    print("    pan            =", rd("/sys/class/graphics/fb0/pan"))

    libc = load_libc()
    fd = os.open(FB_DEV, os.O_RDONLY)
    try:
        rcv, _, var = ioctl_get(libc, fd, FBIOGET_VSCREENINFO, VAR_SIZE)
        rcf, _, fix = ioctl_get(libc, fd, FBIOGET_FSCREENINFO, FIX_SIZE)
        if rcv != 0 or rcf != 0:
            print(f"    !! ioctl 失败 rc={rcv}/{rcf}")
            return 1

        xres = g32(var, V_XRES)
        yres = g32(var, V_YRES)
        xoff = g32(var, V_XOFF)
        yoff = g32(var, V_YOFF)
        bpp = g32(var, V_BPP)
        line_len = g32(fix, F_LINE_LENGTH)
        smem = g32(fix, F_SMEM_LEN) | (g32(fix, F_SMEM_LEN + 4) << 32)

        print("\n【2】ioctl（应用运行中的真实参数）")
        print(f"    xres={xres} yres={yres}  bpp={bpp}")
        print(f"    xoffset={xoff}  yoffset={yoff}   <== 当前显示页的偏移")
        print(f"    line_length={line_len}  smem_len={smem}")
        print(f"    virt_w={g32(var, V_XRES_V)} virt_h={g32(var, V_YRES_V)}")

        need = smem if smem > 0 else line_len * g32(var, V_YRES_V)
        need = min(need, 64 * 1024 * 1024)
        mm = mmap.mmap(fd, need, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
        try:
            data = mm[:]
        finally:
            mm.close()

        bps = bpp // 8
        page_bytes = line_len * yres
        npages = max(1, len(data) // page_bytes) if page_bytes else 1
        print(f"\n【3】显存 {len(data)} 字节；每页 {page_bytes} 字节，共约 {npages} 页")
        print(f"    当前显示起点 yoffset={yoff} → 屏幕正在看第 "
              f"{yoff // yres if yres else 0} 页")

        for p in range(min(npages, 3)):
            base = p * page_bytes
            seg = data[base:base + page_bytes]
            nz, total = page_stats(seg, page_bytes, line_len, xres, yres, bpp)
            ratio = (nz / max(1, total // (bps * 16))) * 100
            cur = "  <== 屏幕正在显示这一页" if p == yoff // yres else ""
            print(f"\n  ---- 第 {p} 页（偏移 {base}）非黑采样 {nz} 个，占 {ratio:.0f}%{cur} ----")
            print(ascii_thumb(seg, xres, yres, bpp, line_len,
                              cols=56, rows=26, ox=0, oy=0))
    finally:
        try:
            os.close(fd)
        except Exception:
            pass

    print("\n抓取结束（未做任何写操作）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
