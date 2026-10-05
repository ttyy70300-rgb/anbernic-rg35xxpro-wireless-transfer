#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
阶段 0-0 第二轮深挖：决定技术路线的关键问题
  A. SDL2 到底有没有 fbdev/kmsdrm 驱动（决定 SDL2 路线生死）
  B. glibc 精确版本（决定能否动态链接）
  C. 固件有没有 Python / pygame（决定是否有更省力路线）
  D. 原厂 app.py 怎么实现震动（震动马达接口线索）
  E. 掌机上直接编译是否可行（gcc 实测）
"""
import argparse
import sys

import paramiko

USER, PWD = "root", "root"

PROBES = [
    ("=== A. SDL2 视频驱动（决定生死）===", "echo skip"),
    ("A1 SDL2 里所有含 fb/drm 的字符串",
     "strings /usr/lib/libSDL2-2.0.so.0 | grep -iE 'fbdev|kmsdrm|drm|directfb|wayland|dispfb' | sort -u | head -30"),
    ("A2 SDL2 里所有含 driver 的字符串",
     "strings /usr/lib/libSDL2-2.0.so.0 | grep -iE '^[a-z0-9_]*driver$' | sort -u | head -40"),
    ("A3 SDL2 配置的 SDL_VIDEO_DRIVER 环境变量线索",
     "strings /usr/lib/libSDL2-2.0.so.0 | grep -E 'SDL_VIDEO_DRIVER|SDL_VIDEODRIVER' | head -10"),
    ("A4 编译期视频驱动列表(SDL_VIDEO_DRIVER_*)",
     "strings /usr/lib/libSDL2-2.0.so.0 | grep -oE 'SDL_VIDEO_DRIVER_[A-Z0-9_]+' | sort -u | head -30"),
    ("A5 SDL2 打开 fbdev 时的设备路径",
     "strings /usr/lib/libSDL2-2.0.so.0 | grep -E '/dev/fb|/dev/dri' | head -10"),
    ("A6 原厂 clock app 用什么图形库",
     "ls -la /mnt/mmc/Roms/APPS/clock/ 2>&1 | head -30"),
    ("A7 原厂 app 的启动脚本",
     "cat /mnt/mmc/Roms/APPS/clock.sh 2>&1; echo '--- 或其他:'; ls /mnt/mmc/Roms/APPS/*.sh 2>&1"),
    ("A8 原厂 app.py 的 import 段",
     "head -40 /mnt/mmc/Roms/APPS/clock/app.py 2>&1"),
    ("A9 原厂有没有 python 可执行",
     "which python python3 python2 2>&1; echo '---'; python3 -V 2>&1; python3 -c 'import pygame; print(\"pygame\", pygame.version.ver)' 2>&1 | head -3"),
    ("A10 固件里有没有 pygame 库",
     "find / -maxdepth 5 -name 'pygame*' -o -maxdepth 5 -name 'sdl2*' 2>/dev/null | grep -v proc | head -10"),

    ("=== B. glibc 精确版本（决定动态链接可行性）===", "echo skip"),
    ("B1 libc.so.6 位置",
     "ls -l /usr/lib/aarch64-linux-gnu/libc.so* /lib/aarch64-linux-gnu/libc.so* 2>&1"),
    ("B2 libc 版本号",
     "strings /usr/lib/aarch64-linux-gnu/libc.so.6 2>/dev/null | grep -E 'GNU C Library|release version' | head -5"),
    ("B3 gcc 版本（掌机自带的）",
     "gcc --version 2>&1 | head -2; echo '---'; ls /usr/bin/*gcc* 2>&1"),
    ("B4 交叉/本地编译 aarch64 是否可行（hello 测试）",
     "cd /tmp && printf '#include <stdio.h>\\nint main(){printf(\"hi from handheld gcc\\\\n\");return 0;}\\n' > t.c && gcc t.c -o t 2>&1 && ./t 2>&1; echo \"exit=$?\"; file ./t 2>&1"),
    ("B5 musl-gcc / arm-linux-gnueabihf-gcc 也在?",
     "ls /usr/bin/ | grep -E 'gcc|g\\+\\+' 2>&1"),

    ("=== C. 原厂 app.py 的震动实现（找马达接口）===", "echo skip"),
    ("C1 app.py 里 vibrat 上下文",
     "grep -n -i -B3 -A8 'vibrat' /mnt/mmc/Roms/APPS/clock/app.py 2>&1 | head -50"),
    ("C2 全卡搜索 vibrat 代码/工具",
     "grep -rliE 'vibrat' /mnt/mmc/Roms/ /mnt/mmc/*.sh 2>/dev/null | head -20"),
    ("C3 原厂二进制里的 vibrat 调用",
     "for f in /mnt/mmc/Roms/System/*.so /mnt/mmc/System/*.so /mnt/mmc/*.so; do [ -f \"$f\" ] && strings \"$f\" 2>/dev/null | grep -q vibrat && echo \"HIT: $f\"; done 2>&1 | head -20"),
    ("C4 全系统搜 haptic/vibrat 设备",
     "find /dev /sys -iname '*haptic*' 2>/dev/null | head; echo '--- ioctl 序号猜测相关:'; ls /dev/misc 2>&1 | head -20"),
    ("C5 原厂时钟 app 的全部文件",
     "find /mnt/mmc/Roms/APPS/clock -type f 2>/dev/null | head -30"),

    ("=== D. 掌机编译可行性深入 ===", "echo skip"),
    ("D1 aarch64 工具链包",
     "ls /usr/aarch64-linux-gnu/ 2>&1 | head; echo '--- musl:'; ls /usr/lib/musl 2>&1 | head"),
    ("D2 pkg-config 能列出 SDL2 吗",
     "pkg-config --modversion sdl2 2>&1; echo '--- cflags:'; pkg-config --cflags --libs sdl2 2>&1"),
    ("D3 SDL2 头文件版本宏",
     "grep -E 'SDL_(MAJOR|MINOR|PATCHLEVEL)' /usr/include/SDL2/SDL_version.h 2>&1 | head -5"),
    ("D4 试编译一个 SDL2 程序（只链接不运行）",
     "cd /tmp && printf '#include <SDL2/SDL.h>\\n#include <stdio.h>\\nint main(){int n=SDL_GetNumVideoDrivers();printf(\"drivers=%%d\\\\n\",n);for(int i=0;i<n;i++)printf(\"  [%%d] %%s\\\\n\",i,SDL_GetVideoDriver(i));return 0;}\\n' > s.c && gcc s.c -o s $(pkg-config --cflags --libs sdl2) 2>&1 | head -5; echo \"build_exit=$?\"; ls -l ./s 2>&1"),
    ("D5 运行 SDL2 驱动枚举",
     "cd /tmp && SDL_VIDEODRIVER=dummy ./s 2>&1; echo '--- 不指定 driver:'; ./s 2>&1"),
    ("D6 内核 fbdev 支持的标志（ioctl 测）",
     "cat /sys/module/fb/parameters/ 2>&1 | head; echo '--- fb 驱动:'; cat /sys/class/graphics/fb0/name 2>&1"),
]

ORDER = ["A", "B", "C", "D"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.3.25")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = set(args.only.upper()) if args.only else set(ORDER)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=USER, password=PWD, timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as e:
        print(f"!! SSH 失败: {e}")
        sys.exit(1)
    print(f"== 已连接 {args.host} ==\n")

    for title, cmd in PROBES:
        head = title.split(".")[0].strip("= ")
        if head and head[0] in "ABCD" and head[1:2] == "" and head not in only:
            continue
        print(f"\n{title}")
        print("-" * 66)
        if cmd == "echo skip":
            continue
        _, out, err = cli.exec_command(cmd, timeout=120)
        data = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
        print(data.strip() or "(空)")

    cli.close()


if __name__ == "__main__":
    main()
