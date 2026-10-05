#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SDL2 崩溃定位实验 —— 逐段执行 sdl_display 的逻辑，每段前先落盘日志。

背景
====
sdl_display.py 在掌机上 rc=139（SIGSEGV），日志断在"输出尺寸 640x480"。
段错误打不出 traceback，只能靠"最后一条已落盘的日志"定位。

而 probe-sdl.py（极简版）完全正常。两者的差异是定位线索：
  - probe-sdl.py：      import sdl2            （无 ext）
  - sdl_display.py：    import sdl2; import sdl2.ext   ← 嫌疑最大
  - probe-sdl.py 用 buf 变量持有像素；sdl_display 曾直接传临时 bytes

本脚本做**控制变量实验**：逐步逼近 sdl_display 的完整流程，
每步都把进度写到 /tmp/sdl-stage.log（用 os.write + fsync，
保证段错误发生前已落盘）。

用法：
  python3 /tmp/probe-sdl-crash.py             # 完整流程
  python3 /tmp/probe-sdl-crash.py noext       # 不 import sdl2.ext
"""
import os
import sys
import time

LOG = "/tmp/sdl-stage.log"


def stage(msg):
    """把阶段写进日志文件并 fsync —— 段错误前必须已落盘。"""
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.write(fd, line.encode())
        os.fsync(fd)
        os.close(fd)
    except Exception:
        pass
    # 同时打 stdout（SSH 能实时看到）
    sys.stdout.write(line)
    sys.stdout.flush()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    stage(f"=== 开始实验 mode={mode} ===")
    stage(f"PYSDL2_DLL_PATH={os.environ.get('PYSDL2_DLL_PATH', '(未设置)')}")

    # ---- 步骤 1：import ----
    stage("A import sdl2 前置")
    import sdl2
    stage(f"A import sdl2 ok  ver={getattr(sdl2, 'version', '?')}")

    if mode != "noext":
        stage("A2 import sdl2.ext 前置")
        try:
            import sdl2.ext  # noqa: F401
            stage("A2 import sdl2.ext ok")
        except Exception as e:
            stage(f"A2 import sdl2.ext FAIL: {type(e).__name__}: {e}")
    else:
        stage("A2 跳过 sdl2.ext（对照）")

    # ---- 步骤 2：Init ----
    stage("B SDL_Init 前置")
    rc = sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)
    stage(f"B SDL_Init rc={rc} err={sdl2.SDL_GetError()}")

    # ---- 步骤 3：CreateWindow ----
    stage("C CreateWindow 前置")
    title = b"CRASHTEST"
    flags = sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN
    win = sdl2.SDL_CreateWindow(
        title, sdl2.SDL_WINDOWPOS_UNDEFINED, sdl2.SDL_WINDOWPOS_UNDEFINED,
        0, 0, flags)
    stage(f"C CreateWindow {'ok' if win else 'NULL'} "
          f"err={sdl2.SDL_GetError()}")
    if not win:
        return 1

    # ---- 步骤 4：CreateRenderer ----
    stage("D CreateRenderer(ACCELERATED) 前置")
    ren = sdl2.SDL_CreateRenderer(win, -1, sdl2.SDL_RENDERER_ACCELERATED)
    stage(f"D CreateRenderer {'ok' if ren else 'NULL'} "
          f"err={sdl2.SDL_GetError()}")
    if not ren:
        stage("D 尝试 SOFTWARE")
        ren = sdl2.SDL_CreateRenderer(win, -1, sdl2.SDL_RENDERER_SOFTWARE)
        stage(f"D SOFTWARE {'ok' if ren else 'NULL'}")
        if not ren:
            return 1

    # ---- 步骤 5：SetHint ----
    stage("E SetHint 前置")
    r = sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"0")
    stage(f"E SetHint r={r}")

    # ---- 步骤 6：GetRendererOutputSize ----
    stage("F GetRendererOutputSize 前置")
    dw = sdl2.c_int(0)
    dh = sdl2.c_int(0)
    sdl2.SDL_GetRendererOutputSize(ren, dw, dh)
    stage(f"F 输出尺寸 {dw.value}x{dh.value}")
    W = dw.value or 640
    H = dh.value or 480

    # ---- 步骤 7：事件泵（sdl_display 新增的步骤）----
    stage("G 首轮事件泵 前置")
    ev = sdl2.SDL_Event()
    for i in range(3):
        n = 0
        while sdl2.SDL_PollEvent(ev):
            n += 1
        sdl2.SDL_PumpEvents()
        stage(f"G   pump#{i} polled={n}")
    stage("G 首轮事件泵 ok")

    # ---- 步骤 8：纯色清屏（sdl_display 的 open() 结尾）----
    stage("H SetRenderDrawColor 前置")
    sdl2.SDL_SetRenderDrawColor(ren, 0, 0, 0, 255)
    stage("H SetRenderDrawColor ok")
    stage("I RenderClear 前置")
    sdl2.SDL_RenderClear(ren)
    stage("I RenderClear ok")
    stage("J RenderPresent 前置")
    sdl2.SDL_RenderPresent(ren)
    stage("J RenderPresent ok")

    # ---- 步骤 9：PIL 路径（sdl_display 的 blit）----
    stage("K PIL import 前置")
    try:
        from PIL import Image, ImageDraw
        stage("K PIL ok")
    except Exception as e:
        stage(f"K PIL FAIL: {e}")
        return 1

    stage("L 建 PIL 图 前置")
    img = Image.new("RGBA", (W, H), (16, 18, 24, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, H - 1], outline=(255, 96, 0), width=3)
    d.line([0, 0, W - 1, H - 1], fill=(0, 214, 255), width=2)
    stage("L 建 PIL 图 ok")

    stage("M rgba.tobytes 前置")
    key = img.convert("RGBA").tobytes()
    stage(f"M tobytes ok {len(key)} 字节")

    # ---- 步骤 10：Surface 创建（两种传参方式对照）----
    stage("N CreateRGBSurfaceWithFormatFrom 前置（用 buf 变量持有）")
    buf = key
    surf = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
        buf, W, H, 32, W * 4, sdl2.SDL_PIXELFORMAT_RGBA32)
    stage(f"N Surface {'ok' if surf else 'NULL'} err={sdl2.SDL_GetError()}")
    if not surf:
        return 1

    stage("O CreateTextureFromSurface 前置")
    tex = sdl2.SDL_CreateTextureFromSurface(ren, surf)
    stage(f"O Texture {'ok' if tex else 'NULL'} err={sdl2.SDL_GetError()}")

    stage("P FreeSurface 前置")
    sdl2.SDL_FreeSurface(surf)
    stage("P FreeSurface ok")

    if not tex:
        return 1

    stage("Q RenderClear/Copy/Present 前置")
    sdl2.SDL_RenderClear(ren)
    sdl2.SDL_RenderCopy(ren, tex, None, None)
    sdl2.SDL_RenderPresent(ren)
    stage("Q 首帧上屏 ok  ← 屏幕应显示深色底 + 橙色边框")

    # ---- 步骤 11：连续多帧（模拟主循环）----
    stage("R 连续 60 帧 前置")
    for i in range(60):
        img2 = Image.new("RGBA", (W, H), (16, 18, 24, 255))
        d2 = ImageDraw.Draw(img2)
        d2.rectangle([2, 2, W - 3, H - 3], outline=(255, 96, 0), width=3)
        d2.rectangle([4 + i, 60, 100 + i, 160], fill=(0, 114, 187, 255))
        k2 = img2.tobytes()
        s2 = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
            k2, W, H, 32, W * 4, sdl2.SDL_PIXELFORMAT_RGBA32)
        t2 = sdl2.SDL_CreateTextureFromSurface(ren, s2)
        sdl2.SDL_FreeSurface(s2)
        sdl2.SDL_RenderClear(ren)
        sdl2.SDL_RenderCopy(ren, t2, None, None)
        sdl2.SDL_RenderPresent(ren)
        sdl2.SDL_DestroyTexture(t2)
        if i % 20 == 0:
            stage(f"R   第 {i} 帧 ok")
        time.sleep(0.03)
    stage("R 连续 60 帧 全部 ok")

    # ---- 步骤 12：清理 ----
    stage("S 清理 前置")
    sdl2.SDL_DestroyTexture(tex)
    sdl2.SDL_DestroyRenderer(ren)
    sdl2.SDL_DestroyWindow(win)
    sdl2.SDL_QuitSubSystem(sdl2.SDL_INIT_VIDEO)
    stage("S 清理 ok")

    stage("=== 实验全部通过，无崩溃 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
