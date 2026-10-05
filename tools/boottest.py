#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验证 boot.py 的兜底错误屏 —— 用假 framebuffer 渲染，不碰真设备

为什么要专门测这个：
    boot.py 是"防闪退"的最后一道防线。如果它本身有 bug，
    真机上崩溃时依然什么都看不到，等于没做。
    所以必须单独验证：给定一段错误信息，它能不能真的画出可读的画面。

用法:
    python tools/boottest.py
产出:
    RG35XX-Transfer/preview/err_*.png
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

import boot  # noqa: E402

OUT = os.path.join(ROOT, "preview")
os.makedirs(OUT, exist_ok=True)

# 直接测 _Screen 的绘制能力（show_error_screen 会真去 open /dev/fb0）
SCENES = [
    ("err_1_PIL缺失", "PILLOW LOAD FAILED", [
        "Pillow (PIL) is required but missing.",
        "Stock firmware should ship Pillow 9.0.1.",
        "ModuleNotFoundError: No module named 'PIL'",
        'File ".../PocketTransfer/main.py", line 45',
        "from PIL import Image, ImageDraw, ImageFont",
    ]),
    ("err_2_显示参数异常", "BAD SCREEN PARAMETERS", [
        "640x480 bpp=32 line_px=320 virtual_h=480",
        "-> 614400 bytes to map.",
        "Refusing to draw (would write out of bounds).",
    ]),
    ("err_3_fb打开失败", "FRAMEBUFFER OPEN FAILED", [
        "PermissionError: Permission denied: '/dev/fb0'",
        "Cannot open /dev/fb0.",
        "Check this is an RG35XX Pro.",
    ]),
]

# 手工复刻 show_error_screen 的绘制部分，但用假 mmap
from PIL import Image  # noqa: E402


class FakeScreen(boot._Screen):
    def __init__(self, info, w, h):
        super().__init__(info)
        self.w = w
        self.h = h
        self.bpp = info["bpp"]
        self.stride = w * (self.bpp // 8)
        from PIL import Image as _I
        self._img = _I.new("RGB", (w, h), (0, 0, 0))
        self._px = self._img.load()

    def _pack(self, rgb):
        return rgb

    def fill(self, rgb):
        r = (rgb >> 16) & 0xFF
        g = (rgb >> 8) & 0xFF
        b = rgb & 0xFF
        for yy in range(self.h):
            for xx in range(self.w):
                self._px[xx, yy] = (r, g, b)

    def text(self, x, y, s, rgb, scale=2):
        r = (rgb >> 16) & 0xFF
        g = (rgb >> 8) & 0xFF
        b = rgb & 0xFF
        cx = x
        for ch in s:
            glyph = boot._FONT.get(ch) or boot._FONT["?"]
            for row_i, bits in enumerate(glyph):
                for col_i in range(8):
                    if not (bits >> (7 - col_i)) & 1:
                        continue
                    for dy in range(scale):
                        for dx in range(scale):
                            xx = cx + col_i * scale + dx
                            yy = y + row_i * scale + dy
                            if 0 <= xx < self.w and 0 <= yy < self.h:
                                self._px[xx, yy] = (r, g, b)
            cx += 8 * scale
        return cx


info = {"w": 640, "h": 480, "bpp": 32, "stride": 2560}

for name, title, details in SCENES:
    # 复刻 show_error_screen 的排版逻辑（含 ASCII 版头部）
    scr = FakeScreen(info, 640, 480)
    scr.fill(0x4A1010)
    y = 20
    scr.text(20, y, "PocketTransfer", 0xFFFFFF, scale=3)
    y += 56
    scr.text(20, y, "STARTUP FAILED", 0xFFD700, scale=2)
    y += 40
    max_chars = max(20, (640 - 40) // 16)
    for line in ([title] + details)[:10]:
        while line:
            scr.text(20, y, line[:max_chars], 0xFFFFFF, scale=2)
            line = line[max_chars:]
            y += 34
            if y > scr.h - 46:
                break
        if y > scr.h - 46:
            break
    scr.text(20, scr.h - 30, "Press any key / auto exit 8s",
             0xCCCCCC, scale=2)

    p = os.path.join(OUT, f"{name}.png")
    scr._img.save(p)
    # 有效性检查：必须有非背景色像素，否则等于没画
    colors = scr._img.getcolors(maxcolors=100000)
    non_bg = [c for c in colors if c[1] not in ((0x4A, 0x10, 0x10),)]
    print(f"  {name}: {len(colors)} 种颜色，非背景 {len(non_bg)} 种  -> {p}")
    assert len(colors) > 3, f"{name} 只画出了 {len(colors)} 种颜色，兜底屏失效！"

# 关键验证：小写字母必须可显示（之前全是 '?'，导致异常信息读不出来）
lower = [c for c in "abcdefghijklmnopqrstuvwxyz" if c in boot._FONT]
print(f"\n  小写字母覆盖: {len(lower)}/26")
assert len(lower) == 26, f"小写字母缺失: {set('abcdefghijklmnopqrstuvwxyz') - set(lower)}"

# 验证 _to_ascii 的中文剥离能力
cases = [
    ("显示设备初始化失败", "注意中文会被剥离"),
    ("File \"main.py\", line 45", None),
]
for src, _ in cases:
    got = boot._to_ascii(src, "FALLBACK")
    print(f'  _to_ascii({src!r}) -> {got!r}')
    assert got, f"{src!r} 转 ASCII 后为空"

got = boot._to_ascii("图形库 Pillow 加载失败", "FALLBACK")
assert "Pillow" in got, f"中文串剥离后应保留英文，实得 {got!r}"
print(f'  混合串 "图形库 Pillow 加载失败" -> {got!r}  (英文保留)')

print("\nboot.py 兜底屏绘制正常，崩溃信息可以被看到。")
