#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fb0 读数异常排查

现象：Python 用 fcntl.ioctl / ctypes ioctl 读 FBIOGET_VSCREENINFO，
     得到的 xres/yres/bpp 全是 0 或错位值（1280/1024/16/94/150/224）。
     而 C 版 fbtest 明确读到 640x480 32bpp line_length=2560。

可能原因：
  A. /dev/fb0 当前被 dmenu.bin 独占，它的 fb 驱动是私有实现，
     ioctl 走的不是标准 struct fb_var_screeninfo 语义
  B. 前面 SSH 探测时 fb0 状态与现在不同（HDMI 插入会切分辨率）
  C. C 版 fbtest 当时读的是另一个设备

本脚本把这些全查清楚，并做决定性对照：
     在同一时刻，先读 fb0，再跑一次 C 版 fbtest 的探测逻辑。
"""
import sys

import paramiko

USER, PWD = "root", "root"

# 决定性对照：把 C 版 fbtest 的探测部分用 Python 精确复刻
# 关键：C 用 struct fb_var_screeninfo，它包含 4 个 __u32 私有头 + 2 个 __u32 reserved
# 这里我枚举多种可能的布局，看哪一种能解出 640/480/32
LAYOUTS = r'''
import ctypes, os, struct

fd = os.open("/dev/fb0", os.O_RDWR)
libc = ctypes.CDLL("libc.so.6", use_errno=True)

def ioctl(req, size):
    buf = (ctypes.c_char * 512)()
    rc = libc.ioctl(ctypes.c_int(fd), ctypes.c_ulong(req), ctypes.byref(buf))
    return rc, bytes(buf[:size])

rc, raw = ioctl(0x4600, 192)
print("  ioctl rc =", rc)
print("  前 200 字节按 u32 全量 dump:")
for i in range(0, 200, 4):
    v = struct.unpack_from("<I", raw, i)[0]
    if v or i < 20:
        print("    [%3d] %10d  0x%08x" % (i, v, v))

print()
print("  尝试解释 640/480/32/2560 的位置:")
for size in (128, 160, 192, 200, 208, 240):
    for i in range(0, min(size, 200) - 4, 4):
        v = struct.unpack_from("<I", raw, i)[0]
        if v == 640:
            print("    640 出现在偏移 %d (size=%d)" % (i, size))
        if v == 480:
            print("    480 出现在偏移 %d (size=%d)" % (i, size))

rc2, raw2 = ioctl(0x4602, 112)
print()
print("  fix info 前 120 字节:")
for i in range(0, 120, 4):
    v = struct.unpack_from("<I", raw2, i)[0]
    if v or i < 20:
        print("    [%3d] %10d  0x%08x" % (i, v, v))
print("  2560(line_length) 位置:",
      [i for i in range(0, 108, 4) if struct.unpack_from("<I", raw2, i)[0] == 2560])

os.close(fd)
'''

SHELL = [
    ("当前 fb0 sysfs 属性（这是内核权威值）",
     "for f in modes virtual_size bits_per_pixel stride blank; do "
     "printf '%-16s = %s\\n' \"$f\" \"$(cat /sys/class/graphics/fb0/$f 2>&1)\"; done"),
    ("谁持有 /dev/fb0",
     "for p in $(ls /proc | grep -E '^[0-9]+$'); do "
     "c=$(cat /proc/$p/comm 2>/dev/null); "
     "for f in /proc/$p/fd/*; do "
     "l=$(readlink $f 2>/dev/null); "
     "case $l in *fb*) echo \"PID $p [$c] -> $l\";; esac; "
     "done; done 2>/dev/null | head -10"),
    ("HDMI 是否接入（会切分辨率）",
     "cat /sys/class/extcon/hdmi/state 2>&1; echo '--- extcon:'; ls /sys/class/extcon/ 2>&1"),
    ("/dev/fb0 的字符设备主次号与驱动",
     "ls -l /dev/fb0; echo '--- /sys/class/graphics/fb0/name:'; "
     "cat /sys/class/graphics/fb0/name 2>&1; echo '--- 驱动绑定:'; "
     "readlink -f /sys/class/graphics/fb0/device 2>&1"),
    ("C 版 fbtest 二进制是否还在，能否重跑对照",
     "ls -la /mnt/mmc/Roms/APPS/PocketTransfer/ 2>&1; "
     "ls -la /mnt/mmc/Roms/APPS/PocketTransfer/fbtest 2>&1"),
    ("fb0 是否支持双缓冲（sysfs 无法判断，看 var.yres_virtual）",
     "cat /sys/class/graphics/fb0/virtual_size 2>&1"),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    sftp = cli.open_sftp()
    with sftp.open("/tmp/layouts.py", "w") as f:
        f.write("# -*- coding: utf-8 -*-\n" + LAYOUTS)
    sftp.close()

    stages = [
        ("结构体布局穷举", "cd /tmp && python3 layouts.py 2>&1"),
    ]
    for t, c in SHELL:
        stages.append((t, c))

    # 最后重跑 C 版 fbtest，看它现在读到什么
    stages.append((
        "决定性对照：重跑 C 版 fbtest",
        "cd /mnt/mmc/Roms/APPS/PocketTransfer && "
        "./fbtest 2>&1 | head -8 || echo '(C 版不在该路径)'"))

    for title, cmd in stages:
        print(f">>> {title}")
        print("-" * 68)
        _, o, e = cli.exec_command(cmd, timeout=60)
        d = (o.read().decode("utf-8", "replace")
             + e.read().decode("utf-8", "replace")).strip()
        print(d or "(空)")
        print()

    cli.close()


if __name__ == "__main__":
    main()
