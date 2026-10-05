#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SDL2 通路最小验证 —— 独立进程，跑 5 秒后自动退出并清理自己的图层。

目的
====
在改动的正式程序跑之前，先用一个最小程序验证：
  1. pysdl2 能在本机 import
  2. SDL_Init(VIDEO) 成功
  3. SDL_CreateWindow(FULLSCREEN_DESKTOP) 成功  ← 关键
  4. SDL_CreateRenderer(ACCELERATED/SOFTWARE) 成功
  5. 能真的把像素送到屏幕上（画三道彩色横条 + 文字块）

安全性
======
- 这是一个**独立进程**，不碰任何系统状态
- 不 kill 任何东西、不写 sysfs、不碰 /tmp/.next
- 窗口由 SDL 自动创建和销毁（进程退出即释放图层）
- 5 秒后自动退出，控制权自动交还 dmenu

预期
====
如果屏幕从"加载中"变成彩色横条 → SDL 通路成立，正式程序一定能显示。
如果屏幕毫无变化 → SDL 也被盖住，需要另寻他法（但原厂应用都在用 SDL，
                       所以这个可能性极低）。
"""
import os
import sys
import time

print("=== SDL2 通路最小验证 ===")
print(f"PYSDL2_DLL_PATH = {os.environ.get('PYSDL2_DLL_PATH', '(未设置)')}")
print()

try:
    import sdl2
    print(f"[1] import sdl2          OK")
except Exception as e:
    print(f"[1] import sdl2          FAIL: {type(e).__name__}: {e}")
    sys.exit(1)

try:
    rc = sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)
    print(f"[2] SDL_Init(VIDEO)      rc={rc} err={sdl2.SDL_GetError()}")
    if rc != 0:
        sys.exit(1)
except Exception as e:
    print(f"[2] SDL_Init             FAIL: {type(e).__name__}: {e}")
    sys.exit(1)

win = None
renderer = None
tex = None
surf = None
try:
    flags = sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN
    win = sdl2.SDL_CreateWindow(
        b"SDLPROBE", sdl2.SDL_WINDOWPOS_UNDEFINED,
        sdl2.SDL_WINDOWPOS_UNDEFINED, 0, 0, flags)
    print(f"[3] CreateWindow(FULLSCREEN_DESKTOP) {'OK' if win else 'FAIL'} "
          f"err={sdl2.SDL_GetError()}")
    if not win:
        sys.exit(1)

    renderer = sdl2.SDL_CreateRenderer(win, -1, sdl2.SDL_RENDERER_ACCELERATED)
    kind = "ACCELERATED"
    if not renderer:
        kind = "SOFTWARE"
        renderer = sdl2.SDL_CreateRenderer(win, -1,
                                           sdl2.SDL_RENDERER_SOFTWARE)
    print(f"[4] CreateRenderer       {'OK' if renderer else 'FAIL'} ({kind}) "
          f"err={sdl2.SDL_GetError()}")
    if not renderer:
        sys.exit(1)

    dw = sdl2.c_int(0)
    dh = sdl2.c_int(0)
    sdl2.SDL_GetRendererOutputSize(renderer, dw, dh)
    W, H = (dw.value or 640), (dh.value or 480)
    print(f"[5] 输出尺寸             {W}x{H}")

    # 画一张测试图：RGB 三条 + 黑白棋盘条
    # 用 RGB24 surface，避开 PIL 依赖，确保纯 SDL 通路
    buf = bytearray(W * H * 3)
    rows = [
        (255, 0, 0),      # 红
        (0, 255, 0),      # 绿
        (0, 0, 255),      # 蓝
        (255, 255, 0),    # 黄
        (255, 0, 255),    # 品红
        (0, 255, 255),    # 青
        (255, 255, 255),  # 白
        (0, 0, 0),        # 黑
    ]
    band = max(1, H // len(rows))
    for y in range(H):
        band_idx = min(len(rows) - 1, y // band)
        r, g, b = rows[band_idx]
        base = y * W * 3
        for x in range(W):
            # 右侧 1/4 叠加棋盘格，便于判断是否有缩放/裁切
            if x > W * 3 // 4 and ((x // 16 + y // 16) % 2 == 0):
                r2, g2, b2 = 20, 20, 20
            else:
                r2, g2, b2 = r, g, b
            o = base + x * 3
            buf[o] = r2
            buf[o + 1] = g2
            buf[o + 2] = b2

    surf = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
        bytes(buf), W, H, 24, W * 3, sdl2.SDL_PIXELFORMAT_RGB24)
    print(f"[6] CreateSurface        {'OK' if surf else 'FAIL'} "
          f"err={sdl2.SDL_GetError()}")
    if not surf:
        sys.exit(1)

    tex = sdl2.SDL_CreateTextureFromSurface(renderer, surf)
    print(f"[7] CreateTexture        {'OK' if tex else 'FAIL'} "
          f"err={sdl2.SDL_GetError()}")
    if not tex:
        sys.exit(1)

    sdl2.SDL_RenderClear(renderer)
    sdl2.SDL_RenderCopy(renderer, tex, None, None)
    sdl2.SDL_RenderPresent(renderer)
    print("[8] RenderPresent        OK  ← 屏幕现在应该显示彩色横条")
    print()
    print("保持 5 秒（请观察屏幕）...")

    t_end = time.time() + 5.0
    while time.time() < t_end:
        ev = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ev):
            pass
        sdl2.SDL_RenderPresent(renderer)
        time.sleep(0.05)

    print("5 秒到，开始清理...")

finally:
    for obj, fn in ((tex, sdl2.SDL_DestroyTexture),
                    (surf, sdl2.SDL_FreeSurface),
                    (renderer, sdl2.SDL_DestroyRenderer),
                    (win, sdl2.SDL_DestroyWindow)):
        try:
            if obj:
                fn(obj)
        except Exception:
            pass
    try:
        sdl2.SDL_QuitSubSystem(sdl2.SDL_INIT_VIDEO)
    except Exception:
        pass
    print("清理完成，控制权交还 dmenu")
