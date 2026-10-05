#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
T4 之后的新问题：mali-fbdev: Can't create EGL window surface

已知：
  - 错误从 "Could not initialize EGL" 变成 "Can't create EGL window surface"
    => EGL 初始化成功了，mali-fbdev 驱动被正确选中
  - 失败点变成 eglCreateWindowSurface

接下来要试的：
  1. EGL_PLATFORM / EGL_EXT_PLATFORM 各种取值
  2. 窗口尺寸：先试小窗口（mali 可能有对齐要求），再试 0x0
  3. 窗口 flag 组合：有无 SDL_WINDOW_FULLSCREEN_DESKTOP
  4. 分辨率对齐：mali-fbdev 要求 stride 对齐，640*4=2560 应已对齐
  5. 直接调 eglGetDisplay / eglInitialize 看具体返回码
  6. mali-fbdev 的 SDL 源码级 log（开 SDL_LOGGING）
  7. 试 SDL_CreateWindowFrom 用 /dev/fb0

另外要查清：
  - 原厂 launcher 跑起来时环境是什么（/etc/init.d/launcher.sh 里怎么启动子进程）
  - 原厂 clock app 在 SSH 手工前台跑能否成功（如果能，说明差异只在环境）
"""
import base64
import sys

import paramiko

USER, PWD = "root", "root"

# ---- 直接调 EGL 的低层探针（用 ctypes 调 libEGL）----
EGL_PROBE = r'''
import os, ctypes, sys
os.environ["LD_LIBRARY_PATH"] = "/usr/lib"
os.environ.setdefault("PYSDL2_DLL_PATH", "/usr/lib")

egl = ctypes.CDLL("libEGL.so.1")
gl = ctypes.CDLL("libGLESv2.so.2")

EGL_DEFAULT_DISPLAY = 0
EGL_NO_CONTEXT = 0
EGL_NO_SURFACE = 0
EGL_NONE = 0x3038
EGL_WIDTH = 0x3057
EGL_HEIGHT = 0x3056
EGL_VENDOR = 0x3053
EGL_VERSION = 0x3054
EGL_EXTENSIONS = 0x3055
EGL_NONE_ATTR = 0x3038

print("  egl lib loaded:", egl)
dpy = egl.eglGetDisplay(EGL_DEFAULT_DISPLAY)
print("  eglGetDisplay ->", hex(dpy) if dpy else "NULL")
if not dpy:
    sys.exit(1)

major = ctypes.c_int()
minor = ctypes.c_int()
ok = egl.eglInitialize(dpy, ctypes.byref(major), ctypes.byref(minor))
print("  eglInitialize ->", ok, "ver", major.value, minor.value,
      "err", hex(egl.eglGetError()))
if not ok:
    sys.exit(1)

q = ctypes.c_char_p()
egl.eglQueryString(dpy, EGL_VENDOR, ctypes.byref(q))
print("  EGL_VENDOR =", q.value)
egl.eglQueryString(dpy, EGL_VERSION, ctypes.byref(q))
print("  EGL_VERSION =", q.value)
egl.eglQueryString(dpy, EGL_EXTENSIONS, ctypes.byref(q))
exts = (q.value or b"").decode()
print("  EGL_EXTENSIONS =", exts[:400])

# 尝试 native window（fbdev 用 pbuffer / native pixmap）
attrs = ctypes.c_int * 1
surf = egl.eglCreateWindowSurface(dpy, EGL_DEFAULT_DISPLAY,
                                  ctypes.c_void_p(0), None)
print("  eglCreateWindowSurface(0) ->", hex(surf) if surf else "NULL",
      "err", hex(egl.eglGetError()))

cfg_attr = (ctypes.c_int * 5)(
    EGL_SURFACE_TYPE if False else 0x3033, 0x0001,   # EGL_WINDOW_BIT
    EGL_RENDERABLE_TYPE, 0x0008,                     # EGL_OPENGL_ES2_BIT
    EGL_NONE)
ctx = egl.eglCreateContext(dpy, EGL_DEFAULT_DISPLAY, EGL_NO_CONTEXT,
                           EGL_NO_CONTEXT, None, None)
print("  eglCreateContext ->", hex(ctx) if ctx else "NULL",
      "err", hex(egl.eglGetError()))
'''

# ---- 逐个试窗口参数组合 ----
WIN_TRIALS = r'''
import os, sys, ctypes
os.environ["LD_LIBRARY_PATH"] = "/usr/lib"
os.environ.setdefault("PYSDL2_DLL_PATH", "/usr/lib")
import sdl2

def log(*a): print(*a, flush=True)

sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)
log("  video driver =", sdl2.SDL_GetCurrentVideoDriver())

TRIALS = [
    ("fullscreen_desktop 0x0", sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN, 0, 0),
    ("shown 640x480",          sdl2.SDL_WINDOW_SHOWN, 640, 480),
    ("shown 320x240",          sdl2.SDL_WINDOW_SHOWN, 320, 240),
    ("shown 640x480 fullscreen", sdl2.SDL_WINDOW_FULLSCREEN | sdl2.SDL_WINDOW_SHOWN, 640, 480),
    ("opengl 320x240",         sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_SHOWN, 320, 240),
    ("opengl 0x0",             sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_SHOWN, 0, 0),
    ("hidden 640x480",         sdl2.SDL_WINDOW_HIDDEN, 640, 480),
    ("borderless 640x480",     sdl2.SDL_WINDOW_BORDERLESS_VISIBLE, 640, 480),
]

for name, flags, w, h in TRIALS:
    win = sdl2.SDL_CreateWindow(b"t", 0, 0, w, h, flags)
    if not win:
        log("  %-28s FAIL: %s" % (name, sdl2.SDL_GetError().decode()))
        continue
    ww, wh = ctypes.c_int(), ctypes.c_int()
    sdl2.SDL_GetWindowSize(win, ctypes.byref(ww), ctypes.byref(wh))
    r = sdl2.SDL_CreateRenderer(win, -1, sdl2.SDL_RENDERER_ACCELERATED)
    m = "accel"
    if not r:
        r = sdl2.SDL_CreateRenderer(win, -1, sdl2.SDL_RENDERER_SOFTWARE)
        m = "soft"
    log("  %-28s window=%dx%d renderer=%s" % (name, ww.value, wh.value,
        m if r else "NONE:" + sdl2.SDL_GetError().decode()))
    if r:
        sdl2.SDL_DestroyRenderer(r)
    sdl2.SDL_DestroyWindow(win)
'''

SHELL_CMDS = [
    ("原厂 launcher 启动脚本里怎么调子进程",
     "grep -nE 'export|LD_LIBRARY|PYSDL|EGL|MALI|\\.sh|exec' /etc/init.d/launcher.sh 2>&1 | head -40"),
    ("原厂 launcher 主 UI 是什么进程",
     "ps aux | grep -v grep | grep -iE 'mainui|\\.sh|retroarch|emulator' | head -10"),
    ("/etc/init.d/launcher.sh 全文前 80 行",
     "head -80 /etc/init.d/launcher.sh 2>&1"),
    ("原厂 dmenu.bin 是什么",
     "ls -la /mnt/vendor/bin/dmenu.bin 2>&1; file /mnt/vendor/bin/dmenu.bin 2>&1"),
    ("原厂 fbtest3 二进制可否跑通（对照）",
     "ls -la /mnt/vendor/bin/fbtest3 2>&1"),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    sftp = cli.open_sftp()
    for name, code in [("eglow.py", EGL_PROBE), ("wintrials.py", WIN_TRIALS)]:
        with sftp.open(f"/tmp/{name}", "w") as f:
            f.write("# -*- coding: utf-8 -*-\n" + code)
    sftp.close()

    stages = [
        ("低层 EGL 探针",
         "cd /tmp && timeout 20 python3 /tmp/eglow.py 2>&1 | grep -v setterm"),
        ("窗口参数组合试验",
         "cd /tmp && timeout 30 python3 /tmp/wintrials.py 2>&1 | grep -v setterm"),
    ]
    for title, cmd in SHELL_CMDS:
        stages.append((title, cmd))

    for title, cmd in stages:
        print(f">>> {title}")
        print("-" * 66)
        _, o, e = cli.exec_command(cmd, timeout=60)
        d = (o.read().decode("utf-8", "replace")
             + e.read().decode("utf-8", "replace")).strip()
        print(d or "(空)")
        print()

    cli.close()


if __name__ == "__main__":
    main()
