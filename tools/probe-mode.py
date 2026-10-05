#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
显示模式真相确认 + 能否切回 640x480

已确认事实（推翻早期结论）：
  早先 C 版 fbtest 读到 640x480 32bpp red=16/green=8/blue=0，
  那是 dmenu.bin 尚未启动、fb0 处于初始默认模式时的状态。
  dmenu.bin 启动后（持有 /dev/fb0）把模式切成 1280x1024 16bpp。
  现在 C 版 fbtest 同样读到 1280x1024 16bpp，证明这不是 Python 的问题。

  stride 都是 2560 字节：
      1280 x 2bpp = 2560   （当前模式）
      640  x 4bpp = 2560   （早先模式）
  说明是同一块显存，扫描线步长一致，只是时序参数与像素打包不同。

待验证：
  1. 能否用 FBIOPUT_VSCREENINFO 切到 640x480x32
  2. 16bpp 下通道偏移全 0，说明是 RGB565 而非 BGR565
  3. dmenu 独占 fb0 的影响：我们能否也拿到可写的 framebuffer
"""
import sys

import paramiko

USER, PWD = "root", "root"

CODE = r'''
import ctypes, os, struct, sys

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FBIOPUT_VSCREENINFO = 0x4601

libc = ctypes.CDLL("libc.so.6", use_errno=True)
fd = os.open("/dev/fb0", os.O_RDWR)

def ioctl(req, size):
    buf = (ctypes.c_char * 256)()
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(req), ctypes.byref(buf))
    return rc, ctypes.get_errno(), bytes(buf[:size])

# ---- 内核 fb_var_screeninfo 精确布局（Linux 4.9 uapi）----
# 偏移: 0 __id, 4 __smem_start(8), 12 __smem_len(8), 20 __type,
#      24 __type_aux, 28 __visual, 32 __xpanstep, 36 __ypanstep,
#      40 __ywstep, 44 __xhstep, 48 __accel, 52 __video_caps,
#      56 __reserved[2], 64 xres, 68 yres, 72 xres_virtual,
#      76 yres_virtual, 80 xoffset, 84 yoffset, 88 bits_per_pixel,
#      92 grayscale, 96 red, 100 green, 104 blue, 108 transp,
#      112 nonstd, 116 activate, 120 height, 124 width, ...
O = dict(xres=64, yres=68, xres_v=72, yres_v=76, xoffset=80, yoffset=84,
         bpp=88, gray=92, red=96, green=100, blue=104, transp=108)

def show(tag, raw):
    g = lambda k: struct.unpack_from("<I", raw, O[k])[0]
    print("  %-10s %dx%d virt=%dx%d bpp=%d rgb=%d/%d/%d t=%d" % (
        tag, g("xres"), g("yres"), g("xres_v"), g("yres_v"), g("bpp"),
        g("red") & 0xFF, g("green") & 0xFF, g("blue") & 0xFF, g("transp") & 0xFF))

rc, eno, raw = ioctl(FBIOGET_VSCREENINFO, 192)
print("GET rc=%d errno=%d" % (rc, eno))
show("current", raw)

# 试切到 640x480 32bpp（照抄早先那组真值）
print()
print("尝试 FBIOPUT_VSCREENINFO 切到 640x480 32bpp ...")
mod = bytearray(raw)
struct.pack_into("<I", mod, O["xres"], 640)
struct.pack_into("<I", mod, O["yres"], 480)
struct.pack_into("<I", mod, O["xres_v"], 640)
struct.pack_into("<I", mod, O["yres_v"], 480)
struct.pack_into("<I", mod, O["bpp"], 32)
struct.pack_into("<I", mod, O["red"], 16 | 0xFF00)      # offset=16 length=8
struct.pack_into("<I", mod, O["green"], 8 | 0xFF00)
struct.pack_into("<I", mod, O["blue"], 0 | 0xFF00)
struct.pack_into("<I", mod, O["transp"], 24 | 0xFF00)

buf = (ctypes.c_char * 192).from_buffer(mod)
rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(FBIOPUT_VSCREENINFO), ctypes.byref(buf))
print("  PUT rc=%d errno=%d (%s)" % (rc, ctypes.get_errno(),
      os.strerror(ctypes.get_errno()) if ctypes.get_errno() else "ok"))

rc, eno, raw = ioctl(FBIOGET_VSCREENINFO, 192)
print("  切换后:")
show("after", raw)
print("  sysfs: virt=%s bpp=%s stride=%s" % (
    open("/sys/class/graphics/fb0/virtual_size").read().strip().replace("\n", ","),
    open("/sys/class/graphics/fb0/bits_per_pixel").read().strip(),
    open("/sys/class/graphics/fb0/stride").read().strip()))

os.close(fd)
'''

SHELL = [
    ("disp 驱动的 modes 定义",
     "cat /sys/class/graphics/fb0/modes 2>&1; "
     "echo '--- disp 平台设备:'; "
     "ls /sys/devices/platform/soc/1000000.disp/ 2>&1 | head -20"),
    ("dmenu.bin 当前是否还在跑（它持有 fb0）",
     "ps aux | grep -v grep | grep dmenu"),
    ("dmenu 启动前后 fb 模式是否变化的日志",
     "dmesg 2>/dev/null | tail -30"),
    ("历史：早先 fbtest 日志（C 版第一次跑通的记录）",
     "cat /mnt/mmc/Roms/APPS/PocketTransfer/logs/stdout.log 2>&1 | head -20"),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    sftp = cli.open_sftp()
    with sftp.open("/tmp/mode.py", "w") as f:
        f.write("# -*- coding: utf-8 -*-\n" + CODE)
    sftp.close()

    stages = [("显示模式确认与切换试验",
               "cd /tmp && python3 mode.py 2>&1")]
    for t, c in SHELL:
        stages.append((t, c))

    for title, cmd in stages:
        print(f">>> {title}")
        print("-" * 68)
        _, o, e = cli.exec_command(cmd, timeout=45)
        d = (o.read().decode("utf-8", "replace")
             + e.read().decode("utf-8", "replace")).strip()
        print(d or "(空)")
        print()

    cli.close()


if __name__ == "__main__":
    main()
