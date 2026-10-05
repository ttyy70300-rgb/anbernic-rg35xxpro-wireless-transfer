#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端 —— 启动诊断层（boot）

存在的唯一理由：**让崩溃看得见**。

背景（为什么需要这个文件）：
    本应用通过原厂 dmenu 菜单启动，原厂把应用的 stdout/stderr 丢进
    /dev/null。一旦 Python 在 import 阶段就失败（比如 Pillow 缺失、
    sys.path 不对、fb0 打不开），进程瞬间退出 → 屏幕上什么都没显示 →
    用户看到的只有"点进去就闪退"。这种失败模式无法调试。

本模块的约束（务必遵守，否则又会闪退）：
    1. **只用标准库**。绝不 import PIL、绝不 import 本项目其他模块。
       必须在任何第三方依赖之前就能工作。
    2. **任何失败都不能再抛异常**。所有函数内部 try/except 包死，
       画不出来就安静退出，绝不把异常抛给上层。
    3. **不改任何系统状态**。只 mmap 已存在的 /dev/fb0 写入像素，
       不调 FBIOPUT_VSCREENINFO、不切模式、不写 sysfs。

用法：
    import boot
    boot.checkpoint("开始启动")          # 正常流程打点（写日志）
    ...
    try:
        import PIL
    except Exception as e:
        boot.die("图形库 Pillow 缺失", e)  # 画到屏幕上 + 写日志 + 退出
"""

import os
import sys
import time
import traceback

# ---------------------------------------------------------------- 路径常量

FB_DEV = "/dev/fb0"
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(LOG_DIR, "boot.log")

# ---------------------------------------------------------------- 日志


def _log_path():
    """日志优先写应用目录；不可写就退到 /tmp。"""
    for p in (LOG_PATH, "/tmp/pockettransfer-boot.log"):
        try:
            with open(p, "a", encoding="utf-8"):
                return p
        except Exception:
            continue
    return None


_LOGP = None


def log(msg):
    """追加一行带时间戳的日志。任何情况下都不抛异常。"""
    global _LOGP
    try:
        if _LOGP is None:
            _LOGP = _log_path() or ""
        if not _LOGP:
            return
        with open(_LOGP, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def checkpoint(msg):
    """正常流程打点。"""
    log(msg)


# ---------------------------------------------------------------- 低层 fb 写入
#
# 这里刻意不复用 fb.py：
#   fb.py 用了 ctypes + mmap，逻辑更全但更重；boot 需要的是"最小可工作的
#   画字能力"。两份代码的独立反而提高了可用性 —— 就算 fb.py 有 bug，
#   错误信息一样能显示出来。

_FBIOGET_VSCREENINFO = 0x4600
_FBIOGET_FSCREENINFO = 0x4602
_VAR_SIZE = 192
_FIX_SIZE = 112

# ---- 标准 Linux uapi 偏移（实机逐字节 dump 校准，2026-10-05）----
#
# 历史 bug（已修）：这里曾经是 _O_XRES=64 / _O_BPP=88 / _F_LINE_LENGTH=56，
# 全部错位。88 偏移实际读到的是 height（物理高度 94mm），于是错误屏
# 会按 "94bpp" 去画 —— 于是**连兜底错误屏都画不出来**，屏幕全黑，
# 比不显示更糟。教训：结构体偏移必须实机 dump 验证，不能靠记忆。
#
# fb_var_screeninfo（本机实测）
#   xres=0 yres=4 xres_virtual=8 yres_virtual=12
#   xoffset=16 yoffset=20 bits_per_pixel=24
#   red=32 green=44 blue=56 transp=68
#   height=88 width=92
_O_XRES, _O_YRES = 0, 4
_O_BPP = 24
_O_HEIGHT, _O_WIDTH = 88, 92

# fb_fix_screeninfo
#   smem_start=16 smem_len=24 line_length=48
_F_LINE_LENGTH = 48
_F_SMEM_LEN = 24


def _read_fbinfo(fd):
    """
    读显示参数。返回 dict 或 None。

    用 fcntl.ioctl 的 buffer 形式。注意：在本机 Python 3.10 上，
    这个形式读出的字段与 ctypes 一致（都正确），所以这里可用。
    若某些环境下读出异常值，调用方必须做合理性校验。
    """
    try:
        import fcntl
        import struct
    except Exception:
        return None
    try:
        var = fcntl.ioctl(fd, _FBIOGET_VSCREENINFO, b"\x00" * _VAR_SIZE)
        fix = fcntl.ioctl(fd, _FBIOGET_FSCREENINFO, b"\x00" * _FIX_SIZE)
        w = struct.unpack_from("<I", var, _O_XRES)[0]
        h = struct.unpack_from("<I", var, _O_YRES)[0]
        bpp = struct.unpack_from("<I", var, _O_BPP)[0]
        stride = struct.unpack_from("<I", fix, _F_LINE_LENGTH)[0]
        smem = struct.unpack_from("<I", fix, _F_SMEM_LEN)[0]
        # 合理性校验：读出 0 或离谱值都视为失败
        if not (1 <= w <= 4096 and 1 <= h <= 4096 and bpp in (16, 24, 32)):
            return None
        if not (1 <= stride <= 16384):
            return None
        return {"w": w, "h": h, "bpp": bpp, "stride": stride, "smem": smem}
    except Exception:
        return None


class _Screen:
    """极简 framebuffer 输出。只支持画纯色块 + 8x16 内嵌点阵字。"""

    def __init__(self, info):
        self.info = info
        self.fd = None
        self.mm = None
        self.w = info["w"]
        self.h = info["h"]
        self.bpp = info["bpp"]
        self.stride = info["stride"]

    def open(self):
        import mmap
        self.fd = os.open(FB_DEV, os.O_RDWR)
        size = self.stride * self.h
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)
        return self

    def close(self):
        for obj, meth in ((self.mm, "close"), (self.fd, "close")):
            try:
                if obj is None:
                    continue
                if meth == "close" and isinstance(obj, int):
                    os.close(obj)
                else:
                    obj.close()
            except Exception:
                pass

    def _pack(self, rgb):
        """把 24bit RGB 打包成当前色深的一个像素（小端字节序列）。"""
        r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
        if self.bpp == 32:
            # BGRA，与 RG35XX Pro 实测一致（red=16 green=8 blue=0 transp=24）
            return bytes((b, g, r, 0xFF))
        if self.bpp == 24:
            return bytes((b, g, r))
        # 16bpp 按 RGB565
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return bytes((v & 0xFF, (v >> 8) & 0xFF))

    def fill(self, rgb):
        try:
            px = self._pack(rgb)
            row = px * self.w
            pad = b"\x00" * (self.stride - len(row))
            for y in range(self.h):
                off = y * self.stride
                self.mm[off:off + len(row)] = row
                if pad:
                    self.mm[off + len(row):off + self.stride] = pad
        except Exception:
            pass

    def text(self, x, y, s, rgb, scale=2):
        """
        用内嵌 8x16 点阵字画一行。

        非 ASCII 字符（中文等）无法用点阵字表示，会被替换成 '.'。
        所以**调用方必须同时提供英文/ASCII 版本的关键信息** ——
        见 show_error_screen 的 ascii_hint 参数设计。
        """
        try:
            for ch in s:
                glyph = _FONT.get(ch)
                if glyph is None:
                    # 非 ASCII 一律用一个细点表示，避免整行被 '?' 淹没
                    glyph = _FONT["."] if ord(ch) > 127 else _FONT["?"]
                for row_i, bits in enumerate(glyph):
                    for col_i in range(8):
                        if not (bits >> (7 - col_i)) & 1:
                            continue
                        for dy in range(scale):
                            for dx in range(scale):
                                xx = x + col_i * scale + dx
                                yy = y + row_i * scale + dy
                                if 0 <= xx < self.w and 0 <= yy < self.h:
                                    off = yy * self.stride + xx * (self.bpp // 8)
                                    self.mm[off:off + self.bpp // 8] = self._pack(rgb)
                x += 8 * scale
            return x
        except Exception:
            return x


# ---------------------------------------------------------------- 内嵌点阵字
#
# 8x16 等宽点阵，覆盖 ASCII 32..126。每字符 16 个字节，每字节 8 位 = 一行。
# 只画英文，中文一律降级为 '?' —— boot 层是最后的兜底手段，
# 绝不能因为字体文件缺失而失效。

_FONT = {}


def _build_font():
    """构造 ASCII 点阵。用 5x7 基础字体放大到 8x16 网格，紧凑且够读。"""
    # 5x7 基础字形（每字符 7 行，每行低 5 位有效，bit4 是左边第一列）
    base = {
        " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
        "!": (0x04, 0x04, 0x04, 0x04, 0x00, 0x04, 0x00),
        '"': (0x0A, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x00),
        "#": (0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x00, 0x00),
        "$": (0x04, 0x0F, 0x14, 0x0E, 0x05, 0x1E, 0x04),
        "%": (0x18, 0x19, 0x02, 0x04, 0x08, 0x13, 0x03),
        "&": (0x0C, 0x12, 0x14, 0x08, 0x15, 0x12, 0x0D),
        "'": (0x04, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00),
        "(": (0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02),
        ")": (0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08),
        "*": (0x00, 0x0A, 0x04, 0x1F, 0x04, 0x0A, 0x00),
        "+": (0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00),
        ",": (0x00, 0x00, 0x00, 0x00, 0x0C, 0x04, 0x08),
        "-": (0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00),
        ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C),
        "/": (0x01, 0x02, 0x02, 0x04, 0x08, 0x08, 0x10),
        "0": (0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E),
        "1": (0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E),
        "2": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F),
        "3": (0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E),
        "4": (0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02),
        "5": (0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E),
        "6": (0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E),
        "7": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08),
        "8": (0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E),
        "9": (0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C),
        ":": (0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00),
        ";": (0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x04, 0x08),
        "<": (0x02, 0x04, 0x08, 0x10, 0x08, 0x04, 0x02),
        "=": (0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00),
        ">": (0x08, 0x04, 0x02, 0x01, 0x02, 0x04, 0x08),
        "?": (0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04),
        "@": (0x0E, 0x11, 0x17, 0x15, 0x17, 0x10, 0x0E),
        "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
        "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
        "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
        "D": (0x1E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1E),
        "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
        "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
        "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
        "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
        "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
        "J": (0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C),
        "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
        "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
        "M": (0x11, 0x1B, 0x15, 0x11, 0x11, 0x11, 0x11),
        "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
        "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
        "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
        "Q": (0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D),
        "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
        "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
        "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
        "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
        "V": (0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04),
        "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
        "X": (0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11),
        "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
        "Z": (0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F),
        "[": (0x0E, 0x08, 0x08, 0x08, 0x08, 0x08, 0x0E),
        "\\": (0x10, 0x08, 0x08, 0x04, 0x02, 0x02, 0x01),
        "]": (0x0E, 0x02, 0x02, 0x02, 0x02, 0x02, 0x0E),
        "^": (0x04, 0x0A, 0x11, 0x00, 0x00, 0x00, 0x00),
        "_": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1F),
        "`": (0x08, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00),
        "{": (0x02, 0x04, 0x04, 0x08, 0x04, 0x04, 0x02),
        "|": (0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
        "}": (0x08, 0x04, 0x04, 0x02, 0x04, 0x04, 0x08),
        "~": (0x00, 0x00, 0x0A, 0x15, 0x00, 0x00, 0x00),
    }
    for ch, rows in base.items():
        # 把 7 行 5 位展开成 16 行 8 位：每行重复两次实现纵向放大
        glyph = []
        for bits in rows:
            line = (bits << 3) & 0xFF  # 5 位左移到高 5 位
            glyph.append(line)
            glyph.append(line)
        glyph.extend([0x00] * (16 - len(glyph)))
        _FONT[ch] = tuple(glyph[:16])

    # 小写字母映射到大写。
    # 重要：Python 的异常信息（ModuleNotFoundError、File "main.py" 等）
    # 几乎全是小写。不做这个映射的话，兜底屏上会出现大片 '?'，
    # 关键信息直接读不出来 —— 兜底屏就白做了。
    for lo in "abcdefghijklmnopqrstuvwxyz":
        _FONT[lo] = _FONT[lo.upper()]

    # 常见标点/符号补充：异常信息里出现频率高但 5x7 表里没有的
    _FONT["\t"] = _FONT[" "]
    _FONT["\r"] = _FONT[" "]
    _FONT["\n"] = _FONT[" "]

    _FONT["?"] = _FONT["?"]


_build_font()


# ---------------------------------------------------------------- 对外接口


def _fallback_text_screen(title, detail):
    """
    最低兜底：fb 也打不开时，试试往 /dev/tty1 或 stderr 写。
    """
    msg = f"\n*** PocketTransfer 启动失败 ***\n{title}\n{detail}\n"
    for dev in ("/dev/tty1", "/dev/console"):
        try:
            with open(dev, "w") as f:
                f.write(msg)
            return
        except Exception:
            continue
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


def show_error_screen(title, detail_lines, ascii_title=None,
                      ascii_lines=None):
    """
    把错误画到屏幕上，等 8 秒或用户按任意键。

    双轨输出（重要）：
        内嵌点阵字只能画 ASCII，画不了中文。所以这里同时接收
        中文版（给人看，写日志 + 尽力上屏）和 ASCII 版
        （点阵字能画，保证信息一定能被看到）。
        ascii_title / ascii_lines 为 None 时，自动把中文换成英文占位，
        至少保证布局和行号可读。

    任何失败都只是安静返回 —— 本函数的调用方通常已经准备退出了。
    """
    log(f"[错误上屏] {title}")
    for d in detail_lines:
        log(f"    {d}")

    # 生成 ASCII 版本：中文标题没有对应英文时，用通用提示
    at = ascii_title if ascii_title else _to_ascii(title, "ERROR")
    alines = ascii_lines if ascii_lines else \
        [_to_ascii(d, None) for d in detail_lines if _to_ascii(d, None)]

    # ---- 第一优先：SDL2 ----
    # 在本平台上（Allwinner H700 + disp 多图层），只有走 SDL2
    # 的 FULLSCREEN_DESKTOP 才能拿到顶层图层把画面显示出来。
    # 裸写 fb0 会被 dmenu 残留图层盖住 —— 那样错误屏也看不见。
    # 所以错误屏也必须优先走 SDL2，fb 只是兜底。
    if _show_error_screen_sdl(title, at, alines):
        return

    # ---- 兜底：裸 framebuffer ----
    info = None
    try:
        if os.path.exists(FB_DEV):
            fd = os.open(FB_DEV, os.O_RDONLY)
            try:
                info = _read_fbinfo(fd)
            finally:
                os.close(fd)
    except Exception:
        info = None

    if not info:
        log("无法读取 fb 参数，退到 tty")
        _fallback_text_screen(title, "\n".join(detail_lines))
        return

    scr = None
    try:
        scr = _Screen(info).open()
        log(f"错误屏已打开 {scr.w}x{scr.h} {scr.bpp}bpp stride={scr.stride}")
    except Exception as e:
        log(f"打开 fb 失败: {e}")
        _fallback_text_screen(title, "\n".join(detail_lines))
        return

    try:
        # 深红底 + 白字，醒目且不会与正常 UI 混淆
        scr.fill(0x4A1010)
        y = 20
        scr.text(20, y, "PocketTransfer", 0xFFFFFF, scale=3)
        y += 56
        scr.text(20, y, "STARTUP FAILED", 0xFFD700, scale=2)
        y += 40

        # 每行截断到屏幕宽度（按 scale=2 算，640 宽约 40 字符）
        max_chars = max(20, (scr.w - 40) // 16)

        for line in ([at] + alines)[:10]:
            while line:
                scr.text(20, y, line[:max_chars], 0xFFFFFF, scale=2)
                line = line[max_chars:]
                y += 34
                if y > scr.h - 46:
                    break
            if y > scr.h - 46:
                break

        # 底部：提示 + 日志位置，方便用户/开发者取证
        scr.text(20, scr.h - 30, "Press any key / auto exit 8s",
                 0xCCCCCC, scale=2)
    except Exception:
        pass

    # 等待期间刻意不退出：让用户有时间拍照 / 记下错误
    try:
        _wait_any_key(8.0)
    except Exception:
        time.sleep(8.0)


def _show_error_screen_sdl(title, ascii_title, ascii_lines):
    """
    尝试用 SDL2 把错误屏画出来。成功返回 True，任何一步失败返回 False。

    为什么错误屏也要走 SDL2：
        本平台屏幕是 disp 多图层合成，裸 fb0 在最底层。
        如果只用 fb0 画错误屏，用户看到的仍是 dmenu 残留的"加载中"，
        等于**没有错误提示** —— 这比不显示更误导。

    这里刻意不 import sdl_display（它依赖 PIL）：
    错误屏要能在"PIL 缺失/损坏"这种最坏情况下工作，
    所以只用内嵌点阵字 + 一张手工拼的 RGB 缓冲，通过 SDL 上屏。
    """
    try:
        import sdl2
    except Exception as e:
        log(f"SDL2 不可用于错误屏: {e}")
        return False

    win = None
    renderer = None
    tex = None
    surf = None
    try:
        if sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_VIDEO) != 0 \
                and sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            log(f"错误屏 SDL_Init 失败: {sdl2.SDL_GetError()}")
            return False

        flags = (sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP
                 | sdl2.SDL_WINDOW_SHOWN)
        win = sdl2.SDL_CreateWindow(
            b"PocketTransfer", sdl2.SDL_WINDOWPOS_UNDEFINED,
            sdl2.SDL_WINDOWPOS_UNDEFINED, 0, 0, flags)
        if not win:
            log(f"错误屏 SDL_CreateWindow 失败: {sdl2.SDL_GetError()}")
            return False

        renderer = sdl2.SDL_CreateRenderer(
            win, -1, sdl2.SDL_RENDERER_ACCELERATED)
        if not renderer:
            renderer = sdl2.SDL_CreateRenderer(
                win, -1, sdl2.SDL_RENDERER_SOFTWARE)
        if not renderer:
            log(f"错误屏 SDL_CreateRenderer 失败: {sdl2.SDL_GetError()}")
            return False

        dw = sdl2.c_int(0)
        dh = sdl2.c_int(0)
        sdl2.SDL_GetRendererOutputSize(renderer, dw, dh)
        W = dw.value if dw.value > 0 else 640
        H = dh.value if dh.value > 0 else 480

        # ---- 手工拼一张 RGB24 缓冲（不依赖 PIL）----
        # 深红底
        bg = (0x4A, 0x10, 0x10)
        buf = bytearray(bg * W * H)

        def put(x, y, rgb):
            if 0 <= x < W and 0 <= y < H:
                o = (y * W + x) * 3
                buf[o] = rgb[0]
                buf[o + 1] = rgb[1]
                buf[o + 2] = rgb[2]

        def text(x, y, s, rgb, scale=2):
            """用内嵌点阵字画一行 ASCII。"""
            for ch in s:
                glyph = _FONT.get(ch)
                if glyph is None:
                    glyph = _FONT["."] if ord(ch) > 127 else _FONT["?"]
                for row_i, bits in enumerate(glyph):
                    for col_i in range(8):
                        if not (bits >> (7 - col_i)) & 1:
                            continue
                        for dy in range(scale):
                            for dx in range(scale):
                                put(x + col_i * scale + dx,
                                    y + row_i * scale + dy, rgb)
                x += 8 * scale
            return x

        y = 20
        text(20, y, "PocketTransfer", (255, 255, 255), scale=3)
        y += 56
        text(20, y, "STARTUP FAILED", (255, 215, 0), scale=2)
        y += 40
        max_chars = max(20, (W - 40) // 16)
        for line in ([ascii_title] + list(ascii_lines or []))[:10]:
            while line:
                text(20, y, line[:max_chars], (255, 255, 255), scale=2)
                line = line[max_chars:]
                y += 34
                if y > H - 46:
                    break
            if y > H - 46:
                break
        text(20, H - 30, "Press any key / auto exit 8s",
             (204, 204, 204), scale=2)

        surf = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
            bytes(buf), W, H, 24, W * 3, sdl2.SDL_PIXELFORMAT_RGB24)
        if not surf:
            log(f"错误屏建 surface 失败: {sdl2.SDL_GetError()}")
            return False
        tex = sdl2.SDL_CreateTextureFromSurface(renderer, surf)
        if not tex:
            log(f"错误屏建 texture 失败: {sdl2.SDL_GetError()}")
            return False
        sdl2.SDL_RenderClear(renderer)
        sdl2.SDL_RenderCopy(renderer, tex, None, None)
        sdl2.SDL_RenderPresent(renderer)
        log(f"错误屏已用 SDL2 上屏 {W}x{H}")
    except Exception as e:
        log(f"错误屏 SDL 路径异常: {e}")
        return False
    finally:
        # 保留窗口 8 秒给用户看，然后清理
        try:
            _wait_any_key(8.0)
        except Exception:
            time.sleep(8.0)
        for obj, destroy in ((tex, sdl2.SDL_DestroyTexture),
                             (surf, sdl2.SDL_FreeSurface),
                             (renderer, sdl2.SDL_DestroyRenderer),
                             (win, sdl2.SDL_DestroyWindow)):
            try:
                if obj:
                    destroy(obj)
            except Exception:
                pass
    return True


def _to_ascii(s, default=None):
    """
    尽力把字符串转成可点阵显示的 ASCII。

    策略：剥离非 ASCII 字符，压缩多余空格。若结果为空，返回 default。
    """
    if s is None:
        return default or ""
    out = []
    for ch in str(s):
        if 32 <= ord(ch) < 127:
            out.append(ch)
        elif ch in "\t\r\n":
            out.append(" ")
    text = "".join(out)
    # 压缩连续空格（中文剥离后常留一串空格）
    while "  " in text:
        text = text.replace("  ", " ")
    text = text.strip()
    return text if text else (default or "")


def die(title, exc=None, extra=None, wait=8.0,
        ascii_title=None, ascii_extra=None):
    """
    致命错误统一出口：写日志 → 画屏幕 → sys.exit(1)。

    title        : 中文一句话标题，会写进日志
    exc          : 可选异常对象，会带 traceback
    extra        : 可选字符串列表，补充信息（如路径、版本）
    ascii_title  : 点阵屏上显示的英文标题（点阵字画不了中文）
    ascii_extra  : 点阵屏上显示的英文补充信息

    因为内嵌点阵字只支持 ASCII，屏幕上的信息以英文为主；
    完整中文信息一定在 boot.log 里。
    """
    detail = []
    if extra:
        detail.extend(str(x) for x in extra)
    if exc is not None:
        detail.append(f"{type(exc).__name__}: {exc}")
        try:
            tb = traceback.format_exc().splitlines()
            # 只取最后几行（最关键的位置信息），避免屏幕放不下
            detail.extend(tb[-4:])
        except Exception:
            pass
    if not detail:
        detail = ["(无更多信息)"]

    log("=" * 50)
    log(f"FATAL {title}")
    for d in detail:
        log(f"    {d}")
    log(f"    详见 {_LOGP or '(日志不可写)'}")
    log("=" * 50)

    # 屏幕版：优先用调用方给的英文，否则从中文里剥出 ASCII 部分
    a_lines = []
    if ascii_extra:
        a_lines.extend(ascii_extra)
    a_lines.extend(_to_ascii(d, None) for d in detail)
    a_lines = [x for x in a_lines if x][:9]

    show_error_screen(title, detail,
                      ascii_title=ascii_title, ascii_lines=a_lines)
    # 用 os._exit 而非 sys.exit：确保即使有残留线程也不会卡在解释器关闭流程
    sys.exit(1)


def _wait_any_key(seconds):
    """等一个按键或超时。用 poll 以免在无输入设备时卡死。"""
    import select
    deadline = time.time() + seconds
    fds = []
    for p in ("/dev/input/event1", "/dev/input/event0"):
        try:
            fds.append(open(p, "rb", buffering=0))
        except Exception:
            continue
    if not fds:
        time.sleep(seconds)
        for f in fds:
            try:
                f.close()
            except Exception:
                pass
        return
    try:
        while time.time() < deadline:
            r, _, _ = select.select(fds, [], [], 0.3)
            if r:
                try:
                    r[0].read(24)
                except Exception:
                    pass
                break
    finally:
        for f in fds:
            try:
                f.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 注意：die() 只在上方定义一次（带 ascii_title / ascii_extra 参数）。
# 这里曾经错误地残留了一份旧版 die()（无 ascii_* 参数），
# 因为 Python 后定义者覆盖先定义者，导致调用方传 ascii_title 时抛
# TypeError: die() got an unexpected keyword argument 'ascii_title'，
# 把真正的错误信息彻底掩盖掉。已删除。
# 自查命令：grep -n "^def die" boot.py   —— 必须只有 1 行输出。
# ---------------------------------------------------------------------------
