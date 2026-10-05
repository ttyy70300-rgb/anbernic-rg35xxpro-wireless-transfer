#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【只读】把 /dev/fb0 当前页导出为 PNG —— 直接肉眼看屏幕到底是什么内容。

严格只读：O_RDONLY 打开 + PROT_READ mmap，不写任何字节。
PNG 写到 /tmp（不用应用目录，避免污染部署校验），再 sftp 取回。
"""
import ctypes
import mmap
import os
import struct
import sys

FB_DEV = "/dev/fb0"
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
V_XRES, V_YRES, V_XRES_V, V_YRES_V = 0, 4, 8, 12
V_XOFF, V_YOFF, V_BPP = 16, 20, 24
F_SMEM_LEN, F_LINE_LENGTH = 24, 48

OUT_DIR = "/tmp"


def load_libc():
    for n in ("libc.so.6", "libc.so", "libc.so.7"):
        try:
            return ctypes.CDLL(n, use_errno=True)
        except OSError:
            continue
    raise RuntimeError("no libc")


def ioctl_get(libc, fd, req, size):
    buf = (ctypes.c_char * size)()
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(req), ctypes.byref(buf))
    return rc, bytes(buf)


def g32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def main():
    try:
        from PIL import Image
    except Exception as e:
        print("需要 Pillow:", e)
        return 1

    libc = load_libc()
    fd = os.open(FB_DEV, os.O_RDONLY)
    try:
        _, var = ioctl_get(libc, fd, FBIOGET_VSCREENINFO, 192)
        _, fix = ioctl_get(libc, fd, FBIOGET_FSCREENINFO, 112)
        xres, yres = g32(var, V_XRES), g32(var, V_YRES)
        xoff, yoff, bpp = g32(var, V_XOFF), g32(var, V_YOFF), g32(var, V_BPP)
        vh = g32(var, V_YRES_V)
        line = g32(fix, F_LINE_LENGTH)
        smem = g32(fix, F_SMEM_LEN)
        print(f"参数 {xres}x{yres} bpp={bpp} stride={line} "
              f"xoff={xoff} yoff={yoff} virt_h={vh} smem={smem}")

        need = min(smem if smem else line * vh, 64 * 1024 * 1024)
        mm = mmap.mmap(fd, need, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
        try:
            data = mm[:]
        finally:
            mm.close()
    finally:
        os.close(fd)

    bps = bpp // 8
    os.makedirs(OUT_DIR, exist_ok=True)

    # 导出：整块虚拟区 + 当前显示页（按 yoffset 定位）
    for tag, base in (("full_virtual", 0),
                      ("shown_page", yoff * line)):
        if base + line * yres > len(data):
            print(f"跳过 {tag}: 越界")
            continue
        seg = data[base:base + line * yres]
        img = Image.new("RGB", (xres, yres))
        px = img.load()
        for y in range(yres):
            row = seg[y * line:(y + 1) * line]
            for x in range(xres):
                o = x * bps
                if bpp == 32:
                    b, g, r = row[o], row[o + 1], row[o + 2]
                else:
                    v = struct.unpack_from("<H", row, o)[0]
                    r = ((v >> 11) & 0x1F) * 255 // 31
                    g = ((v >> 5) & 0x3F) * 255 // 63
                    b = (v & 0x1F) * 255 // 31
                px[x, y] = (r, g, b)
        path = os.path.join(OUT_DIR, f"{tag}.png")
        img.save(path)
        print("已保存", path, img.size)

    return 0


if __name__ == "__main__":
    sys.exit(main())
