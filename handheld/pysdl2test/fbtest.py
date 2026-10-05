#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
framebuffer 模块自测：只验证 fb.py，不含 UI

依次验证：
  1. open + 探测参数是否与 fbtest（C 版）实测一致
  2. fill 整屏纯色
  3. blit 整张 Pillow 图（色带 + 方块）
  4. blit 的"画面无变化则跳过"是否生效
  5. 双缓冲翻页是否真的生效（yoffset 回读）
  6. 各项耗时（决定 UI 刷新策略）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

import fb as fbmod

HERE = os.path.dirname(os.path.abspath(__file__))
LOGP = None


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    if LOGP:
        LOGP.write(s + "\n")
        LOGP.flush()


def main():
    global LOGP
    LOGP = open(os.path.join(HERE, "fbtest.log"), "w", encoding="utf-8")
    log("=== framebuffer 模块自测 ===")
    log(f"结构体大小 var={fbmod._VAR_SIZE} fix={fbmod._FIX_SIZE} "
        f"(应为 192 / 112)")

    t = time.time()
    f = fbmod.Framebuffer()
    try:
        f.open()
    except Exception as e:
        log("[FATAL] open 失败:", e)
        import traceback
        traceback.print_exc()
        return 2
    log(f"打开耗时 {time.time()-t:.2f}s")
    log("实测参数:", f.info())
    log("期望    : 640x480 virtual=640x480或960 bpp=32 stride_px=640 "
        "rgb=16/8/0")

    if f.width != 640 or f.height != 480 or f.bpp != 32:
        log(f"[WARN] 参数与预期不符，但继续测试（以实测为准）")

    # --- 1. 整屏填色 ---
    t = time.time()
    f.fill(0x1A1A2E)
    log(f"fill 0x1A1A2E        耗时 {time.time()-t:.3f}s")

    # --- 2. blit 色带图 ---
    img = Image.new("RGB", (f.width, f.height))
    d = ImageDraw.Draw(img)
    h = f.height // 3
    d.rectangle([0, 0, f.width, h], fill=(220, 40, 40))
    d.rectangle([0, h, f.width, 2 * h], fill=(40, 200, 60))
    d.rectangle([0, 2 * h, f.width, f.height], fill=(50, 80, 230))
    for i in range(6):
        x = 40 + i * 95
        d.rectangle([x, 195, x + 70, 295], fill=(255, 255 - i * 30, i * 40))
    try:
        from PIL import ImageFont
        f20 = ImageFont.truetype("/mnt/vendor/bin/default.ttf", 22)
        d.text((20, 155), "Pillow -> framebuffer", font=f20, fill=(255, 255, 255))
    except Exception as e:
        log("字体不可用:", e)

    t = time.time()
    f.blit(img, force=True)
    log(f"blit 整屏(首次)      耗时 {time.time()-t:.3f}s")

    t = time.time()
    f.blit(img)
    log(f"blit 同一张图(应跳过) 耗时 {time.time()-t:.3f}s")

    # --- 3. 双缓冲 ---
    log(f"翻页前 _back={f._back} yoffset={f.yoffset_now()}")
    ok = f.swap()
    log(f"swap() -> {ok}  翻页后 _back={f._back} yoffset={f.yoffset_now()}")
    if f.double:
        expect = f._back * f.height
        got = f.yoffset_now()
        if got == expect:
            log(f"[OK] 双缓冲翻页生效，yoffset={got}")
        else:
            log(f"[WARN] yoffset 期望 {expect} 实际 {got}，"
                f"可能驱动不接受翻页")

        # 往新页画不同内容
        t = time.time()
        img2 = Image.new("RGB", (f.width, f.height), (18, 24, 32))
        d2 = ImageDraw.Draw(img2)
        d2.rectangle([0, 0, f.width, 60], fill=(0, 114, 187))
        try:
            d2.text((16, 18), "page 2 (flicker-free)",
                    font=f20, fill=(255, 255, 255))
        except Exception:
            pass
        f.blit(img2, force=True)
        log(f"第二页 blit          耗时 {time.time()-t:.3f}s")
    else:
        log("单缓冲，跳过翻页测试")

    # --- 4. 压力：连续 10 次全刷，测平均耗时 ---
    t = time.time()
    for i in range(10):
        f.fill(0x000000)
    log(f"fill x10 平均        {(time.time()-t)/10:.3f}s/次")

    t = time.time()
    for i in range(3):
        f.blit(img, force=True)
    log(f"blit x3 平均         {(time.time()-t)/3:.3f}s/次")

    # --- 5. 局部刷（后续 UI 优化用）---
    small = Image.new("RGB", (200, 60), (255, 255, 255))
    rgb = small.tobytes()
    t = time.time()
    mv = memoryview(f.mm)
    for y in range(60):
        row = rgb[y * 600:(y + 1) * 600]
        out = bytearray(200 * 4)
        out[0::4] = row[2::3]
        out[1::4] = row[1::3]
        out[2::4] = row[0::3]
        out[3::4] = b"\xff" * 200
        off = ((f._back * f.height + 100 + y) * f.line_px + 100) * 4
        mv[off:off + 800] = out
    log(f"局部刷 200x60        耗时 {time.time()-t:.3f}s")

    # --- 6. 单像素写 ---
    t = time.time()
    for i in range(2000):
        f.px(i % f.width, 400 + (i // f.width) % 40, 0xFFFF00)
    log(f"px x2000             耗时 {time.time()-t:.3f}s")

    log("")
    log("=== 自测完成，画面保持 45 秒 ===")
    log("（屏幕上应有：红/绿/蓝三条横带 + 6 个渐变色块 + 一行白字）")
    time.sleep(45)
    f.close()
    log("已关闭")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
