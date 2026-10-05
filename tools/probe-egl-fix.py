#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验证 EGL 失败的修复方案

假设：/usr/lib/libEGL.so.1 转发到 libmali.so.0（mali 真身），
     但同时存在 mesa 版 /usr/lib/aarch64-linux-gnu/libEGL.so.1.1.0，
     动态链接器解析顺序不对导致拿到 mesa，mesa 无 DRM 设备 → EGL 初始化失败。

依次尝试：
  T1 基线：不设任何 LD_LIBRARY_PATH
  T2 预加载 mali 版 libEGL
  T3 预加载 libmali 本体
  T4 只设 LD_LIBRARY_PATH=/usr/lib
  T5 SDL_VIDEODRIVER=mali + 预加载
  T6 改用 SDL_RENDER_DRIVER=software（绕过 EGL 走 fbdev 软渲染）
  T7 直接走 /dev/fb0 裸 framebuffer（sb2py 兜底方案）
"""
import sys

import paramiko

USER, PWD = "root", "root"

# 每次测试的 python 代码（注意用 base64 传，避免引号地狱）
PROBE = r'''
import os, sys, ctypes
os.environ.setdefault("PYSDL2_DLL_PATH", "/usr/lib")
import sdl2
sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)
drv = sdl2.SDL_GetCurrentVideoDriver()
n = sdl2.SDL_GetNumVideoDrivers()
names = [sdl2.SDL_GetVideoDriver(i).decode() for i in range(n)]
w = sdl2.SDL_CreateWindow(b"t", 0, 0, 640, 480,
                         sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN)
if not w:
    print("   CreateWindow FAIL:", sdl2.SDL_GetError().decode())
    sys.exit(1)
print("   CreateWindow OK, driver =", drv)
ww, wh = ctypes.c_int(), ctypes.c_int()
sdl2.SDL_GetWindowSize(w, ctypes.byref(ww), ctypes.byref(wh))
print("   window =", ww.value, "x", wh.value)
r = sdl2.SDL_CreateRenderer(w, -1, sdl2.SDL_RENDERER_ACCELERATED)
mode = "accel"
if not r:
    r = sdl2.SDL_CreateRenderer(w, -1, sdl2.SDL_RENDERER_SOFTWARE)
    mode = "software"
    print("   accel renderer fail:", sdl2.SDL_GetError().decode())
if not r:
    print("   CreateRenderer FAIL")
    sys.exit(1)
print("   Renderer OK, mode =", mode)
sdl2.SDL_SetRenderDrawColor(r, 200, 30, 30, 255)
sdl2.SDL_RenderClear(r)
sdl2.SDL_RenderPresent(r)
print("   RenderPresent OK")
'''

TESTS = [
    ("T1 基线（不设任何变量）", []),
    ("T2 预加载 mali 版 libEGL", ["/usr/lib/libEGL.so.1"]),
    ("T3 预加载 libmali 本体", ["/usr/lib/libmali.so.1"]),
    ("T4 LD_LIBRARY_PATH=/usr/lib", [("LD_LIBRARY_PATH", "/usr/lib")]),
    ("T5 SDL_VIDEODRIVER=mali", [("SDL_VIDEODRIVER", "mali")]),
    ("T6 SDL_RENDER_DRIVER=software", [("SDL_RENDER_DRIVER", "software")]),
    ("T7 软渲染 + 预加载 libmali", [("SDL_RENDER_DRIVER", "software"),
                                ("LD_PRELOAD", "/usr/lib/libmali.so.1")]),
    ("T8 SDL_VIDEODRIVER=dummy（对照，会黑屏但能建窗）",
     [("SDL_VIDEODRIVER", "dummy")]),
    ("T9 显式 LIBGL_ALWAYS_SOFTWARE=1", [("LIBGL_ALWAYS_SOFTWARE", "1")]),
    ("T10 预加载 mesa 版 libEGL（对照，确认不是 mesa 的问题）",
     ["/usr/lib/aarch64-linux-gnu/libEGL.so.1"]),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    # 先把测试脚本传上去
    sftp = cli.open_sftp()
    with sftp.open("/tmp/egltest.py", "w") as f:
        f.write("# -*- coding: utf-8 -*-\n" + PROBE)
    sftp.chmod("/tmp/egltest.py", 0o755)
    sftp.close()
    print("== EGL 方案验证 @", host, "==\n")

    for title, envs in TESTS:
        print(f">>> {title}")
        prefix = ""
        for e in envs:
            if isinstance(e, tuple):
                prefix += f" {e[0]}={e[1]}"
            else:
                prefix += f" LD_PRELOAD={e}"
        cmd = (f"cd /tmp && PYSDL2_DLL_PATH=/usr/lib{prefix} "
               f"timeout 20 python3 /tmp/egltest.py 2>&1 | grep -v setterm")
        _, o, e = cli.exec_command(cmd, timeout=40)
        d = (o.read().decode("utf-8", "replace")
             + e.read().decode("utf-8", "replace")).strip()
        print(d or "(空)")
        print()

    cli.close()


if __name__ == "__main__":
    main()
