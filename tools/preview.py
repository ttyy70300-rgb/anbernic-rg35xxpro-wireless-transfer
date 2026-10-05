#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
渲染预览 —— 把掌机端 UI 画到 PNG，推文件之前先看效果

用途：避免"推上去才发现排版错/字看不见"，一次循环省几分钟。

用法:
    python tools/preview.py
产出:
    RG35XX-Transfer/preview/*.png
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

import main as appmod  # noqa: E402

OUT = os.path.join(ROOT, "preview")
os.makedirs(OUT, exist_ok=True)


class PreviewFB:
    """只提供 draw_frame 需要的属性，不碰任何真实设备。"""

    def __init__(self, w, h, bpp, line_px, rgb, transp, smem):
        self.width, self.height = w, h
        self.bpp = bpp
        self.line_px = line_px
        self.hoffset, self.goffset, self.boffset = rgb
        self.toffset = transp
        self.smem_len = smem


fonts = appmod.Fonts()
print(f"字体来源: {fonts.source or '(内置点阵)'}")


CASES = [
    ("01_实际状态_640x960", 640, 960, 32, 640, (16, 8, 0), 24, {
        "mode": 0, "model": "RG35xxPRO", "base": "/mnt/mmc",
        "ip": "192.168.3.25", "input": "/dev/input/event1",
        "motor": True, "font": "default.ttf",
        "keys": [], "msgs": [], "fps": 29.8,
    }),
    ("02_按键后_640x960", 640, 960, 32, 640, (16, 8, 0), 24, {
        "mode": 0, "model": "RG35xxPRO", "base": "/mnt/mmc",
        "ip": "192.168.3.25", "input": "/dev/input/event1",
        "motor": True, "font": "default.ttf",
        "keys": ["11:52:01 A  code=304 val=+1",
                 "11:52:03 B  code=305 val=+1",
                 "11:52:05 START  code=311 val=+1"],
        "msgs": ["11:52:05 START → 退出", "11:52:01 A → 震动测试"],
        "fps": 30.0,
    }),
    ("03_dmenu期间_1280x1024_16bpp", 1280, 1024, 16, 1280, (0, 0, 0), 0, {
        "mode": 0, "model": "RG35xxPRO", "base": "/mnt/mmc",
        "ip": "192.168.3.25", "input": "/dev/input/event1",
        "motor": True, "font": "default.ttf",
        "keys": ["11:52:01 A  code=304 val=+1"],
        "msgs": [], "fps": 18.3,
    }),
    ("04_无卡根告警_640x480", 640, 480, 32, 640, (16, 8, 0), 24, {
        "mode": 0, "model": "RG35xxPRO", "base": "",
        "ip": "0.0.0.0", "input": "-",
        "motor": False, "font": "builtin",
        "keys": [], "msgs": ["警告: 未拿到卡根，功能将受限"], "fps": 0.0,
    }),
]

for name, w, h, bpp, line_px, rgb, transp, st in CASES:
    sim = PreviewFB(w, h, bpp, line_px, rgb, transp,
                    line_px * (bpp // 8) * h)
    img = appmod.draw_frame(sim, fonts, st)
    p = os.path.join(OUT, f"{name}.png")
    img.save(p)
    print(f"  已生成 {p}  ({img.size[0]}x{img.size[1]})")

print(f"\n预览图目录: {OUT}")
