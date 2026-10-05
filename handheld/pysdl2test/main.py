#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 阶段 0-C/0-D：pysdl2 图形栈验证程序

一次性验证四件事：
  1. SDL2 能否建窗口（mali 驱动，硬件加速）
  2. Pillow 能否绘文字 + SDL_CreateTextureFromSurface 上屏
  3. evdev 能否读到按键（对照原厂 input.py 的 24 字节 struct）
  4. 震动马达 /sys/.../moto 能否写

对照原厂 Roms/APPS/clock 的做法：
  - main.py 负责 board.ini 判机型 + ensure_sdl2()（zip 自解压兜底）
  - graphic.py 负责 SDL_CreateWindow(FULLSCREEN_DESKTOP) + Renderer + Pillow→Texture
  - input.py 直接裸读 /dev/input/event1，用 struct 'llHHI' 解 24 字节
  - 震动走 subprocess 写 sysfs

运行：python3 main.py
退出：START 键（code 311）持续 2 秒，或 A 键（304）立即退出
"""
import ctypes
import os
import struct
import subprocess
import sys
import time

# 必须与原厂 .sh 一致：告诉 pysdl2 去哪找 libSDL2
os.environ.setdefault("PYSDL2_DLL_PATH", "/usr/lib")

try:
    import sdl2
except ImportError:
    print("[FATAL] pysdl2 未安装")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("[FATAL] Pillow 未安装")
    sys.exit(1)

SCREEN_W, SCREEN_H = 640, 480
FONT_PATH = "/mnt/vendor/bin/default.ttf"

MOTO = "/sys/class/power_supply/axp2202-battery/moto"

KEYMAP = {
    304: "A", 305: "B", 306: "Y", 307: "X",
    308: "L1", 309: "R1", 314: "L2", 315: "R2",
    310: "SELECT", 311: "START", 312: "MENUF",
    17: "DY", 16: "DX", 114: "V+", 115: "V-",
}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def hw_model():
    """照抄原厂 main.py 的 board.ini 判定"""
    try:
        with open("/mnt/vendor/oem/board.ini") as f:
            return f.read().splitlines()[0].strip()
    except Exception:
        return "unknown"


def load_font(size):
    for p in (FONT_PATH, os.path.join(os.path.dirname(__file__), "font.ttf")):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception as e:
                log(f"字体 {p} 加载失败: {e}")
    return ImageFont.load_default()


class Evdev:
    """裸读 /dev/input/event1，与原厂 input.py 同构"""

    def __init__(self, path="/dev/input/event1"):
        self.path = path
        self.f = None

    def open(self):
        try:
            self.f = open(self.path, "rb", buffering=0)
            log(f"evdev 打开成功: {self.path}")
            return True
        except Exception as e:
            log(f"evdev 打开失败 {self.path}: {e}")
            return False

    def poll(self, max_events=64):
        """返回本轮 [(code, value), ...]"""
        out = []
        if not self.f:
            return out
        for _ in range(max_events):
            try:
                raw = self.f.read(24)
            except Exception:
                break
            if not raw or len(raw) < 24:
                break
            _sec, _usec, etype, code, value = struct.unpack("llHHI", raw)
            if etype == 1 and value != 0:  # EV_KEY
                out.append((code, 1 if value == 1 else -1))
        return out

    def close(self):
        if self.f:
            try:
                self.f.close()
            except Exception:
                pass


def vibrate(on, ms=200):
    """震动马达：写 1 开 / 0 关"""
    try:
        with open(MOTO, "w") as f:
            f.write("1" if on else "0")
        return True
    except Exception as e:
        log(f"震动写入失败: {e}")
        return False


class Screen:
    """照抄原厂 graphic.py 的 UserInterface 骨架"""

    def __init__(self):
        self.window = None
        self.renderer = None
        self.img = None
        self.draw = None
        self.f_title = load_font(30)
        self.f_body = load_font(20)
        self.f_small = load_font(15)

    def init(self):
        sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"0")

        drv = sdl2.SDL_GetCurrentVideoDriver()
        log(f"SDL_Init ok, 当前视频驱动 = {drv}")

        n = sdl2.SDL_GetNumVideoDrivers()
        log("可用视频驱动: " + ", ".join(
            sdl2.SDL_GetVideoDriver(i).decode() for i in range(n)))

        self.window = sdl2.SDL_CreateWindow(
            b"PocketTransfer",
            sdl2.SDL_WINDOWPOS_UNDEFINED, sdl2.SDL_WINDOWPOS_UNDEFINED,
            0, 0,
            sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN,
        )
        if not self.window:
            log(f"[FATAL] CreateWindow 失败: {sdl2.SDL_GetError().decode()}")
            return False

        ww, wh = ctypes.c_int(), ctypes.c_int()
        sdl2.SDL_GetWindowSize(self.window, ctypes.byref(ww), ctypes.byref(wh))
        log(f"窗口实际尺寸 = {ww.value}x{wh.value}")

        self.renderer = sdl2.SDL_CreateRenderer(self.window, -1,
                                                sdl2.SDL_RENDERER_ACCELERATED)
        if not self.renderer:
            log(f"硬件加速渲染器不可用({sdl2.SDL_GetError().decode()})，回退软件")
            self.renderer = sdl2.SDL_CreateRenderer(self.window, -1,
                                                    sdl2.SDL_RENDERER_SOFTWARE)
        if not self.renderer:
            log(f"[FATAL] CreateRenderer 失败: {sdl2.SDL_GetError().decode()}")
            return False

        ri = sdl2.SDL_GetRendererInfo(self.renderer)
        log(f"渲染器就绪 (软件={bool(ri.flags & sdl2.SDL_RENDERER_SOFTWARE)})")
        return True

    def present(self):
        """Pillow RGBA 图 → SDL Texture → 上屏"""
        sdl2.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
        sdl2.SDL_RenderClear(self.renderer)

        rgba = self.img.tobytes()
        surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
            rgba, SCREEN_W, SCREEN_H, 32, SCREEN_W * 4,
            sdl2.SDL_PIXELFORMAT_RGBA32)
        if not surface:
            log(f"CreateRGBSurface 失败: {sdl2.SDL_GetError().decode()}")
            return
        tex = sdl2.SDL_CreateTextureFromSurface(self.renderer, surface)
        sdl2.SDL_FreeSurface(surface)
        if not tex:
            log(f"CreateTexture 失败: {sdl2.SDL_GetError().decode()}")
            return

        ww, wh = ctypes.c_int(), ctypes.c_int()
        sdl2.SDL_GetWindowSize(self.window, ctypes.byref(ww), ctypes.byref(wh))
        sdl2.SDL_RenderCopy(self.renderer, tex, None,
                            sdl2.SDL_Rect(0, 0, ww.value, wh.value))
        sdl2.SDL_RenderPresent(self.renderer)
        sdl2.SDL_DestroyTexture(tex)

    def begin(self):
        self.img = Image.new("RGBA", (SCREEN_W, SCREEN_H), "#141414")
        self.draw = ImageDraw.Draw(self.img)
        return self.img, self.draw

    def quit(self):
        if self.renderer:
            sdl2.SDL_DestroyRenderer(self.renderer)
        if self.window:
            sdl2.SDL_DestroyWindow(self.window)
        sdl2.SDL_Quit()


def main():
    log("=" * 50)
    log(f"机型 = {hw_model()}")
    log(f"Python = {sys.version.split()[0]}")
    log("=" * 50)

    scr = Screen()
    if not scr.init():
        log("初始化失败，退出")
        return 2

    ev = Evdev()
    ev.open()

    # 测试震动：开机短震一次
    log("测试震动马达 (200ms)...")
    vibrate(True)
    time.sleep(0.2)
    vibrate(False)

    f_title, f_body, f_small = scr.f_title, scr.f_body, scr.f_small
    last_keys = []
    frames = 0
    t0 = time.time()
    start_hold = 0.0
    running = True

    while running:
        scr.begin()
        d = scr.draw

        # 顶部标题条
        d.rectangle([0, 0, SCREEN_W, 44], fill="#0072bb")
        d.text((12, 8), "PocketTransfer 0-C/0-D", font=f_title, fill="#ffffff")

        # 状态行
        d.text((12, 58), "Pillow 绘字 + SDL 纹理上屏", font=f_body, fill="#00ff00")
        d.text((12, 86), f"分辨率 {SCREEN_W}x{SCREEN_H}  机型 {hw_model()}",
               font=f_small, fill="#c8c8c8")

        # 按键回显
        d.text((12, 120), "最近按键:", font=f_body, fill="#ffd700")
        if last_keys:
            for i, k in enumerate(last_keys[-8:]):
                d.text((24, 150 + i * 22), k, font=f_small, fill="#00d7ff")
        else:
            d.text((24, 150), "(按任意键试试)", font=f_small, fill="#666666")

        # 底部提示
        d.rectangle([0, SCREEN_H - 34, SCREEN_W, SCREEN_H], fill="#004f7f")
        d.text((12, SCREEN_H - 28), "A=退出   START 2秒=退出   按键即时震动",
               font=f_small, fill="#ffffff")

        scr.present()
        frames += 1

        # 处理输入
        for code, val in ev.poll():
            name = KEYMAP.get(code, f"key{code}")
            log(f"按键 {name} (code={code} value={val})")
            last_keys.append(f"{name}  code={code}  val={val}")
            vibrate(True)
            time.sleep(0.08)
            vibrate(False)

            if code == 304:  # A
                log("A 键 → 退出")
                running = False
            elif code == 311:  # START
                start_hold = time.time()

        if start_hold and time.time() - start_hold >= 2.0:
            log("START 持续 2 秒 → 退出")
            running = False

        # 帧率统计
        el = time.time() - t0
        if el > 5:
            log(f"帧数 {frames}, {el:.1f}s, {frames/el:.1f} FPS")
            frames, t0 = 0, time.time()

    ev.close()
    scr.quit()
    log("正常退出")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
