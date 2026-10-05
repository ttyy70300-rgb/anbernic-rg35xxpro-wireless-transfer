#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
EGL_BAD_DISPLAY (0x3008) 根因定位

已确认事实：
  - eglGetDisplay(EGL_DEFAULT_DISPLAY) 返回非空句柄
  - eglInitialize 返回 0，errno = 0x3008 = EGL_BAD_DISPLAY
  - GPU 实际设备节点是 /dev/mali0（不是 /dev/mali）
  - libmali.so 是 ARM 旧版 mali 用户态（0.20.0）

EGL_BAD_DISPLAY 意味着：EGL 实现内部认不出当前 display。
最常见原因：mali 用户态库按老路径 /dev/mali 打开设备，而这台机器只有 /dev/mali0。

验证方案：
  A. 确认 /dev/mali0 的 major:minor 与 /dev/mali 是否需匹配
  B. 试 libmali 的其它版本/变体
  C. 试 LIBEGL_NO_DEVICE / EGL_PLATFORM 等环境变量
  D. 试 mesh EGL 扩展
  E. 关键对照：跑原厂 clock app 走 SSH 前台，看是否也失败
     —— 若也失败，说明 EGL 需要 launcher 提供的某些上下文/环境，
        不是我们代码的问题，而是「不能在 SSH 会话里开图形窗口」
  F. 原厂 cexpert / dmenu 是否占着 GPU（可能需要先关掉某个服务）
"""
import sys

import paramiko

USER, PWD = "root", "root"

SNIPPETS = {
    "malidev.py": r'''
import os, ctypes
os.environ["LD_LIBRARY_PATH"] = "/usr/lib"

# 先看 /dev/mali* 的实际节点
import glob
for p in sorted(glob.glob("/dev/mali*")):
    st = os.stat(p)
    print("  node", p, "major", os.major(st.st_rdev), "minor", os.minor(st.st_rdev))

# 试软链 /dev/mali -> /dev/mali0 后再初始化
if not os.path.exists("/dev/mali") and os.path.exists("/dev/mali0"):
    try:
        os.symlink("/dev/mali0", "/dev/mali")
        print("  created symlink /dev/mali -> /dev/mali0")
    except Exception as e:
        print("  symlink failed:", e)

egl = ctypes.CDLL("libEGL.so.1")
EGL_NONE = 0x3038
major = ctypes.c_int(); minor = ctypes.c_int()
dpy = egl.eglGetDisplay(0)
print("  eglGetDisplay ->", hex(dpy) if dpy else "NULL")
if dpy:
    ok = egl.eglInitialize(dpy, ctypes.byref(major), ctypes.byref(minor))
    print("  eglInitialize ->", ok, "ver", major.value, minor.value,
          "err", hex(egl.eglGetError()))
''',
    "envs.py": r'''
import os, ctypes, sys
os.environ["LD_LIBRARY_PATH"] = "/usr/lib"
os.environ.setdefault("PYSDL2_DLL_PATH", "/usr/lib")

def attempt(tag):
    egl = ctypes.CDLL("libEGL.so.1")
    a, b = ctypes.c_int(), ctypes.c_int()
    d = egl.eglGetDisplay(0)
    if not d:
        print("  %-34s GetDisplay NULL" % tag); return
    ok = egl.eglInitialize(d, ctypes.byref(a), ctypes.byref(b))
    print("  %-34s init=%s ver=%d.%d err=%s" % (
        tag, ok, a.value, b.value, hex(egl.eglGetError())))

attempt("baseline")

for tag, k, v in [
    ("EGL_PLATFORM=surfaceless", "EGL_PLATFORM", "surfaceless"),
    ("EGL_PLATFORM=device",       "EGL_PLATFORM", "device"),
    ("EGL_LOG_LEVEL=debug",       "EGL_LOG_LEVEL", "debug"),
    ("MESA_DEBUG=1",              "MESA_DEBUG", "1"),
    ("LIBGL_DEBUG=verbose",       "LIBGL_DEBUG", "verbose"),
    ("EGL_NO_DEVICE=1",           "EGL_NO_DEVICE", "1"),
    ("MALI_NO_DEVICE=1",          "MALI_NO_DEVICE", "1"),
    ("LD_PRELOAD libmali",        "LD_PRELOAD", "/usr/lib/libmali.so.1"),
]:
    # 必须在 import/load 前设，所以用子进程更可靠；这里简化直接改 os.environ
    os.environ[k] = v
    try:
        attempt(k + "=" + v)
    except Exception as e:
        print("  %-34s EXC %s" % (k, e))
''',
    "clockapp.py": r'''
# 对照实验：原厂 clock app 的图形初始化能否在 SSH 会话里成功
import os, sys
os.environ["LD_LIBRARY_PATH"] = "/usr/lib"
os.environ["PYSDL2_DLL_PATH"] = "/usr/lib"
sys.path.insert(0, "/mnt/mmc/Roms/APPS/clock")
os.chdir("/mnt/mmc/Roms/APPS/clock")
try:
    import sdl2
    sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)
    print("  clock: SDL_Init ok, driver =", sdl2.SDL_GetCurrentVideoDriver())
    w = sdl2.SDL_CreateWindow(b"c", 0, 0, 0, 0,
            sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN)
    if not w:
        print("  clock: CreateWindow FAIL:", sdl2.SDL_GetError().decode())
    else:
        print("  clock: CreateWindow OK")
        r = sdl2.SDL_CreateRenderer(w, -1, sdl2.SDL_RENDERER_ACCELERATED)
        print("  clock: accel renderer =", bool(r),
              sdl2.SDL_GetError().decode() if not r else "")
except Exception as e:
    import traceback; traceback.print_exc()
''',
}

SHELL = [
    ("GPU 设备与模块状态", "ls -la /dev/mali* 2>&1; echo '--- 模块:'; "
     "lsmod | grep -iE 'mali|drm|ion' ; echo '--- dmesg gpu:'; "
     "dmesg 2>/dev/null | grep -iE 'mali|gpu|drm' | tail -15"),
    ("cexpert 进程（GPU 初始化守护）", "ps aux | grep -v grep | grep -iE 'cexpert|dmenu|muos' | head"),
    ("原厂 dmenu/muos 用了什么图形栈", "readelf -d /mnt/vendor/bin/dmenu.bin 2>/dev/null | grep -iE 'NEEDED' | head -20"),
    ("原厂 cexpert 依赖", "readelf -d /mnt/vendor/bin/cexpert 2>/dev/null | grep -iE 'NEEDED' | head -20"),
    ("有没有 weston/wayland", "ps aux | grep -v grep | grep -iE 'weston|wayland|Xorg' | head; "
     "echo '--- weston.sh:'; cat /etc/profile.d/weston.sh 2>/dev/null | head -20"),
    ("disp 设备与 sunxi 显示服务", "ls -la /dev/disp /dev/sunxi-reg 2>&1; "
     "echo '--- 进程:'; ps aux | grep -v grep | grep -iE 'disp|sunxi|deephy' | head"),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    sftp = cli.open_sftp()
    for name, code in SNIPPETS.items():
        with sftp.open(f"/tmp/{name}", "w") as f:
            f.write("# -*- coding: utf-8 -*-\n" + code)
    sftp.close()

    stages = [
        ("设备节点与软链试验", "cd /tmp && timeout 25 python3 /tmp/malidev.py 2>&1 | grep -v setterm"),
        ("环境变量组合试验", "cd /tmp && timeout 40 python3 /tmp/envs.py 2>&1 | grep -v setterm"),
        ("对照：原厂 clock app 在 SSH 会话里能否建窗",
         "cd /tmp && timeout 25 python3 /tmp/clockapp.py 2>&1 | grep -v setterm"),
    ]
    for t, c in SHELL:
        stages.append((t, c))

    for title, cmd in stages:
        print(f">>> {title}")
        print("-" * 66)
        _, o, e = cli.exec_command(cmd, timeout=70)
        d = (o.read().decode("utf-8", "replace")
             + e.read().decode("utf-8", "replace")).strip()
        print(d or "(空)")
        print()

    cli.close()


if __name__ == "__main__":
    main()
