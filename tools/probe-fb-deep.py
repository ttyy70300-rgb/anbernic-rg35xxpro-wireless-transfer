#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【只读】framebuffer 参数深度探测 —— 排查 bpp=94 之谜。

严格只读约束：
    - /dev/fb0 以 O_RDONLY 打开
    - 只调 FBIOGET_VSCREENINFO / FBIOGET_FSCREENINFO（纯读 ioctl）
    - 不写任何字节进 framebuffer
    - 不碰 sysfs 写、不 kill 进程、不动 /tmp/.next、不切显示模式

输出四段：
    1) sysfs 权威值
    2) ctypes ioctl 读到的字段值
    3) var / fix 结构体逐字节 hex dump —— 反推真实字段布局
    4) 若 bpp 非法，扫描 16/32 位对齐的所有偏移，找疑似真实位置

另附：重复读 5 次，观察值是否稳定（dmenu 持锁重绘时可能读到垃圾）。
"""
import ctypes
import os
import struct
import sys
import time

FB_DEV = "/dev/fb0"
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
VAR_SIZE = 192
FIX_SIZE = 112

O_XRES, O_YRES, O_XRES_V, O_YRES_V = 64, 68, 72, 76
O_XOFF, O_YOFF, O_BPP, O_GRAY = 80, 84, 88, 92
O_RED, O_GREEN, O_BLUE, O_TRANSP = 96, 100, 104, 108
F_LINE_LENGTH = 56
F_SMEM_LEN = 8


def rd(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception as e:
        return f"<读取失败 {type(e).__name__}: {e}>"


def load_libc():
    for name in ("libc.so.6", "libc.so", "libc.so.7"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    raise RuntimeError("无法加载 libc")


def ioctl_raw(libc, fd, req, size):
    buf = (ctypes.c_char * size)()
    ctypes.set_errno(0)
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(req), ctypes.byref(buf))
    err = ctypes.get_errno()
    return rc, err, bytes(buf)


def g32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def g16(b, off):
    return struct.unpack_from("<H", b, off)[0]


def hexdump(b, label):
    print(f"--- {label} ({len(b)} 字节) ---")
    for base in range(0, len(b), 16):
        chunk = b[base:base + 16]
        hx = " ".join(f"{c:02x}" for c in chunk)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        print(f"  {base:3d}: {hx:<47}  {asc}")


def main():
    print("=" * 62)
    print("framebuffer 只读深度探测")
    print("=" * 62)

    print("\n【1】sysfs 权威值")
    print("    virtual_size   =", rd("/sys/class/graphics/fb0/virtual_size"))
    print("    bits_per_pixel =", rd("/sys/class/graphics/fb0/bits_per_pixel"))
    print("    stride         =", rd("/sys/class/graphics/fb0/stride"))
    print("    name           =", rd("/sys/class/graphics/fb0/name"))
    print("    blank          =", rd("/sys/class/graphics/fb0/blank"))

    if not os.path.exists(FB_DEV):
        print(f"\n!! {FB_DEV} 不存在，无法继续")
        return 1

    libc = load_libc()
    fd = os.open(FB_DEV, os.O_RDONLY)
    try:
        print("\n【2】ioctl 读回值（连续读 5 次，看稳定性）")
        first_var = None
        first_fix = None
        for i in range(5):
            rc, err, var = ioctl_raw(libc, fd, FBIOGET_VSCREENINFO, VAR_SIZE)
            rc2, err2, fix = ioctl_raw(libc, fd, FBIOGET_FSCREENINFO, FIX_SIZE)
            if rc == 0 and first_var is None:
                first_var = var
            if rc2 == 0 and first_fix is None:
                first_fix = fix
            bpp_v = g32(var, O_BPP) if rc == 0 else -1
            w_v = g32(var, O_XRES) if rc == 0 else -1
            ll_v = g32(fix, F_LINE_LENGTH) if rc2 == 0 else -1
            print(f"    #{i+1} rc={rc}/{rc2} xres={w_v} bpp={bpp_v} line_length={ll_v}")
            time.sleep(0.15)

        if first_var is not None:
            print("\n    字段明细（取第 1 次成功读取）：")
            print("      xres        =", g32(first_var, O_XRES))
            print("      yres        =", g32(first_var, O_YRES))
            print("      xres_virtual=", g32(first_var, O_XRES_V))
            print("      yres_virtual=", g32(first_var, O_YRES_V))
            print("      xoffset     =", g32(first_var, O_XOFF))
            print("      yoffset     =", g32(first_var, O_YOFF))
            print("      bits_per_pxl=", g32(first_var, O_BPP), "  <== 期望 16 或 32")
            print("      grayscale   =", g32(first_var, O_GRAY))
            print("      red  =", g32(first_var, O_RED),
                  " green =", g32(first_var, O_GREEN),
                  " blue =", g32(first_var, O_BLUE),
                  " transp =", g32(first_var, O_TRANSP))

        if first_fix is not None:
            smem = g32(first_fix, F_SMEM_LEN) | (g32(first_fix, F_SMEM_LEN + 4) << 32)
            print("      line_length =", g32(first_fix, F_LINE_LENGTH))
            print("      smem_len    =", smem)

        if first_var is not None:
            print("\n【3】var 结构体逐字节 dump（反推真实布局）")
            hexdump(first_var, "fb_var_screeninfo")

        if first_fix is not None:
            print("")
            hexdump(first_fix[:64], "fb_fix_screeninfo (前 64 字节)")

        if first_var is not None:
            bad = g32(first_var, O_BPP)
            if bad not in (16, 32):
                print(f"\n【4】bpp 字段 (偏移 {O_BPP}) 读出 {bad}，非法！扫描候选偏移：")
                print("      32 位对齐扫描（值为 16 或 32 的位置）：")
                found = False
                for off in range(0, 160, 4):
                    v = g32(first_var, off)
                    if v in (16, 32):
                        print(f"        偏移 {off:3d} -> {v}")
                        found = True
                if not found:
                    print("        （无）")
                print("      16 位对齐扫描：")
                found = False
                for off in range(0, 160, 2):
                    v = g16(first_var, off)
                    if v in (16, 32):
                        print(f"        偏移 {off:3d} -> {v}")
                        found = True
                if not found:
                    print("        （无）")
    finally:
        try:
            os.close(fd)
        except Exception:
            pass

    print("\n探测结束（未做任何写操作）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
