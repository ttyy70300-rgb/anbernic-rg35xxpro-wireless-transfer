#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 PC 端程序图标 icon.ico
===========================

画一个「掌机 ⇄ 电脑」的图标，语义一眼可辨：正在传文件。

用多尺寸打包（16/24/32/48/64/128/256），Windows 会在任务栏、
资源管理器、Alt+Tab 各处按需挑合适的那张，不会糊。

用法：
    python tools/make-icon.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "pc", "icon.ico")

S = 256          # 主画布尺寸
BG_TOP = (78, 163, 255)
BG_BOT = (36, 92, 170)


def rounded(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def draw_icon(size=S):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ---- 背景：竖直渐变圆角方块 ----
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        col = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t)
                    for i in range(3)) + (255,)
        gd.line([(0, y), (size, y)], fill=col)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)

    k = size / 256.0
    W = (255, 255, 255, 255)
    DIM = (255, 255, 255, 120)

    # ---- 左侧：掌机（竖长方形 + 十字键 + 两个按键点）----
    lx0, ly0 = int(40 * k), int(62 * k)
    lx1, ly1 = int(112 * k), int(194 * k)
    rounded(d, [lx0, ly0, lx1, ly1], int(12 * k), W)
    # 屏幕
    rounded(d, [int(52 * k), int(76 * k), int(100 * k), int(132 * k)],
            int(4 * k), BG_BOT + (255,))
    # 十字键
    cx, cy, a = int(66 * k), int(158 * k), int(9 * k)
    d.rectangle([cx - a, cy - int(3 * k), cx + a, cy + int(3 * k)],
                fill=BG_BOT + (255,))
    d.rectangle([cx - int(3 * k), cy - a, cx + int(3 * k), cy + a],
                fill=BG_BOT + (255,))
    # 两颗按键
    for (bx, by) in ((92, 150), (92, 168)):
        rr = int(5 * k)
        d.ellipse([int(bx * k) - rr, int(by * k) - rr,
                   int(bx * k) + rr, int(by * k) + rr],
                  fill=BG_BOT + (255,))

    # ---- 右侧：电脑显示器 ----
    mx0, my0 = int(140 * k), int(70 * k)
    mx1, my1 = int(216 * k), int(150 * k)
    rounded(d, [mx0, my0, mx1, my1], int(10 * k), W)
    rounded(d, [int(150 * k), int(80 * k), int(206 * k), int(132 * k)],
            int(3 * k), BG_BOT + (255,))
    # 支架
    d.rectangle([int(172 * k), int(150 * k), int(184 * k), int(172 * k)],
                fill=W)
    rounded(d, [int(156 * k), int(170 * k), int(200 * k), int(180 * k)],
            int(4 * k), W)

    # ---- 中间：双向箭头（传输的语义）----
    ay = int(178 * k)
    ac = (255, 214, 102, 255)
    d.polygon([(int(120 * k), ay - int(7 * k)),
               (int(160 * k), ay),
               (int(120 * k), ay + int(7 * k))], fill=ac)
    d.rectangle([int(108 * k), ay - int(3 * k),
                 int(140 * k), ay + int(3 * k)], fill=ac)

    return img


def main():
    base = draw_icon(S)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32),
             (24, 24), (16, 16)]
    frames = [base.resize(s, Image.LANCZOS) for s in sizes]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    frames[0].save(OUT, format="ICO",
                   sizes=[(f.width, f.height) for f in frames],
                   append_images=frames[1:])
    # 同时存一份 png 供 tkinter 用
    png = os.path.join(os.path.dirname(OUT), "icon.png")
    base.resize((64, 64), Image.LANCZOS).save(png)
    print(f"已生成 {OUT}  ({os.path.getsize(OUT)} 字节)")
    print(f"已生成 {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
