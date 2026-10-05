#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【只读】显示设备全景探测 —— 确认画布尺寸与内容有效区。

只读约束：
    - /dev/fb* 一律 O_RDONLY
    - mmap 用 PROT_READ
    - 不写任何字节、不调任何 PUT ioctl、不碰 sysfs 写

目的：
    1) 列出 /sys/class/graphics/ 下所有设备及其权威参数
    2) 用【标准 Linux uapi 偏移】读 ioctl，验证修正后的偏移表
    3) mmap 只读，把屏幕内容降采样成 ASCII 图 —— 看有效区在哪、
       宽高比多少（dmenu 正在跑，屏幕上有界面，能直接看出布局）
"""
import ctypes
import glob
import mmap
import os
import struct
import sys

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
VAR_SIZE = 192
FIX_SIZE = 112

# ---- 标准 Linux uapi 偏移（本次由实机 dump 反推确认）----
# fb_var_screeninfo
V_XRES, V_YRES, V_XRES_V, V_YRES_V = 0, 4, 8, 12
V_XOFF, V_YOFF, V_BPP, V_GRAY = 16, 20, 24, 28
V_RED, V_GREEN, V_BLUE, V_TRANSP = 32, 44, 56, 68
V_NONSTD, V_ACTIVATE, V_HEIGHT, V_WIDTH = 80, 84, 88, 92
# fb_fix_screeninfo
F_SMEM_START, F_SMEM_LEN, F_LINE_LENGTH = 16, 24, 48


def rd(p):
    try:
        with open(p, "r") as f:
            return f.read().strip()
    except Exception:
        return None


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


def main():
    print("=" * 64)
    print("显示设备全景探测（只读）")
    print("=" * 64)

    print("\n【1】/sys/class/graphics/ 设备列表")
    for d in sorted(glob.glob("/sys/class/graphics/fb*")):
        nm = os.path.basename(d)
        vals = {}
        for f in ("name", "virtual_size", "bits_per_pixel", "stride",
                  "blank", "pan", "rotate", "modes"):
            v = rd(os.path.join(d, f))
            if v is not None:
                vals[f] = v
        print(f"  {nm}: {vals}")
    other = [os.path.basename(x) for x in glob.glob("/sys/class/graphics/*")
             if not os.path.basename(x).startswith("fb")]
    if other:
        print("  其他 graphics 节点:", other)

    print("\n【2】ioctl 读取（标准 uapi 偏移）")
    libc = load_libc()
    maps = {}
    for dev in sorted(glob.glob("/dev/fb*")):
        print(f"\n  --- {dev} ---")
        try:
            fd = os.open(dev, os.O_RDONLY)
        except Exception as e:
            print(f"    打开失败: {e}")
            continue
        try:
            rc, err, var = ioctl_get(libc, fd, FBIOGET_VSCREENINFO, VAR_SIZE)
            rc2, err2, fix = ioctl_get(libc, fd, FBIOGET_FSCREENINFO, FIX_SIZE)
            if rc != 0:
                print(f"    GET_VSCREENINFO 失败 rc={rc} errno={err}")
                continue
            vw, vh = g32(var, V_XRES), g32(var, V_YRES)
            vvw, vvh = g32(var, V_XRES_V), g32(var, V_YRES_V)
            bpp = g32(var, V_BPP)
            ll = g32(fix, F_LINE_LENGTH) if rc2 == 0 else -1
            smem = g32(fix, F_SMEM_LEN) if rc2 == 0 else -1
            smem_start = (g32(fix, F_SMEM_START) |
                          (g32(fix, F_SMEM_START + 4) << 32)) if rc2 == 0 else -1
            print(f"    xres={vw} yres={vh}  virt={vvw}x{vvh}  bpp={bpp}")
            print(f"    xoffset={g32(var, V_XOFF)} yoffset={g32(var, V_YOFF)}")
            print(f"    height={g32(var, V_HEIGHT)}mm width={g32(var, V_WIDTH)}mm")
            print(f"    red=({g32(var, V_RED)}/{g32(var, V_RED+4)})"
                  f" green=({g32(var, V_GREEN)}/{g32(var, V_GREEN+4)})"
                  f" blue=({g32(var, V_BLUE)}/{g32(var, V_BLUE+4)})"
                  f" transp=({g32(var, V_TRANSP)}/{g32(var, V_TRANSP+4)})")
            print(f"    line_length={ll} smem_len={smem} smem_start=0x{smem_start:x}")

            # 自洽性
            ok = True
            if bpp not in (16, 32):
                print(f"    !! bpp={bpp} 仍非法")
                ok = False
            if ll <= 0:
                print(f"    !! line_length={ll} 非法")
                ok = False
            if ok:
                calc = vvw * bpp // 8
                flag = "OK" if calc == ll else f"不一致(算得{calc})"
                print(f"    >> 自洽检查: virt_w*bpp/8 = {calc} vs line_length = {ll}  [{flag}]")
                maps[dev] = (vw, vh, vvw, vvh, bpp, ll, smem)
        finally:
            os.close(fd)

    print("\n【3】只读 mmap 内容分析（看有效区与宽高比）")
    for dev, (vw, vh, vvw, vvh, bpp, ll, smem) in maps.items():
        rows = vvh
        need = ll * rows
        if smem > 0:
            need = min(need, smem)
        try:
            fd = os.open(dev, os.O_RDONLY)
            mm = mmap.mmap(fd, need, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
        except Exception as e:
            print(f"  {dev} mmap 失败: {e}")
            continue
        try:
            data = mm[:]
        finally:
            mm.close()
            os.close(fd)

        bps = bpp // 8
        # 逐行统计非黑像素，找有效行范围
        nonblack_rows = []
        step_y = max(1, rows // 200)
        for y in range(0, rows, step_y):
            base = y * ll
            row = data[base:base + min(ll, len(data) - base)]
            if not row:
                continue
            nz = 0
            for i in range(0, len(row) - bps + 1, bps * 8):
                if row[i:i + bps] != b"\x00" * bps:
                    nz += 1
            if nz > 2:
                nonblack_rows.append(y)
        if nonblack_rows:
            print(f"  {dev}: 非黑行范围 {nonblack_rows[0]} .. {nonblack_rows[-1]}"
                  f"  (画布高 {rows})")
        else:
            print(f"  {dev}: 全黑（可能当前无内容）")

        # ASCII 缩略图：48 列 × 24 行
        cols, arows = 48, 24
        print(f"  {dev} ASCII 缩略图（{vw}x{vh} 可见区，{cols}x{arows} 采样）:")
        for ay in range(arows):
            line = []
            for ax in range(cols):
                x = int(ax * vw / cols)
                y = int(ay * vh / arows)
                off = y * ll + x * bps
                if off + bps > len(data):
                    line.append(" ")
                    continue
                px = data[off:off + bps]
                if px == b"\x00" * bps:
                    line.append(".")
                else:
                    # 16bpp: 亮起处用 #，中灰用 +
                    if bpp == 16:
                        v = struct.unpack_from("<H", px, 0)[0]
                        lum = ((v >> 11) & 0x1F) * 3 + ((v >> 5) & 0x3F) * 2 + (v & 0x1F) * 3
                        line.append("#" if lum > 200 else ("+" if lum > 60 else "-"))
                    else:
                        line.append("#")
            print("    |" + "".join(line) + "|")

    print("\n探测结束（未做任何写操作）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
