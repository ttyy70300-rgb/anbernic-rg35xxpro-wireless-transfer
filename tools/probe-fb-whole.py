#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
只读全显存扫描器 —— 找出"屏幕上到底在显示哪一段内存"

背景
====
现场（2026-10-05 12:5x）：
  - 我们的程序 main.py (PID 4278) 稳定跑 29.9 FPS
  - 唯一持有 /dev/fb0 的进程就是它
  - 程序内部写回自检通过（6 点采样 0 异常）
  - 导出第 0 页得到的是我们**完整正确**的 UI
  - 但物理屏显示原厂"加载中"页面

推理
====
如果屏幕显示的既不是第 0 页也不是第 1 页，
那它要么来自 fb0 之外（disp 硬件图层），
要么来自 fb0 内部**我们没预料到的偏移**。

本脚本做的事（全部只读）
======================
1. 打开 /dev/fb0，读 var/fix 拿权威参数（不写任何东西）
2. mmap 整个显存（只读 PROT_READ）
3. 从**多个可能的起点**（0 / line_length*480 / smem_len-可见区 …）
   切出 640x480 的画面，统计非黑占比 + 生成 ASCII 缩略图
4. 在整个 7MB 里按 stride*480 对齐全量滑动，报告每一段的内容特征
5. 顺便检查：是否有 **两次 UI**（说明程序画了两页）
6. 尝试读 /sys/kernel/debug 下可读的 disp 状态

绝不写：mmap 用 PROT_READ，不调任何写类 ioctl。
"""
import ctypes
import ctypes.util
import os
import struct
import sys

FB = "/dev/fb0"

# --- 标准 uapi 偏移（与 fb.py 保持一致） ---
O_XRES, O_YRES, O_XRES_V, O_YRES_V = 0, 4, 8, 12
O_XOFF, O_YOFF, O_BPP = 16, 20, 24
O_RED, O_GREEN, O_BLUE, O_TRANSP = 32, 44, 56, 68
O_HEIGHT, O_WIDTH = 88, 92
VAR_SIZE = 192
F_SMEM_START, F_SMEM_LEN, F_LINE_LENGTH = 16, 24, 48
FIX_SIZE = 112


def load_libc():
    for name in ("libc.so.6", "libc.so", "libc"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    path = ctypes.util.find_library("c")
    if path:
        return ctypes.CDLL(path, use_errno=True)
    raise RuntimeError("找不到 libc")


def i32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def main():
    libc = load_libc()

    # FBIOGET_VSCREENINFO = 0x4600, FBIOGET_FSCREENINFO = 0x4602
    FBIOGET_VSCREENINFO = 0x4600
    FBIOGET_FSCREENINFO = 0x4602

    fd = os.open(FB, os.O_RDONLY)
    print(f"打开 {FB} (O_RDONLY) -> fd={fd}")

    var = ctypes.create_string_buffer(VAR_SIZE)
    fix = ctypes.create_string_buffer(FIX_SIZE)

    r = libc.ioctl(fd, FBIOGET_VSCREENINFO, var)
    print(f"ioctl FBIOGET_VSCREENINFO -> {r} errno={ctypes.get_errno()}")
    if r != 0:
        print("!! 读不到 var，退出")
        return 1
    vb = var.raw

    r2 = libc.ioctl(fd, FBIOGET_FSCREENINFO, fix)
    print(f"ioctl FBIOGET_FSCREENINFO -> {r2} errno={ctypes.get_errno()}")
    fb = fix.raw if r2 == 0 else b"\x00" * FIX_SIZE

    W = i32(vb, O_XRES)
    H = i32(vb, O_YRES)
    VW = i32(vb, O_XRES_V)
    VH = i32(vb, O_YRES_V)
    XOFF = i32(vb, O_XOFF)
    YOFF = i32(vb, O_YOFF)
    BPP = i32(vb, O_BPP)

    LINE = i32(fb, F_LINE_LENGTH) if r2 == 0 else 0
    SMEM_LEN = i32(fb, F_SMEM_LEN) if r2 == 0 else 0
    SMEM_START = i32(fb, F_SMEM_START) if r2 == 0 else 0

    print()
    print("=== 权威参数 ===")
    print(f"  可见区      {W} x {H}")
    print(f"  虚拟区      {VW} x {VH}")
    print(f"  offset      x={XOFF} y={YOFF}")
    print(f"  bpp         {BPP}")
    print(f"  line_length {LINE}")
    print(f"  smem_len    {SMEM_LEN} ({SMEM_LEN/1048576:.2f} MB)")
    print(f"  smem_start  0x{SMEM_START:x}")

    if LINE <= 0 or SMEM_LEN <= 0 or BPP <= 0:
        print("!! 参数不合法，退出")
        return 1

    bytepp = BPP // 8
    page_bytes = LINE * H
    n_pages = SMEM_LEN // page_bytes if page_bytes else 0
    print(f"  每页字节    {page_bytes}  (={LINE} x {H})")
    print(f"  页数        {n_pages}")

    print()
    print("=== 逐页分析（每页按 640x480 可见区切片）===")
    print("  页 |   字节区间           | 非黑占比 | 特征")
    print("  ---+----------------------+----------+------")

    # 用只读 mmap
    import mmap
    with mmap.mmap(fd, SMEM_LEN, prot=mmap.PROT_READ) as mm:
        report = []
        for p in range(min(n_pages, 8)):
            base = p * page_bytes
            # 采样：按 16x16 网格取 640/16 x 480/16 = 40x30 = 1200 个点
            nonblack = 0
            total = 0
            hist = {}
            for gy in range(0, H, 16):
                row = base + gy * LINE
                if row + W * bytepp > len(mm):
                    break
                for gx in range(0, W, 16):
                    o = row + gx * bytepp
                    if o + bytepp > len(mm):
                        break
                    if bytepp == 4:
                        b, g, rr, a = mm[o], mm[o+1], mm[o+2], mm[o+3]
                    elif bytepp == 2:
                        lo, hi = mm[o], mm[o+1]
                        rr = (hi >> 3) << 3
                        g = ((hi & 7) << 3) | (lo >> 5)
                        b = (lo & 0x1f) << 3
                    else:
                        rr = g = b = mm[o]
                    total += 1
                    if rr > 24 or g > 24 or b > 24:
                        nonblack += 1
                    key = (rr >> 5, g >> 5, b >> 5)
                    hist[key] = hist.get(key, 0) + 1
            pct = (nonblack * 100.0 / total) if total else 0.0
            # 特征：主色调
            top = sorted(hist.items(), key=lambda kv: -kv[1])[:3]
            feat = ", ".join(f"rgb{k[0]*32},{k[1]*32},{k[2]*32}x{v}"
                             for k, v in top)
            mark = ""
            if pct > 60:
                mark = "  <== 有内容！"
            report.append((p, base, base + page_bytes, pct, feat, mark))
            print(f"  {p:2d} | {base:9d}-{base+page_bytes:9d} | {pct:6.1f}% | {feat}{mark}")

        print()
        print("=== 全显存滑动扫描（stride*480 对齐，找所有「有内容」的段）===")
        step = page_bytes
        found = []
        off = 0
        while off + page_bytes <= SMEM_LEN:
            nonblack = 0
            total = 0
            for gy in range(0, H, 32):
                row = off + gy * LINE
                if row + W * bytepp > len(mm):
                    break
                for gx in range(0, W, 32):
                    o = row + gx * bytepp
                    if o + bytepp > len(mm):
                        break
                    if bytepp >= 4:
                        rr, g, b = mm[o+2], mm[o+1], mm[o]
                    elif bytepp == 2:
                        lo, hi = mm[o], mm[o+1]
                        rr = (hi >> 3) << 3
                        g = ((hi & 7) << 3) | (lo >> 5)
                        b = (lo & 0x1f) << 3
                    else:
                        rr = g = b = mm[o]
                    total += 1
                    if rr > 24 or g > 24 or b > 24:
                        nonblack += 1
            pct = (nonblack * 100.0 / total) if total else 0.0
            if pct > 5:
                found.append((off, pct))
            off += step

        print(f"  {len(found)} 段有内容：")
        for off, pct in found:
            print(f"    偏移 {off:9d} ({off/page_bytes:.2f} 页) 非黑 {pct:.1f}%")

        # --- 关键：验证 yoffset 指向的可见区到底是哪一段 ---
        print()
        print("=== 屏幕扫描区验证 ===")
        scan_start = (YOFF * LINE) + (XOFF * bytepp)
        print(f"  由 yoffset={YOFF} 推出扫描起点 = {scan_start}")
        # 逐行比对 yoffset=0 与 yoffset=480 两种可能
        print()
        print("=== 各候选页 ASCII 缩略图（40x30 采样）===")
        for label, base in (("yoffset=0 页", 0),
                            ("yoffset=480 页", LINE * 480)):
            if base + page_bytes > SMEM_LEN:
                print(f"  [{label}] 超出显存，跳过")
                continue
            print(f"  [{label}] base={base}")
            for gy in range(0, H, 16):
                row = base + gy * LINE
                line = "    "
                for gx in range(0, W, 16):
                    o = row + gx * bytepp
                    if o + bytepp > len(mm):
                        line += "?"
                        continue
                    if bytepp >= 4:
                        rr, g, b = mm[o+2], mm[o+1], mm[o]
                    elif bytepp == 2:
                        lo, hi = mm[o], mm[o+1]
                        rr = (hi >> 3) << 3
                        g = ((hi & 7) << 3) | (lo >> 5)
                        b = (lo & 0x1f) << 3
                    else:
                        rr = g = b = mm[o]
                    lum = (rr * 299 + g * 587 + b * 114) // 1000
                    line += " .:-=+*#%@"[min(9, lum // 26)]
                print(line)
            print()

    os.close(fd)
    print("完成（全程只读）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
