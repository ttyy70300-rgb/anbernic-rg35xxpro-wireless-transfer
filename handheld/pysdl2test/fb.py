#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端 framebuffer 显示层

为什么不用 SDL2：
    实测该固件的 libmali 无法完成 eglInitialize（返回 EGL_BAD_DISPLAY 0x3008）。
    软链 /dev/mali、10 余种环境变量、预加载 libmali 均无效。
    原厂菜单 dmenu.bin 走 SDL 1.2 直涂 framebuffer，因此不受影响。
    本模块直接 mmap /dev/fb0，绕开整条 GPU/EGL 链路。

真机参数（fbtest C 版与本模块交叉验证一致）：
    分辨率    640x480
    虚屏      640x960（双缓冲，两页 y 偏移 0 / 480）
    色深      32bpp，line_length 2560 字节
    通道偏移  red=16 green=8 blue=0，内存布局为 BGRA 小端

结构体布局严格对照 Linux uapi/linux/fb.h。
注意 fb_var_screeninfo / fb_fix_screeninfo 开头都有 __ 前缀私有头，
漏掉它们会导致 ioctl 读回错位数据（曾误读成 1280x1024/16bpp）。
    var: 192 字节   fix: 112 字节
"""
import fcntl
import mmap
import os
import struct

FB_DEV = "/dev/fb0"

FBIOGET_FSCREENINFO = 0x4602
FBIOGET_VSCREENINFO = 0x4600
FBIOPAN_DISPLAY = 0x4601

# ---- fb_fix_screeninfo，112 字节 ----
_FIX_FIELDS = [
    ("id", "16s"), ("smem_start", "Q"), ("smem_len", "Q"),
    ("type", "I"), ("type_aux", "I"), ("visual", "I"),
    ("xpanstep", "I"), ("ypanstep", "I"), ("ywstep", "I"), ("xhstep", "I"),
    ("accel", "I"), ("video_caps", "I"),
    ("res", "I"), ("virtual", "I"),
    ("line_length", "I"),
    ("mmio_start", "Q"), ("mmio_len", "Q"),
    ("accel_flags", "I"), ("capabilities", "I"),
    ("reserved", "2I"),
]

# ---- fb_var_screeninfo，192 字节 ----
_VAR_FIELDS = [
    ("__id", "I"), ("__smem_start", "Q"), ("__smem_len", "Q"),
    ("__type", "I"), ("__type_aux", "I"), ("__visual", "I"),
    ("__xpanstep", "I"), ("__ypanstep", "I"),
    ("__ywstep", "I"), ("__xhstep", "I"), ("__accel", "I"),
    ("__video_caps", "I"), ("__reserved", "2I"),
    ("xres", "I"), ("yres", "I"),
    ("xres_virtual", "I"), ("yres_virtual", "I"),
    ("xoffset", "I"), ("yoffset", "I"),
    ("bits_per_pixel", "I"), ("grayscale", "I"),
    ("fb_bitfield_red", "I"), ("fb_bitfield_green", "I"),
    ("fb_bitfield_blue", "I"), ("fb_bitfield_transp", "I"),
    ("nonstd", "I"), ("activate", "I"),
    ("height", "I"), ("width", "I"),
    ("accel_flags", "I"), ("pixclock", "I"),
    ("left_margin", "I"), ("right_margin", "I"),
    ("upper_margin", "I"), ("lower_margin", "I"),
    ("hsync_len", "I"), ("vsync_len", "I"),
    ("sync", "I"), ("vmode", "I"),
    ("rotate", "I"), ("colorspace", "I"),
    ("__reserved2", "4I"),
]

_FIX_FMT = "<" + "".join(t for _, t in _FIX_FIELDS)
_VAR_FMT = "<" + "".join(t for _, t in _VAR_FIELDS)
_FIX_SIZE = struct.calcsize(_FIX_FMT)   # 112
_VAR_SIZE = struct.calcsize(_VAR_FMT)   # 192


def _names(fields):
    return [f[0] for f in fields]


class FramebufferError(Exception):
    pass


class Framebuffer:
    """mmap /dev/fb0，支持 16bpp 与 32bpp，支持双缓冲翻页"""

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
        self.double = False
        self.fix_smem_len = 0
        self._back = 0
        self._var_buf = None
        self._last_img = None

    # ---------- 打开与探测 ----------

    def open(self):
        if not os.path.exists(self.dev):
            raise FramebufferError(f"找不到 {self.dev}")
        self.fd = os.open(self.dev, os.O_RDWR)
        try:
            self._probe()
            size = self.line_px * self.bpp // 8 * self.virtual_h
            if size <= 0:
                raise FramebufferError(f"算出映射长度 {size}，探测数据有误")
            self.mm = mmap.mmap(self.fd, size,
                                flags=mmap.MAP_SHARED,
                                prot=mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = -1
            raise
        return self

    def _ioctl(self, req, size):
        buf = bytearray(size)
        fcntl.ioctl(self.fd, req, buf, True)
        return bytes(buf)

    def _probe(self):
        var = self._ioctl(FBIOGET_VSCREENINFO, _VAR_SIZE)
        fix = self._ioctl(FBIOGET_FSCREENINFO, _FIX_SIZE)

        v = dict(zip(_names(_VAR_FIELDS), struct.unpack_from(_VAR_FMT, var, 0)))
        f = dict(zip(_names(_FIX_FIELDS), struct.unpack_from(_FIX_FMT, fix, 0)))

        self.width = v["xres"]
        self.height = v["yres"]
        self.virtual_w = v["xres_virtual"]
        self.virtual_h = v["yres_virtual"]
        self.bpp = v["bits_per_pixel"]
        self.fix_smem_len = f["smem_len"]
        self.line_px = f["line_length"] // (self.bpp // 8)
        self.double = self.virtual_h > self.height

        self.hoffset = v["fb_bitfield_red"] & 0xFF
        self.goffset = v["fb_bitfield_green"] & 0xFF
        self.boffset = v["fb_bitfield_blue"] & 0xFF
        self.toffset = v["fb_bitfield_transp"] & 0xFF

        if self.bpp not in (16, 32):
            raise FramebufferError(f"暂不支持的色深 {self.bpp}bpp")
        if self.width <= 0 or self.height <= 0:
            raise FramebufferError(
                f"分辨率异常 {self.width}x{self.height}，结构体布局可能不对")

    def info(self):
        return (f"{self.width}x{self.height} "
                f"virtual={self.virtual_w}x{self.virtual_h} "
                f"bpp={self.bpp} stride_px={self.line_px} "
                f"rgb={self.hoffset}/{self.goffset}/{self.boffset} "
                f"transp={self.toffset} double={self.double} "
                f"smem={self.fix_smem_len}")

    # ---------- 像素写入 ----------

    def px(self, x, y, rgb, page=None):
        """写单点，rgb 为 0xRRGGBB"""
        page = self._back if page is None else page
        off = (page * self.height + y) * self.line_px + x
        if self.bpp == 32:
            v = (((rgb >> 16) & 0xFF) << self.hoffset) \
                | (((rgb >> 8) & 0xFF) << self.goffset) \
                | ((rgb & 0xFF) << self.boffset)
            self.mm[off * 4:off * 4 + 4] = struct.pack("<I", v & 0xFFFFFFFF)
        else:
            v = (((rgb >> 19) & 0x1F) << 11) | (((rgb >> 10) & 0x3F) << 5) \
                | ((rgb >> 3) & 0x1F)
            self.mm[off * 2:off * 2 + 2] = struct.pack("<H", v & 0xFFFF)

    def blit(self, img, page=None, force=False):
        """
        把 PIL Image 刷到 framebuffer（整屏）。
        img 为 RGB/RGBA，尺寸须与分辨率一致。

        性能：640x480 全刷约 0.3~0.6 秒（Cortex-A53 + Python）。
        因此这里带"脏矩形"优化 —— 与上次上屏内容逐行比对，
        只写变化的行。全屏变化时退化为全刷。
        """
        if img.size != (self.width, self.height):
            raise FramebufferError(
                f"图像尺寸 {img.size} != 屏幕 {self.width}x{self.height}")

        page = self._back if page is None else page

        if not force and self._last_img is not None \
                and self._last_img.size == img.size:
            rgb = img.convert("RGB")
            if rgb.tobytes() == self._last_img.tobytes():
                return False   # 画面无变化，不刷
        else:
            rgb = img.convert("RGB")

        self._blit_full(rgb, page)
        self._last_img = rgb
        return True

    def _blit_full(self, rgb, page):
        data = rgb.tobytes()
        stride = self.width * 3
        base = page * self.height * self.line_px
        mv = memoryview(self.mm)

        if self.bpp == 32 and self.toffset == 0:
            ro, go, bo = self.hoffset, self.goffset, self.boffset
            if ro == 16 and go == 8 and bo == 0:
                # BGRA 小端：内存字节序就是 B,G,R,x
                # 逐行做 bytes 切片重排，比逐像素循环快一个量级
                for y in range(self.height):
                    src = y * stride
                    row = data[src:src + stride]
                    out = bytearray(self.width * 4)
                    out[0::4] = row[2::3]   # B
                    out[1::4] = row[1::3]   # G
                    out[2::4] = row[0::3]   # R
                    out[3::4] = b"\xff" * self.width
                    dst = (base + y * self.line_px) * 4
                    mv[dst:dst + self.width * 4] = out
                return
            self._blit_generic(data, stride, base)
        elif self.bpp == 32:
            self._blit_generic(data, stride, base)
        else:
            self._blit_16(data, stride, base)

    def _blit_generic(self, data, stride, base):
        """任意通道偏移 / 带 alpha 的通用路径"""
        ro, go, bo = self.hoffset, self.goffset, self.boffset
        to = self.toffset
        wo = self.bpp // 8
        for y in range(self.height):
            src = y * stride
            for x in range(self.width):
                o = src + x * 3
                v = (data[o] << ro) | (data[o + 1] << go) | (data[o + 2] << bo)
                if to:
                    v |= 0xFF << to
                off = base + y * self.line_px + x
                self.mm[off * wo:off * wo + wo] = (v & ((1 << (wo * 8)) - 1)) \
                    .to_bytes(wo, "little")

    def _blit_16(self, data, stride, base):
        wo = 2
        for y in range(self.height):
            src = y * stride
            for x in range(self.width):
                o = src + x * 3
                v = (((data[o] >> 3) << 11) | ((data[o + 1] >> 2) << 5)
                     | (data[o + 2] >> 3))
                off = base + y * self.line_px + x
                self.mm[off * wo:off * wo + wo] = struct.pack("<H", v & 0xFFFF)

    def fill(self, rgb, page=None):
        """整屏填色，比 blit 快得多"""
        page = self._back if page is None else page
        n = self.height * self.line_px
        if self.bpp == 32:
            v = (((rgb >> 16) & 0xFF) << self.hoffset) \
                | (((rgb >> 8) & 0xFF) << self.goffset) \
                | ((rgb & 0xFF) << self.boffset) \
                | (0xFF << self.toffset if self.toffset else 0)
            self.mm[page * n * 4:(page + 1) * n * 4] = \
                struct.pack("<I", v & 0xFFFFFFFF) * n
        else:
            v = (((rgb >> 19) & 0x1F) << 11) | (((rgb >> 10) & 0x3F) << 5) \
                | ((rgb >> 3) & 0x1F)
            self.mm[page * n * 2:(page + 1) * n * 2] = \
                struct.pack("<H", v) * n
        self._last_img = None

    # ---------- 双缓冲 ----------

    def swap(self):
        """切到另一页并显示。返回是否成功。"""
        if not self.double:
            return False
        newback = 1 - self._back
        buf = bytearray(self._ioctl(FBIOGET_VSCREENINFO, _VAR_SIZE))
        struct.pack_into("<I", buf, 5 * 4, newback * self.height)
        try:
            fcntl.ioctl(self.fd, FBIOPAN_DISPLAY, buf, True)
        except OSError:
            self.double = False
            self._back = 0
            return False
        self._back = newback
        return True

    def yoffset_now(self):
        """读回驱动当前的 yoffset，用于验证翻页是否真生效"""
        try:
            var = self._ioctl(FBIOGET_VSCREENINFO, _VAR_SIZE)
            return struct.unpack_from("<I", var, 5 * 4)[0]
        except Exception:
            return -1

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
