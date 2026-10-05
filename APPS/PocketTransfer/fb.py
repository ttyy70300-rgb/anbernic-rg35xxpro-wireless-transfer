#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端 framebuffer 显示层
========================================

【为什么不用 SDL2】
    该固件的 libmali 无法完成 eglInitialize（EGL_BAD_DISPLAY 0x3008）。
    软链 /dev/mali、10 余种环境变量、预加载 libmali 均无效。
    原厂菜单 dmenu.bin 走 SDL 1.2 直涂 framebuffer，因此不受影响。
    本模块直接 mmap /dev/fb0，绕开整条 GPU/EGL 链路。

【为什么用 ctypes 调 libc.ioctl】
    在这台 4.9 内核 + Python 3.10 上，fcntl.ioctl 的可变 buffer 形式
    读 FBIOGET_VSCREENINFO 不可靠。ctypes 调 libc 的 ioctl 与 C 程序
    完全一致，实测可正确读到参数。

【永远不要切换显示模式 —— 血泪教训】
    曾经用 FBIOPUT_VSCREENINFO 切模式，结果：
      - ioctl 返回 0（报成功）
      - 紧接着 GET 读回"新值"（看起来生效了）
      - 但驱动实际没有换模式，显示时序由 disp 驱动独立控制
      - 程序按新参数算 mmap 长度并按新 stride 寻址 → 写越界
      - → SIGBUS → 进程被内核立刻杀死 → 用户看到"点进去就闪退"
    本模块【只读】显示参数，绝不写回。相关常量与代码已彻底删除。

【结构体偏移：曾经全部错位，已按实机 dump 校准】
    Linux 的 fb_var_screeninfo / fb_fix_screeninfo 是 uapi 结构体，
    但不同 BSP 内核可能裁剪或改动。本模块的偏移表由实机逐字节 dump
    反推确认（见 tools/probe-fb-deep.py 输出）：

      fb_var_screeninfo（本机实测）
        xres=0 yres=4 xres_virtual=8 yres_virtual=12
        xoffset=16 yoffset=20 bits_per_pixel=24 grayscale=28
        red=32 green=44 blue=56 transp=68（每个 fb_bitfield 12 字节）
        nonstd=80 activate=84 height=88 width=92

      fb_fix_screeninfo
        smem_start=16 smem_len=24 line_length=48

    历史 bug：曾误以为 var 有 64 字节私有头，把 xres 读在 64、bpp 读在 88。
    于是 88 偏移读到了 height（物理高度 94mm）→ 报 "不支持的色深 94bpp"。
    教训：**偏移正确性必须用实机 dump 验证，不能靠记忆或猜测。**

【sysfs 是权威兜底】
    /sys/class/graphics/fb0/ 下的 virtual_size / bits_per_pixel / stride
    由内核直接暴露，不含任何 ABI 歧义。本模块探测时会与 ioctl 结果
    交叉验证；若 ioctl 读出的值明显异常，则以 sysfs 为准。

【显示模式现状】
    /sys/class/graphics/fb0/modes 报告支持：
        U:1280x1024p-59
        U:640x480p-59
    当前（dmenu 运行中）为 1280x1024 16bpp，stride 2560。
    本模块在 open() 时把探测结果完整写入日志，便于远程核对。
"""
import ctypes
import mmap
import os
import struct

FB_DEV = "/dev/fb0"
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
# 注意：FBIOPUT_VSCREENINFO（0x4601）已被刻意移除，见文件头说明。

# ---- fb_var_screeninfo 字段偏移（实机 dump 校准）----
O_XRES, O_YRES, O_XRES_V, O_YRES_V = 0, 4, 8, 12
O_XOFF, O_YOFF, O_BPP, O_GRAY = 16, 20, 24, 28
O_RED, O_GREEN, O_BLUE, O_TRANSP = 32, 44, 56, 68
O_NONSTD, O_ACTIVATE, O_HEIGHT, O_WIDTH = 80, 84, 88, 92
VAR_SIZE = 192          # 内核实际约 160 字节，多要一些不会出错

# ---- fb_fix_screeninfo 字段偏移 ----
F_SMEM_START, F_SMEM_LEN, F_LINE_LENGTH = 16, 24, 48
FIX_SIZE = 112

SYSFS_FB = "/sys/class/graphics/fb0"

# RGB565 查表：8bit -> 5bit / 6bit
_TBL_R5 = bytes((i >> 3) for i in range(256))
_TBL_B5 = _TBL_R5
_TBL_G6 = bytes(((i >> 2) & 0x3F) for i in range(256))


class FramebufferError(Exception):
    pass


def _load_libc():
    """
    加载 libc 用于 ioctl。

    延迟到首次真正调用 ioctl 时才加载 —— 这样在 PC 上做离线测试
    （tools/selftest.py 用假 framebuffer 验证字节打包逻辑）时
    不会因为 Linux 专有的 libc.so.6 不存在而直接 import 失败。
    真机（Ubuntu 22.04 aarch64）上正常可用。
    """
    if os.name != "posix":
        raise FramebufferError(
            "ioctl 仅能在 Linux 上使用（当前系统不是 posix）")
    for name in ("libc.so.6", "libc.so", "libc.so.7"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
    raise FramebufferError("无法加载 libc，ioctl 不可用")


_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        _libc = _load_libc()
    return _libc


def _ioctl(fd, req, size):
    buf = (ctypes.c_char * size)()
    ctypes.set_errno(0)
    rc = _get_libc().ioctl(ctypes.c_int(fd), ctypes.c_ulong(req),
                           ctypes.byref(buf))
    if rc != 0:
        raise FramebufferError(
            f"ioctl 0x{req:04x} 失败: {os.strerror(ctypes.get_errno())}")
    return bytes(buf)


def _g32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def _read_sysfs(name):
    """读 sysfs 里的一个属性，失败返回 None。"""
    try:
        with open(os.path.join(SYSFS_FB, name), "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _sysfs_int(name, sep=None):
    v = _read_sysfs(name)
    if not v:
        return None
    try:
        if sep:
            parts = v.split(sep)
            return tuple(int(x) for x in parts)
        return int(v)
    except Exception:
        return None


class Framebuffer:
    """mmap /dev/fb0，支持 16bpp / 32bpp。只读参数，绝不切模式。"""

    def __init__(self, dev=FB_DEV):
        self.dev = dev
        self.fd = -1
        self.mm = None
        self.width = 0
        self.height = 0
        self.virtual_w = 0
        self.virtual_h = 0
        self.bpp = 0
        self.line_px = 0
        self.hoffset = 0
        self.goffset = 0
        self.boffset = 0
        self.toffset = 0
        self.smem_len = 0
        self.smem_start = 0
        self.probe_note = []
        self._last_key = None

    # ---------- 打开与探测 ----------

    def open(self):
        if not os.path.exists(self.dev):
            raise FramebufferError(f"找不到显示设备 {self.dev}")
        self.fd = os.open(self.dev, os.O_RDWR)
        try:
            self._probe()
            # 映射长度取三者中的最大值，确保不会因为任一参数偏差而写越界：
            #   - stride * 虚拟高（常规算法）
            #   - fix.smem_len（驱动报告的显存实际大小）
            size = self.line_px * (self.bpp // 8) * self.virtual_h
            if self.smem_len:
                size = max(size, self.smem_len)
            if size <= 0:
                raise FramebufferError(
                    f"映射长度算得 {size}，探测数据异常 "
                    f"({self.width}x{self.height} bpp={self.bpp} "
                    f"line_px={self.line_px} smem={self.smem_len})")
            self.mm = mmap.mmap(self.fd, size, flags=mmap.MAP_SHARED,
                                prot=mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = -1
            raise
        return self

    def _probe(self):
        """
        读显示参数。ioctl 为主，sysfs 交叉验证并兜底。

        三条自洽性检查，任何一条不过都直接拒绝继续 ——
        宁可启动时报明错，也不能带着错参数去写 framebuffer（会 SIGBUS）。
        """
        note = self.probe_note
        var = _ioctl(self.fd, FBIOGET_VSCREENINFO, VAR_SIZE)
        fix = _ioctl(self.fd, FBIOGET_FSCREENINFO, FIX_SIZE)

        w = _g32(var, O_XRES)
        h = _g32(var, O_YRES)
        vw = _g32(var, O_XRES_V)
        vh = _g32(var, O_YRES_V)
        bpp = _g32(var, O_BPP)
        line_bytes = _g32(fix, F_LINE_LENGTH)
        smem = _g32(fix, F_SMEM_LEN) | (_g32(fix, F_SMEM_LEN + 4) << 32)
        smem_start = _g32(fix, F_SMEM_START) | \
            (_g32(fix, F_SMEM_START + 4) << 32)

        note.append(f"ioctl: {w}x{h} virt={vw}x{vh} bpp={bpp} "
                    f"line={line_bytes} smem={smem}")

        # ---------- sysfs 交叉验证 ----------
        # sysfs 由内核直接暴露，没有 ABI 歧义，是权威兜底。
        s_vsize = _sysfs_int("virtual_size", ",")
        s_bpp = _sysfs_int("bits_per_pixel")
        s_stride = _sysfs_int("stride")
        s_modes = _read_sysfs("modes")
        if s_modes:
            note.append("sysfs modes: " + s_modes.replace("\n", " | "))
        note.append(f"sysfs: virtual_size={s_vsize} bpp={s_bpp} "
                    f"stride={s_stride}")

        # bpp 兜底：ioctl 读不出合法值时信 sysfs
        if bpp not in (16, 32):
            if s_bpp in (16, 32):
                note.append(f"ioctl bpp={bpp} 非法，改用 sysfs bpp={s_bpp}")
                bpp = s_bpp
            else:
                raise FramebufferError(
                    f"不支持的色深 {bpp}bpp（ioctl 与 sysfs 均不可信："
                    f"sysfs bpp={s_bpp}）")

        # line_length 兜底：ioctl 为 0 时信 sysfs stride
        if not line_bytes:
            if s_stride:
                note.append(f"ioctl line_length=0，改用 sysfs stride={s_stride}")
                line_bytes = s_stride
            else:
                raise FramebufferError(
                    f"行宽探测失败（ioctl line_length=0 且 sysfs stride 不可读）"
                    f" (xres={w} yres={h} bpp={bpp})")

        # 分辨率兜底：ioctl 读出异常值时信 sysfs virtual_size
        if not (1 <= w <= 4096 and 1 <= h <= 4096):
            if s_vsize and len(s_vsize) == 2 and \
                    1 <= s_vsize[0] <= 4096 and 1 <= s_vsize[1] <= 4096:
                note.append(f"ioctl 分辨率 {w}x{h} 异常，"
                            f"改用 sysfs {s_vsize[0]}x{s_vsize[1]}")
                w, h = s_vsize
            else:
                raise FramebufferError(f"分辨率异常 {w}x{h}")

        self.width = w
        self.height = h
        self.virtual_w = vw if 1 <= vw <= 8192 else w
        self.virtual_h = vh if 1 <= vh <= 8192 else h
        self.bpp = bpp
        self.smem_len = smem
        self.smem_start = smem_start

        bytepp = bpp // 8
        self.line_px = line_bytes // bytepp

        # ---- 自洽性检查 1：stride 必须能容纳一行可见宽度 ----
        # 历史上出现过「改了 fb_var 但驱动实际仍用旧 stride」的情况，
        # 此时按新 width 作画会写越界 → SIGBUS → 进程瞬间消失（表现为闪退）。
        if self.line_px < self.width:
            raise FramebufferError(
                f"行宽不自洽：line_px={self.line_px} < width={self.width}，"
                f"显示参数不可信，拒绝继续（否则会写越界）"
                f" [line_bytes={line_bytes} bpp={bpp}]")

        # ---- 自洽性检查 2：stride 应等于 virt_w * bytepp（若不等于，
        #      说明 virt_w 读得不对，用 stride 反推更可靠）----
        calc = self.virtual_w * bytepp
        if calc != line_bytes:
            if calc > 0 and line_bytes % bytepp == 0:
                note.append(f"virt_w({self.virtual_w})*bytepp({bytepp})="
                            f"{calc} != line_length({line_bytes})，"
                            f"按 stride 反推 virt_w={line_bytes // bytepp}")
                self.virtual_w = line_bytes // bytepp
            else:
                note.append(f"stride 与 virt_w 不一致且无法反推："
                            f"calc={calc} line_bytes={line_bytes}")

        # ---- 自洽性检查 3：可见区不能超过虚拟区 ----
        if self.width > self.virtual_w or self.height > self.virtual_h:
            note.append(f"可见区 {self.width}x{self.height} 超出虚拟区 "
                        f"{self.virtual_w}x{self.virtual_h}，裁剪到虚拟区")
            self.width = min(self.width, self.virtual_w)
            self.height = min(self.height, self.virtual_h)

        # ---------- 色彩 bitfield ----------
        # 实测该驱动把 red/green/blue/transp 的 bitfield 全填 0，
        # 因此按 bpp 推断标准布局（这也是绝大多数 fb 驱动的实际约定）。
        ro = _g32(var, O_RED)
        rl = _g32(var, O_RED + 4)
        go = _g32(var, O_GREEN)
        gl = _g32(var, O_GREEN + 4)
        bo = _g32(var, O_BLUE)
        bl = _g32(var, O_BLUE + 4)
        to = _g32(var, O_TRANSP)
        if rl == 0 and gl == 0 and bl == 0:
            if bpp == 16:
                ro, rl = 11, 5
                go, gl = 5, 6
                bo, bl = 0, 5
            else:  # 32
                ro, rl = 16, 8
                go, gl = 8, 8
                bo, bl = 0, 8
                to = 24 if to == 0 else to
            note.append("bitfield 全 0（驱动未填），按 bpp 推断标准布局")
        self.hoffset = ro
        self.goffset = go
        self.boffset = bo
        self.toffset = to
        note.append(f"通道: r<<{ro} g<<{go} b<<{bo} a<<{to} "
                    f"(len {rl}/{gl}/{bl})")
        note.append(f"最终: {self.width}x{self.height} "
                    f"virt={self.virtual_w}x{self.virtual_h} "
                    f"bpp={self.bpp} stride_px={self.line_px}")

    # ------------------------------------------------------------------
    # 【已移除】set_mode() —— 切换显示模式
    #
    # 曾经这里有一个用 FBIOPUT_VSCREENINFO 切模式的方法。它已被永久删除，
    # 原因是一个真实的、代价很高的 bug：
    #
    #     调用 FBIOPUT_VSCREENINFO 切换 640x480 后：
    #       - ioctl 返回 0（成功）
    #       - 紧接着 GET 读回新值 640x480（看起来生效了）
    #       - 但 /sys/class/graphics/fb0/virtual_size 仍是旧值
    #     → 驱动实际没有换模式，显示时序由 disp 驱动独立控制，
    #       改写 fb_var_screeninfo 只改变了"软件视角"。
    #     → 程序按新 width 计算 mmap 长度并按新 stride 寻址，
    #       而硬件实际仍用旧 stride → 写入越界 → SIGBUS → 进程被立刻杀死。
    #     → 用户看到的现象就是"点进去就闪退"，且屏幕上没有任何错误信息。
    #
    # 结论：**在本类设备上永远不要切换显示模式**。
    #       只在 open() 时读一次参数，然后按读到的参数作画。
    #
    # 如果需要支持新的分辨率/色深，应该扩展 blit() 的分支，
    # 而不是去改驱动的显示参数。
    # ------------------------------------------------------------------

    def info(self):
        return (f"{self.width}x{self.height} "
                f"virtual={self.virtual_w}x{self.virtual_h} "
                f"bpp={self.bpp} stride_px={self.line_px} "
                f"rgb={self.hoffset}/{self.goffset}/{self.boffset} "
                f"smem={self.smem_len}")

    # ---------- 绘制 ----------

    def px(self, x, y, rgb):
        off = y * self.line_px + x
        if self.bpp == 32:
            v = (((rgb >> 16) & 0xFF) << self.hoffset) \
                | (((rgb >> 8) & 0xFF) << self.goffset) \
                | ((rgb & 0xFF) << self.boffset)
            self.mm[off * 4:off * 4 + 4] = struct.pack("<I", v & 0xFFFFFFFF)
        else:
            v = (((rgb >> 19) & 0x1F) << 11) | (((rgb >> 10) & 0x3F) << 5) \
                | ((rgb >> 3) & 0x1F)
            self.mm[off * 2:off * 2 + 2] = struct.pack("<H", v & 0xFFFF)

    def fill(self, rgb):
        n = self.height * self.line_px
        limit = len(self.mm)
        if self.bpp == 32:
            v = (((rgb >> 16) & 0xFF) << self.hoffset) \
                | (((rgb >> 8) & 0xFF) << self.goffset) \
                | ((rgb & 0xFF) << self.boffset)
            if self.toffset:
                v |= 0xFF << self.toffset
            need = n * 4
            if need > limit:
                n = limit // 4
                need = n * 4
            self.mm[0:need] = struct.pack("<I", v & 0xFFFFFFFF) * n
        else:
            v = (((rgb >> 19) & 0x1F) << 11) | (((rgb >> 10) & 0x3F) << 5) \
                | ((rgb >> 3) & 0x1F)
            need = n * 2
            if need > limit:
                n = limit // 2
                need = n * 2
            self.mm[0:need] = struct.pack("<H", v & 0xFFFF) * n
        self._last_key = None

    def blit(self, img, force=False):
        """
        把 PIL Image 整屏刷入。
        带变化检测：画面与上次完全相同则跳过，配合 UI 只在必要时重绘。
        """
        if img.size != (self.width, self.height):
            # 图像尺寸与屏幕不符时按屏幕尺寸拉伸
            img = img.resize((self.width, self.height))

        rgb = img.convert("RGB")
        key = rgb.tobytes()

        if not force and key == self._last_key:
            return False
        self._last_key = key

        data = key
        stride = self.width * 3
        mv = memoryview(self.mm)

        # 安全上限：所有写入都必须落在这个字节数以内。
        # 越界写入 framebuffer 会触发 SIGBUS，进程直接消失（闪退）。
        limit = len(self.mm)
        row_bytes = self.width * (self.bpp // 8)

        if self.bpp == 32 and self.hoffset == 16 \
                and self.goffset == 8 and self.boffset == 0:
            # BGRA 小端：按字节切片重排，比逐像素循环快约一个量级
            alpha = b"\xff" * self.width if self.toffset == 24 else None
            for y in range(self.height):
                src = y * stride
                row = data[src:src + stride]
                out = bytearray(self.width * 4)
                out[0::4] = row[2::3]
                out[1::4] = row[1::3]
                out[2::4] = row[0::3]
                if alpha:
                    out[3::4] = alpha
                else:
                    out[3::4] = b"\x00" * self.width
                dst = y * self.line_px * 4
                if dst + row_bytes > limit:
                    break
                mv[dst:dst + row_bytes] = out
        elif self.bpp == 16:
            # RGB565（16bpp 下 bitfield 全 0 即标准 5/6/5 打包）。
            # u16 = r5<<11 | g6<<5 | b5，小端存放：
            #   byte0(低) = ((g6 & 0x07) << 5) | b5
            #   byte1(高) = (r5 << 3) | (g6 >> 3)
            # 已用 tools/bench-565.py 逐像素比对验证一致。
            n = self.width
            for y in range(self.height):
                src = y * stride
                r5 = data[src:src + stride:3].translate(_TBL_R5)
                g6 = data[src + 1:src + stride:3].translate(_TBL_G6)
                b5 = data[src + 2:src + stride:3].translate(_TBL_B5)
                out = bytearray(n * 2)
                out[0::2] = bytes(((g6[i] & 0x07) << 5) | b5[i]
                                  for i in range(min(n, len(g6))))
                out[1::2] = bytes((r5[i] << 3) | (g6[i] >> 3)
                                  for i in range(min(n, len(g6))))
                dst = y * self.line_px * 2
                if dst + row_bytes > limit:
                    break
                mv[dst:dst + row_bytes] = out
        else:
            self._blit_generic(data, stride, limit, row_bytes)
        return True

    def _blit_generic(self, data, stride, limit, row_bytes):
        ro, go, bo = self.hoffset, self.goffset, self.boffset
        to = self.toffset
        wo = self.bpp // 8
        mask = (1 << (wo * 8)) - 1
        for y in range(self.height):
            if y * self.line_px * wo + row_bytes > limit:
                break
            src = y * stride
            for x in range(self.width):
                o = src + x * 3
                v = (data[o] << ro) | (data[o + 1] << go) | (data[o + 2] << bo)
                if to:
                    v |= 0xFF << to
                off = (y * self.line_px + x) * wo
                self.mm[off:off + wo] = (v & mask).to_bytes(wo, "little")

    def close(self):
        if self.mm:
            try:
                self.mm.close()
            except Exception:
                pass
            self.mm = None
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = -1

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()
